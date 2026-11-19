"""Ferramenta resumir_produtor (Etapa 3.3, Pilar 4.3 — Issue #11):
Resumo operacional e financeiro sob demanda formatado como rascunho.
"""

from __future__ import annotations

from datetime import date
import re
import unicodedata

import psycopg
from typing import Annotated
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps
from sabor.agent.schemas import Rascunho
from sabor.tools.base import (
    Fonte,
    SemPermissao,
    _aplicar_filtro_produtor,
    _mascarar_opcional,
    _resolver_usuario,
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
    fonte: Fonte


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
    pedidos_em_aberto: list[PedidoElegivelResumo] = Field(default_factory=list)
    alertas: list[str] = Field(default_factory=list)
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


def _chunk_tem_nc_para_produtor(
    texto_nc: str,
    prod_id: str,
    doc_produtor_id: str | None,
) -> bool:
    """Verifica se há apontamento/NC aplicável ao `prod_id` no chunk.

    Em documentos individuais (`doc_produtor_id == prod_id`), avalia o chunk inteiro.
    Em documentos coletivos (`doc_produtor_id is None`, ex.: relatório da ADAGRO),
    isola apenas os blocos/linhas que citam o `prod_id` para não atribuir a um produtor
    regular a não conformidade de outro produtor citado no mesmo chunk.
    """
    if doc_produtor_id is not None and doc_produtor_id.upper() == prod_id.upper():
        blocos = [texto_nc]
    else:
        padrao_alvo = re.compile(rf'\b{re.escape(prod_id)}\b', re.IGNORECASE)
        blocos = []
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

    for bloco in blocos:
        bloco_low = bloco.lower()
        if any(termo in bloco_low for termo in _TERMOS_NC_ADAGRO):
            return True
    return False


def _remover_acentos(texto: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto) if unicodedata.category(c) != 'Mn'
    ).lower()


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
      pendentes/vencidos, pedidos em aberto elegíveis e alertas documentais
      (certificação orgânica, ADAGRO, SIE, laudos) de uma só vez, já gerando
      o rascunho de mensagem em `rascunhos` da resposta estruturada.

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

    # Busca NCs da ADAGRO nos chunks do próprio produtor OU no relatório geral de inspeção
    chunks_nc = conn.execute(
        """
        SELECT DISTINCT c.arquivo, COALESCE(c.secao, 'GERAL'), c.texto, d.produtor_id
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE (
            d.produtor_id = %s
            OR (d.tipo_doc = 'inspecao' AND c.texto ILIKE %s)
        )
        ORDER BY c.arquivo
        """,
        (prod_id, f'%{prod_id}%'),
    ).fetchall()

    for arq_nc, secao_nc, texto_nc, doc_prod_id in chunks_nc:
        if _chunk_tem_nc_para_produtor(texto_nc, prod_id, doc_prod_id):
            _adicionar_fonte(Fonte(arquivo=arq_nc, referencia=secao_nc))
            alertas.append(f'Não conformidade / apontamento ADAGRO registrado ({arq_nc}).')
            break

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

    # 4. Pedidos em aberto com os produtos do produtor, filtrados por elegibilidade
    pedidos_elegiveis: list[PedidoElegivelResumo] = []
    if status_prod == 'ativo':
        linhas_pedidos = conn.execute(
            """
            SELECT DISTINCT
                p.id,
                COALESCE(p.comprador_normalizado, p.comprador) AS comprador,
                p.data_entrega,
                pr.nome AS produto_nome,
                pi.quantidade,
                pi.unidade,
                pi.organico,
                pr.origem_animal
            FROM pedidos p
            JOIN pedido_itens pi ON pi.pedido_id = p.id
            JOIN produtor_produtos pp ON pp.produto_id = pi.produto_id
            JOIN produtos pr ON pr.id = pi.produto_id
            WHERE pp.produtor_id = %s
              AND p.status = 'pendente'
              AND COALESCE(p.tipo, 'compra') IN ('compra', 'compra_recorrente', 'cotacao')
            ORDER BY p.data_entrega NULLS LAST, p.id, pr.nome
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
            pedidos_elegiveis.append(
                PedidoElegivelResumo(
                    pedido_id=ped_id,
                    comprador=_mascarar_opcional(mascarador, comprador),
                    data_entrega=dt_entrega,
                    produto=prod_nome,
                    quantidade=float(qtd) if qtd is not None else None,
                    unidade=unidade,
                    organico=exige_org,
                    fonte=fonte_ped,
                )
            )

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
        linhas_rascunho.append('Avisos e pendências documentais:')
        for alerta in alertas:
            linhas_rascunho.append(f'- {alerta}')

    rascunho = Rascunho(
        destinatario=f'{nome_completo} ({prod_id})',
        canal='padrao',
        conteudo='\n'.join(linhas_rascunho),
    )

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
        pedidos_em_aberto=pedidos_elegiveis,
        alertas=alertas,
        rascunho=rascunho,
        fontes=fontes,
    )