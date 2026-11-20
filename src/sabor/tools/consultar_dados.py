"""Ferramentas de consulta estruturada SQL read-only (Issue #7):
produtores, financeiro, lotes, preços e sazonalidade.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Literal

import psycopg
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps
from sabor.tools.base import (
    _MESES,
    Fonte,
    SemPermissao,
    Usuario,
    _aplicar_filtro_produtor,
    Mascarador,
    _mascarar_opcional,
    _mascarar_pix,
    _resolver_produto_id,
    _resolver_usuario,
)

_MESES_NOME = (
    'janeiro', 'fevereiro', 'marco', 'abril', 'maio', 'junho',
    'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro',
)

# =============================================================================
# 0. listar_produtos_catalogo
# =============================================================================


class ProdutoCatalogo(BaseModel):
    id: int
    nome: str
    unidade: str
    categoria: str | None
    origem_animal: bool


class CatalogoProdutosResultado(BaseModel):
    produtos: list[ProdutoCatalogo]
    total: int
    fonte: Fonte


def listar_produtos_catalogo(
    ctx: RunContext[AgentDeps],
    *,
    categoria: str | None = None,
    termo: str | None = None,
) -> CatalogoProdutosResultado:
    """Lista o catálogo canônico diretamente da tabela `produtos`.

    O catálogo mostra os produtos cadastrados, não estoque nem disponibilidade atual.
    Para descobrir quais produtores fornecem um produto, use `buscar_produtores`.
    """
    condicoes: list[str] = []
    parametros: list[str] = []

    if categoria and categoria.strip():
        condicoes.append('categoria ILIKE %s')
        parametros.append(f'%{categoria.strip()}%')
    if termo and termo.strip():
        condicoes.append('nome ILIKE %s')
        parametros.append(f'%{termo.strip()}%')

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ''
    linhas = ctx.deps.conn.execute(
        f"""
        SELECT id, nome, unidade, categoria, origem_animal
        FROM produtos
        {where_sql}
        ORDER BY categoria NULLS LAST, nome
        """,
        parametros,
    ).fetchall()
    produtos = [
        ProdutoCatalogo(
            id=row[0],
            nome=row[1],
            unidade=row[2],
            categoria=row[3],
            origem_animal=bool(row[4]),
        )
        for row in linhas
    ]
    return CatalogoProdutosResultado(
        produtos=produtos,
        total=len(produtos),
        fonte=Fonte(arquivo='produtos', referencia='catálogo canônico'),
    )


# =============================================================================
# 1. buscar_produtores
# =============================================================================


class ProdutorResultado(BaseModel):
    id: str
    nome_completo: str
    municipio: str
    status: Literal['ativo', 'inativo']
    produtos: list[str]
    produtos_origem_animal: list[str] = Field(default_factory=list)
    certificacao_organica_declarada: bool
    certificacao_organica_vigente: bool
    validade_certificacao: date | None
    registro_sie: str | None
    dap_caf: str | None
    cpf: str | None
    telefone: str | None
    email: str | None
    chave_pix: str | None
    banco: str | None
    agencia: str | None
    conta_corrente: str | None
    fonte: Fonte


def buscar_produtores(
    ctx: RunContext[AgentDeps],
    *,
    produtor_id: str | None = None,
    nome: str | None = None,
    produto: str | None = None,
    municipio: str | None = None,
    organico: bool | None = None,
    ativo: bool | None = None,
) -> list[ProdutorResultado]:
    """Busca produtores por ID, nome, produto/categoria, município, orgânico e status."""
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    data_referencia = ctx.deps.data_referencia
    mascarador = ctx.deps.mascarador

    produtor_id_norm = produtor_id.strip().upper() if produtor_id and produtor_id.strip() else None
    produtor_id_filtro = _aplicar_filtro_produtor(usuario, produtor_id_norm)

    condicoes: list[str] = []
    parametros: list[object] = []

    if produtor_id_filtro is not None:
        condicoes.append('p.id = %s')
        parametros.append(produtor_id_filtro)

    if nome is not None and nome.strip():
        condicoes.append('p.nome_completo ILIKE %s')
        parametros.append(f'%{nome.strip()}%')

    if municipio is not None:
        condicoes.append('p.municipio ILIKE %s')
        parametros.append(municipio)

    if ativo is not None:
        condicoes.append('p.status = %s')
        parametros.append('ativo' if ativo else 'inativo')

    if organico is not None:
        if organico:
            condicoes.append('(p.certificacao_organica AND p.validade_certificacao >= %s)')
        else:
            condicoes.append(
                '(NOT p.certificacao_organica OR p.validade_certificacao IS NULL '
                'OR p.validade_certificacao < %s)'
            )
        parametros.append(data_referencia)

    if produto is not None:
        produto_id = _resolver_produto_id(conn, produto)
        if produto_id is not None:
            condicoes.append(
                'p.id IN (SELECT produtor_id FROM produtor_produtos WHERE produto_id = %s)'
            )
            parametros.append(produto_id)
        else:
            condicoes.append(
                'p.id IN ('
                'SELECT pp.produtor_id FROM produtor_produtos pp '
                'JOIN produtos pr ON pr.id = pp.produto_id '
                'WHERE pr.categoria ILIKE %s)'
            )
            parametros.append(produto)

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ''

    linhas = conn.execute(
        f"""
        SELECT p.id, p.nome_completo, p.cpf, p.telefone, p.email, p.municipio,
               p.certificacao_organica, p.validade_certificacao, p.status,
               p.chave_pix, p.banco, p.agencia, p.conta_corrente,
               p.dap_caf, p.registro_sie,
               COALESCE(ARRAY_AGG(DISTINCT pr.nome) FILTER (WHERE pr.nome IS NOT NULL), '{{}}')
                   AS produtos,
               COALESCE(
                   ARRAY_AGG(DISTINCT pr.nome) FILTER (WHERE pr.origem_animal), '{{}}'
               ) AS produtos_origem_animal
        FROM produtores p
        LEFT JOIN produtor_produtos pp ON pp.produtor_id = p.id
        LEFT JOIN produtos pr ON pr.id = pp.produto_id
        {where_sql}
        GROUP BY p.id
        ORDER BY p.id
        """,
        parametros,
    ).fetchall()

    resultado: list[ProdutorResultado] = []
    for row in linhas:
        (
            pid,
            nome,
            cpf,
            telefone,
            email,
            municipio_,
            org_decl,
            validade,
            status,
            pix,
            banco,
            agencia,
            conta,
            dap,
            sie,
            produtos_lista,
            produtos_animais,
        ) = row
        vigente = bool(org_decl) and validade is not None and validade >= data_referencia
        resultado.append(
            ProdutorResultado(
                id=pid,
                nome_completo=nome,
                municipio=municipio_,
                status=status,
                produtos=list(produtos_lista or []),
                produtos_origem_animal=list(produtos_animais or []),
                certificacao_organica_declarada=bool(org_decl),
                certificacao_organica_vigente=vigente,
                validade_certificacao=validade,
                registro_sie=sie,
                dap_caf=dap,
                cpf=_mascarar_opcional(mascarador, cpf, 'CPF'),
                telefone=_mascarar_opcional(mascarador, telefone, 'TEL'),
                email=_mascarar_opcional(mascarador, email, 'EMAIL'),
                chave_pix=_mascarar_pix(mascarador, pix),
                banco=_mascarar_opcional(mascarador, banco, 'BANCO'),
                agencia=_mascarar_opcional(mascarador, agencia, 'AGENCIA'),
                conta_corrente=_mascarar_opcional(mascarador, conta, 'CONTA'),
                fonte=Fonte(arquivo='produtores.csv', referencia=pid),
            )
        )
    return resultado


# =============================================================================
# 2. consultar_financeiro
# =============================================================================


class ItemFinanceiro(BaseModel):
    id_transacao: str
    data: date
    produtor_id: str
    produtor_nome: str | None = None
    comprador: str
    produto: str
    quantidade: float
    unidade: str
    valor_unitario: float
    valor_total: float
    status_pagamento: Literal['pago', 'pendente']
    vencido: bool
    dias_atraso: int = 0
    data_vencimento: date | None
    data_pagamento: date | None
    alerta_divergencia: bool
    chave_pix_deposito: str | None
    fonte: Fonte


class ResumoFinanceiro(BaseModel):
    itens: list[ItemFinanceiro]
    total_vendido: float
    total_pago: float
    total_pendente: float
    total_vencido: float


def consultar_financeiro(
    ctx: RunContext[AgentDeps],
    *,
    produtor_id: str | None = None,
    comprador: str | None = None,
    periodo_inicio: date | None = None,
    periodo_fim: date | None = None,
    status: Literal['pago', 'pendente', 'vencido'] | None = None,
    apenas_divergentes: bool = False,
) -> ResumoFinanceiro:
    """Consulta vendas, pendências e vencidos (já inclui nome do produtor e dias de atraso)."""
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    data_referencia = ctx.deps.data_referencia
    mascarador = ctx.deps.mascarador

    produtor_id_filtro = _aplicar_filtro_produtor(usuario, produtor_id)

    condicoes: list[str] = []
    parametros: list[object] = []

    if produtor_id_filtro is not None:
        condicoes.append('f.produtor_id = %s')
        parametros.append(produtor_id_filtro)
    if comprador is not None:
        condicoes.append('f.comprador_normalizado ILIKE %s')
        parametros.append(f'%{comprador}%')
    if periodo_inicio is not None:
        condicoes.append('f.data >= %s')
        parametros.append(periodo_inicio)
    if periodo_fim is not None:
        condicoes.append('f.data <= %s')
        parametros.append(periodo_fim)
    if status == 'pago':
        condicoes.append("f.status_pagamento = 'pago'")
    elif status == 'pendente':
        condicoes.append("f.status_pagamento = 'pendente'")
    elif status == 'vencido':
        condicoes.append("f.status_pagamento = 'pendente' AND f.data_vencimento < %s")
        parametros.append(data_referencia)
    if apenas_divergentes:
        condicoes.append('f.alerta_divergencia')

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ''

    linhas = conn.execute(
        f"""
        SELECT f.id_transacao, f.data, f.produtor_id,
               COALESCE(f.comprador_normalizado, f.comprador),
               f.produto, f.quantidade, f.unidade, f.valor_unitario, f.valor_total,
               f.status_pagamento, f.data_vencimento, f.data_pagamento,
               f.alerta_divergencia, f.chave_pix_deposito, p.nome_completo
        FROM financeiro f
        JOIN produtores p ON p.id = f.produtor_id
        {where_sql}
        ORDER BY f.data
        """,
        parametros,
    ).fetchall()

    itens: list[ItemFinanceiro] = []
    for row in linhas:
        (
            idt,
            data_,
            pid,
            comp,
            prod,
            qtd,
            uni,
            vu,
            vt,
            status_pg,
            venc,
            pago_em,
            divergencia,
            pix_dep,
        ) = row[:14]
        pnome = row[14] if len(row) > 14 else None
        vencido = status_pg == 'pendente' and venc is not None and venc < data_referencia
        dias_atraso = (data_referencia - venc).days if vencido and venc is not None else 0
        itens.append(
            ItemFinanceiro(
                id_transacao=idt,
                data=data_,
                produtor_id=pid,
                produtor_nome=pnome,
                comprador=comp,
                produto=prod,
                quantidade=float(qtd),
                unidade=uni,
                valor_unitario=float(vu),
                valor_total=float(vt),
                status_pagamento=status_pg,
                vencido=vencido,
                dias_atraso=dias_atraso,
                data_vencimento=venc,
                data_pagamento=pago_em,
                alerta_divergencia=bool(divergencia),
                chave_pix_deposito=_mascarar_pix(mascarador, pix_dep),
                fonte=Fonte(arquivo='financeiro.csv', referencia=idt),
            )
        )

    def soma(pred) -> float:
        return round(sum(i.valor_total for i in itens if pred(i)), 2)

    return ResumoFinanceiro(
        itens=itens,
        total_vendido=soma(lambda _: True),
        total_pago=soma(lambda i: i.status_pagamento == 'pago'),
        total_pendente=soma(lambda i: i.status_pagamento == 'pendente'),
        total_vencido=soma(lambda i: i.vencido),
    )


# =============================================================================
# 3. rastrear_lote
# =============================================================================


class LoteResultado(BaseModel):
    lote_id: str
    produto: str
    produtor_id: str
    produtor_nome: str
    quantidade: float
    unidade: str
    data_colheita: date
    data_entrega: date
    comprador: str | None
    refrigerado: bool
    temperatura_transporte_c: float | None
    validade: date | None
    inspecao_visual: str | None
    transacao_id: str | None
    fonte: Fonte


class RastrearLoteResultado(BaseModel):
    encontrado: bool
    lote: LoteResultado | None = None
    candidatos_ambiguos: list[str] = Field(default_factory=list)
    mensagem: str | None = None


_PADRAO_LOTE_ID = re.compile(r'^LOT-\d{4}-\d{4}$', re.IGNORECASE)
_PADRAO_NUMERO = re.compile(r'(\d{4})[-\s]?(\d{3,4})')


def rastrear_lote(
    ctx: RunContext[AgentDeps],
    *,
    lote_id: str | None = None,
    texto: str | None = None,
) -> RastrearLoteResultado:
    """Rastreia lote por ID exato (LOT-2026-0089) ou aproximado (ex.: 'lote 2024-089')."""
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    mascarador = ctx.deps.mascarador

    if not lote_id and not texto:
        raise ValueError('Informe lote_id ou texto.')

    bruto = (lote_id or texto or '').strip()
    candidato_exato = bruto.upper()

    if _PADRAO_LOTE_ID.match(candidato_exato):
        linha = conn.execute(
            'SELECT lote_id FROM lotes WHERE lote_id = %s', (candidato_exato,)
        ).fetchone()
        if linha:
            return _montar_resultado_lote(
                conn, usuario, linha[0], mascarador=mascarador
            )

    match = _PADRAO_NUMERO.search(bruto)
    if not match:
        return RastrearLoteResultado(
            encontrado=False,
            mensagem=f"Não encontrei um identificador de lote em '{bruto}'.",
        )

    sufixo = match.group(2).lstrip('0') or '0'
    linhas = conn.execute(
        'SELECT lote_id FROM lotes WHERE lote_id LIKE %s', ('LOT-%-%',)
    ).fetchall()
    candidatos = [row[0] for row in linhas if row[0].rsplit('-', 1)[-1].lstrip('0') == sufixo]

    if len(candidatos) == 1:
        return _montar_resultado_lote(
            conn,
            usuario,
            candidatos[0],
            mascarador=mascarador,
            confirmar=True,
            texto_original=bruto,
        )
    if len(candidatos) > 1:
        return RastrearLoteResultado(
            encontrado=False,
            candidatos_ambiguos=candidatos,
            mensagem=f"'{bruto}' bate com mais de um lote. Qual deles você quis dizer?",
        )
    return RastrearLoteResultado(
        encontrado=False,
        mensagem=f"Nenhum lote encontrado para '{bruto}'.",
    )


def _montar_resultado_lote(
    conn: psycopg.Connection,
    usuario: Usuario,
    lote_id: str,
    *,
    mascarador: Mascarador,
    confirmar: bool = False,
    texto_original: str = '',
) -> RastrearLoteResultado:
    linha = conn.execute(
        """
        SELECT l.lote_id, l.produto, l.produtor_id, p.nome_completo, l.quantidade,
               l.unidade, l.data_colheita, l.data_entrega,
               COALESCE(l.comprador_normalizado, l.comprador), l.refrigerado,
               l.temperatura_transporte_c, l.validade, l.inspecao_visual, l.transacao_id
        FROM lotes l
        JOIN produtores p ON p.id = l.produtor_id
        WHERE l.lote_id = %s
        """,
        (lote_id,),
    ).fetchone()
    if linha is None:
        return RastrearLoteResultado(encontrado=False, mensagem=f'Lote {lote_id} não existe.')

    (
        lid,
        produto,
        pid,
        pnome,
        qtd,
        uni,
        colheita,
        entrega,
        comprador,
        refrig,
        temp,
        validade,
        insp,
        transacao_id,
    ) = linha

    if usuario.perfil == 'produtor' and pid != usuario.produtor_id:
        raise SemPermissao(f'Perfil produtor ({usuario.produtor_id}) não pode ver o lote {lid}.')

    lote = LoteResultado(
        lote_id=lid,
        produto=produto,
        produtor_id=pid,
        produtor_nome=pnome,
        quantidade=float(qtd),
        unidade=uni,
        data_colheita=colheita,
        data_entrega=entrega,
        comprador=comprador,
        refrigerado=refrig,
        temperatura_transporte_c=float(temp) if temp is not None else None,
        validade=validade,
        inspecao_visual=_mascarar_opcional(mascarador, insp),
        transacao_id=transacao_id,
        fonte=Fonte(arquivo='lotes.csv', referencia=lid),
    )
    mensagem = (
        f"'{texto_original}' não bate exato com um ID; confirmando: você quis dizer {lid}?"
        if confirmar
        else None
    )
    return RastrearLoteResultado(encontrado=True, lote=lote, mensagem=mensagem)


# =============================================================================
# 4. consultar_preco
# =============================================================================


class FaixaPreco(BaseModel):
    tipo: Literal['varejo', 'atacado']
    organico: bool
    preco_unitario: float


class PrecoResultado(BaseModel):
    produto: str
    unidade: str
    preco_aplicavel: float | None
    organico_disponivel: bool
    faixas: list[FaixaPreco]
    volume_minimo_atacado: float
    desconto_atacado_pct: float
    observacao: str | None
    fonte: Fonte


def consultar_preco(
    ctx: RunContext[AgentDeps],
    produto: str,
    *,
    organico: bool | None = None,
    quantidade: float | None = None,
) -> PrecoResultado | None:
    """Consulta preço vigente (convencional ou orgânico) e regra de desconto de atacado."""
    conn = ctx.deps.conn
    produto_id = _resolver_produto_id(conn, produto)
    if produto_id is None:
        return None

    linha = conn.execute(
        """
        SELECT produto, unidade, preco_convencional, preco_organico,
               desconto_atacado_pct, volume_minimo_atacado, observacao, referencia
        FROM precos
        WHERE produto_id = %s AND referencia <= %s
        ORDER BY referencia DESC
        LIMIT 1
        """,
        (produto_id, ctx.deps.data_referencia),
    ).fetchone()
    if linha is None:
        return None

    nome, unidade, p_conv, p_org, desc_pct, vol_min, obs, referencia_tabela = linha
    usar_organico = bool(organico) and p_org is not None
    base = p_org if usar_organico else p_conv

    faixas: list[FaixaPreco] = []
    if base is not None:
        faixas.append(FaixaPreco(tipo='varejo', organico=usar_organico, preco_unitario=float(base)))
        if vol_min and quantidade is not None and quantidade >= float(vol_min):
            faixas.append(
                FaixaPreco(
                    tipo='atacado',
                    organico=usar_organico,
                    preco_unitario=round(float(base) * (1 - float(desc_pct) / 100), 2),
                )
            )

    return PrecoResultado(
        produto=nome,
        unidade=unidade,
        preco_aplicavel=faixas[-1].preco_unitario if faixas else None,
        organico_disponivel=p_org is not None,
        faixas=faixas,
        volume_minimo_atacado=float(vol_min or 0),
        desconto_atacado_pct=float(desc_pct or 0),
        observacao=_mascarar_opcional(ctx.deps.mascarador, obs),
        fonte=Fonte(
            arquivo=f'tabela_precos_{_MESES_NOME[referencia_tabela.month - 1]}'
            f'_{referencia_tabela.year}.csv',
            referencia=nome,
        ),
    )


# =============================================================================
# 5. consultar_safra
# =============================================================================


class SafraResultado(BaseModel):
    produto: str
    mes: int
    nivel: Literal['nenhuma', 'baixa', 'media', 'alta']
    observacao: str | None
    produtores: list[str]
    fonte: Fonte


def consultar_safra(
    ctx: RunContext[AgentDeps],
    produto: str,
    mes: int | str,
) -> SafraResultado | None:
    """Consulta disponibilidade sazonal de um produto em determinado mês (1-12 ou abreviação)."""
    conn = ctx.deps.conn
    produto_id = _resolver_produto_id(conn, produto)
    if produto_id is None:
        return None

    mes_num = mes if isinstance(mes, int) else _MESES.get(str(mes).strip().lower()[:3])
    if mes_num is None or not (1 <= mes_num <= 12):
        raise ValueError(f'Mês inválido: {mes!r}')

    linha = conn.execute(
        """
        SELECT s.produto, sm.nivel, s.observacao
        FROM sazonalidade s
        JOIN sazonalidade_mensal sm ON sm.produto = s.produto
        WHERE s.produto_id = %s AND sm.mes = %s
        """,
        (produto_id, mes_num),
    ).fetchone()
    if linha is None:
        return None
    nome, nivel, obs = linha

    produtores = [
        row[0]
        for row in conn.execute(
            'SELECT DISTINCT produtor_id FROM produtor_produtos WHERE produto_id = %s ORDER BY 1',
            (produto_id,),
        ).fetchall()
    ]

    return SafraResultado(
        produto=nome,
        mes=mes_num,
        nivel=nivel,
        observacao=_mascarar_opcional(ctx.deps.mascarador, obs),
        produtores=produtores,
        fonte=Fonte(arquivo='sazonalidade.csv', referencia=f'{nome} · mês {mes_num}'),
    )


# =============================================================================
# 6. listar_lotes (apoio aos relatórios, Issue #15)
# =============================================================================


class LoteResumo(BaseModel):
    lote_id: str
    produto: str
    produtor_id: str
    data_colheita: date
    data_entrega: date
    comprador: str | None
    fonte: Fonte


def listar_lotes(
    ctx: RunContext[AgentDeps],
    *,
    produtor_id: str | None = None,
) -> list[LoteResumo]:
    """Lista os lotes do mais recente para o mais antigo (por data de colheita/fabricação).

    Não é exportada em `sabor.tools`, então não vira tool do agente: serve aos relatórios
    (#15), que precisam dos últimos lotes de cada produto. Aplica o mesmo filtro de perfil
    das demais consultas.
    """
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    produtor_id_filtro = _aplicar_filtro_produtor(usuario, produtor_id)

    where_sql = 'WHERE l.produtor_id = %s' if produtor_id_filtro is not None else ''
    parametros = [produtor_id_filtro] if produtor_id_filtro is not None else []
    linhas = conn.execute(
        f"""
        SELECT l.lote_id, l.produto, l.produtor_id, l.data_colheita, l.data_entrega,
               COALESCE(l.comprador_normalizado, l.comprador)
        FROM lotes l
        {where_sql}
        ORDER BY l.data_colheita DESC, l.lote_id DESC
        """,
        parametros,
    ).fetchall()
    return [
        LoteResumo(
            lote_id=lid,
            produto=produto,
            produtor_id=pid,
            data_colheita=colheita,
            data_entrega=entrega,
            comprador=_mascarar_opcional(ctx.deps.mascarador, comprador),
            fonte=Fonte(arquivo='lotes.csv', referencia=lid),
        )
        for lid, produto, pid, colheita, entrega, comprador in linhas
    ]
