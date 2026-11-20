"""Ferramenta buscar_documentos (Issue #8 — Busca Híbrida Lexical + Vetorial com RRF)."""

from __future__ import annotations

from datetime import date
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps
from sabor.db import EMBEDDING_DIM
from sabor.tools.base import Fonte, _aplicar_filtro_produtor, _resolver_usuario

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
        model='gemini-embedding-001',
        contents=pergunta,
        config=types.EmbedContentConfig(
            task_type='RETRIEVAL_QUERY',
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
        raise ValueError('Informe uma pergunta ou termo de busca.')
    if k < 1:
        raise ValueError('O parâmetro k deve ser maior ou igual a 1.')

    conn = ctx.deps.conn
    usuario = _resolver_usuario(ctx.deps.usuario)
    data_referencia = ctx.deps.data_referencia
    mascarador = ctx.deps.mascarador

    produtor_id_filtro = _aplicar_filtro_produtor(usuario, produtor_id)

    condicoes: list[str] = []
    parametros_where: list[object] = []

    if tipo_doc is not None:
        condicoes.append('cb.tipo_doc = %s')
        parametros_where.append(tipo_doc)

    if produtor_id_filtro is not None:
        if usuario.perfil == 'produtor':
            condicoes.append(
                '(cb.produtor_id = %s OR (cb.produtor_id IS NULL '
                "AND cb.metadados->'produtores_citados' @> to_jsonb(%s::text)))"
            )
        else:
            condicoes.append(
                "(cb.produtor_id = %s OR cb.metadados->'produtores_citados' @> to_jsonb(%s::text))"
            )
        parametros_where.extend([produtor_id_filtro, produtor_id_filtro])

    if apenas_vigentes:
        condicoes.append('COALESCE(cb.validade, p.validade_certificacao) >= %s')
        parametros_where.append(data_referencia)

    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ''

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
        citacao = f'[{arquivo} · {secao}]' if secao else f'[{arquivo}]'

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
                fonte=Fonte(arquivo=arquivo, referencia=secao or 'documento'),
            )
        )
        if len(resultados) >= k:
            break

    if not resultados:
        return BuscaDocumentosResultado(
            encontrado=False,
            resultados=[],
            mensagem='sem resultado relevante',
        )

    return BuscaDocumentosResultado(
        encontrado=True,
        resultados=resultados,
        mensagem=None,
    )


# =============================================================================
# Inventário do acervo (apoio aos relatórios, Issue #15)
# =============================================================================


class DocumentoInventario(BaseModel):
    arquivo: str
    tipo_doc: TipoDocumento
    produtor_id: str | None
    titulo: str | None
    validade: date | None
    fonte: Fonte


def listar_documentos(
    ctx: RunContext[AgentDeps],
    *,
    tipo_doc: TipoDocumento | None = None,
) -> list[DocumentoInventario]:
    """Inventário do acervo ingerido (tabela `documentos`), sem busca semântica.

    Uma busca sem resultado não prova que um documento falta; o inventário prova. Não é
    exportada em `sabor.tools`, então não vira tool do agente: serve aos relatórios (#15). O
    perfil produtor vê os próprios documentos e os gerais que o citam, como em
    `buscar_documentos`.
    """
    usuario = _resolver_usuario(ctx.deps.usuario)
    produtor_id_filtro = _aplicar_filtro_produtor(usuario, None)

    condicoes: list[str] = []
    parametros: list[object] = []
    if tipo_doc is not None:
        condicoes.append('d.tipo_doc = %s')
        parametros.append(tipo_doc)
    if produtor_id_filtro is not None:
        condicoes.append(
            '(d.produtor_id = %s OR (d.produtor_id IS NULL AND EXISTS ('
            'SELECT 1 FROM chunks c WHERE c.arquivo = d.arquivo '
            "AND (d.metadados || c.metadados)->'produtores_citados' @> to_jsonb(%s::text))))"
        )
        parametros.extend([produtor_id_filtro, produtor_id_filtro])
    where_sql = f"WHERE {' AND '.join(condicoes)}" if condicoes else ''

    linhas = ctx.deps.conn.execute(
        f"""
        SELECT d.arquivo, d.tipo_doc, d.produtor_id, d.titulo, d.validade
        FROM documentos d
        {where_sql}
        ORDER BY d.arquivo
        """,
        parametros,
    ).fetchall()
    return [
        DocumentoInventario(
            arquivo=arquivo,
            tipo_doc=t_doc,
            produtor_id=pid,
            titulo=titulo,
            validade=validade,
            fonte=Fonte(arquivo=arquivo, referencia=titulo or 'documento'),
        )
        for arquivo, t_doc, pid, titulo, validade in linhas
    ]
