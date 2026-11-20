"""Testes unitários das dependências do agente (`AgentDeps`) e data de referência."""

from datetime import date
from unittest.mock import MagicMock

from sabor.agent.deps import (
    DATA_REF_PADRAO,
    AgentDeps,
    MascaradorNoOp,
    obter_data_referencia,
)
from sabor.agent.mascarador import Mascarador


def test_obter_data_referencia_padrao_e_env(monkeypatch):
    monkeypatch.setenv('DATA_REF_DATE', '2026-11-15')
    assert obter_data_referencia() == date(2026, 11, 15)

    monkeypatch.setenv('DATA_REF_DATE', '')
    assert obter_data_referencia() == DATA_REF_PADRAO


def test_agent_deps_usa_mascarador_por_turno_e_noop_funciona():
    conn_mock = MagicMock()
    deps = AgentDeps(conn=conn_mock, usuario='Seu Antônio', dataset_version='sha256-abc')
    assert deps.conn is conn_mock
    assert deps.usuario == 'Seu Antônio'
    assert deps.dataset_version == 'sha256-abc'
    assert isinstance(deps.data_referencia, date)
    assert isinstance(deps.mascarador, Mascarador)

    noop = MascaradorNoOp()
    assert noop.mascarar('CPF 123') == 'CPF 123'
    assert noop.restaurar('CPF 123') == 'CPF 123'
