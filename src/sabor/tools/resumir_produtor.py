"""Ferramenta resumir_produtor (Etapa 3.3, Pilar 4.3 — Issue #11):
Resumo operacional e financeiro sob demanda formatado como rascunho.
"""

from __future__ import annotations

from datetime import date
import re
import unicodedata

import psycopg
from typing import Annotated, Literal
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps
from sabor.agent.schemas import Flag, Rascunho
from sabor.tools.base import (
    Fonte,
    SemPermissao,
    _aplicar_filtro_produtor,
    _mascarar_opcional,
    _resolver_usuario,
    registrar_flags,
    registrar_rascunhos,
)
from sabor.tools.consultar_dados import consultar_financeiro


class PedidoElegivelResumo(BaseModel):
    pedido_id: str
    comprador: str | None = None
    data_entrega: date | None = None
    produto: str
    quantidade: float | None = None
    unidade: str | None = None
    organico: bool | None = None
    comprador_deve_vencido: float = 0.0
    """Quanto o comprador deste pedido deve, já vencido, a este produtor (ex.: SuperBom e P003)."""
    fonte: Fonte


class PendenciaComprador(BaseModel):
    """Valores a receber do produtor, somados por comprador."""

    comprador: str | None = None
    vencido: float = 0.0
    a_vencer: float = 0.0
    maior_atraso_dias: int = 0
    primeiro_vencimento: date | None = None
    fontes: list[Fonte] = Field(default_factory=list)


class ResumoProdutorResultado(BaseModel):
    encontrado: bool = True
    produtor_id: str | None = None
    produtor_nome: str | None = None
    data_referencia: date
    vendas_setembro: float = 0.0
    vendas_outubro: float = 0.0
    total_vendido_periodo: float = 0.0
    total_pendente: float = 0.0
    total_vencido: float = 0.0
    pendencias_por_comprador: list[PendenciaComprador] = Field(default_factory=list)
    """Quem deve ao produtor: vencidos primeiro, do maior para o menor valor."""
    certificacao_organica: Literal['vigente', 'vencida', 'sem_validade', 'nao_possui'] | None = (
        None
    )
    """Situação na data de referência: o cadastro sozinho não basta (P008 vencida)."""
    validade_certificacao: date | None = None
    registro_sie: str | None = None
    """Registro sanitário (SIE) da agroindústria; nulo quando não há."""
    pedidos_em_aberto: list[PedidoElegivelResumo] = Field(default_factory=list)
    alertas: list[str] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    """Conformidade e inadimplência; o runner as anexa à resposta sem depender do modelo."""
    rascunho: Rascunho | None = None
    fontes: list[Fonte] = Field(default_factory=list)
    mensagem: str | None = None


_TERMOS_NC_ADAGRO = (
    'nc-',
    'não conformidade',
    'nao conformidade',
    'interdi',
    'suspens',
    'irregular',
    'bloque',
    'pendente',
)

_PADRAO_PRODUTOR_ID = re.compile(r'\bP\d{3}\b', re.IGNORECASE)


def _blocos_do_produtor(texto_nc: str, prod_id: str, doc_produtor_id: str | None) -> list[str]:
    """Trechos do chunk que dizem respeito ao `prod_id`.

    Em documentos individuais (`doc_produtor_id == prod_id`), é o chunk inteiro.
    Em documentos coletivos (`doc_produtor_id is None`, ex.: relatório da ADAGRO),
    isola apenas os blocos/linhas que citam o `prod_id` para não atribuir a um produtor
    regular a não conformidade de outro produtor citado no mesmo chunk.
    """
    if doc_produtor_id is not None and doc_produtor_id.upper() == prod_id.upper():
        return [texto_nc]
    padrao_alvo = re.compile(rf'\b{re.escape(prod_id)}\b', re.IGNORECASE)
    blocos: list[str] = []
    for paragrafo in re.split(r'\n\s*\n+', texto_nc):
        if not padrao_alvo.search(paragrafo):
            continue
        outros = {
            pid.upper()
            for pid in _PADRAO_PRODUTOR_ID.findall(paragrafo)
            if pid.upper() != prod_id.upper()
        }
        if outros:
            blocos.extend(
                linha for linha in paragrafo.splitlines() if padrao_alvo.search(linha)
            )
        else:
            blocos.append(paragrafo)
    return blocos


