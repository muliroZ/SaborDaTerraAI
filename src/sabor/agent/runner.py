"""Construção do agente único PydanticAI e orquestração de `processar_turno` (Etapas 1 a 5)."""

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
import inspect
import logging
import re
import time
from types import ModuleType
from typing import Any

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded
)
from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from sabor import tools as modulo_tools
from sabor.agent.deps import AgentDeps, obter_data_referencia, obter_versao_dataset
from sabor.agent.fallback import (
    construir_capability_fallback,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
    ler_segundos_env,
)
from sabor.agent.guardrails import (
    criar_retorno_orcamento_turno,
    executar_guardrail_entrada,
    obter_usage_limits_turno,
)
from sabor.agent.mascarador import Mascarador
from sabor.agent.prompts import SYSTEM_PROMPT_BASE, instrucoes_contexto_turno
from sabor.agent.schemas import (
    RespostaAgente,
    RespostaTurno,
    RetornoAntecipado,
    TentativaFallback,
)
from sabor.db import conectar_readonly

logger = logging.getLogger(__name__)

TIMEOUT_TURNO_S = 20.0
"""Limite global de tempo do turno completo (em segundos) antes de acionar falha segura (#37).

Configurável em `TURNO_TIMEOUT_S`.
"""

FOLGA_ENCERRAMENTO_S = 1.0
"""Quanto o turno espera, além do prazo, a execução cancelada terminar (ex.: uma tool síncrona
em andamento) antes de devolver a falha segura sem ela (#49)."""

MENSAGEM_FALHA_SEGURA = (
    'Não consegui responder agora devido a uma instabilidade temporária nos provedores de IA. '
    'Por favor, tente novamente em instantes.'
)
"""Mensagem padrão devolvida quando todos os modelos falham ou o turno excede 20s."""

MENSAGEM_ENTRADA_VAZIA = 'Por favor, envie uma mensagem ou dúvida para que eu possa ajudar.'
"""Mensagem devolvida quando a entrada está vazia antes de acionar o LLM (Etapa 2)."""

NOMES_FERRAMENTAS_ESPERADAS = (
    'extrair_e_classificar_pedido',
    'consultar_dados',  # compatibilidade com testes e integrações legadas
    'buscar_produtores',
    'consultar_financeiro',
    'rastrear_lote',
    'consultar_preco',
    'consultar_safra',
    'buscar_documentos',
    'resumir_produtor',
    'rascunhar_notificacao',
)
"""Nomes de referência previstos na arquitetura (#7, #8, #9, #11, #12)."""

_PADRAO_SNAKE_CASE = re.compile(r'^[a-z][a-z0-9_]*$')


def carregar_ferramentas_registradas(
    modulo: ModuleType = modulo_tools,
) -> list[Callable[..., Any]]:
    """Carrega as ferramentas públicas em `snake_case` definidas em `sabor.tools`.

    Prioriza a ordem de `NOMES_FERRAMENTAS_ESPERADAS` e inclui também qualquer outra função
    pública em `snake_case` declarada no módulo pelo responsável pelas tools.
    """
    ferramentas: list[Callable[..., Any]] = []
    vistas: set[str] = set()

    for nome in NOMES_FERRAMENTAS_ESPERADAS:
        fn = getattr(modulo, nome, None)
        if callable(fn) and not inspect.isclass(fn):
            ferramentas.append(fn)
            vistas.add(nome)

    nome_modulo = getattr(modulo, '__name__', None)
    for nome, fn in vars(modulo).items():
        if nome in vistas or not _PADRAO_SNAKE_CASE.match(nome):
            continue
        if callable(fn) and not inspect.isclass(fn):
            mod_origem = getattr(fn, '__module__', None)
            pertence_ao_modulo = (
                nome_modulo is None
                or mod_origem in (None, nome_modulo)
                or (isinstance(mod_origem, str) and mod_origem.startswith(f'{nome_modulo}.'))
            )
            if pertence_ao_modulo:
                ferramentas.append(fn)
                vistas.add(nome)

    return ferramentas


