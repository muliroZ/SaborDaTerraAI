from __future__ import annotations


"""Ferramentas (tools) read-only do agente SaborDaTerraAI.

Convenções para implementação das ferramentas (Issues #7, #8, #9, #11, #12):
1. Nomenclatura: defina as funções públicas em `snake_case` (sem prefixo `_`).
   O agente descobre e registra automaticamente as funções públicas definidas neste módulo.
   Nomes de referência previstos na arquitetura:
   - `extrair_e_classificar_pedido` (Pilar 4.1 - Issue #9)
   - `consultar_dados` (SQL parametrizada - Issue #7)
   - `buscar_documentos` (Pilar 4.2: busca híbrida lexical + vetorial - Issue #8)
   - `resumir_produtor` (Pilar 4.3: resumo do produtor - Issue #11)
   - `rascunhar_notificacao` (Pilar 4.3: notificação e proposta de alocação - Issue #12)
   Caso seja necessário criar ferramentas auxiliares adicionais, basta seguir `snake_case`.

2. Assinatura e Acesso a Dados:
   - Receba `ctx: RunContext[AgentDeps]` como primeiro parâmetro quando precisar de contexto.
   - Utilize exclusivamente `ctx.deps.conn` (conexão somente leitura `sabor_ro`) e
     `ctx.deps.data_referencia` para regras temporais.
"""

"""Ferramentas (tools) read-only do agente SaborDaTerraAI (Issue #7).

SQL parametrizada e read-only sobre produtores, financeiro, lotes, preços e
sazonalidade. Todas as funções públicas em `snake_case` recebem
`ctx: RunContext[AgentDeps]` e utilizam `ctx.deps.conn` (`sabor_ro`).


Estado das dependências quando este arquivo foi escrito (17/11/2026):

  - #4 (carga dos dados): concluída. As queries assumem o schema de
    `docs/Arquitetura...md` / `schema.sql` já populado.
  - #33 (mascarador): NÃO iniciada. Ver `Mascarador` / `SEM_MASCARAMENTO`
    abaixo — a assinatura já prevê o parâmetro, mas por enquanto ele não
    mascara nada.
  - #13 (seletor de usuário simulado): não vista. O modelo `Usuario` abaixo
    é um placeholder mínimo só para poder aplicar a regra "perfil produtor
    só vê os próprios dados" exigida por esta issue. Trocar quando #13
    definir o formato real.
    
"""

import re
from datetime import date
from typing import Literal, Protocol

from google import genai
from google.genai import types
from sabor.db import EMBEDDING_DIM

import psycopg
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

DATA_REFERENCIA_PADRAO = date(2026, 10, 30)
TABELA_PRECOS_REFERENCIA_PADRAO = date(2026, 10, 1)

_MESES = {
    'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
    'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12,
}


# ---------------------------------------------------------------------------
# Perfil do usuário e aplicação do filtro de linha
# ---------------------------------------------------------------------------

class Usuario(BaseModel):
    """Perfil simulado logado. Decide o filtro de linha aplicado nas tools."""

    perfil: Literal['funcionario', 'produtor']
    produtor_id: str | None = None  # obrigatório quando perfil == 'produtor'


class SemPermissao(PermissionError):
    """Perfil produtor tentando ver dado de outro produtor."""


def _resolver_usuario(usuario: Usuario | str) -> Usuario:
    """Converte `AgentDeps.usuario` (str ou Usuario) para o contrato `Usuario`."""
    if isinstance(usuario, Usuario):
        return usuario
    texto = usuario.strip()
    if re.fullmatch(r'P\d{3}', texto, re.IGNORECASE):
        return Usuario(perfil='produtor', produtor_id=texto.upper())
    return Usuario(perfil='funcionario')


def _aplicar_filtro_produtor(usuario: Usuario, produtor_id: str | None) -> str | None:
    """Resolve o produtor_id efetivo da consulta, aplicando o perfil.

    - Perfil funcionário: passa produtor_id como veio (None = sem filtro).
    - Perfil produtor: sempre trava no próprio produtor_id. Se pedir
      explicitamente o dado de outro produtor, nega com `SemPermissao`.
    """
    if usuario.perfil == 'funcionario':
        return produtor_id
    if usuario.produtor_id is None:
        raise ValueError("Usuario com perfil='produtor' precisa de produtor_id.")
    if produtor_id is not None and produtor_id != usuario.produtor_id:
        raise SemPermissao(
            f"Perfil produtor ({usuario.produtor_id}) não pode consultar dados de {produtor_id}."
        )
    return usuario.produtor_id


