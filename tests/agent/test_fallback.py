"""Testes unitários da cadeia de modelos, `ModelFallback` e metadados de auditoria (#37)."""

import httpx
import psycopg
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
import pytest

from sabor.agent.fallback import (
    MODELO_FALLBACK_1,
    MODELO_FALLBACK_2,
    MODELO_PRIMARIO,
    TIMEOUT_POR_MODELO_S,
    construir_capability_fallback,
    construir_modelos_padrao,
    deve_fazer_fallback_excecao,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
    resposta_invalida_para_fallback,
)
from sabor.agent.schemas import RespostaAgente


def test_construir_modelos_padrao_preserva_ordem_e_provedores():
    modelos = construir_modelos_padrao(
        gemini_api_key='fake-gemini',
        openrouter_api_key='fake-openrouter',
    )
    assert [m.model_name for m in modelos] == [
        MODELO_PRIMARIO,
        MODELO_FALLBACK_1,
        MODELO_FALLBACK_2,
    ]
    assert [m.system for m in modelos] == ['google', 'openrouter', 'openrouter']
    for m in modelos:
        assert m.settings is not None
        assert m.settings.get('timeout') == TIMEOUT_POR_MODELO_S


def test_deve_fazer_fallback_excecao_gatilhos_e_nao_gatilhos():
    # Gatilhos válidos: 429, 5xx, timeouts, transporte, UnexpectedModelBehavior
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=429, model_name='m')) is True
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=500, model_name='m')) is True
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=503, model_name='m')) is True
    assert deve_fazer_fallback_excecao(TimeoutError('tempo esgotado')) is True
    assert deve_fazer_fallback_excecao(httpx.ReadTimeout('read timeout')) is True
    assert deve_fazer_fallback_excecao(httpx.ConnectError('falha de rede')) is True
    assert deve_fazer_fallback_excecao(UnexpectedModelBehavior('schema quebrado')) is True

    erro_api_timeout = ModelAPIError(model_name='m', message='falha')
    erro_api_timeout.__cause__ = httpx.ConnectTimeout('connect timeout')
    assert deve_fazer_fallback_excecao(erro_api_timeout) is True

    # Não-gatilhos: 400, 401, 403, 404, 422, erros de banco e erros de negócio
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=400, model_name='m')) is False
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=401, model_name='m')) is False
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=403, model_name='m')) is False
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=404, model_name='m')) is False
    assert deve_fazer_fallback_excecao(ModelHTTPError(status_code=422, model_name='m')) is False
    assert deve_fazer_fallback_excecao(psycopg.OperationalError('erro sql')) is False
    assert deve_fazer_fallback_excecao(ValueError('regra de negocio')) is False


def test_resposta_invalida_para_fallback_valida_output_type():
    assert resposta_invalida_para_fallback(ModelResponse(parts=[])) is True

    invalida_tool = ModelResponse(
        parts=[ToolCallPart(tool_name='final_result', args={'fontes': []})]
    )
    assert resposta_invalida_para_fallback(invalida_tool) is True

    valida_tool = ModelResponse(
        parts=[
            ToolCallPart(
                tool_name='final_result',
                args={
                    'texto': 'Temos queijo coalho de P003.',
                    'fontes': [{'arquivo': 'lotes.csv', 'referencia': 'LOT-2026-0089'}],
                },
            )
        ]
    )
    assert resposta_invalida_para_fallback(valida_tool) is False

    tool_dominio = ModelResponse(
        parts=[ToolCallPart(tool_name='consultar_dados', args={'filtro': 'P003'})]
    )
    assert resposta_invalida_para_fallback(tool_dominio) is False

    resp_json_quebrado = ModelResponse(parts=[TextPart(content='{invalido')])
    assert resposta_invalida_para_fallback(resp_json_quebrado) is True


