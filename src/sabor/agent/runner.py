"""Construção do agente único PydanticAI e orquestração de `processar_turno` (Etapas 1 a 5)."""

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import inspect
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
)
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model

from sabor import tools as modulo_tools
from sabor.agent.deps import AgentDeps, obter_data_referencia
from sabor.agent.fallback import (
    TIMEOUT_POR_MODELO_S,
    configuracao_padrao_modelo,
    construir_capability_fallback,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
)
from sabor.agent.mascarador import Mascarador
from sabor.agent.prompts import SYSTEM_PROMPT_BASE, instrucoes_contexto_turno
from sabor.agent.schemas import RespostaAgente, RespostaTurno, TentativaFallback
from sabor.db import conectar_readonly

TIMEOUT_TURNO_S = 20.0
"""Limite global de tempo do turno completo (em segundos) antes de acionar falha segura (#37)."""

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
    timeout_s: float = TIMEOUT_POR_MODELO_S,
) -> Agent[AgentDeps, RespostaAgente]:
    """Cria e configura o agente único PydanticAI com prompt, fallback e tools disponíveis."""
    capability_fallback = construir_capability_fallback(
        modelos,
        gemini_api_key=gemini_api_key,
        openrouter_api_key=openrouter_api_key,
        timeout_s=timeout_s,
    )
    ferramentas_ativas = list(tools) if tools is not None else carregar_ferramentas_registradas()
    agente: Agent[AgentDeps, RespostaAgente] = Agent(
        deps_type=AgentDeps,
        output_type=RespostaAgente,
        system_prompt=SYSTEM_PROMPT_BASE,
        tools=ferramentas_ativas,
        retries=0,
        capabilities=[capability_fallback],
    )
    agente.system_prompt(instrucoes_contexto_turno)
    return agente


def _restaurar_pii_resposta(resposta: RespostaAgente, mascarador: Any) -> RespostaAgente:
    """Etapa 4c: Restaura tokens só no texto do operador; nunca em rascunhos (#33)."""
    restaurar_fn = getattr(mascarador, 'restaurar', None)
    if not callable(restaurar_fn):
        return resposta

    texto_restaurado = restaurar_fn(resposta.texto)
    return resposta.model_copy(update={'texto': texto_restaurado})


def processar_turno(
    sessao_id: str,
    mensagem: str,
    usuario: str = 'Dona Lúcia',
    *,
    agente: Agent[AgentDeps, RespostaAgente] | None = None,
    deps: AgentDeps | None = None,
    timeout_turno_s: float = TIMEOUT_TURNO_S,
    # Hooks de integração com as demais issues do diagrama Agent System:
    carregar_memoria: Callable[[str], Sequence[ModelMessage]] | None = None,  # Etapa 1 (#31)
    guardrail_entrada: Callable[[str], str | None] | None = None,  # Etapa 2
    pos_processar: Callable[[RespostaAgente], RespostaAgente] | None = None,  # Etapa 4
    registrar_auditoria: Callable[[RespostaTurno], None] | None = None,  # Etapa 5a (#35)
    salvar_memoria: Callable[[str, str, RespostaTurno], None] | None = None,  # Etapa 5b (#31)
) -> RespostaTurno:
    """Executa o turno completo do diagrama Agent System (etapas 1 a 5) com retornos antecipados.

    Fluxo:
      1. Memória (#31): carrega o histórico da `sessao_id`.
      2. Guardrail de entrada: valida a mensagem antes do LLM; se bloqueada, retorna antecipado.
      3. PydanticAI (#37): monta contexto (3.1), decide/executa ferramentas read-only (3.2-3.3)
         e gera `RespostaAgente` (3.4) sob `ModelFallback` e teto global de `timeout_turno_s` (20s).
         Em caso de falha de todos os provedores ou estouro de tempo, retorna falha segura.
      4. Pós-processamento (#33): restaura PII para o operador interno e aplica validações de saída.
      5. Persistência (#35 e #31): registra auditoria (modelo/provedor efetivos) e salva memória.
    """
    inicio = time.perf_counter()

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
    motivo_bloqueio: str | None = None
    if not mensagem_limpa:
        motivo_bloqueio = MENSAGEM_ENTRADA_VAZIA
    elif guardrail_entrada is not None:
        motivo_bloqueio = guardrail_entrada(mensagem_limpa)

    if motivo_bloqueio is not None:
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        turno_bloqueado = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=motivo_bloqueio,
            bloqueado_guardrail=True,
            latencia_ms=latencia_ms,
        )
        if registrar_auditoria is not None:
            registrar_auditoria(turno_bloqueado)
        return turno_bloqueado

    # -----------------------------------------------------------------------
    # Etapa 3: Execução do Agente PydanticAI + ModelFallback + Falha Segura (#37)
    # -----------------------------------------------------------------------
    agente_exec = agente if agente is not None else criar_agente()
    gerenciador_conn = (
        nullcontext(deps.conn) if deps is not None else conectar_readonly()
    )

    tentativas_erro: list[TentativaFallback] = []
    try:
        with gerenciador_conn as conn_ro:
            deps_exec = (
                deps
                if deps is not None
                else AgentDeps(
                    conn=conn_ro,
                    data_referencia=obter_data_referencia(),
                    usuario=usuario,
                    mascarador=Mascarador(),
                )
            )
            mascarar_fn = getattr(deps_exec.mascarador, 'mascarar', None)
            mensagem_para_llm = (
                mascarar_fn(mensagem_limpa) if callable(mascarar_fn) else mensagem_limpa
            )
            settings_turno = configuracao_padrao_modelo(
                timeout_s=min(TIMEOUT_POR_MODELO_S, timeout_turno_s)
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                futuro = executor.submit(
                    agente_exec.run_sync,
                    mensagem_para_llm,
                    deps=deps_exec,
                    message_history=historico,
                    model_settings=settings_turno,
                )
                resultado = futuro.result(timeout=timeout_turno_s)
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
    if salvar_memoria is not None:
        salvar_memoria(sessao_id, mensagem_limpa, turno)

    return turno
