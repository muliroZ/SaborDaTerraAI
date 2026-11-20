"""Testes da observabilidade do agente (#39): span do turno, trace_id na auditoria e exportador."""

import json
import logging
from unittest.mock import MagicMock

import google.auth
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import DefaultCredentialsError
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings
import pytest

from sabor import observabilidade as obs
from sabor.agent.deps import AgentDeps
from sabor.agent.fallback import MODELO_PRIMARIO
from sabor.agent.runner import criar_agente, processar_turno
from sabor.agent.schemas import RespostaTurno

# O provider global do OpenTelemetry só pode ser definido uma vez por processo.
_EXPORTADOR = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTADOR))
trace.set_tracer_provider(_PROVIDER)

PERGUNTA = 'Quanto o produtor P001 vendeu em setembro?'


@pytest.fixture(autouse=True)
def _limpar_spans():
    _EXPORTADOR.clear()
    yield
    _EXPORTADOR.clear()


def _span_turno():
    spans = [s for s in _EXPORTADOR.get_finished_spans() if s.name == obs.NOME_SPAN_TURNO]
    assert len(spans) == 1
    return spans[0]


def _hex(trace_id: int) -> str:
    return format(trace_id, '032x')


def test_trace_id_sem_span_ativo_e_uuid():
    assert len(obs.trace_id_atual()) == 36  # UUID com hífens


def test_turno_abre_span_e_auditoria_grava_o_mesmo_trace_id():
    auditados: list[str] = []

    turno = processar_turno(
        'sess-obs',
        '   ',
        deps=AgentDeps(conn=MagicMock()),
        registrar_auditoria=lambda t: auditados.append(t.trace_id),
    )

    span = _span_turno()
    assert turno.trace_id == _hex(span.context.trace_id)
    assert auditados == [turno.trace_id]
    assert span.attributes['sabor.sessao_id'] == 'sess-obs'
    assert span.attributes['sabor.retorno_antecipado'] == 'ENTRADA_VAZIA'
    assert span.attributes['sabor.bloqueado_guardrail'] is True
    assert span.attributes['sabor.cache_hit'] is False
    assert 'sabor.latencia_ms' in span.attributes


def test_spans_do_pydantic_ai_ficam_dentro_do_trace_do_turno():
    """O agente roda noutra thread: o contexto precisa ser copiado para ela."""

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={'texto': 'P001 vendeu R$ 1.090,00.', 'fontes': [], 'flags': []},
                )
            ]
        )

    agente = criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[])
    agente.instrument = InstrumentationSettings(tracer_provider=_PROVIDER, include_content=False)

    turno = processar_turno(
        'sess-obs-2',
        PERGUNTA,
        agente=agente,
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _: None,
    )

    span_turno = _span_turno()
    filhos = [s for s in _EXPORTADOR.get_finished_spans() if s is not span_turno]
    assert filhos, 'o PydanticAI não emitiu spans'
    assert {_hex(s.context.trace_id) for s in filhos} == {turno.trace_id}
    assert any(s.parent and s.parent.span_id == span_turno.context.span_id for s in filhos)

    assert span_turno.attributes['sabor.modelo'] == MODELO_PRIMARIO
    assert span_turno.attributes['sabor.falha_segura'] is False
    assert 'sabor.retorno_antecipado' not in span_turno.attributes

    # Sem conteúdo da conversa em nenhum span (#33).
    for span in _EXPORTADOR.get_finished_spans():
        assert PERGUNTA not in json.dumps(dict(span.attributes or {}), default=str)


def test_falha_segura_marca_o_span_como_erro():
    turno = RespostaTurno(
        sessao_id='s', usuario='u', texto='x', falha_segura=True, tokens_entrada=10
    )
    with trace.get_tracer('teste').start_as_current_span('t') as span:
        obs._registrar_desfecho(span, turno)

    finalizado = _EXPORTADOR.get_finished_spans()[0]
    assert finalizado.status.status_code == StatusCode.ERROR
    assert finalizado.attributes['sabor.tokens'] == 10
    assert 'sabor.modelo' not in finalizado.attributes  # None não vira atributo


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------


@pytest.fixture
def config_isolada(monkeypatch):
    """Isola `configurar_observabilidade` do provider global e do estado do Agent."""
    chamadas: dict[str, list] = {'provider': [], 'instrument': []}
    monkeypatch.setattr(obs, '_configurado', False)
    monkeypatch.setattr(obs.trace, 'set_tracer_provider', chamadas['provider'].append)
    monkeypatch.setattr(Agent, 'instrument_all', staticmethod(chamadas['instrument'].append))
    return chamadas


def test_desligada_sem_variavel(monkeypatch, config_isolada):
    monkeypatch.delenv('OBSERVABILIDADE', raising=False)
    assert obs.configurar_observabilidade() is False
    assert config_isolada['provider'] == []


def test_console_instrumenta_sem_conteudo_e_e_idempotente(monkeypatch, config_isolada):
    monkeypatch.setenv('OBSERVABILIDADE', 'console')
    assert obs.configurar_observabilidade() is True
    assert obs.configurar_observabilidade() is True

    assert len(config_isolada['provider']) == 1
    (settings,) = config_isolada['instrument']
    assert settings.include_content is False


def test_gcp_sem_credenciais_nao_derruba_o_app(monkeypatch, config_isolada):
    def sem_credenciais(**_):
        raise DefaultCredentialsError('sem ADC')

    monkeypatch.setenv('OBSERVABILIDADE', 'gcp')
    monkeypatch.setattr(google.auth, 'default', sem_credenciais)
    assert obs.configurar_observabilidade() is False
    assert config_isolada['provider'] == []


def test_exportador_gcp_usa_telemetry_api_com_projeto(monkeypatch):
    monkeypatch.delenv('GOOGLE_CLOUD_PROJECT', raising=False)
    monkeypatch.setattr(google.auth, 'default', lambda **_: (AnonymousCredentials(), 'proj-x'))

    exportador, atributos = obs._criar_exportador('gcp')

    assert exportador._endpoint == obs.ENDPOINT_TELEMETRY_API
    assert atributos == {'gcp.project_id': 'proj-x'}


def test_destino_invalido():
    with pytest.raises(ValueError, match='OBSERVABILIDADE inválida'):
        obs._criar_exportador('datadog')


def test_log_json_leva_o_trace_do_span_ativo():
    formatador = obs.FormatadorCloudLogging('proj-x')
    registro = logging.LogRecord('sabor.agent', logging.WARNING, __file__, 1, 'oi %s', ('x',), None)
    with trace.get_tracer('teste').start_as_current_span('t') as span:
        linha = json.loads(formatador.format(registro))

    assert linha['severity'] == 'WARNING'
    assert linha['message'] == 'oi x'
    assert linha['logging.googleapis.com/trace'] == (
        f'projects/proj-x/traces/{_hex(span.get_span_context().trace_id)}'
    )