def test_model_fallback_executa_em_ordem_em_429_e_schema_invalido():
    chamadas: list[str] = []

    def fn_primario(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        raise ModelHTTPError(status_code=429, model_name=MODELO_PRIMARIO, body='rate limit')

    def fn_qwen_schema_invalido(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        return ModelResponse(
            parts=[ToolCallPart(tool_name='final_result', args={'fontes': 'invalido'})]
        )

    def fn_mistral_sucesso(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('mistral')
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Respondido pelo Mistral como fallback 2.',
                        'fontes': [{'arquivo': 'lotes.csv', 'referencia': 'LOT-2026-0089'}],
                        'flags': [],
                        'rascunhos': [],
                    },
                )
            ]
        )

    m1 = FunctionModel(fn_primario, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen_schema_invalido, model_name=MODELO_FALLBACK_1)
    m3 = FunctionModel(fn_mistral_sucesso, model_name=MODELO_FALLBACK_2)

    agente_teste = Agent(
        output_type=RespostaAgente,
        retries=0,
        capabilities=[construir_capability_fallback([m1, m2, m3])],
    )

    resultado = agente_teste.run_sync('Qual o lote do queijo?')
    assert chamadas == ['gemini', 'qwen', 'mistral']
    assert resultado.output.texto == 'Respondido pelo Mistral como fallback 2.'

    modelo_ef, provedor_ef, tentativas = extrair_metadados_resposta(resultado.all_messages())
    assert modelo_ef == MODELO_FALLBACK_2
    assert provedor_ef == 'function'
    assert len(tentativas) == 2
    assert tentativas[0].modelo == MODELO_PRIMARIO
    assert tentativas[0].outcome == 'error'
    assert tentativas[1].modelo == MODELO_FALLBACK_1
    assert tentativas[1].outcome == 'rejected'


def test_model_fallback_nao_aciona_em_erro_autenticacao_ou_negocio():
    chamadas: list[str] = []

    def fn_primario_401(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        raise ModelHTTPError(status_code=401, model_name=MODELO_PRIMARIO, body='invalid api key')

    def fn_qwen(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        return ModelResponse(parts=[ToolCallPart(tool_name='final_result', args={'texto': 'ok'})])

    m1 = FunctionModel(fn_primario_401, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen, model_name=MODELO_FALLBACK_1)

    agente_teste = Agent(
        output_type=RespostaAgente,
        retries=0,
        capabilities=[construir_capability_fallback([m1, m2])],
    )

    with pytest.raises(ModelHTTPError) as exc_info:
        agente_teste.run_sync('Teste erro 401')
    assert exc_info.value.status_code == 401
    assert chamadas == ['gemini']


def test_extrair_tentativas_quando_todos_modelos_falham():
    def fn_falha_503(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name='m', body='unavailable')

    m1 = FunctionModel(fn_falha_503, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_falha_503, model_name=MODELO_FALLBACK_1)

    agente_teste = Agent(
        output_type=RespostaAgente,
        retries=0,
        capabilities=[construir_capability_fallback([m1, m2])],
    )

    with pytest.raises(FallbackExceptionGroup) as exc_info:
        agente_teste.run_sync('Teste todos falham')

    tentativas = extrair_tentativas_de_excecao(exc_info.value)
    assert [t.modelo for t in tentativas] == [MODELO_PRIMARIO, MODELO_FALLBACK_1]
    assert all(t.outcome == 'error' for t in tentativas)


def test_modelo_fallback_segue_ferramentas_e_output_type():
    tool_executada = False

    def consultar_dados(dominio: str) -> str:
        nonlocal tool_executada
        tool_executada = True
        return f'dados_de_{dominio}'

    def fn_primario_timeout(messages, info: AgentInfo) -> ModelResponse:
        raise TimeoutError('Gemini timeout')

    passo_qwen = 0

    def fn_qwen_usa_tool_e_responde(messages, info: AgentInfo) -> ModelResponse:
        nonlocal passo_qwen
        passo_qwen += 1
        if passo_qwen == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='consultar_dados', args={'dominio': 'lotes'})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Lote consultado via fallback [lotes.csv · LOT-2026-0089].',
                        'fontes': [{'arquivo': 'lotes.csv', 'referencia': 'LOT-2026-0089'}],
                        'flags': [],
                        'rascunhos': [],
                    },
                )
            ]
        )

    m1 = FunctionModel(fn_primario_timeout, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen_usa_tool_e_responde, model_name=MODELO_FALLBACK_1)

    agente_teste = Agent(
        output_type=RespostaAgente,
        tools=[consultar_dados],
        retries=0,
        capabilities=[construir_capability_fallback([m1, m2])],
    )

    resultado = agente_teste.run_sync('Consulte os lotes')
    assert tool_executada is True
    assert resultado.output.fontes[0].formatar() == '[lotes.csv · LOT-2026-0089]'

