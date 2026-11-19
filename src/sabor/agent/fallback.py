"""Cadeia de modelos, capability `ModelFallback` com circuit breaker e metadados (#37)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import logging
import math
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any
import unicodedata

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
    RetryModelRequest,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext, ToolDefinition

from sabor.agent.schemas import RespostaAgente, TentativaFallback

if TYPE_CHECKING:
    from pydantic_ai.models import ModelRequestContext

logger = logging.getLogger(__name__)

MODELO_PRIMARIO = 'gemini-3.5-flash-lite'
"""Modelo principal via Google AI Studio (ADR)."""

MODELO_FALLBACK_1 = 'mistralai/mistral-small-3.2-24b-instruct'
"""Primeiro modelo de contingência via OpenRouter."""

MODELO_FALLBACK_2 = 'qwen/qwen3-32b'
"""Segundo modelo de contingência via OpenRouter."""

TIMEOUT_POR_MODELO_S = 12.0
"""Timeout padrão por chamada de modelo, em segundos (configurável em `MODELO_TIMEOUT_S`).

Em 18/11 o Gemini 3.5 Flash-Lite levou ~9 s por chamada, inclusive numa mensagem trivial,
e o limite anterior de 8 s derrubava todo turno (#49). Com 12 s há folga para a chamada do
primário, e o teto do turno (20 s, #37) continua valendo para o conjunto das chamadas.
"""

COOLDOWN_CIRCUIT_BREAKER_S = 180.0
"""Tempo padrão de quarentena (em segundos) de um modelo após falha transitória (5xx/429/timeout).

Configurável em `FALLBACK_COOLDOWN_S` (`0` desliga o circuit breaker). Evita insistir no mesmo
modelo lento ou indisponível a cada passo do turno e nas perguntas seguintes enquanto durar o
pico de instabilidade, voltando automaticamente a testá-lo assim que o cooldown expirar.
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
    if not math.isfinite(segundos) or segundos <= 0:
        raise ValueError(
            f'{variavel} deve ser um número finito maior que zero, recebido {bruto!r}.'
        )
    return segundos


def obter_timeout_por_modelo() -> float:
    """Timeout por chamada de modelo: `MODELO_TIMEOUT_S` ou `TIMEOUT_POR_MODELO_S`."""
    return ler_segundos_env('MODELO_TIMEOUT_S', TIMEOUT_POR_MODELO_S)


def obter_cooldown_circuit_breaker() -> float:
    """Cooldown do circuit breaker em segundos: `FALLBACK_COOLDOWN_S` ou padrão (`0` desliga)."""
    load_dotenv()
    bruto = (os.getenv('FALLBACK_COOLDOWN_S') or '').strip()
    if not bruto:
        return COOLDOWN_CIRCUIT_BREAKER_S
    try:
        segundos = float(bruto.replace(',', '.'))
    except ValueError:
        mensagem = f'FALLBACK_COOLDOWN_S deve ser um número de segundos, recebido {bruto!r}.'
        raise ValueError(mensagem) from None
    if not math.isfinite(segundos) or segundos < 0:
        raise ValueError(
            f'FALLBACK_COOLDOWN_S deve ser um número finito >= 0, recebido {bruto!r}.'
        )
    return segundos


def _eh_erro_rede_ou_timeout(
    exc: BaseException | None,
    *,
    seguir_contexto: bool = True,
) -> bool:
    """Verifica se a exceção (ou sua causa imediata) é timeout ou erro de transporte HTTP."""
    atual = exc
    visitados: set[int] = set()
    while atual is not None and id(atual) not in visitados:
        visitados.add(id(atual))
        if isinstance(atual, (TimeoutError, httpx.TimeoutException, httpx.TransportError)):
            return True
        atual = atual.__cause__ or (atual.__context__ if seguir_contexto else None)
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


_PADRAO_CITACAO_FONTE = re.compile(r'\[([^[\]·\n]+?)\s*(?:·|-)\s*([^[\]\n]+?)\]')
_PADRAO_CITACAO_FONTE_PONTO = re.compile(r'\[([^[\]·\n]+?)\s*·\s*([^[\]\n]+?)\]')
_PADRAO_LINHA_FONTES = re.compile(
    r'(?im)^[ \t]*(?:[-*]\s*)?\*{0,2}fontes?\*{0,2}\s*:.*(?:\n|$)'
)
_PADRAO_GRUPO_CITACOES_PARENTESES = re.compile(
    r'[ \t]*\(\s*(?:(?:conforme|segundo|vide|fontes?)\s*:?\s*)?'
    r'`?\[[^[\]·\n]+?\s*·\s*[^[\]\n]+?\]`?'
    r'(?:\s*(?:,|;|e|\+)?\s*`?\[[^[\]·\n]+?\s*·\s*[^[\]\n]+?\]`?)*\s*\)'
)
_PADRAO_GRUPO_CITACOES_INLINE = re.compile(
    r'(?:,\s*(?:conforme|segundo|vide)\s+|[ \t]+(?:conforme|segundo|vide)\s+|[ \t]*)'
    r'`?\[[^[\]·\n]+?\s*·\s*[^[\]\n]+?\]`?'
    r'(?:\s*(?:,|;|e|\+)?\s*`?\[[^[\]·\n]+?\s*·\s*[^[\]\n]+?\]`?)*'
)

_TERMOS_SOLICITACAO_RELATORIO = (
    'relat',
    'levantamento',
    'dossie',
    'planilha',
    'baixar',
    'download',
    'exportar',
    'checklist',
    'consolidado',
    'consolidada',
    'balanco',
    'panorama',
    'quadro geral',
    'adagro',
    'ped-2026-014',
    'ped-014',
)

_DOMINIOS_RELATORIO_COOPERATIVA = (
    'contas',
    'receber',
    'vencid',
    'inadimpl',
    'financ',
    'organic',
    'certific',
    'adagro',
    'dossie',
    'ped-2026-014',
    'ped-014',
    'inspec',
    'comprador',
)

_PADRAO_RELATORIO_PRODUTOR = re.compile(
    r'\b(?:da|do|de|produtor|produtora|sobre)\s+(?:p\d{3}\b|[a-z]{3,})'
)


def _normalizar_busca(texto: str) -> str:
    sem_acentos = ''.join(
        c for c in unicodedata.normalize('NFKD', texto) if not unicodedata.combining(c)
    )
    return sem_acentos.lower()


def mensagem_solicita_relatorio(texto: str) -> bool:
    """Indica se o pedido do usuário solicita explicitamente um dos relatórios da cooperativa.

    Pedidos de relatório sobre um produtor específico (ex.: "gere um relatório da Maria das
    Graças") sem menção a contas a receber, orgânicos vigentes ou ADAGRO devem ir para
    `resumir_produtor`, e não para `gerar_relatorio`.
    """
    if not texto or not texto.strip():
        return False
    norm = _normalizar_busca(texto)
    if not any(termo in norm for termo in _TERMOS_SOLICITACAO_RELATORIO):
        return False
    if any(dom in norm for dom in _DOMINIOS_RELATORIO_COOPERATIVA):
        return True
    if _PADRAO_RELATORIO_PRODUTOR.search(norm):
        return False
    return True


def limpar_citacoes_inline_texto(texto: str) -> str:
    """Remove blocos `Fontes:` e colchetes `[arquivo · referencia]` redundantes do corpo do texto.

    Como exige o separador `·`, tokens de mascaramento PII (`[CPF_1]`, `[TEL_1]`, etc.) permanecem
    intactos.
    """
    if not texto:
        return texto
    limpo = _PADRAO_LINHA_FONTES.sub('', texto)
    limpo = _PADRAO_GRUPO_CITACOES_PARENTESES.sub('', limpo)
    limpo = _PADRAO_GRUPO_CITACOES_INLINE.sub('', limpo)
    limpo = re.sub(r'[ \t]+([.,;:])', r'\1', limpo)
    limpo = re.sub(r'\n{3,}', '\n\n', limpo).strip()
    return limpo or texto.strip()


def _mensagens_turno_atual(mensagens: Sequence[ModelMessage]) -> Sequence[ModelMessage]:
    """Recorta apenas as mensagens a partir do último `UserPromptPart` (turno atual)."""
    for idx in range(len(mensagens) - 1, -1, -1):
        msg = mensagens[idx]
        if isinstance(msg, ModelRequest) and any(
            isinstance(p, UserPromptPart) for p in msg.parts
        ):
            return mensagens[idx:]
    return mensagens


def _extrair_texto_usuario_turno(
    ctx: RunContext[Any],
    msgs_turno: Sequence[ModelMessage],
) -> str:
    partes: list[str] = []
    prompt_ctx = getattr(ctx, 'prompt', None)
    if isinstance(prompt_ctx, str):
        partes.append(prompt_ctx)
    elif isinstance(prompt_ctx, Sequence):
        partes.extend(p for p in prompt_ctx if isinstance(p, str))

    for msg in msgs_turno:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    if isinstance(part.content, str):
                        partes.append(part.content)
                    elif isinstance(part.content, Sequence):
                        partes.extend(c for c in part.content if isinstance(c, str))
    return ' '.join(partes)


def _coletar_fontes_de_retorno_tool(valor: Any, destino: list[tuple[str, str]]) -> None:
    """Extrai pares `(arquivo, referencia)` reais de objetos `Fonte` nos retornos das tools."""
    if valor is None:
        return
    if isinstance(valor, dict):
        arq = valor.get('arquivo')
        ref = valor.get('referencia')
        if isinstance(arq, str) and isinstance(ref, str) and arq.strip() and ref.strip():
            destino.append((arq.strip(), ref.strip()))
            return
        for v in valor.values():
            _coletar_fontes_de_retorno_tool(v, destino)
        return
    if isinstance(valor, (list, tuple, set)):
        for item in valor:
            _coletar_fontes_de_retorno_tool(item, destino)
        return
    arq = getattr(valor, 'arquivo', None)
    ref = getattr(valor, 'referencia', None)
    if isinstance(arq, str) and isinstance(ref, str) and arq.strip() and ref.strip():
        destino.append((arq.strip(), ref.strip()))
        return
    if hasattr(valor, 'model_dump'):
        try:
            _coletar_fontes_de_retorno_tool(valor.model_dump(), destino)
        except Exception:
            pass


def _normalizar_resposta_texto_livre(
    response: ModelResponse,
    mensagens_contexto: Sequence[ModelMessage] = (),
) -> ModelResponse:
    """Converte resposta final em texto Markdown puro (comum no Mistral/Qwen via OpenRouter
    após retorno de tools) em `ToolCallPart('final_result')`, extraindo as citações
    `[arquivo · referencia]` ou reaproveitando as fontes retornadas pelas tools no turno.
    Strings que começam com `{` (JSON malformado) não são convertidas para continuarem sendo
    rejeitadas por `resposta_invalida_para_fallback`.
    """
    if not response.parts or any(isinstance(p, ToolCallPart) for p in response.parts):
        return response

    texto = ''.join(p.content for p in response.parts if isinstance(p, TextPart)).strip()
    if not texto or texto.startswith('{'):
        return response

    try:
        RespostaAgente.model_validate_json(texto)
        return response
    except ValidationError:
        pass

    pares_fontes: list[tuple[str, str]] = [
        (arq.strip(), ref.strip()) for arq, ref in _PADRAO_CITACAO_FONTE.findall(texto)
    ]
    if not pares_fontes and mensagens_contexto:
        for msg in _mensagens_turno_atual(mensagens_contexto):
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        _coletar_fontes_de_retorno_tool(part.content, pares_fontes)

    fontes: list[dict[str, str]] = []
    vistas: set[tuple[str, str]] = set()
    for arq, ref in pares_fontes:
        chave = (arq, ref)
        if arq and ref and chave not in vistas:
            vistas.add(chave)
            fontes.append({'arquivo': arq, 'referencia': ref})

    texto_limpo = limpar_citacoes_inline_texto(texto)

    nova_part = ToolCallPart(
        tool_name='final_result',
        args={
            'texto': texto_limpo,
            'fontes': fontes,
            'flags': [],
            'rascunhos': [],
        },
        tool_call_id='final_result_text_fallback',
    )
    return ModelResponse(
        parts=[nova_part],
        usage=response.usage,
        model_name=response.model_name,
        timestamp=response.timestamp,
        provider_name=response.provider_name,
        provider_url=response.provider_url,
        provider_details=response.provider_details,
        provider_response_id=response.provider_response_id,
        finish_reason=response.finish_reason,
        run_id=response.run_id,
        metadata=response.metadata,
    )


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


def _eh_falha_circuit_breaker(exc: Exception) -> bool:
    """Identifica falhas transitórias de provedor/latência que abrem o circuit breaker.

    Diferente de `deve_fazer_fallback_excecao`, exclui `UnexpectedModelBehavior` e
    `ValidationError` para que um desvio pontual de schema faça fallback no passo atual
    sem colocar o modelo em quarentena para todas as próximas perguntas.
    """
    if isinstance(exc, ModelHTTPError):
        return exc.status_code == 429 or exc.status_code >= 500
    if isinstance(exc, ModelRequestTimeout):
        return True
    if _eh_erro_rede_ou_timeout(exc, seguir_contexto=False):
        return True
    if isinstance(exc, ModelAPIError):
        return _eh_erro_rede_ou_timeout(exc.__cause__, seguir_contexto=False)
    return False


def _chave_modelo(modelo: Model | str) -> str:
    """Identificador estável de um candidato da cadeia para controle de quarentena."""
    if isinstance(modelo, str):
        return modelo
    return f'{modelo.system}:{modelo.model_name}'


class _EstadoCircuitBreaker:
    """Estado thread-safe compartilhado entre turnos de um agente para quarentena por modelo."""

    def __init__(
        self,
        cooldown_s: float,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cooldown_s = cooldown_s
        self._relogio = relogio
        self._lock = threading.Lock()
        self._quarentena_ate: dict[str, float] = {}

    def em_cooldown(self, chave: str) -> bool:
        if self.cooldown_s <= 0:
            return False
        agora = self._relogio()
        with self._lock:
            ate = self._quarentena_ate.get(chave)
            if ate is None:
                return False
            if agora >= ate:
                self._quarentena_ate.pop(chave, None)
                return False
            return True

    def registrar_falha(self, chave: str) -> None:
        if self.cooldown_s <= 0:
            return
        expira = self._relogio() + self.cooldown_s
        with self._lock:
            self._quarentena_ate[chave] = expira

    def registrar_sucesso(self, chave: str) -> None:
        with self._lock:
            self._quarentena_ate.pop(chave, None)

    def modelos_em_cooldown(self) -> list[str]:
        if self.cooldown_s <= 0:
            return []
        agora = self._relogio()
        with self._lock:
            ativos = [k for k, ate in self._quarentena_ate.items() if agora < ate]
            for k in list(self._quarentena_ate):
                if agora >= self._quarentena_ate[k]:
                    self._quarentena_ate.pop(k, None)
            return ativos

    def limpar(self) -> None:
        with self._lock:
            self._quarentena_ate.clear()


@dataclass(init=False)
class FallbackComCircuitBreaker(ModelFallback[Any]):
    """`ModelFallback` com circuit breaker (quarentena temporária por modelo com falha).

    Mantém a ordem original do ADR (`Gemini -> Qwen -> Mistral`). Quando um modelo sofre
    falha transitória (`429`, `5xx`, timeout ou erro de rede), entra em quarentena por
    `cooldown_s` segundos: os próximos passos do mesmo turno e as perguntas seguintes
    roteiam direto para o primeiro modelo saudável. Se todos estiverem em quarentena ou os
    saudáveis também falharem, os modelos em quarentena ainda são tentados como último recurso.
    """

    _circuit: _EstadoCircuitBreaker = field(repr=False)

    def __init__(
        self,
        *models: Model | str,
        fallback_on: Any = (ModelAPIError, ContentFilterError, OutputTokenLimitExceeded),
        sticky: bool = False,
        cooldown_s: float | None = None,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(*models, fallback_on=fallback_on, sticky=sticky)
        segundos = obter_cooldown_circuit_breaker() if cooldown_s is None else float(cooldown_s)
        self._circuit = _EstadoCircuitBreaker(cooldown_s=segundos, relogio=relogio)

    @property
    def cooldown_s(self) -> float:
        return self._circuit.cooldown_s

    def limpar_circuit_breaker(self) -> None:
        """Zera quarentenas ativas (útil em testes ou reinício manual)."""
        self._circuit.limpar()

    def _particionar_por_saude(self) -> tuple[list[Model | str], list[Model | str]]:
        saudaveis: list[Model | str] = []
        em_quarentena: list[Model | str] = []
        for candidato in self.models:
            if self._circuit.em_cooldown(_chave_modelo(candidato)):
                em_quarentena.append(candidato)
            else:
                saudaveis.append(candidato)
        return saudaveis, em_quarentena

    async def prepare_tools(
        self,
        ctx: RunContext[Any],
        tool_defs: list[ToolDefinition],
    ) -> list[ToolDefinition]:
        """Oculta `gerar_relatorio` quando a pergunta não pede relatório/levantamento/dossiê.

        Mantém a lista de ferramentas estável entre os passos do turno (nunca remove uma
        ferramenta que já aparece no histórico `tool_calls` do turno atual).
        """
        tool_defs = await super().prepare_tools(ctx, tool_defs)
        if len(tool_defs) <= 1:
            return tool_defs

        msgs_turno = _mensagens_turno_atual(getattr(ctx, 'messages', ()) or ())
        texto_usuario = _extrair_texto_usuario_turno(ctx, msgs_turno)

        if not mensagem_solicita_relatorio(texto_usuario):
            return [t for t in tool_defs if t.name != 'gerar_relatorio']

        return tool_defs

    async def prepare_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        if self._step != ctx.run_step:
            self._step = ctx.run_step
            saudaveis, em_quarentena = self._particionar_por_saude()
            self._remaining = [*saudaveis, *em_quarentena]
            self._attempted_models = []
            self._attempted_ids = set()
            self._chain = self._new_chain()
            self._pin_checked = False
            self._moved = False

            chave_atual = _chave_modelo(request_context.model)
            if saudaveis and self._circuit.em_cooldown(chave_atual):
                self._pin_checked = True
                proximo = self._remaining.pop(0)
                if not any(m is request_context.model for m in self._remaining) and (
                    proximo is not request_context.model
                ):
                    self._remaining.append(request_context.model)
                if proximo is not request_context.model:
                    logger.info(
                        'Circuit breaker ativo para %s; roteando direto para %s',
                        chave_atual,
                        _chave_modelo(proximo),
                    )
                    self._moved = True
                    raise RetryModelRequest(proximo)

        return await super().prepare_model_request(ctx, request_context)

    async def on_model_request_error(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        error: Exception,
    ) -> ModelResponse:
        if _eh_falha_circuit_breaker(error):
            chave = _chave_modelo(request_context.model)
            self._circuit.registrar_falha(chave)
            if self._circuit.cooldown_s > 0:
                logger.warning(
                    'Circuit breaker aberto para %s por %.0fs (%s)',
                    chave,
                    self._circuit.cooldown_s,
                    type(error).__name__,
                )
        return await super().on_model_request_error(
            ctx, request_context=request_context, error=error
        )

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        response_norm = _normalizar_resposta_texto_livre(
            response, getattr(request_context, 'messages', ())
        )
        resultado = await super().after_model_request(
            ctx, request_context=request_context, response=response_norm
        )
        self._circuit.registrar_sucesso(_chave_modelo(request_context.model))
        return resultado


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
    """Instancia os modelos da cadeia: Gemini 3.5 Flash-Lite -> Mistral Small -> Qwen3 32B."""
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
        # Qwen3-32B ativa raciocínio longo por padrão no OpenRouter e, com `tool_choice='required'`,
        # o roteador pode cair no provedor `Tenstorrent`, estourando facilmente o timeout de 30s.
        # Desabilitar `reasoning`, manter `tool_choice='auto'` e ignorar `Tenstorrent` mantém o
        # roteamento nos provedores rápidos (ex.: DeepInfra).
        settings_qwen = OpenAIChatModelSettings(
            timeout=settings.get('timeout'),
            request_timeout=settings.get('request_timeout'),
            max_retries=settings.get('max_retries'),
            extra_body={
                'reasoning': {'enabled': False, 'effort': 'none'},
                'tool_choice': 'auto',
                'provider': {'ignore': ['Tenstorrent']},
            },
        )
        modelos.append(
            OpenAIChatModel(
                MODELO_FALLBACK_2,
                provider=openrouter_provider,
                settings=settings_qwen,
            )
        )
    return modelos


def construir_capability_fallback(
    modelos: Sequence[Model | str] | None = None,
    *,
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    timeout_s: float | None = None,
    cooldown_s: float | None = None,
    relogio: Callable[[], float] = time.monotonic,
) -> FallbackComCircuitBreaker:
    """Cria a capability `ModelFallback` com circuit breaker, cadeia de modelos e gatilhos."""
    cadeia = (
        list(modelos)
        if modelos is not None
        else construir_modelos_padrao(
            gemini_api_key=gemini_api_key,
            openrouter_api_key=openrouter_api_key,
            timeout_s=timeout_s,
        )
    )
    return FallbackComCircuitBreaker(
        *cadeia,
        fallback_on=[
            deve_fazer_fallback_excecao,
            resposta_invalida_para_fallback,
            ContentFilterError,
            OutputTokenLimitExceeded,
        ],
        cooldown_s=cooldown_s,
        relogio=relogio,
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