def _chunk_tem_nc_para_produtor(
    texto_nc: str,
    prod_id: str,
    doc_produtor_id: str | None,
) -> bool:
    """Verifica se há apontamento/NC aplicável ao `prod_id` no chunk."""
    return any(
        any(termo in bloco.lower() for termo in _TERMOS_NC_ADAGRO)
        for bloco in _blocos_do_produtor(texto_nc, prod_id, doc_produtor_id)
    )


_PADRAO_NC = re.compile(r'NC-(\d+)\s*\(([^)]+)\)\s*:\s*([^\n]+)', re.IGNORECASE)
_PADRAO_NUMERO_DOC = re.compile(r'(?:Laudo|Relat[óo]rio)\s+N[º°o]\.?\s*:\s*(\S+)', re.IGNORECASE)


def _descrever_ncs(bloco: str) -> list[str]:
    """Descreve cada NC do bloco com código, severidade, prazo e status.

    Ex.: "NC-001 (menor): Lixeira sem pedal (prazo: …; status: pendente de verificação)".
    """
    descricoes: list[str] = []
    for parte in re.split(r'(?=\bNC-\d+)', bloco):
        achado = _PADRAO_NC.match(parte.strip())
        if achado is None:
            continue
        codigo, severidade, descricao = achado.groups()
        texto = f'NC-{codigo} ({severidade.strip().lower()}): {descricao.strip().rstrip(".")}'
        extras = []
        if prazo := re.search(r'Prazo[^:\n]*:\s*([^\n]+)', parte, re.IGNORECASE):
            extras.append(f'prazo: {prazo.group(1).strip().rstrip(".")}')
        if status := re.search(r'Status:\s*([^\n]+)', parte, re.IGNORECASE):
            extras.append(f'status: {status.group(1).strip().rstrip(".").lower()}')
        if extras:
            texto += f" ({'; '.join(extras)})"
        descricoes.append(texto)
    return descricoes


def _rotulo_documento(
    conn: psycopg.Connection, arquivo: str, tipo_doc: str | None, titulo: str | None
) -> str:
    """Identifica o documento pelo cabeçalho, com tipo, número e órgão emissor.

    Ex.: "Laudo VS-CAE-2026-0034 (Vigilância Sanitária Municipal de Caetés)".

    O órgão vem do próprio documento: o laudo do Cícero é da vigilância municipal, não da
    ADAGRO (F7 do Bloco 4).
    """
    cabecalho = conn.execute(
        'SELECT texto FROM chunks WHERE arquivo = %s ORDER BY ordem LIMIT 1', (arquivo,)
    ).fetchone()
    texto = cabecalho[0] if cabecalho else ''
    numero = _PADRAO_NUMERO_DOC.search(texto)
    orgao = next(
        (
            linha.strip()
            for linha in texto.splitlines()
            if linha.strip() and not linha.strip().startswith('=')
        ),
        None,
    )
    tipo = 'Inspeção' if tipo_doc == 'inspecao' else 'Laudo' if tipo_doc == 'laudo' else 'Documento'
    rotulo = f'{tipo} {numero.group(1)}' if numero else (titulo or tipo)
    return f'{rotulo} ({orgao})' if orgao else rotulo


def _remover_acentos(texto: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn'
    ).lower()


_PALAVRAS_GENERICAS_COMPRADOR = frozenset({
    'rede', 'atacado', 'prefeitura', 'municipal', 'mercado', 'restaurante', 'lanchonete',
    'hotel', 'fazenda', 'unidade', 'garanhuns', 'dona', 'chef', 'produtor', 'produtora',
})


