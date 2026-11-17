"""Construção do agente único PydanticAI e descoberta dinâmica das ferramentas em `snake_case`."""

from collections.abc import Callable, Sequence
import inspect
import re
from types import ModuleType
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models import Model

from sabor import tools as modulo_tools
from sabor.agent.deps import AgentDeps
from sabor.agent.fallback import TIMEOUT_POR_MODELO_S, construir_capability_fallback
from sabor.agent.prompts import SYSTEM_PROMPT_BASE, instrucoes_contexto_turno
from sabor.agent.schemas import RespostaAgente

NOMES_FERRAMENTAS_ESPERADAS = (
    'extrair_e_classificar_pedido',
    'consultar_dados',
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
            if nome_modulo is None or mod_origem in (None, nome_modulo):
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