# ---------------------------------------------------------------------------
# Mascarador (interface esperada da Issue #33)
# ---------------------------------------------------------------------------

class Mascarador(Protocol):
    """Interface mínima esperada da Issue #33: substitui dado pessoal por token."""

    def mascarar(self, texto: str) -> str: ...


class _SemMascaramento:
    """No-op: usado enquanto a Issue #33 não existe."""

    def mascarar(self, texto: str) -> str:
        return texto


SEM_MASCARAMENTO: Mascarador = _SemMascaramento()


def _mascarar_opcional(mascarador: Mascarador, valor: str | None) -> str | None:
    return mascarador.mascarar(valor) if valor is not None else None


def _mascarar_pix(mascarador: Mascarador, valor: str | None) -> str | None:
    if valor is None:
        return None
    mascarar_campo_fn = getattr(mascarador, 'mascarar_campo', None)
    if callable(mascarar_campo_fn):
        return mascarar_campo_fn(valor, 'PIX')
    return mascarador.mascarar(valor)


# ---------------------------------------------------------------------------
# Fonte (convenção de citação)
# ---------------------------------------------------------------------------

class Fonte(BaseModel):
    arquivo: str
    registro: str | None = None

    def __str__(self) -> str:
        return f"{self.arquivo} · {self.registro}" if self.registro else self.arquivo


# ---------------------------------------------------------------------------
# Resolução de produto_id (nome ou sinônimo)
# ---------------------------------------------------------------------------

def _resolver_produto_id(conn: psycopg.Connection, nome_ou_sinonimo: str) -> int | None:
    chave = nome_ou_sinonimo.strip().lower()
    linha = conn.execute("SELECT id FROM produtos WHERE lower(nome) = %s", (chave,)).fetchone()
    if linha:
        return linha[0]
    linha = conn.execute(
        "SELECT produto_id FROM produto_sinonimos WHERE lower(sinonimo) = %s", (chave,)
    ).fetchone()
    return linha[0] if linha else None


# =============================================================================
# 1. buscar_produtores
# =============================================================================