def _mesmo_comprador(a: str | None, b: str | None) -> bool:
    """Compara o comprador do pedido com o do financeiro, que nem sempre têm o mesmo nome
    ("Dona Cremilda - Lanchonete da Feira" × "Lanchonete da Feira").

    Basta uma palavra distintiva em comum (4+ letras, fora das genéricas como "rede" ou
    "garanhuns"). A comparação é só entre os compradores deste produtor, então o risco de
    confundir dois clientes é pequeno.
    """
    if not a or not b:
        return False
    palavras_a, palavras_b = (
        {
            p for p in re.findall(r'[a-z0-9]{4,}', _remover_acentos(nome))
            if p not in _PALAVRAS_GENERICAS_COMPRADOR
        }
        for nome in (a, b)
    )
    return bool(palavras_a & palavras_b)


def _formatar_data_curta(valor: date | None) -> str:
    return valor.strftime('%d/%m') if valor else 'sem vencimento'


def _formatar_brl(valor: float) -> str:
    return f'R$ {valor:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def _resolver_produtor_por_id_ou_nome(
    conn: psycopg.Connection, identificador: str
) -> tuple[str, str, str, bool, date | None, str | None] | None:
    """Resolve produtor por ID exato (ex.: 'P001') ou nome (ex.: 'Maria das Graças')."""
    termo = identificador.strip()
    if not termo:
        return None

    linha = conn.execute(
        """
        SELECT id, nome_completo, status, certificacao_organica,
               validade_certificacao, registro_sie
        FROM produtores
        WHERE UPPER(id) = UPPER(%s)
           OR nome_completo ILIKE %s
        ORDER BY (UPPER(id) = UPPER(%s)) DESC, id ASC
        LIMIT 1
        """,
        (termo, f'%{termo}%', termo),
    ).fetchone()
    if linha is not None:
        return linha

    termo_norm = _remover_acentos(termo)
    todas = conn.execute(
        """
        SELECT id, nome_completo, status, certificacao_organica,
               validade_certificacao, registro_sie
        FROM produtores
        ORDER BY id
        """
    ).fetchall()
    for row in todas:
        if termo_norm in _remover_acentos(row[1]):
            return row
    return None


