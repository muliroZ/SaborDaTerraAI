"""Observabilidade do agente (etapa 5c, #39): traces OpenTelemetry de cada turno.

O PydanticAI emite spans OpenTelemetry das chamadas ao modelo e das tools. Este módulo:

- abre um span por turno (`instrumentar_turno`) com sessão, cache, retorno antecipado, modelo,
  tokens e latência, pai dos spans do PydanticAI;
- usa o `trace_id` desse span como `RespostaTurno.trace_id`, que a auditoria (#35) grava;
- configura o exportador pela variável `OBSERVABILIDADE`:

  | valor     | destino                                                                  |
  |-----------|--------------------------------------------------------------------------|
  | (vazia)   | desligado: nenhum span é exportado e o `trace_id` vira um UUID           |
  | `gcp`     | Cloud Trace pela Telemetry API (OTLP), autenticado pela service account  |
  | `otlp`    | qualquer backend OTLP pelas variáveis `OTEL_EXPORTER_OTLP_*` (ex.: Logfire) |
  | `console` | imprime os spans no terminal (depuração local)                           |

Os spans nunca carregam texto da conversa: o PydanticAI é instrumentado com
`include_content=False` (sem prompts, respostas, argumentos ou retornos de tools) e o span do
turno só leva metadados. O texto, mesmo mascarado (#33), fica só no banco.
"""

from collections.abc import Callable
import functools
import json
import logging
import os
import threading
from typing import Any
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

ENDPOINT_TELEMETRY_API = 'https://telemetry.googleapis.com/v1/traces'
"""Endpoint OTLP/HTTP do Cloud Trace (Telemetry API)."""

NOME_SPAN_TURNO = 'turno do agente'
DESTINOS = ('gcp', 'otlp', 'console')

_tracer = trace.get_tracer('sabor.agent')
_trava = threading.Lock()
_configurado = False


# ---------------------------------------------------------------------------
# Configuração do exportador
# ---------------------------------------------------------------------------


def destino_configurado() -> str:
    """Valor normalizado de `OBSERVABILIDADE` (vazio = desligado)."""
    return (os.getenv('OBSERVABILIDADE') or '').strip().lower()


def _criar_exportador(destino: str) -> tuple[Any, dict[str, str]]:
    """Devolve o exportador de spans e os atributos de resource que o destino exige."""
    if destino == 'gcp':
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        credenciais, projeto = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
        projeto = os.getenv('GOOGLE_CLOUD_PROJECT') or projeto
        if not projeto:
            raise RuntimeError(
                'Projeto do GCP não identificado: defina GOOGLE_CLOUD_PROJECT '
                'para OBSERVABILIDADE=gcp.'
            )
        exportador = OTLPSpanExporter(
            endpoint=ENDPOINT_TELEMETRY_API, session=AuthorizedSession(credenciais)
        )
        # A Telemetry API exige `gcp.project_id` no resource de cada span.
        return exportador, {'gcp.project_id': projeto}
    if destino == 'otlp':
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(), {}
    if destino == 'console':
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter(), {}
    raise ValueError(f'OBSERVABILIDADE inválida: {destino!r} (use {", ".join(DESTINOS)} ou vazio)')


def configurar_observabilidade() -> bool:
    """Liga a exportação dos traces conforme `OBSERVABILIDADE`. Idempotente.

    Devolve `True` se os traces estão sendo exportados. Uma falha aqui (ex.: sem credenciais
    do GCP na máquina local) só é registrada no log: a observabilidade nunca derruba o app.
    """
    global _configurado
    destino = destino_configurado()
    if not destino:
        return False
    with _trava:
        if _configurado:
            return True
        try:
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from pydantic_ai import Agent
            from pydantic_ai.models.instrumented import InstrumentationSettings

            exportador, atributos = _criar_exportador(destino)
            servico = os.getenv('OTEL_SERVICE_NAME') or os.getenv('K_SERVICE') or 'sabor-app'
            provider = TracerProvider(
                resource=Resource.create({SERVICE_NAME: servico, **atributos})
            )
            provider.add_span_processor(BatchSpanProcessor(exportador))
            trace.set_tracer_provider(provider)
            Agent.instrument_all(
                InstrumentationSettings(tracer_provider=provider, include_content=False)
            )
            if destino == 'gcp':
                _configurar_logs_cloud_logging(atributos['gcp.project_id'])
        except Exception:
            logger.warning(
                'Observabilidade desligada: falha ao configurar OBSERVABILIDADE=%s',
                destino,
                exc_info=True,
            )
            return False
        _configurado = True
    logger.info('Observabilidade ligada: traces exportados para %s', destino)
    return True


