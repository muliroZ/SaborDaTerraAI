"""Testes unitários do Passo 5: System Prompt, contexto dinâmico e descoberta de tools."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sabor.agent.deps import AgentDeps
from sabor.agent.prompts import SYSTEM_PROMPT_BASE, formatar_contexto_dinamico
from sabor.agent.runner import (
    NOMES_FERRAMENTAS_ESPERADAS,
    carregar_ferramentas_registradas,
    criar_agente,
)


def test_system_prompt_cobre_requisitos_do_case():
    prompt = SYSTEM_PROMPT_BASE.lower()
    # Regra 1: Português coloquial e resposta em PT-BR mesmo para inglês (PED-011)
    assert 'português brasileiro (pt-br)' in prompt
    assert 'ped-011' in prompt
    # Regra 2: Ambiguidades e pedidos incompletos (data x dia da semana, queijo, manteiga)
    assert 'aquele queijo' in prompt
    assert 'manteiga de garrafa ou queijo manteiga' in prompt
    assert 'ambiguidade' in prompt
    # Regra 3: Citar fonte no formato [arquivo · registro/seção]
    assert '[arquivo · registro/seção]' in prompt
    # Regra 4: Dizer "não encontrei" e não extrapolar 180 produtores / PAA
    assert 'não encontrei' in prompt
    assert '180 produtores' in prompt
    assert 'paa' in prompt
    # Regra 5: Conformidade (P008, P017, SIE), inadimplência (SuperBom) e privacidade
    assert 'p008' in prompt
    assert 'p017' in prompt
    assert 'superbom' in prompt


def test_formatar_contexto_dinamico_enxuto():
    ctx_str = formatar_contexto_dinamico(
        data_referencia=date(2026, 10, 30),
        usuario='Dona Lúcia',
        dataset_version='sha256-v1',
    )
    assert ctx_str == 'Data de referência: 2026-10-30 | Operador: Dona Lúcia | Dataset: sha256-v1'


def test_carregar_ferramentas_registradas_descobre_padrao_e_extras_snake_case():
    modulo_vazio = SimpleNamespace()
    assert carregar_ferramentas_registradas(modulo_vazio) == []  # type: ignore[arg-type]

    # Carrega tanto os nomes esperados quanto ferramentas extras em snake_case (ignorando _privadas)
    modulo_mock = SimpleNamespace(
        consultar_dados=lambda ctx: {},
        ferramenta_extra_safra=lambda ctx: {},
        _helper_interno=lambda: None,
        ClasseIgnorada=dict,
    )
    carregadas = carregar_ferramentas_registradas(modulo_mock)  # type: ignore[arg-type]
    assert len(carregadas) == 2


def test_agente_injeta_contexto_e_aceita_ferramentas_mock():
    def consultar_dados(ctx: RunContext[AgentDeps], dominio: str) -> str:
        """Mock de consulta."""
        return dominio

    prompts_recebidos: list[str] = []
    tools_disponiveis: list[str] = []

    def fn_inspeciona(messages, info: AgentInfo) -> ModelResponse:
        tools_disponiveis.extend(t.name for t in info.function_tools)
        for msg in messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, SystemPromptPart):
                        prompts_recebidos.append(part.content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Contexto validado.',
                        'fontes': [],
                        'flags': [],
                        'rascunhos': [],
                    },
                )
            ]
        )

    modelo_teste = FunctionModel(fn_inspeciona, model_name='modelo-teste')
    agente = criar_agente([modelo_teste], tools=[consultar_dados])

    deps = AgentDeps(
        conn=MagicMock(),
        data_referencia=date(2026, 10, 30),
        dataset_version='v-test',
        usuario='Seu Antônio',
    )

    res = agente.run_sync('Teste de contexto', deps=deps)
    assert res.output.texto == 'Contexto validado.'
    assert tools_disponiveis == ['consultar_dados']
    assert 'consultar_dados' in NOMES_FERRAMENTAS_ESPERADAS

    texto_prompts = '\n'.join(prompts_recebidos)
    esperado = 'Data de referência: 2026-10-30 | Operador: Seu Antônio | Dataset: v-test'
    assert esperado in texto_prompts
