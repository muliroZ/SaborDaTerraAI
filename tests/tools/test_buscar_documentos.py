from __future__ import annotations

"""Testes da ferramenta buscar_documentos (Issue #8)."""

import os
from dataclasses import dataclass
from datetime import date
from unittest.mock import MagicMock

import pytest

from sabor.agent.deps import AgentDeps
from sabor.db import EMBEDDING_DIM
from sabor.tools.base import SemPermissao
from sabor.tools.documentos import buscar_documentos

from dotenv import load_dotenv

load_dotenv()
_TEM_GEMINI_KEY = bool(os.getenv("GEMINI_API_KEY"))

DATA_REF = date(2026, 10, 30)


@dataclass
class _Ctx:
    deps: AgentDeps


def _ctx_funcionario(conn, mascarador=None) -> _Ctx:
    kwargs = {"conn": conn, "usuario": "Dona Lúcia", "data_referencia": DATA_REF}
    if mascarador is not None:
        kwargs["mascarador"] = mascarador
    return _Ctx(deps=AgentDeps(**kwargs))


def _ctx_produtor(conn, produtor_id: str) -> _Ctx:
    return _Ctx(deps=AgentDeps(conn=conn, usuario=produtor_id, data_referencia=DATA_REF))


# =============================================================================
# 1. Unitários — validação de entrada, perfil, limiar e mascaramento (sem API)
# =============================================================================

def test_buscar_documentos_rejeita_pergunta_vazia():
    ctx = _ctx_funcionario(MagicMock())
    with pytest.raises(ValueError):
        buscar_documentos(ctx, "   ")


def test_buscar_documentos_rejeita_k_invalido():
    ctx = _ctx_funcionario(MagicMock())
    with pytest.raises(ValueError):
        buscar_documentos(ctx, "certificado mel", k=0)


def test_buscar_documentos_bloqueia_produtor_consultando_terceiro():
    ctx = _ctx_produtor(MagicMock(), "P001")
    with pytest.raises(SemPermissao):
        buscar_documentos(ctx, "certificado", produtor_id="P002")


def test_buscar_documentos_abaixo_do_limiar_devolve_sem_resultado_relevante(monkeypatch):
    monkeypatch.setattr(
        "sabor.tools.documentos._gerar_embedding_consulta",
        lambda _: [0.0] * EMBEDDING_DIM,
    )
    conn = MagicMock()
    # Simula 1 candidato retornado pelo SQL com similaridade baixa (0.20) e sem match lexical
    conn.execute.return_value.fetchall.return_value = [
        (
            "contratos/contrato_pnae_garanhuns.txt",
            "OBJETO",
            "contrato",
            None,
            None,
            "Texto irrelevante para a consulta.",
            None,
            None,
            None,
            None,
            None,
            0.016393,  # rank 1 apenas no vetorial (1/61)
            0.20,      # abaixo de LIMIAR_SIMILARIDADE_VETORIAL (0.55)
            None,      # sem match lexical
        )
    ]

    res = buscar_documentos(_ctx_funcionario(conn), "certificado fitossanitário internacional")
    assert res.encontrado is False
    assert res.resultados == []
    assert res.mensagem == "sem resultado relevante"


def test_buscar_documentos_aplica_mascaramento_e_formata_citacao(monkeypatch):
    monkeypatch.setattr(
        "sabor.tools.documentos._gerar_embedding_consulta",
        lambda _: [0.1] * EMBEDDING_DIM,
    )

    class _MascaradorFake:
        def mascarar(self, texto: str) -> str:
            return texto.replace("123.456.789-00", "[CPF_MASCARADO]")

    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        (
            "certificacoes/cert_organica_jose_santos_P002.txt",
            "IDENTIFICAÇÃO DO PRODUTOR",
            "certificado",
            "P002",
            date(2027, 3, 20),
            "Produtor José Antônio, CPF 123.456.789-00.",
            "José Antônio dos Santos",
            "ativo",
            date(2027, 3, 20),
            None,
            "SDR/PE-2024-0001234",
            0.032787,
            0.85,
            0.42,
        )
    ]

    res = buscar_documentos(
        _ctx_funcionario(conn, mascarador=_MascaradorFake()),
        "certificado P002",
    )
    assert res.encontrado is True
    assert len(res.resultados) == 1
    item = res.resultados[0]
    assert item.citacao == "[certificacoes/cert_organica_jose_santos_P002.txt · IDENTIFICAÇÃO DO PRODUTOR]"
    assert item.trecho == "Produtor José Antônio, CPF [CPF_MASCARADO]."
    assert item.vigente is True


