"""Testes unitários dos contratos, dependências e orquestração do agente."""

from datetime import date
from unittest.mock import MagicMock

from pydantic import ValidationError
import pytest

from sabor.agent import (
    DATA_REF_PADRAO,
    AgentDeps,
    Flag,
    Fonte,
    MascaradorNoOp,
    Rascunho,
    RespostaAgente,
    RespostaTurno,
    obter_data_referencia,
)


def test_fonte_formatar_segue_convencao():
    fonte = Fonte(arquivo='lotes.csv', referencia='LOT-2026-0089')
    assert fonte.formatar() == '[lotes.csv · LOT-2026-0089]'


def test_obter_data_referencia_padrao_e_env(monkeypatch):
    monkeypatch.setenv('DATA_REF_DATE', '2026-11-15')
    assert obter_data_referencia() == date(2026, 11, 15)

    monkeypatch.setenv('DATA_REF_DATE', '')
    assert obter_data_referencia() == DATA_REF_PADRAO


def test_resposta_agente_valores_padrao_e_validacao():
    resp = RespostaAgente(texto='Não encontrei essa informação nos dados.')
    assert resp.texto == 'Não encontrei essa informação nos dados.'
    assert resp.fontes == []
    assert resp.flags == []
    assert resp.rascunhos == []

    with pytest.raises(ValidationError):
        RespostaAgente()  # type: ignore[call-arg]


def test_agent_deps_e_mascarador_noop():
    conn_mock = MagicMock()
    deps = AgentDeps(conn=conn_mock, usuario='Seu Antônio', dataset_version='sha256-abc')
    assert deps.conn is conn_mock
    assert deps.usuario == 'Seu Antônio'
    assert deps.dataset_version == 'sha256-abc'
    assert isinstance(deps.data_referencia, date)
    assert isinstance(deps.mascarador, MascaradorNoOp)
    assert deps.mascarador.mascarar('CPF 123') == 'CPF 123'
    assert deps.mascarador.restaurar('CPF 123') == 'CPF 123'


def test_resposta_turno_de_resposta_agente():
    resposta_llm = RespostaAgente(
        texto='O lote LOT-2026-0089 é de Cícero (P003).',
        fontes=[Fonte(arquivo='lotes.csv', referencia='LOT-2026-0089')],
        flags=[Flag(tipo='conformidade', descricao='Queijaria com SIE ativo')],
        rascunhos=[
            Rascunho(
                destinatario='P003 - Cícero',
                canal='whatsapp',
                conteudo='Olá Seu Cícero, novo pedido de queijo coalho.',
            )
        ],
    )
    turno = RespostaTurno.de_resposta_agente(
        sessao_id='sessao-01',
        usuario='Dona Lúcia',
        resposta=resposta_llm,
        modelo_efetivo='gemini-3.5-flash-lite',
        provedor_efetivo='google-gla',
        latencia_ms=420,
    )
    assert turno.sessao_id == 'sessao-01'
    assert turno.usuario == 'Dona Lúcia'
    assert turno.texto == resposta_llm.texto
    assert len(turno.fontes) == 1
    assert turno.fontes[0].formatar() == '[lotes.csv · LOT-2026-0089]'
    assert len(turno.flags) == 1
    assert len(turno.rascunhos) == 1
    assert turno.modelo_efetivo == 'gemini-3.5-flash-lite'
    assert turno.provedor_efetivo == 'google-gla'
    assert turno.latencia_ms == 420
    assert turno.bloqueado_guardrail is False
    assert turno.falha_segura is False