def criar_agente(
    modelos: Sequence[Model | str] | None = None,
    *,
    tools: Sequence[Callable[..., Any]] | None = None,
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    timeout_s: float | None = None,
) -> Agent[AgentDeps, RespostaAgente]:
    """Cria e configura o agente único PydanticAI com prompt, fallback e tools disponíveis.

    `timeout_s` é o limite de cada chamada de modelo da cadeia de fallback; `None` usa
    `MODELO_TIMEOUT_S` do `.env` ou o padrão de `fallback.TIMEOUT_POR_MODELO_S`.
    """
    capability_fallback = construir_capability_fallback(
        modelos,
        gemini_api_key=gemini_api_key,
        openrouter_api_key=openrouter_api_key,
        timeout_s=timeout_s,
    )
    ferramentas_ativas = list(tools) if tools is not None else carregar_ferramentas_registradas()
    # `instructions` (e não `system_prompt`): o PydanticAI só envia o system prompt quando o
    # `message_history` está vazio; as instructions vão em toda requisição, inclusive nos
    # turnos seguintes que carregam a memória da sessão (#31).
    agente: Agent[AgentDeps, RespostaAgente] = Agent(
        deps_type=AgentDeps,
        output_type=RespostaAgente,
        instructions=SYSTEM_PROMPT_BASE,
        tools=ferramentas_ativas,
        retries=0,
        capabilities=[capability_fallback],
    )
    agente.instructions(instrucoes_contexto_turno)
    return agente


def _restaurar_pii_resposta(resposta: RespostaAgente, mascarador: Any) -> RespostaAgente:
    """Etapa 4c: Restaura tokens só no texto do operador; nunca em rascunhos (#33)."""
    restaurar_fn = getattr(mascarador, 'restaurar', None)
    if not callable(restaurar_fn):
        return resposta

    texto_restaurado = restaurar_fn(resposta.texto)
    return resposta.model_copy(update={'texto': texto_restaurado})


def obter_timeout_turno() -> float:
    """Teto do turno completo: `TURNO_TIMEOUT_S` ou `TIMEOUT_TURNO_S` (20 s, #37)."""
    return ler_segundos_env('TURNO_TIMEOUT_S', TIMEOUT_TURNO_S)


def _cancelar_consulta_em_andamento(conn: Any) -> None:
    """Cancela no servidor a consulta que uma execução abandonada ainda está rodando.

    `psycopg` segura `conn.lock` durante cada `execute`; lock livre = nada a cancelar.
    """
    lock = getattr(conn, 'lock', None)
    if lock is not None and not lock.locked():
        return
    try:
        conn.cancel_safe(timeout=2.0)
    except Exception:
        logger.warning('Não consegui cancelar a consulta do turno abandonado', exc_info=True)


def _fechar_quando_livre(conn: Any) -> None:
    """Fecha a conexão quando nenhuma consulta a estiver usando.

    `Connection.close()` do psycopg não pega o lock e finaliza o `PGconn` mesmo com outra
    thread no meio de uma consulta, o que não é seguro na libpq. Sob o lock, o fechamento
    espera a consulta em curso; uma tool órfã que tente outra consulta depois recebe
    "connection is closed", sem tocar no `PGconn` já finalizado.
    """
    lock = getattr(conn, 'lock', None)
    try:
        if lock is None:
            conn.close()
        else:
            with lock:
                conn.close()
    except Exception:
        logger.warning('Falha ao fechar a conexão do turno', exc_info=True)