def resumir_produtor(
    ctx: RunContext[AgentDeps],
    produtor: Annotated[
        str,
        Field(
            description=(
                "Código identificador (ex.: 'P001') ou nome parcial/completo "
                "do produtor (ex.: 'Maria das Graças', 'Seu Antônio')."
            )
        ),
    ],
) -> ResumoProdutorResultado:
    """Consolida o panorama completo de um produtor e gera um rascunho.

    QUANDO USAR:
    - Sempre que o usuário pedir um "resumo", "panorama", "situação geral",
      "extrato completo" ou "levantamento" de um produtor específico
      (ex.: "faz o resumo da Maria das Graças", "qual a situação do P001?",
      "me dá um resumo do meu cadastro").
    - Quando for necessário consolidar vendas de setembro/outubro, valores
      pendentes/vencidos, pedidos em aberto elegíveis, situação da certificação
      orgânica (vigente, vencida ou não possui, com a validade), registro SIE e
      alertas documentais (ADAGRO, laudos) de uma só vez, já gerando o rascunho
      de mensagem.
    - O resultado já é o panorama completo: não complemente com
      `buscar_documentos`, `consultar_financeiro` ou `buscar_produtores` sobre o
      mesmo produtor.

    QUANDO NÃO USAR:
    - Para buscar listas de produtores filtrando por município ou produto
      (use `buscar_produtores`).
    - Para consultar transações financeiras específicas por comprador ou
      divergências isoladas (use `consultar_financeiro`).
    """

    conn = ctx.deps.conn
    data_ref = ctx.deps.data_referencia
    usuario = _resolver_usuario(ctx.deps.usuario)
    mascarador = ctx.deps.mascarador

    termo = produtor.strip()
    if re.fullmatch(r'P\d{3}', termo, re.IGNORECASE):
        _aplicar_filtro_produtor(usuario, termo.upper())

    produtor_row = _resolver_produtor_por_id_ou_nome(conn, termo)
    if produtor_row is None:
        if usuario.perfil == 'produtor':
            raise SemPermissao(
                f'Perfil produtor ({usuario.produtor_id}) só pode consultar o próprio resumo.'
            )
        return ResumoProdutorResultado(
            encontrado=False,
            data_referencia=data_ref,
            mensagem=f"Produtor '{produtor}' não encontrado.",
        )

    (
        prod_id,
        nome_completo,
        status_prod,
        cert_organica,
        validade_cert,
        registro_sie,
    ) = produtor_row

    # 1. Controle de Acesso por Perfil: Produtor só pode pedir o próprio resumo
    _aplicar_filtro_produtor(usuario, prod_id)

    fontes: list[Fonte] = [Fonte(arquivo='produtores.csv', referencia=prod_id)]
    fontes_vistas: set[tuple[str, str]] = {('produtores.csv', prod_id)}

    def _adicionar_fonte(f: Fonte) -> None:
        chave = (f.arquivo, f.referencia)
        if chave not in fontes_vistas:
            fontes_vistas.add(chave)
            fontes.append(f)

    # 2. Vendas no período e Pendências reaproveitando consultar_financeiro (#7)
    fin_geral = consultar_financeiro(ctx, produtor_id=prod_id)
    for item in fin_geral.itens:
        _adicionar_fonte(item.fonte)

    vendas_setembro = round(
        sum(
            i.valor_total
            for i in fin_geral.itens
            if i.data.year == data_ref.year and i.data.month == 9
        ),
        2,
    )
    vendas_outubro = round(
        sum(
            i.valor_total
            for i in fin_geral.itens
            if i.data.year == data_ref.year and i.data.month == 10
        ),
        2,
    )

    # Quem deve ao produtor, por comprador (F5 do Bloco 4): o total sozinho não diz que os
    # R$ 6.020,00 vencidos do Cícero são da SuperBom.
    pendencias: dict[str, PendenciaComprador] = {}
    for item in fin_geral.itens:
        if item.status_pagamento != 'pendente':
            continue
        pend = pendencias.setdefault(
            item.comprador,
            PendenciaComprador(comprador=_mascarar_opcional(mascarador, item.comprador)),
        )
        if item.vencido:
            pend.vencido = round(pend.vencido + item.valor_total, 2)
            pend.maior_atraso_dias = max(pend.maior_atraso_dias, item.dias_atraso)
        else:
            pend.a_vencer = round(pend.a_vencer + item.valor_total, 2)
        if item.data_vencimento and (
            pend.primeiro_vencimento is None or item.data_vencimento < pend.primeiro_vencimento
        ):
            pend.primeiro_vencimento = item.data_vencimento
        pend.fontes.append(item.fonte)
    vencidos_por_comprador = {nome: p.vencido for nome, p in pendencias.items() if p.vencido > 0}
    pendencias_ordenadas = sorted(
        pendencias.values(), key=lambda p: (-p.vencido, -p.a_vencer, p.comprador or '')
    )

    # 3. Alertas: certificação vencendo ou vencida (P004, P008), NC ADAGRO, laudos faltando
    alertas: list[str] = []

    docs_produtor = conn.execute(
        """
        SELECT arquivo, tipo_doc, titulo, validade
        FROM documentos
        WHERE produtor_id = %s
        ORDER BY arquivo
        """,
        (prod_id,),
    ).fetchall()

    tipos_docs_presentes: set[str] = set()
    validade_doc_cert: date | None = None
    for arq, t_doc, _titulo, val_doc in docs_produtor:
        tipos_docs_presentes.add(t_doc)
        _adicionar_fonte(Fonte(arquivo=arq, referencia=t_doc.upper()))
        if t_doc == 'certificado' and val_doc is not None:
            validade_doc_cert = val_doc

    data_val_efetiva = validade_cert or validade_doc_cert
    cert_vigente = (
        bool(cert_organica) and data_val_efetiva is not None and data_val_efetiva >= data_ref
    )

    if cert_organica or validade_doc_cert is not None:
        if data_val_efetiva is None:
            situacao_cert = 'sem_validade'
        else:
            situacao_cert = 'vigente' if cert_vigente else 'vencida'
    else:
        situacao_cert = 'nao_possui'

    if status_prod != 'ativo':
        alertas.append('Cadastro inativo na cooperativa.')

    if (cert_organica or validade_doc_cert is not None) and data_val_efetiva is not None:
        dias_restantes = (data_val_efetiva - data_ref).days
        if dias_restantes < 0:
            alertas.append(
                f"Certificação orgânica VENCIDA em {data_val_efetiva.strftime('%d/%m/%Y')}."
            )
        elif dias_restantes <= 45:
            alertas.append(
                f"Certificação orgânica vencendo em {data_val_efetiva.strftime('%d/%m/%Y')} "
                f'({dias_restantes} dias).'
            )
    elif cert_organica and data_val_efetiva is None:
        alertas.append('Certificação orgânica declarada sem data de validade comprovada.')

    # Não conformidades nos documentos do próprio produtor (laudos) ou no relatório coletivo
    # de inspeção. O alerta diz de onde veio, com número, órgão, severidade e status, sem o
    # caminho do arquivo, porque ele vai no rascunho para o produtor (F7 do Bloco 4).
    chunks_nc = conn.execute(
        """
        SELECT DISTINCT c.arquivo, COALESCE(c.secao, 'GERAL'), c.texto, d.produtor_id,
               d.tipo_doc, d.titulo, c.ordem
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE (
            d.produtor_id = %s
            OR (d.tipo_doc = 'inspecao' AND c.texto ILIKE %s)
        )
        ORDER BY c.arquivo, c.ordem
        """,
        (prod_id, f'%{prod_id}%'),
    ).fetchall()

    ncs_por_documento: dict[str, tuple[str | None, str | None, list[str], str | None]] = {}
    for arq_nc, secao_nc, texto_nc, doc_prod_id, tipo_doc, titulo, _ordem in chunks_nc:
        blocos = [
            b
            for b in _blocos_do_produtor(texto_nc, prod_id, doc_prod_id)
            if any(termo in b.lower() for termo in _TERMOS_NC_ADAGRO)
        ]
        if not blocos:
            continue
        _adicionar_fonte(Fonte(arquivo=arq_nc, referencia=secao_nc))
        _tipo, _titulo, descricoes, resultado = ncs_por_documento.setdefault(
            arq_nc, (tipo_doc, titulo, [], None)
        )
        for bloco in blocos:
            for descricao in _descrever_ncs(bloco):
                if descricao not in descricoes:
                    descricoes.append(descricao)
            if achado := re.search(r'RESULTADO GERAL:\s*([^\n]+)', bloco, re.IGNORECASE):
                resultado = achado.group(1).strip().lower()
        ncs_por_documento[arq_nc] = (tipo_doc, titulo, descricoes, resultado)

    for arq_nc, (tipo_doc, titulo, descricoes, resultado) in ncs_por_documento.items():
        rotulo_doc = _rotulo_documento(conn, arq_nc, tipo_doc, titulo)
        detalhe = '; '.join(descricoes) if descricoes else 'situação irregular registrada'
        alerta = f'{rotulo_doc} — {detalhe}.'
        if resultado:
            alerta += f' Resultado geral: {resultado}.'
        alertas.append(alerta)

    # Verifica produtos de origem animal: exigem registro SIE e laudo sanitário
    produtos_info = conn.execute(
        """
        SELECT DISTINCT COALESCE(pr.nome, pp.nome_original), COALESCE(pr.origem_animal, false)
        FROM produtor_produtos pp
        LEFT JOIN produtos pr ON pr.id = pp.produto_id
        WHERE pp.produtor_id = %s
        """,
        (prod_id,),
    ).fetchall()

    tem_origem_animal = any(bool(orig_animal) for _, orig_animal in produtos_info)
    if tem_origem_animal:
        if not registro_sie:
            alertas.append('Produto de origem animal sem registro SIE (comercialização bloqueada).')
        if 'laudo' not in tipos_docs_presentes:
            alertas.append('Laudo sanitário/veterinário faltando na pasta documental.')

    # 4. Pedidos em aberto com os produtos do produtor, filtrados por elegibilidade.
    # Os itens vêm da triagem (`sabor.triagem` grava `pedido_itens`), assim como o tipo e a data
    # de entrega: as colunas extraídas de `pedidos` não são preenchidas pela carga. Itens
    # ambíguos ficam de fora, porque ainda não se sabe qual produto o comprador quer.
    pedidos_elegiveis: list[PedidoElegivelResumo] = []
    if status_prod == 'ativo':
        linhas_pedidos = conn.execute(
            """
            SELECT DISTINCT
                p.id,
                COALESCE(p.comprador_normalizado, p.comprador, t.comprador) AS comprador,
                COALESCE(t.data_entrega, p.data_entrega) AS data_entrega,
                pr.nome AS produto_nome,
                pi.quantidade,
                pi.unidade,
                pi.organico,
                pr.origem_animal
            FROM pedidos p
            JOIN triagem_pedidos t ON t.pedido_id = p.id AND t.status = 'ok'
            JOIN pedido_itens pi ON pi.pedido_id = p.id
            JOIN produtor_produtos pp ON pp.produto_id = pi.produto_id
            JOIN produtos pr ON pr.id = pi.produto_id
            WHERE pp.produtor_id = %s
              AND p.status = 'pendente'
              AND NOT pi.ambiguo
              AND COALESCE(t.tipo, p.tipo, 'compra') IN ('compra', 'compra_recorrente', 'cotacao')
            ORDER BY data_entrega NULLS LAST, p.id, pr.nome
            """,
            (prod_id,),
        ).fetchall()

        for (
            ped_id,
            comprador,
            dt_entrega,
            prod_nome,
            qtd,
            unidade,
            exige_org,
            origem_animal,
        ) in linhas_pedidos:
            if exige_org is True and not cert_vigente:
                continue
            if origem_animal and not registro_sie:
                continue

            fonte_ped = Fonte(arquivo='pedidos.csv', referencia=ped_id)
            _adicionar_fonte(fonte_ped)
            deve_vencido = round(sum(
                valor
                for nome, valor in vencidos_por_comprador.items()
                if _mesmo_comprador(comprador, nome)
            ), 2)
            pedidos_elegiveis.append(
                PedidoElegivelResumo(
                    pedido_id=ped_id,
                    comprador=_mascarar_opcional(mascarador, comprador),
                    data_entrega=dt_entrega,
                    produto=prod_nome,
                    quantidade=float(qtd) if qtd is not None else None,
                    unidade=unidade,
                    organico=exige_org,
                    comprador_deve_vencido=deve_vencido,
                    fonte=fonte_ped,
                )
            )

    alertas_documentais = list(alertas)

    # Pedido de quem já deve ao produtor: a alocação precisa passar pela gestão.
    pedidos_por_devedor: dict[str, list[str]] = {}
    valor_por_devedor: dict[str, float] = {}
    for ped in pedidos_elegiveis:
        if ped.comprador_deve_vencido > 0 and ped.comprador:
            ids = pedidos_por_devedor.setdefault(ped.comprador, [])
            if ped.pedido_id not in ids:
                ids.append(ped.pedido_id)
            valor_por_devedor[ped.comprador] = ped.comprador_deve_vencido
    for comprador, ids in pedidos_por_devedor.items():
        alertas.append(
            f"{', '.join(ids)} é de {comprador}, que tem "
            f'{_formatar_brl(valor_por_devedor[comprador])} vencidos com o produtor; '
            'a alocação depende de validação da gestão.'
        )

    # Flags determinísticas (F5 do Bloco 4): com dois resumos no turno, o modelo deixava de
    # copiar as flags, e às vezes marcava como inadimplente o produtor, que é o credor.
    rotulo = f'{nome_completo} ({prod_id})'
    flags = [Flag(tipo='conformidade', descricao=f'{rotulo}: {a}') for a in alertas_documentais]
    for pend in pendencias_ordenadas:
        if pend.vencido <= 0:
            continue
        descricao = (
            f'{pend.comprador} tem {_formatar_brl(pend.vencido)} vencidos com {rotulo} '
            f'({pend.maior_atraso_dias} dias de atraso).'
        )
        ids = [
            pid
            for comprador, lista_ids in pedidos_por_devedor.items()
            if _mesmo_comprador(comprador, pend.comprador)
            for pid in lista_ids
        ]
        if ids:
            descricao += (
                f" Pedidos em aberto desse comprador ({', '.join(ids)}) dependem de "
                'validação da gestão.'
            )
        flags.append(Flag(tipo='inadimplencia', descricao=descricao))
    registrar_flags(ctx.deps, flags)

    # 5. Rascunho em formatação padrão;
    primeiro_nome = nome_completo.split()[0] if nome_completo else prod_id
    linhas_rascunho = [
        f'Olá, {primeiro_nome} ({prod_id})! Segue o seu resumo na Cooperativa Sabor da Terra '
        f"(referência: {data_ref.strftime('%d/%m/%Y')}):",
        '',
        'Financeiro:',
        f'- Vendas em setembro: {_formatar_brl(vendas_setembro)}',
        f'- Vendas em outubro: {_formatar_brl(vendas_outubro)}',
        f'- Valor pendente a receber: {_formatar_brl(fin_geral.total_pendente)}',
    ]
    if fin_geral.total_vencido > 0:
        linhas_rascunho.append(f'- Valor vencido: {_formatar_brl(fin_geral.total_vencido)}')
    for pend in pendencias_ordenadas:
        partes = []
        if pend.vencido > 0:
            partes.append(
                f'{_formatar_brl(pend.vencido)} vencidos '
                f'({pend.maior_atraso_dias} dias de atraso)'
            )
        if pend.a_vencer > 0:
            partes.append(f'{_formatar_brl(pend.a_vencer)} a vencer')
        linhas_rascunho.append(f"  - {pend.comprador}: {'; '.join(partes)}")

    if pedidos_elegiveis:
        linhas_rascunho.append('')
        linhas_rascunho.append('Pedidos em aberto com seus produtos:')
        for ped in pedidos_elegiveis:
            qtd_str = (
                f'{ped.quantidade:g} {ped.unidade}'
                if ped.quantidade is not None and ped.unidade
                else 'quantidade a confirmar'
            )
            entrega_str = (
                f" (entrega em {ped.data_entrega.strftime('%d/%m')})" if ped.data_entrega else ''
            )
            linhas_rascunho.append(f'- {ped.pedido_id}: {ped.produto} — {qtd_str}{entrega_str}')
    else:
        linhas_rascunho.append('')
        linhas_rascunho.append('Pedidos em aberto: nenhum pedido elegível no momento.')

    if alertas:
        linhas_rascunho.append('')
        linhas_rascunho.append('Avisos e pendências:')
        for alerta in alertas:
            linhas_rascunho.append(f'- {alerta}')

    rascunho = Rascunho(
        destinatario=f'{nome_completo} ({prod_id})',
        canal='padrao',
        conteudo='\n'.join(linhas_rascunho),
    )
    registrar_rascunhos(ctx.deps, [rascunho])

    return ResumoProdutorResultado(
        encontrado=True,
        produtor_id=prod_id,
        produtor_nome=nome_completo,
        data_referencia=data_ref,
        vendas_setembro=vendas_setembro,
        vendas_outubro=vendas_outubro,
        total_vendido_periodo=fin_geral.total_vendido,
        total_pendente=fin_geral.total_pendente,
        total_vencido=fin_geral.total_vencido,
        pendencias_por_comprador=pendencias_ordenadas,
        certificacao_organica=situacao_cert,
        validade_certificacao=data_val_efetiva,
        registro_sie=registro_sie,
        pedidos_em_aberto=pedidos_elegiveis,
        alertas=alertas,
        flags=flags,
        rascunho=rascunho,
        fontes=fontes,
    )