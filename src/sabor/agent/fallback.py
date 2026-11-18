"""Cadeia de modelos, capability `ModelFallback` e extração de metadados de auditoria (#37)."""

from collections.abc import Sequence
import os
from typing import Any

from dotenv import load_dotenv
import httpx
from pydantic import ValidationError
from pydantic_ai.capabilities import ModelFallback
from pydantic_ai.exceptions import (
    ContentFilterError,
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    ModelRequestTimeout,
    OutputTokenLimitExceeded,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from sabor.agent.schemas import RespostaAgente, TentativaFallback

MODELO_PRIMARIO = 'gemini-3.5-flash-lite'
"""Modelo principal via Google AI Studio (ADR)."""

MODELO_FALLBACK_1 = 'qwen/qwen3-32b'
"""Primeiro modelo de contingência via OpenRouter (ADR)."""

MODELO_FALLBACK_2 = 'mistralai/mistral-small-3.2-24b-instruct'
"""Segundo modelo de contingência via OpenRouter (ADR)."""

TIMEOUT_POR_MODELO_S = 12.0
"""Timeout padrão por chamada de modelo, em segundos (configurável em `MODELO_TIMEOUT_S`).

Em 18/11 o Gemini 3.5 Flash-Lite levou ~9 s por chamada, inclusive numa mensagem trivial,
e o limite anterior de 8 s derrubava todo turno (#49). Com 12 s há folga para a chamada do
primário, e o teto do turno (20 s, #37) continua valendo para o conjunto das chamadas.
"""


def ler_segundos_env(variavel: str, padrao: float) -> float:
    """Lê uma duração em segundos do ambiente/.env; vazio ou ausente usa o padrão."""
    load_dotenv()
    bruto = (os.getenv(variavel) or '').strip()
    if not bruto:
        return padrao
    try:
        segundos = float(bruto.replace(',', '.'))
    except ValueError:
        mensagem = f'{variavel} deve ser um número de segundos, recebido {bruto!r}.'
        raise ValueError(mensagem) from None
    if segundos <= 0:
        raise ValueError(f'{variavel} deve ser maior que zero, recebido {bruto!r}.')
    return segundos


def obter_timeout_por_modelo() -> float:
    """Timeout por chamada de modelo: `MODELO_TIMEOUT_S` ou `TIMEOUT_POR_MODELO_S`."""
    return ler_segundos_env('MODELO_TIMEOUT_S', TIMEOUT_POR_MODELO_S)


def _eh_erro_rede_ou_timeout(exc: BaseException | None) -> bool:
    """Verifica se a exceção (ou sua causa imediata) é timeout ou erro de transporte HTTP."""
    atual = exc
    visitados: set[int] = set()
    while atual is not None and id(atual) not in visitados:
        visitados.add(id(atual))
        if isinstance(atual, (TimeoutError, httpx.TimeoutException, httpx.TransportError)):
            return True
        atual = atual.__cause__ or atual.__context__
    return False


def deve_fazer_fallback_excecao(exc: Exception) -> bool:
    """Decide se uma exceção de chamada ao modelo deve acionar o próximo provedor.

    Gatilhos permitidos (ADR 4.2 / #37):
      - Timeout de rede ou transporte (`TimeoutError`, `httpx.TimeoutException`,
        `httpx.TransportError`) e prazo total da requisição esgotado (`ModelRequestTimeout`)
      - HTTP 429 (Rate Limit) ou HTTP 5xx (`ModelHTTPError`)
      - Falha de schema/comportamento do modelo (`UnexpectedModelBehavior`, `ValidationError`)

    Não-gatilhos (retornam `False`):
      - Erros de autenticação/configuração/requisição inválida (HTTP 400, 401, 403, 404, 422)
      - Erros de banco de dados (`psycopg.Error`) ou regras de negócio
    """
    if isinstance(exc, ModelHTTPError):
        return exc.status_code == 429 or exc.status_code >= 500

    if isinstance(exc, (UnexpectedModelBehavior, ValidationError, ModelRequestTimeout)):
        return True

    if _eh_erro_rede_ou_timeout(exc):
        return True

    if isinstance(exc, ModelAPIError):
        return _eh_erro_rede_ou_timeout(exc.__cause__)

    return False


def resposta_invalida_para_fallback(response: ModelResponse) -> bool:
    """Response handler do `ModelFallback`: rejeita respostas vazias ou com `output_type` inválido.

    Executado no hook `after_model_request` antes de o agente aceitar a resposta, garantindo
    que falhas de schema em `RespostaAgente` acionem imediatamente o próximo modelo da fila.
    """
    if not response.parts:
        return True

    tool_calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
    if tool_calls:
        for call in tool_calls:
            try:
                args = call.args_as_dict()
            except (ValueError, TypeError):
                return True
            if call.tool_name == 'final_result':
                try:
                    RespostaAgente.model_validate(args)
                except ValidationError:
                    return True
        return False

    texto = ''.join(p.content for p in response.parts if isinstance(p, TextPart)).strip()
    if not texto:
        return True

    try:
        RespostaAgente.model_validate_json(texto)
    except ValidationError:
        return True
    return False


def configuracao_padrao_modelo(timeout_s: float | None = None) -> ModelSettings:
    """Configurações por chamada de modelo: prazo total e nenhuma retentativa no SDK (#49).

    - `request_timeout`: prazo da requisição inteira, aplicado pelo PydanticAI a qualquer modelo.
      O `timeout` do cliente HTTP só limita a espera entre bytes, e o OpenRouter manda bytes de
      keep-alive enquanto processa: uma chamada ao Qwen levou ~35 s com `timeout=12`.
    - `max_retries=0`: quem tenta de novo é a cadeia `ModelFallback` (#37), trocando de
      provedor; o cliente OpenAI-compatível repetiria cada requisição 2 vezes.
    """
    segundos = timeout_s if timeout_s is not None else obter_timeout_por_modelo()
    return ModelSettings(timeout=segundos, request_timeout=segundos, max_retries=0)


def construir_modelos_padrao(
    *,
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    timeout_s: float | None = None,
) -> list[Model]:
    """Instancia os modelos da cadeia ADR: Gemini 3.5 Flash-Lite -> Qwen3 32B -> Mistral Small."""
    load_dotenv()
    chave_gemini = (
        gemini_api_key if gemini_api_key is not None else os.getenv('GEMINI_API_KEY')
    ) or ''
    chave_openrouter = (
        openrouter_api_key if openrouter_api_key is not None else os.getenv('OPENROUTER_API_KEY')
    ) or ''

    settings = configuracao_padrao_modelo(timeout_s)
    google_provider = GoogleProvider(api_key=chave_gemini or 'gemini-key-ausente')
    primario: Model = GoogleModel(
        MODELO_PRIMARIO,
        provider=google_provider,
        settings=settings,
    )

    modelos: list[Model] = [primario]
    if chave_openrouter.strip():
        openrouter_provider = OpenRouterProvider(api_key=chave_openrouter.strip())
        modelos.append(
            OpenAIChatModel(
                MODELO_FALLBACK_1,
                provider=openrouter_provider,
                settings=settings,
            )
        )
        modelos.append(
            OpenAIChatModel(
                MODELO_FALLBACK_2,
                provider=openrouter_provider,
                settings=settings,
            )
        )
    return modelos


def construir_capability_fallback(
    modelos: Sequence[Model | str] | None = None,
    *,
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    timeout_s: float | None = None,
) -> ModelFallback[Any]:
    """Cria a capability `ModelFallback` do PydanticAI 2.68.0 com a cadeia de modelos e gatilhos."""
    cadeia = (
        list(modelos)
        if modelos is not None
        else construir_modelos_padrao(
            gemini_api_key=gemini_api_key,
            openrouter_api_key=openrouter_api_key,
            timeout_s=timeout_s,
        )
    )
    return ModelFallback(
        *cadeia,
        fallback_on=[
            deve_fazer_fallback_excecao,
            resposta_invalida_para_fallback,
            ContentFilterError,
            OutputTokenLimitExceeded,
        ],
    )


def extrair_metadados_resposta(
    mensagens: Sequence[ModelMessage],
) -> tuple[str | None, str | None, list[TentativaFallback]]:
    """Extrai `model_name`, `provider_name` e `failed_attempts` reais dos `ModelResponse`."""
    modelo_efetivo: str | None = None
    provedor_efetivo: str | None = None
    tentativas: list[TentativaFallback] = []

    for msg in mensagens:
        if isinstance(msg, ModelResponse):
            if msg.model_name:
                modelo_efetivo = msg.model_name
            provedor_msg = msg.provider_name or msg.canonical_system
            if not provedor_msg and msg.model_id and ':' in msg.model_id:
                provedor_msg = msg.model_id.split(':', 1)[0]
            if provedor_msg:
                provedor_efetivo = provedor_msg
            if msg.failed_attempts:
                tentativas.extend(
                    TentativaFallback.de_attempt(att) for att in msg.failed_attempts
                )

    return modelo_efetivo, provedor_efetivo, tentativas


def extrair_tentativas_de_excecao(exc: BaseException) -> list[TentativaFallback]:
    """Extrai o histórico de tentativas quando todos os modelos do `ModelFallback` falham."""
    if isinstance(exc, FallbackExceptionGroup):
        return [TentativaFallback.de_attempt(att) for att in exc.attempts]
    return []
