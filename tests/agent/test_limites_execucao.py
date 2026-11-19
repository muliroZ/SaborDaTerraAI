"""Regressões de orçamento em consultas com memória e múltiplas rodadas de tools."""

import logging
from unittest.mock import MagicMock

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, UsageLimits
import pytest

from sabor.agent import guardrail_entry as guardrails
from sabor.agent.deps import AgentDeps
from sabor.agent.fallback import ler_segundos_env
from sabor.agent.runner import criar_agente, processar_turno


@pytest.fixture(autouse=True)
def ambiente_controlado(monkeypatch):
    monkeypatch.setattr(guardrails, 'load_dotenv', lambda: None)
    for nome in (
        'GUARDRAIL_MAX_CHARS',
        'SESSAO_REQUEST_LIMIT',
        'TURNO_REQUEST_LIMIT',
        'TURNO_TOKENS_LIMIT',
    ):
        monkeypatch.delenv(nome, raising=False)


@pytest.mark.parametrize('valor', ['', '   '])
def test_limites_vazios_usam_padrao_sem_falhar(monkeypatch, valor):
    for nome in ('TURNO_REQUEST_LIMIT', 'TURNO_TOKENS_LIMIT', 'SESSAO_REQUEST_LIMIT'):
        monkeypatch.setenv(nome, valor)
    limites = guardrails.obter_usage_limits_turno()
    assert limites.request_limit == 8
    assert limites.total_tokens_limit == 32_000
    assert guardrails.obter_limites_configurados()['sessao_request_limit'] == 50


def test_limites_configurados_sao_respeitados(monkeypatch):
    monkeypatch.setenv('TURNO_REQUEST_LIMIT', ' 6 ')
    monkeypatch.setenv('TURNO_TOKENS_LIMIT', '24000')
    limites = guardrails.obter_usage_limits_turno()
    assert limites.request_limit == 6
    assert limites.total_tokens_limit == 24_000


@pytest.mark.parametrize('valor', ['abc', '1.5', '0', '-1'])
def test_configuracao_invalida_usa_padrao_seguro(monkeypatch, valor):
    monkeypatch.setenv('TURNO_TOKENS_LIMIT', valor)
    limites = guardrails.obter_usage_limits_turno()
    assert limites.total_tokens_limit == 32_000


@pytest.mark.parametrize('valor', ['0', '-1'])
def test_limites_nao_positivos_usam_padrao_seguro(monkeypatch, valor):
    monkeypatch.setenv('TURNO_TOKENS_LIMIT', valor)
    assert guardrails.obter_usage_limits_turno().total_tokens_limit == 32_000


@pytest.mark.parametrize('valor', ['nan', 'inf', '-inf'])
def test_timeout_nao_permite_desativar_prazo_com_valor_nao_finito(monkeypatch, valor):
    monkeypatch.setenv('TURNO_TIMEOUT_S', valor)
    with pytest.raises(ValueError, match='TURNO_TIMEOUT_S'):
        ler_segundos_env('TURNO_TIMEOUT_S', 90)


def _agente_multietapas():
    def consultar_etapa(etapa: int) -> str:
        return f'Consulta {etapa} concluída.'

    def modelo(messages, info):
        concluidas = sum(
            isinstance(p, ToolReturnPart) and p.tool_name == 'consultar_etapa'
            for m in messages
            for p in m.parts
        )
        if concluidas < 4:
            part = ToolCallPart('consultar_etapa', {'etapa': concluidas + 1})
        else:
            part = ToolCallPart('final_result', {'texto': 'Consulta concluída.'})
        # Uso explícito para simular contexto reenviado em cada rodada, sem API paga.
        return ModelResponse(parts=[part], usage=RequestUsage(input_tokens=4000, output_tokens=100))

    return criar_agente([FunctionModel(modelo)], tools=[consultar_etapa])


def test_consulta_de_cinco_chamadas_conclui_e_registra_consumo(caplog):
    salvos = []

    def salvar(sid, msg, turno, historico_modelo_json=None):
        salvos.append(historico_modelo_json)

    with caplog.at_level(logging.INFO, logger='sabor.agent.runner'):
        turno = processar_turno(
            'sessao',
            'Consulte os dados do pedido CONFIDENCIAL-TESTE',
            agente=_agente_multietapas(),
            deps=AgentDeps(conn=MagicMock()),
            salvar_memoria=salvar,
        )
    assert turno.texto == 'Consulta concluída.'
    assert not turno.bloqueado_guardrail and not turno.falha_segura
    assert len(salvos) == 1
    historico = ModelMessagesTypeAdapter.validate_json(salvos[0])
    assert sum(isinstance(p, ToolCallPart) for m in historico for p in m.parts) == 5
    assert 'Turno concluído' in caplog.text
    assert 'chamadas=5' in caplog.text
    assert 'tokens_total=20500' in caplog.text
    assert 'CONFIDENCIAL-TESTE' not in caplog.text


@pytest.mark.parametrize(
    'limites',
    [
        UsageLimits(request_limit=4, total_tokens_limit=32_000),
        UsageLimits(request_limit=8, total_tokens_limit=12_000),
    ],
)
def test_tetos_continuam_ativos_e_proximo_turno_tem_orcamento_novo(limites, caplog):
    agente = _agente_multietapas()
    deps = AgentDeps(conn=MagicMock())
    salvos = []
    with caplog.at_level(logging.WARNING, logger='sabor.agent.runner'):
        interrompido = processar_turno(
            'sessao',
            'Consulte os dados do pedido',
            agente=agente,
            deps=deps,
            usage_limits=limites,
            salvar_memoria=lambda sid, msg, turno: salvos.append(turno),
        )
    assert interrompido.retorno_antecipado.motivo == 'ORCAMENTO_TURNO_ESGOTADO'
    assert salvos == [interrompido]
    assert 'Orçamento do turno esgotado' in caplog.text
    assert ('request_limit' if limites.request_limit == 4 else 'total_tokens_limit') in caplog.text
    seguinte = processar_turno(
        'sessao',
        'Consulte os dados do pedido',
        agente=agente,
        deps=deps,
    )
    assert seguinte.texto == 'Consulta concluída.'


def test_retornos_de_tools_no_historico_nao_consumem_cota_de_perguntas(monkeypatch):
    agente = _agente_multietapas()
    deps = AgentDeps(conn=MagicMock())
    turno_com_tools = [
        ModelRequest(parts=[UserPromptPart(content='Consulte os dados do pedido')]),
        ModelResponse(parts=[ToolCallPart('consultar_etapa', {'etapa': 1})]),
        ModelRequest(parts=[ToolReturnPart('consultar_etapa', 'Consulta 1 concluída.')]),
        ModelResponse(parts=[ToolCallPart('consultar_etapa', {'etapa': 2})]),
        ModelRequest(parts=[ToolReturnPart('consultar_etapa', 'Consulta 2 concluída.')]),
    ]
    # O histórico tem três ModelRequests, mas apenas uma pergunta de usuário.
    monkeypatch.setenv('SESSAO_REQUEST_LIMIT', '2')
    segundo = processar_turno(
        'sessao',
        'E para o hotel?',
        agente=agente,
        deps=deps,
        carregar_memoria=lambda sid: turno_com_tools,
    )
    assert segundo.texto == 'Consulta concluída.'
    terceiro = processar_turno(
        'sessao',
        'E para o restaurante?',
        agente=agente,
        deps=deps,
        carregar_memoria=lambda sid: turno_com_tools + turno_com_tools,
    )
    assert terceiro.retorno_antecipado.motivo == 'COTA_SESSAO_ESGOTADA'