def _executar_com_prazo(
    agente: Agent[AgentDeps, RespostaAgente],
    mensagem: str,
    *,
    prazo_s: float,
    conn_propria: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Roda o agente numa thread própria e devolve o resultado em até `prazo_s` (#49).

    - O prazo é aplicado dentro da thread com `asyncio.wait_for`, que cancela a requisição
      HTTP em andamento em vez de deixá-la rodar (e seguir para o próximo modelo) no fundo.
    - Se a execução não encerrar logo após o cancelamento (ex.: tool síncrona no meio de uma
      consulta), o turno não a espera: levanta `TimeoutError` em `prazo_s + FOLGA_ENCERRAMENTO_S`
      e encerra o executor sem bloquear (`shutdown(wait=False)`).
    - Tools síncronas rodam em threads que o cancelamento do asyncio não interrompe. Por isso
      a conexão aberta pelo turno é fechada só quando estiver livre (`_fechar_quando_livre`)
      e, num turno abandonado, a consulta em andamento é cancelada no servidor.
    """

    async def _rodar() -> Any:
        return await asyncio.wait_for(agente.run(mensagem, **kwargs), timeout=prazo_s)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sabor-turno')
    futuro: Future[Any] = executor.submit(lambda: asyncio.run(_rodar()))
    if conn_propria is not None:
        futuro.add_done_callback(lambda _f: _fechar_quando_livre(conn_propria))
    try:
        return futuro.result(timeout=prazo_s + FOLGA_ENCERRAMENTO_S)
    except TimeoutError:
        if conn_propria is not None:
            _cancelar_consulta_em_andamento(conn_propria)
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _contar_turnos_historico(historico: Sequence[ModelMessage]) -> int:
    """Conta quantas requisições de usuário já ocorreram na sessão atual."""
    return sum(1 for msg in historico if isinstance(msg, ModelRequest))


def processar_turno(
    sessao_id: str,
    mensagem: str,
    usuario: str = 'Dona Lúcia',
    *,
    agente: Agent[AgentDeps, RespostaAgente] | None = None,
    deps: AgentDeps | None = None,
    timeout_turno_s: float | None = None,
    usage_limits: UsageLimits | None = None,
    requisicoes_na_sessao: int | None = None,
    cache_repo: Any | None = None,
    # Hooks de integração com as demais issues do diagrama Agent System:
    carregar_memoria: Callable[[str], Sequence[ModelMessage]] | None = None,  # Etapa 1 (#31)
    guardrail_entrada: Callable[[str], RetornoAntecipado | str | None] | None = None,  # Etapa 2
    pos_processar: Callable[[RespostaAgente], RespostaAgente] | None = None,  # Etapa 4
    registrar_auditoria: Callable[[RespostaTurno], None] | None = None,  # Etapa 5a (#35)
    salvar_memoria: Callable[..., None] | None = None,  # Etapa 5b (#31)
) -> RespostaTurno:
    """Executa o turno completo do diagrama Agent System (etapas 1 a 5) com retornos antecipados."""
    inicio = time.perf_counter()

    def _salvar_turno(turno: RespostaTurno, historico_modelo_json: str | None = None) -> None:
        if salvar_memoria is None:
            return
        try:
            assinatura = inspect.signature(salvar_memoria)
            aceita_historico = (
                'historico_modelo_json' in assinatura.parameters
                or any(p.kind == inspect.Parameter.VAR_KEYWORD
                       for p in assinatura.parameters.values())
            )
        except (TypeError, ValueError):
            aceita_historico = False
        if aceita_historico:
            salvar_memoria(
                sessao_id, mensagem.strip(), turno,
                historico_modelo_json=historico_modelo_json,
            )
        else:
            salvar_memoria(sessao_id, mensagem.strip(), turno)

    # -----------------------------------------------------------------------
    # Etapa 1: Carregar memória da sessão (Issue #31)
    # -----------------------------------------------------------------------
    historico: list[ModelMessage] = (
        list(carregar_memoria(sessao_id)) if carregar_memoria is not None else []
    )

    # -----------------------------------------------------------------------
    # Etapa 2: Guardrail de entrada (retorno antecipado sem chamada LLM)
    # -----------------------------------------------------------------------
    mensagem_limpa = mensagem.strip()
    retorno_antecipado: RetornoAntecipado | None = None

    if not mensagem_limpa:
        retorno_antecipado = RetornoAntecipado(
            motivo='ENTRADA_VAZIA',
            mensagem=MENSAGEM_ENTRADA_VAZIA,
        )
    elif guardrail_entrada is not None:
        resultado_hook = guardrail_entrada(mensagem_limpa)
        if isinstance(resultado_hook, RetornoAntecipado):
            retorno_antecipado = resultado_hook
        elif isinstance(resultado_hook, str):
            retorno_antecipado = RetornoAntecipado(
                motivo='FORA_DE_ESCOPO',
                mensagem=resultado_hook,
            )
    else:
        dataset_ver = (deps.dataset_version if deps is not None else None) or 'v1'
        contagem_sessao = (
            requisicoes_na_sessao
            if requisicoes_na_sessao is not None
            else _contar_turnos_historico(historico)
        )
        retorno_antecipado = executar_guardrail_entrada(
            mensagem=mensagem_limpa,
            dataset_version=dataset_ver,
            requisicoes_na_sessao=contagem_sessao,
            cache_repo=cache_repo,
        )

    if retorno_antecipado is not None:
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        eh_cache_hit = retorno_antecipado.motivo == 'CACHE_HIT'
        turno_antecipado = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=retorno_antecipado.mensagem,
            fontes=list(retorno_antecipado.fontes),
            bloqueado_guardrail=not eh_cache_hit,
            retorno_antecipado=retorno_antecipado,
            latencia_ms=latencia_ms,
        )
        if registrar_auditoria is not None:
            registrar_auditoria(turno_antecipado)
        _salvar_turno(turno_antecipado)
        return turno_antecipado

    # -----------------------------------------------------------------------
    # Etapa 3: Execução do Agente PydanticAI + UsageLimits (2c) + Fallback (#37)
    # -----------------------------------------------------------------------
    agente_exec = agente if agente is not None else criar_agente()
    prazo_turno_s = timeout_turno_s if timeout_turno_s is not None else obter_timeout_turno()
    limites_exec = usage_limits if usage_limits is not None else obter_usage_limits_turno()
    # Não consulta o banco para entradas vazias/barradas. Nas mensagens aceitas, calcula o
    # fingerprint uma vez e reutiliza a conexão readonly durante a chamada do agente.
    conn_propria = conectar_readonly() if deps is None else None
    try:
        deps_exec = deps or AgentDeps(
            conn=conn_propria,
            data_referencia=obter_data_referencia(),
            usuario=usuario,
            dataset_version=obter_versao_dataset(conn_propria),
            mascarador=Mascarador(),
        )
        if deps is not None and not deps.dataset_version:
            deps_exec.dataset_version = obter_versao_dataset(deps.conn)
    except BaseException:
        if conn_propria is not None:
            _fechar_quando_livre(conn_propria)
        raise
    mascarar_fn = getattr(deps_exec.mascarador, 'mascarar', None)
    mensagem_para_llm = (
        mascarar_fn(mensagem_limpa) if callable(mascarar_fn) else mensagem_limpa
    )

    tentativas_erro: list[TentativaFallback] = []
    try:
        # O timeout de cada chamada vem dos modelos do agente (`criar_agente(timeout_s=...)`);
        # o turno só impõe o teto global.
        resultado = _executar_com_prazo(
            agente_exec,
            mensagem_para_llm,
            prazo_s=prazo_turno_s,
            conn_propria=conn_propria,
            deps=deps_exec,
            message_history=historico,
            usage_limits=limites_exec,
        )
    except UsageLimitExceeded:
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        retorno_orcamento = criar_retorno_orcamento_turno()
        turno_orcamento = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=retorno_orcamento.mensagem,
            bloqueado_guardrail=True,
            retorno_antecipado=retorno_orcamento,
            latencia_ms=latencia_ms,
        )
        if registrar_auditoria is not None:
            registrar_auditoria(turno_orcamento)
        _salvar_turno(turno_orcamento)
        return turno_orcamento
    except (
        TimeoutError,
        httpx.TimeoutException,
        httpx.TransportError,
        FallbackExceptionGroup,
        ModelAPIError,
        UnexpectedModelBehavior,
    ) as exc:
        tentativas_erro = extrair_tentativas_de_excecao(exc)
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        turno_falha = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=MENSAGEM_FALHA_SEGURA,
            falha_segura=True,
            tentativas_fallback=tentativas_erro,
            latencia_ms=latencia_ms,
        )
        if registrar_auditoria is not None:
            registrar_auditoria(turno_falha)
        # Grava também a falha segura para o histórico exibido ficar coerente; a memória
        # (#31) não a repassa ao modelo nos turnos seguintes.
        _salvar_turno(turno_falha)
        return turno_falha

    # -----------------------------------------------------------------------
    # Etapa 4: Pós-processamento e restauração de PII para o operador (#33)
    # -----------------------------------------------------------------------
    resposta_final = _restaurar_pii_resposta(resultado.output, deps_exec.mascarador)
    if pos_processar is not None:
        resposta_final = pos_processar(resposta_final)

    # -----------------------------------------------------------------------
    # Etapa 5: Auditoria (#35) e persistência de memória da sessão (#31)
    # -----------------------------------------------------------------------
    modelo_efetivo, provedor_efetivo, tentativas = extrair_metadados_resposta(
        resultado.new_messages()
    )
    latencia_ms = int((time.perf_counter() - inicio) * 1000)
    turno = RespostaTurno.de_resposta_agente(
        sessao_id=sessao_id,
        usuario=usuario,
        resposta=resposta_final,
        modelo_efetivo=modelo_efetivo,
        provedor_efetivo=provedor_efetivo,
        tentativas_fallback=tentativas,
        latencia_ms=latencia_ms,
    )

    if registrar_auditoria is not None:
        registrar_auditoria(turno)
    historico_modelo_json = resultado.new_messages_json()
    if isinstance(historico_modelo_json, bytes):
        historico_modelo_json = historico_modelo_json.decode('utf-8')
    if callable(mascarar_fn):
        # A serialização contém prompts e resultados de tools; aplique a mesma proteção
        # antes de persistir o histórico completo no banco.
        historico_modelo_json = mascarar_fn(historico_modelo_json)
    _salvar_turno(turno, historico_modelo_json)

    return turno