class ProdutorResultado(BaseModel):
    id: str
    nome_completo: str
    municipio: str
    status: Literal['ativo', 'inativo']
    produtos: list[str]
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
    produto: str | None = None,
    municipio: str | None = None,
    organico: bool | None = None,
    ativo: bool | None = None,
) -> list[ProdutorResultado]:
    """Busca produtores por produto/categoria, município, orgânico e status."""
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    data_referencia = ctx.deps.data_referencia
    mascarador = ctx.deps.mascarador

    produtor_id_filtro = _aplicar_filtro_produtor(usuario, None)

    condicoes: list[str] = []
    parametros: list[object] = []

    if produtor_id_filtro is not None:
        condicoes.append("p.id = %s")
        parametros.append(produtor_id_filtro)

    if municipio is not None:
        condicoes.append("p.municipio ILIKE %s")
        parametros.append(municipio)

    if ativo is not None:
        condicoes.append("p.status = %s")
        parametros.append('ativo' if ativo else 'inativo')

    if organico is not None:
        if organico:
            condicoes.append("(p.certificacao_organica AND p.validade_certificacao >= %s)")
        else:
            condicoes.append(
                "(NOT p.certificacao_organica OR p.validade_certificacao IS NULL "
                "OR p.validade_certificacao < %s)"
            )
        parametros.append(data_referencia)

    if produto is not None:
        produto_id = _resolver_produto_id(conn, produto)
        if produto_id is not None:
            condicoes.append(
                "p.id IN (SELECT produtor_id FROM produtor_produtos WHERE produto_id = %s)"
            )
            parametros.append(produto_id)
        else:
            condicoes.append(
                "p.id IN ("
                "SELECT pp.produtor_id FROM produtor_produtos pp "
                "JOIN produtos pr ON pr.id = pp.produto_id "
                "WHERE pr.categoria ILIKE %s)"
            )
            parametros.append(produto)

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ""

    linhas = conn.execute(
        f"""
        SELECT p.id, p.nome_completo, p.cpf, p.telefone, p.email, p.municipio,
               p.certificacao_organica, p.validade_certificacao, p.status,
               p.chave_pix, p.banco, p.agencia, p.conta_corrente,
               p.dap_caf, p.registro_sie,
               COALESCE(ARRAY_AGG(DISTINCT pr.nome) FILTER (WHERE pr.nome IS NOT NULL), '{{}}')
                   AS produtos
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
        (pid, nome, cpf, telefone, email, municipio_, org_decl, validade, status,
         pix, banco, agencia, conta, dap, sie, produtos_lista) = row
        vigente = bool(org_decl) and validade is not None and validade >= data_referencia
        resultado.append(ProdutorResultado(
            id=pid,
            nome_completo=nome,
            municipio=municipio_,
            status=status,
            produtos=list(produtos_lista or []),
            certificacao_organica_declarada=bool(org_decl),
            certificacao_organica_vigente=vigente,
            validade_certificacao=validade,
            registro_sie=sie,
            dap_caf=dap,
            cpf=_mascarar_opcional(mascarador, cpf),
            telefone=_mascarar_opcional(mascarador, telefone),
            email=_mascarar_opcional(mascarador, email),
            chave_pix=_mascarar_pix(mascarador, pix),
            banco=_mascarar_opcional(mascarador, banco),
            agencia=_mascarar_opcional(mascarador, agencia),
            conta_corrente=_mascarar_opcional(mascarador, conta),
            fonte=Fonte(arquivo="produtores.csv", registro=pid),
        ))
    return resultado


# =============================================================================
# 2. consultar_financeiro
# =============================================================================

class ItemFinanceiro(BaseModel):
    id_transacao: str
    data: date
    produtor_id: str
    comprador: str
    produto: str
    quantidade: float
    unidade: str
    valor_unitario: float
    valor_total: float
    status_pagamento: Literal['pago', 'pendente']
    vencido: bool
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
    """Consulta vendas, pendências e vencidos em relação à data de referência."""
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    data_referencia = ctx.deps.data_referencia
    mascarador = ctx.deps.mascarador

    produtor_id_filtro = _aplicar_filtro_produtor(usuario, produtor_id)

    condicoes: list[str] = []
    parametros: list[object] = []

    if produtor_id_filtro is not None:
        condicoes.append("f.produtor_id = %s")
        parametros.append(produtor_id_filtro)
    if comprador is not None:
        condicoes.append("f.comprador_normalizado ILIKE %s")
        parametros.append(f"%{comprador}%")
    if periodo_inicio is not None:
        condicoes.append("f.data >= %s")
        parametros.append(periodo_inicio)
    if periodo_fim is not None:
        condicoes.append("f.data <= %s")
        parametros.append(periodo_fim)
    if status == 'pago':
        condicoes.append("f.status_pagamento = 'pago'")
    elif status == 'pendente':
        condicoes.append("f.status_pagamento = 'pendente'")
    elif status == 'vencido':
        condicoes.append("f.status_pagamento = 'pendente' AND f.data_vencimento < %s")
        parametros.append(data_referencia)
    if apenas_divergentes:
        condicoes.append("f.alerta_divergencia")

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ""

    linhas = conn.execute(
        f"""
        SELECT f.id_transacao, f.data, f.produtor_id,
               COALESCE(f.comprador_normalizado, f.comprador),
               f.produto, f.quantidade, f.unidade, f.valor_unitario, f.valor_total,
               f.status_pagamento, f.data_vencimento, f.data_pagamento,
               f.alerta_divergencia, f.chave_pix_deposito
        FROM financeiro f
        {where_sql}
        ORDER BY f.data
        """,
        parametros,
    ).fetchall()

    itens: list[ItemFinanceiro] = []
    for (idt, data_, pid, comp, prod, qtd, uni, vu, vt,
         status_pg, venc, pago_em, divergencia, pix_dep) in linhas:
        vencido = status_pg == 'pendente' and venc is not None and venc < data_referencia
        itens.append(ItemFinanceiro(
            id_transacao=idt, data=data_, produtor_id=pid, comprador=comp, produto=prod,
            quantidade=float(qtd), unidade=uni,
            valor_unitario=float(vu), valor_total=float(vt),
            status_pagamento=status_pg, vencido=vencido,
            data_vencimento=venc, data_pagamento=pago_em,
            alerta_divergencia=bool(divergencia),
            chave_pix_deposito=_mascarar_pix(mascarador, pix_dep),
            fonte=Fonte(arquivo="financeiro.csv", registro=idt),
        ))

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
    """Rastreia lote por ID exato (LOT-2026-0089) ou referência aproximada (ex.: 'lote 2024-089')."""
    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)

    if not lote_id and not texto:
        raise ValueError("Informe lote_id ou texto.")

    bruto = (lote_id or texto or "").strip()
    candidato_exato = bruto.upper()

    if _PADRAO_LOTE_ID.match(candidato_exato):
        linha = conn.execute(
            "SELECT lote_id FROM lotes WHERE lote_id = %s", (candidato_exato,)
        ).fetchone()
        if linha:
            return _montar_resultado_lote(conn, usuario, linha[0])

    match = _PADRAO_NUMERO.search(bruto)
    if not match:
        return RastrearLoteResultado(
            encontrado=False,
            mensagem=f"Não encontrei um identificador de lote em '{bruto}'.",
        )

    sufixo = match.group(2).lstrip('0') or '0'
    linhas = conn.execute(
        "SELECT lote_id FROM lotes WHERE lote_id LIKE %s", ('LOT-%-%',)
    ).fetchall()
    candidatos = [
        row[0] for row in linhas
        if row[0].rsplit('-', 1)[-1].lstrip('0') == sufixo
    ]

    if len(candidatos) == 1:
        return _montar_resultado_lote(
            conn, usuario, candidatos[0], confirmar=True, texto_original=bruto,
        )
    if len(candidatos) > 1:
        return RastrearLoteResultado(
            encontrado=False, candidatos_ambiguos=candidatos,
            mensagem=f"'{bruto}' bate com mais de um lote. Qual deles você quis dizer?",
        )
    return RastrearLoteResultado(
        encontrado=False, mensagem=f"Nenhum lote encontrado para '{bruto}'.",
    )


def _montar_resultado_lote(
    conn: psycopg.Connection,
    usuario: Usuario,
    lote_id: str,
    *,
    confirmar: bool = False,
    texto_original: str = "",
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
        return RastrearLoteResultado(encontrado=False, mensagem=f"Lote {lote_id} não existe.")

    (lid, produto, pid, pnome, qtd, uni, colheita, entrega, comprador,
     refrig, temp, validade, insp, transacao_id) = linha

    if usuario.perfil == 'produtor' and pid != usuario.produtor_id:
        raise SemPermissao(f"Perfil produtor ({usuario.produtor_id}) não pode ver o lote {lid}.")

    lote = LoteResultado(
        lote_id=lid, produto=produto, produtor_id=pid, produtor_nome=pnome,
        quantidade=float(qtd), unidade=uni, data_colheita=colheita, data_entrega=entrega,
        comprador=comprador, refrigerado=refrig,
        temperatura_transporte_c=float(temp) if temp is not None else None,
        validade=validade, inspecao_visual=insp, transacao_id=transacao_id,
        fonte=Fonte(arquivo="lotes.csv", registro=lid),
    )
    mensagem = (
        f"'{texto_original}' não bate exato com um ID; confirmando: você quis dizer {lid}?"
        if confirmar else None
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
    referencia: date = TABELA_PRECOS_REFERENCIA_PADRAO,
) -> PrecoResultado | None:
    """Consulta preço vigente (convencional ou orgânico) e regra de desconto de atacado."""
    conn = ctx.deps.conn
    produto_id = _resolver_produto_id(conn, produto)
    if produto_id is None:
        return None

    linha = conn.execute(
        """
        SELECT produto, unidade, preco_convencional, preco_organico,
               desconto_atacado_pct, volume_minimo_atacado, observacao
        FROM precos
        WHERE produto_id = %s AND referencia = %s
        """,
        (produto_id, referencia),
    ).fetchone()
    if linha is None:
        return None

    nome, unidade, p_conv, p_org, desc_pct, vol_min, obs = linha
    usar_organico = bool(organico) and p_org is not None
    base = p_org if usar_organico else p_conv

    faixas: list[FaixaPreco] = []
    if base is not None:
        faixas.append(FaixaPreco(tipo='varejo', organico=usar_organico, preco_unitario=float(base)))
        if vol_min and quantidade is not None and quantidade >= float(vol_min):
            faixas.append(FaixaPreco(
                tipo='atacado', organico=usar_organico,
                preco_unitario=round(float(base) * (1 - float(desc_pct) / 100), 2),
            ))

    return PrecoResultado(
        produto=nome,
        unidade=unidade,
        preco_aplicavel=faixas[-1].preco_unitario if faixas else None,
        organico_disponivel=p_org is not None,
        faixas=faixas,
        volume_minimo_atacado=float(vol_min or 0),
        desconto_atacado_pct=float(desc_pct or 0),
        observacao=obs,
        fonte=Fonte(arquivo="tabela_precos_outubro_2026.csv", registro=nome),
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
        raise ValueError(f"Mês inválido: {mes!r}")

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
        row[0] for row in conn.execute(
            "SELECT DISTINCT produtor_id FROM produtor_produtos WHERE produto_id = %s ORDER BY 1",
            (produto_id,),
        ).fetchall()
    ]

    return SafraResultado(
        produto=nome, mes=mes_num, nivel=nivel, observacao=obs, produtores=produtores,
        fonte=Fonte(arquivo="sazonalidade.csv", registro=f"{nome} · mês {mes_num}"),
    )


# =============================================================================
# 6. buscar_documentos (Issue #8 — Busca Híbrida Lexical + Vetorial com RRF)
# =============================================================================

TipoDocumento = Literal['certificado', 'laudo', 'inspecao', 'contrato', 'ficha']

RRF_K = 60
LIMIAR_SIMILARIDADE_VETORIAL = 0.55
LIMIAR_RRF = 0.010


class DocumentoResultado(BaseModel):
    arquivo: str
    secao: str | None
    citacao: str
    tipo_doc: TipoDocumento
    produtor_id: str | None
    produtor_nome: str | None
    produtor_status: str | None
    registro_sie: str | None
    dap_caf: str | None
    validade: date | None
    vigente: bool | None
    trecho: str
    score_rrf: float
    similaridade_vetorial: float | None
    score_lexical: float | None
    fonte: Fonte


class BuscaDocumentosResultado(BaseModel):
    encontrado: bool
    resultados: list[DocumentoResultado] = Field(default_factory=list)
    mensagem: str | None = None


def _gerar_embedding_consulta(pergunta: str) -> list[float]:
    """Gera embedding de 768 dimensões com task_type RETRIEVAL_QUERY."""
    client = genai.Client()
    resposta = client.models.embed_content(
        model="gemini-embedding-001",
        contents=pergunta,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=EMBEDDING_DIM,
        ),
    )
    return list(resposta.embeddings[0].values)


def buscar_documentos(
    ctx: RunContext[AgentDeps],
    pergunta: str,
    *,
    tipo_doc: TipoDocumento | None = None,
    produtor_id: str | None = None,
    apenas_vigentes: bool = False,
    k: int = 5,
) -> BuscaDocumentosResultado:
    """Busca híbrida (vetorial + lexical via RRF) sobre certificações, laudos,
    inspeções, contratos e fichas técnicas na view `chunks_busca`.

    Combina similaridade semântica com busca lexical por códigos exatos
    (ex.: NC-001, PE-SIE-0756, LOT-2026-0089), cruza validade documental com
    a data de referência e devolve trechos mascarados com `[arquivo · seção]`.
    """
    pergunta_limpa = pergunta.strip()
    if not pergunta_limpa:
        raise ValueError("Informe uma pergunta ou termo de busca.")
    if k < 1:
        raise ValueError("O parâmetro k deve ser maior ou igual a 1.")

    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    data_referencia = ctx.deps.data_referencia
    mascarador = ctx.deps.mascarador

    produtor_id_filtro = _aplicar_filtro_produtor(usuario, produtor_id)

    condicoes: list[str] = []
    parametros_where: list[object] = []

    if tipo_doc is not None:
        condicoes.append("cb.tipo_doc = %s")
        parametros_where.append(tipo_doc)

    if produtor_id_filtro is not None:
        if usuario.perfil == 'produtor':
            condicoes.append(
                "(cb.produtor_id = %s OR "
                "(cb.produtor_id IS NULL AND cb.metadados->'produtores_citados' @> to_jsonb(%s::text)))"
            )
        else:
            condicoes.append(
                "(cb.produtor_id = %s OR cb.metadados->'produtores_citados' @> to_jsonb(%s::text))"
            )
        parametros_where.extend([produtor_id_filtro, produtor_id_filtro])

    if apenas_vigentes:
        condicoes.append("COALESCE(cb.validade, p.validade_certificacao) >= %s")
        parametros_where.append(data_referencia)

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ""

    vetor = _gerar_embedding_consulta(pergunta_limpa)
    vetor_str = str(vetor)
    pool_limite = max(k * 4, 20)

    parametros_sql: list[object] = [
        *parametros_where,
        vetor_str,
        vetor_str,
        vetor_str,
        pool_limite,
        pergunta_limpa,
        pergunta_limpa,
        pergunta_limpa,
        pool_limite,
        RRF_K,
        RRF_K,
        pool_limite,
    ]

    linhas = conn.execute(
        f"""
        WITH filtrados AS (
            SELECT cb.id,
                   cb.arquivo,
                   cb.ordem,
                   cb.secao,
                   cb.tipo_doc,
                   cb.produtor_id,
                   cb.validade,
                   cb.texto,
                   cb.embedding,
                   cb.texto_busca,
                   cb.metadados,
                   p.nome_completo AS produtor_nome,
                   p.status AS produtor_status,
                   p.validade_certificacao AS produtor_validade_cert,
                   p.registro_sie AS produtor_sie,
                   p.dap_caf AS produtor_dap
            FROM chunks_busca cb
            LEFT JOIN produtores p ON p.id = cb.produtor_id
            {where_sql}
        ),
        vetorial AS (
            SELECT id,
                   1.0 - (embedding <=> %s::vector) AS similaridade,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector ASC) AS rank_vet
            FROM filtrados
            ORDER BY embedding <=> %s::vector ASC
            LIMIT %s
        ),
        lexical AS (
            SELECT id,
                   ts_rank_cd(texto_busca, websearch_to_tsquery('portuguese', %s)) AS score_lex,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank_cd(texto_busca, websearch_to_tsquery('portuguese', %s)) DESC
                   ) AS rank_lex
            FROM filtrados
            WHERE texto_busca @@ websearch_to_tsquery('portuguese', %s)
            ORDER BY score_lex DESC
            LIMIT %s
        ),
        combinado AS (
            SELECT COALESCE(v.id, l.id) AS id,
                   v.similaridade,
                   l.score_lex,
                   (
                       COALESCE(1.0 / (%s + v.rank_vet), 0.0) +
                       COALESCE(1.0 / (%s + l.rank_lex), 0.0)
                   ) AS score_rrf
            FROM vetorial v
            FULL OUTER JOIN lexical l ON v.id = l.id
        )
        SELECT f.arquivo,
               f.secao,
               f.tipo_doc,
               f.produtor_id,
               f.validade,
               f.texto,
               f.produtor_nome,
               f.produtor_status,
               f.produtor_validade_cert,
               f.produtor_sie,
               f.produtor_dap,
               c.score_rrf,
               c.similaridade,
               c.score_lex
        FROM combinado c
        JOIN filtrados f ON f.id = c.id
        ORDER BY c.score_rrf DESC, c.similaridade DESC NULLS LAST
        LIMIT %s
        """,
        parametros_sql,
    ).fetchall()

    resultados: list[DocumentoResultado] = []
    for row in linhas:
        (
            arquivo,
            secao,
            t_doc,
            pid,
            val_doc,
            texto,
            p_nome,
            p_status,
            p_val_cert,
            p_sie,
            p_dap,
            score_rrf,
            sim_vet,
            score_lex,
        ) = row

        score_rrf_f = float(score_rrf)
        sim_vet_f = float(sim_vet) if sim_vet is not None else None
        score_lex_f = float(score_lex) if score_lex is not None else None

        relevante_vetorial = sim_vet_f is not None and sim_vet_f >= LIMIAR_SIMILARIDADE_VETORIAL
        relevante_lexical = score_lex_f is not None and score_lex_f > 0.0
        if score_rrf_f < LIMIAR_RRF or not (relevante_vetorial or relevante_lexical):
            continue

        validade_efetiva = val_doc or p_val_cert
        vigente = (validade_efetiva >= data_referencia) if validade_efetiva is not None else None
        citacao = f"[{arquivo} · {secao}]" if secao else f"[{arquivo}]"

        resultados.append(
            DocumentoResultado(
                arquivo=arquivo,
                secao=secao,
                citacao=citacao,
                tipo_doc=t_doc,
                produtor_id=pid,
                produtor_nome=p_nome,
                produtor_status=p_status,
                registro_sie=p_sie,
                dap_caf=p_dap,
                validade=validade_efetiva,
                vigente=vigente,
                trecho=mascarador.mascarar(texto),
                score_rrf=round(score_rrf_f, 6),
                similaridade_vetorial=round(sim_vet_f, 4) if sim_vet_f is not None else None,
                score_lexical=round(score_lex_f, 4) if score_lex_f is not None else None,
                fonte=Fonte(arquivo=arquivo, registro=secao),
            )
        )
        if len(resultados) >= k:
            break

    if not resultados:
        return BuscaDocumentosResultado(
            encontrado=False,
            resultados=[],
            mensagem="sem resultado relevante",
        )

    return BuscaDocumentosResultado(
        encontrado=True,
        resultados=resultados,
        mensagem=None,
    )