# =============================================================================
# 2. Integração — Perguntas 1, 6, 7, 9 e 10 de docs/projeto.md
# =============================================================================

_TEM_GEMINI_KEY = bool(os.getenv("GEMINI_API_KEY"))


def _base_documentos_populada(conn) -> bool:
    total = conn.execute("SELECT COUNT(*) FROM chunks_busca").fetchone()[0]
    return total > 0


@pytest.mark.skipif(not _TEM_GEMINI_KEY, reason="GEMINI_API_KEY não configurada no ambiente")
def test_pergunta_1_certificacao_organica_vigente_mel(conn):
    if not _base_documentos_populada(conn):
        pytest.skip("Tabela chunks_busca vazia (execute scripts/ingest_docs.py antes).")

    # Busca todos os certificados de mel e cruza validade x data_referencia (30/10/2026)
    res_todos = buscar_documentos(
        _ctx_funcionario(conn),
        "certificação orgânica mel produtor",
        tipo_doc="certificado",
        k=10,
    )
    assert res_todos.encontrado is True
    vigentes = {r.produtor_id for r in res_todos.resultados if r.vigente is True}
    vencidos = {r.produtor_id for r in res_todos.resultados if r.vigente is False}

    assert {"P002", "P018"}.issubset(vigentes)
    assert "P008" in vencidos

    # Com filtro apenas_vigentes=True antes da similaridade, P008 não deve aparecer
    res_vigentes = buscar_documentos(
        _ctx_funcionario(conn),
        "certificação orgânica mel produtor",
        tipo_doc="certificado",
        apenas_vigentes=True,
        k=10,
    )
    assert "P008" not in {r.produtor_id for r in res_vigentes.resultados}


@pytest.mark.skipif(not _TEM_GEMINI_KEY, reason="GEMINI_API_KEY não configurada no ambiente")
def test_pergunta_6_frango_caipira_e_codigo_nc_001(conn):
    if not _base_documentos_populada(conn):
        pytest.skip("Tabela chunks_busca vazia (execute scripts/ingest_docs.py antes).")

    res = buscar_documentos(
        _ctx_funcionario(conn),
        "NC-001 frango caipira SIE suspensão ADAGRO",
        k=5,
    )
    assert res.encontrado is True
    assert any(
        "inspecao_adagro_2026.txt" in r.arquivo and ("NC-001" in r.trecho or "P017" in r.trecho)
        for r in res.resultados
    )


@pytest.mark.skipif(not _TEM_GEMINI_KEY, reason="GEMINI_API_KEY não configurada no ambiente")
def test_pergunta_7_prazo_pagamento_pnae_e_preco_queijo_contrato(conn):
    if not _base_documentos_populada(conn):
        pytest.skip("Tabela chunks_busca vazia (execute scripts/ingest_docs.py antes).")

    res = buscar_documentos(
        _ctx_funcionario(conn),
        "prazo de pagamento PNAE preço queijo coalho contrato",
        tipo_doc="contrato",
        k=5,
    )
    assert res.encontrado is True
    texto_unido = "\n".join(r.trecho for r in res.resultados)
    assert any("contrato_pnae_garanhuns.txt" in r.arquivo for r in res.resultados)
    assert "45" in texto_unido
    assert "34" in texto_unido


@pytest.mark.skipif(not _TEM_GEMINI_KEY, reason="GEMINI_API_KEY não configurada no ambiente")
def test_pergunta_9_armazenamento_e_validade_queijo_coalho(conn):
    if not _base_documentos_populada(conn):
        pytest.skip("Tabela chunks_busca vazia (execute scripts/ingest_docs.py antes).")

    res = buscar_documentos(
        _ctx_funcionario(conn),
        "como armazenar queijo coalho temperatura validade",
        tipo_doc="ficha",
        k=5,
    )
    assert res.encontrado is True
    texto_unido = "\n".join(r.trecho for r in res.resultados)
    assert any("queijo_coalho.txt" in r.arquivo for r in res.resultados)
    assert "30" in texto_unido


@pytest.mark.skipif(not _TEM_GEMINI_KEY, reason="GEMINI_API_KEY não configurada no ambiente")
def test_pergunta_10_documentos_inspecao_adagro(conn):
    if not _base_documentos_populada(conn):
        pytest.skip("Tabela chunks_busca vazia (execute scripts/ingest_docs.py antes).")

    res = buscar_documentos(
        _ctx_funcionario(conn),
        "ADAGRO laudo sanitário inspeção conformidade",
        k=5,
    )
    assert res.encontrado is True
    assert any(r.tipo_doc in {"inspecao", "laudo", "certificado"} for r in res.resultados)