# ---------------------------------------------------------------------------
# Span do turno
# ---------------------------------------------------------------------------


def trace_id_atual() -> str:
    """`trace_id` do span ativo (32 hex, o mesmo do Cloud Trace) ou um UUID sem tracing."""
    contexto = trace.get_current_span().get_span_context()
    if contexto.is_valid:
        return format(contexto.trace_id, '032x')
    return str(uuid4())


def atributos_turno(turno: Any) -> dict[str, Any]:
    """Metadados do turno para o span. Nunca inclui texto da pergunta ou da resposta."""
    retorno = getattr(turno, 'retorno_antecipado', None)
    atributos: dict[str, Any] = {
        'sabor.cache_hit': bool(turno.cache_hit),
        'sabor.retorno_antecipado': retorno.motivo if retorno is not None else None,
        'sabor.falha_segura': bool(turno.falha_segura),
        'sabor.bloqueado_guardrail': bool(turno.bloqueado_guardrail),
        'sabor.modelo': turno.modelo_efetivo,
        'sabor.provedor': turno.provedor_efetivo,
        'sabor.tokens_entrada': turno.tokens_entrada,
        'sabor.tokens_saida': turno.tokens_saida,
        'sabor.latencia_ms': turno.latencia_ms,
        'sabor.ferramentas': list(turno.ferramentas),
        'sabor.regras_disparadas': list(turno.regras_disparadas),
        'sabor.tentativas_fallback': len(turno.tentativas_fallback),
    }
    if turno.tokens_entrada is not None or turno.tokens_saida is not None:
        atributos['sabor.tokens'] = (turno.tokens_entrada or 0) + (turno.tokens_saida or 0)
    # OpenTelemetry não aceita None nem listas vazias como valor de atributo.
    return {k: v for k, v in atributos.items() if v is not None and v != []}


def _registrar_desfecho(span: Span, turno: Any) -> None:
    span.set_attributes(atributos_turno(turno))
    if turno.falha_segura:
        span.set_status(Status(StatusCode.ERROR, 'falha segura'))


def instrumentar_turno(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Envolve `processar_turno` num span com os metadados do turno (#39).

    Tudo o que o turno cria dentro dele (spans do PydanticAI, `RespostaTurno.trace_id` e a
    auditoria) fica no mesmo trace. Sem `OBSERVABILIDADE`, o span não grava nada.
    """

    @functools.wraps(fn)
    def _envolvido(sessao_id: str, *args: Any, **kwargs: Any) -> Any:
        with _tracer.start_as_current_span(
            NOME_SPAN_TURNO, attributes={'sabor.sessao_id': sessao_id}
        ) as span:
            turno = fn(sessao_id, *args, **kwargs)
            _registrar_desfecho(span, turno)
            return turno

    return _envolvido


# ---------------------------------------------------------------------------
# Logs correlacionados no Cloud Logging
# ---------------------------------------------------------------------------


class FormatadorCloudLogging(logging.Formatter):
    """Uma linha JSON por log, no formato que o Cloud Run envia ao Cloud Logging.

    `severity` vira o nível do log, e `logging.googleapis.com/trace` liga a linha ao trace do
    turno: no Cloud Trace, os logs aparecem dentro do trace.
    """

    def __init__(self, projeto: str) -> None:
        super().__init__()
        self.projeto = projeto

    def format(self, record: logging.LogRecord) -> str:
        entrada: dict[str, Any] = {
            'severity': record.levelname,
            'message': record.getMessage(),
            'logger': record.name,
        }
        if record.exc_info:
            entrada['message'] += '\n' + self.formatException(record.exc_info)
        contexto = trace.get_current_span().get_span_context()
        if contexto.is_valid:
            entrada['logging.googleapis.com/trace'] = (
                f'projects/{self.projeto}/traces/{format(contexto.trace_id, "032x")}'
            )
            entrada['logging.googleapis.com/spanId'] = format(contexto.span_id, '016x')
            entrada['logging.googleapis.com/trace_sampled'] = contexto.trace_flags.sampled
        return json.dumps(entrada, ensure_ascii=False)


def _configurar_logs_cloud_logging(projeto: str) -> None:
    """Logs do pacote `sabor` em JSON (nível INFO) na saída padrão, que o Cloud Run coleta."""
    handler = logging.StreamHandler()
    handler.setFormatter(FormatadorCloudLogging(projeto))
    raiz = logging.getLogger('sabor')
    raiz.handlers = [
        h for h in raiz.handlers if not isinstance(h.formatter, FormatadorCloudLogging)
    ]
    raiz.addHandler(handler)
    raiz.setLevel(logging.INFO)
    raiz.propagate = False
