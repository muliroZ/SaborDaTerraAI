"""Testes unitários da cadeia de modelos, `ModelFallback` e metadados de auditoria (#37)."""

import httpx
import psycopg
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    ModelRequestTimeout,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
import pytest

from sabor.agent import fallback as modulo_fallback
from sabor.agent.fallback import (
    COOLDOWN_CIRCUIT_BREAKER_S,
    MODELO_FALLBACK_1,
    MODELO_FALLBACK_2,
    MODELO_PRIMARIO,
    TIMEOUT_POR_MODELO_S,
    FallbackComCircuitBreaker,
    construir_capability_fallback,
    construir_modelos_padrao,
    deve_fazer_fallback_excecao,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
    obter_cooldown_circuit_breaker,
    resposta_invalida_para_fallback,
)
from sabor.agent.schemas import RespostaAgente


def test_construir_modelos_padrao_preserva_ordem_e_provedores(monkeypatch):
    monkeypatch.setattr(modulo_fallback, 'load_dotenv', lambda: None)
    monkeypatch.delenv('MODELO_TIMEOUT_S', raising=False)
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
    assert deve_fazer_fallback_excecao(ModelRequestTimeout('m', 12.0)) is True  # #49
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


def _resposta_final(texto: str) -> ModelResponse:
    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name='final_result',
                args={
                    'texto': texto,
                    'fontes': [{'arquivo': 'lotes.csv', 'referencia': 'LOT-2026-0089'}],
                    'flags': [],
                    'rascunhos': [],
                },
            )
        ]
    )


def test_qwen_desabilita_reasoning_preservando_timeout():
    modelos = construir_modelos_padrao(
        gemini_api_key='fake-gemini',
        openrouter_api_key='fake-openrouter',
        timeout_s=15.0,
    )
    qwen = modelos[1]
    assert qwen.model_name == MODELO_FALLBACK_1
    assert qwen.settings is not None
    assert qwen.settings.get('timeout') == 15.0
    assert qwen.settings.get('request_timeout') == 15.0
    assert qwen.settings.get('max_retries') == 0
    assert qwen.settings.get('extra_body') == {'reasoning': {'enabled': False}}


def test_obter_cooldown_circuit_breaker_env(monkeypatch):
    monkeypatch.setattr(modulo_fallback, 'load_dotenv', lambda: None)
    monkeypatch.delenv('FALLBACK_COOLDOWN_S', raising=False)
    assert obter_cooldown_circuit_breaker() == COOLDOWN_CIRCUIT_BREAKER_S == 180.0

    monkeypatch.setenv('FALLBACK_COOLDOWN_S', '0')
    assert obter_cooldown_circuit_breaker() == 0.0

    monkeypatch.setenv('FALLBACK_COOLDOWN_S', '45,5')
    assert obter_cooldown_circuit_breaker() == 45.5

    for invalido in ('-5', 'abc', 'nan', 'inf'):
        monkeypatch.setenv('FALLBACK_COOLDOWN_S', invalido)
        with pytest.raises(ValueError):
            obter_cooldown_circuit_breaker()


def test_circuit_breaker_pula_modelos_em_quarentena_no_mesmo_turno_e_nos_proximos():
    """Cenário real: Gemini 503 + Qwen timeout -> passo 2 e turno 2 vão direto ao Mistral."""
    agora = [100.0]
    chamadas: list[str] = []

    def consultar_estoque(produto: str) -> str:
        return f'estoque_de_{produto}'

    def fn_gemini(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        raise ModelHTTPError(status_code=503, model_name=MODELO_PRIMARIO, body='high demand')

    def fn_qwen(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        raise ModelRequestTimeout(MODELO_FALLBACK_1, 20.0)

    def fn_mistral(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('mistral')
        ja_chamou_tool = any(
            getattr(p, 'part_kind', None) == 'tool-return'
            for m in messages
            for p in getattr(m, 'parts', [])
        )
        if not ja_chamou_tool and len(chamadas) == 3:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='consultar_estoque', args={'produto': 'mel'})]
            )
        return _resposta_final('Respondido direto pelo Mistral.')

    m1 = FunctionModel(fn_gemini, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen, model_name=MODELO_FALLBACK_1)
    m3 = FunctionModel(fn_mistral, model_name=MODELO_FALLBACK_2)

    cap = construir_capability_fallback([m1, m2, m3], cooldown_s=180.0, relogio=lambda: agora[0])
    assert isinstance(cap, FallbackComCircuitBreaker)
    agente = Agent(
        output_type=RespostaAgente,
        tools=[consultar_estoque],
        retries=0,
        capabilities=[cap],
    )

    # Turno 1 (2 passos: tool + resposta final): Gemini e Qwen só são tentados no passo 1
    r1 = agente.run_sync('Qual o estoque de mel?')
    assert r1.output.texto == 'Respondido direto pelo Mistral.'
    assert chamadas == ['gemini', 'qwen', 'mistral', 'mistral']

    # Turno 2 (10 s depois, ainda em quarentena): vai direto ao Mistral
    agora[0] += 10.0
    r2 = agente.run_sync('Outra pergunta na sequência')
    assert r2.output.texto == 'Respondido direto pelo Mistral.'
    assert chamadas == ['gemini', 'qwen', 'mistral', 'mistral', 'mistral']
    modelo_ef, _, tentativas_t2 = extrair_metadados_resposta(r2.all_messages())
    assert modelo_ef == MODELO_FALLBACK_2
    assert tentativas_t2 == []


def test_circuit_breaker_recupera_primario_apos_cooldown_expirar():
    agora = [0.0]
    gemini_saudavel = [False]
    chamadas: list[str] = []

    def fn_gemini(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        if not gemini_saudavel[0]:
            raise ModelHTTPError(status_code=503, model_name=MODELO_PRIMARIO, body='down')
        return _resposta_final('Gemini recuperado.')

    def fn_mistral(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('mistral')
        return _resposta_final('Mistral de contingência.')

    m1 = FunctionModel(fn_gemini, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_mistral, model_name=MODELO_FALLBACK_2)
    cap = construir_capability_fallback([m1, m2], cooldown_s=180.0, relogio=lambda: agora[0])
    agente = Agent(output_type=RespostaAgente, retries=0, capabilities=[cap])

    assert agente.run_sync('p1').output.texto == 'Mistral de contingência.'
    agora[0] = 60.0
    assert agente.run_sync('p2').output.texto == 'Mistral de contingência.'
    assert chamadas == ['gemini', 'mistral', 'mistral']

    # Cooldown expira e Google normaliza: testa o primário e volta para ele
    gemini_saudavel[0] = True
    agora[0] = 181.0
    assert agente.run_sync('p3').output.texto == 'Gemini recuperado.'
    agora[0] = 190.0
    assert agente.run_sync('p4').output.texto == 'Gemini recuperado.'
    assert chamadas == ['gemini', 'mistral', 'mistral', 'gemini', 'gemini']


def test_circuit_breaker_renova_quarentena_se_primario_continuar_falhando():
    agora = [0.0]
    chamadas: list[str] = []

    def fn_gemini(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        raise ModelHTTPError(status_code=503, model_name=MODELO_PRIMARIO, body='still down')

    def fn_qwen(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        return _resposta_final('Qwen respondeu.')

    m1 = FunctionModel(fn_gemini, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen, model_name=MODELO_FALLBACK_1)
    cap = construir_capability_fallback([m1, m2], cooldown_s=120.0, relogio=lambda: agora[0])
    agente = Agent(output_type=RespostaAgente, retries=0, capabilities=[cap])

    agente.run_sync('p1')
    agora[0] = 121.0  # expirou: sonda Gemini, falha de novo e renova quarentena até t=241
    agente.run_sync('p2')
    agora[0] = 150.0  # dentro da quarentena renovada: pula Gemini
    agente.run_sync('p3')
    assert chamadas == ['gemini', 'qwen', 'gemini', 'qwen', 'qwen']


def test_circuit_breaker_usa_quarentenado_como_ultimo_recurso_se_saudavel_falhar():
    agora = [0.0]
    estado = {'gemini_ok': False, 'qwen_ok': True}
    chamadas: list[str] = []

    def fn_gemini(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        if not estado['gemini_ok']:
            raise ModelHTTPError(status_code=503, model_name=MODELO_PRIMARIO, body='down')
        return _resposta_final('Gemini salvou como último recurso.')

    def fn_qwen(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        if not estado['qwen_ok']:
            raise ModelHTTPError(status_code=503, model_name=MODELO_FALLBACK_1, body='down')
        return _resposta_final('Qwen OK.')

    m1 = FunctionModel(fn_gemini, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen, model_name=MODELO_FALLBACK_1)
    cap = construir_capability_fallback([m1, m2], cooldown_s=180.0, relogio=lambda: agora[0])
    agente = Agent(output_type=RespostaAgente, retries=0, capabilities=[cap])

    assert agente.run_sync('p1').output.texto == 'Qwen OK.'
    # Agora Qwen cai e Gemini voltou, mesmo ainda dentro do cooldown
    estado['gemini_ok'] = True
    estado['qwen_ok'] = False
    agora[0] = 30.0
    assert agente.run_sync('p2').output.texto == 'Gemini salvou como último recurso.'
    # Sucesso fecha o circuito do Gemini; próxima pergunta já vai direto para o Gemini
    agora[0] = 40.0
    assert agente.run_sync('p3').output.texto == 'Gemini salvou como último recurso.'
    assert chamadas == ['gemini', 'qwen', 'qwen', 'gemini', 'gemini']


def test_circuit_breaker_todos_em_quarentena_tenta_cadeia_novamente():
    agora = [0.0]
    chamadas: list[str] = []

    def fn_erro(nome: str):
        def _fn(messages, info: AgentInfo) -> ModelResponse:
            chamadas.append(nome)
            raise ModelHTTPError(status_code=503, model_name=nome, body='down')

        return _fn

    m1 = FunctionModel(fn_erro('gemini'), model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_erro('qwen'), model_name=MODELO_FALLBACK_1)
    cap = construir_capability_fallback([m1, m2], cooldown_s=180.0, relogio=lambda: agora[0])
    agente = Agent(output_type=RespostaAgente, retries=0, capabilities=[cap])

    with pytest.raises(FallbackExceptionGroup):
        agente.run_sync('p1')
    agora[0] = 10.0
    with pytest.raises(FallbackExceptionGroup):
        agente.run_sync('p2')
    assert chamadas == ['gemini', 'qwen', 'gemini', 'qwen']


def test_circuit_breaker_desligado_com_cooldown_zero():
    chamadas: list[str] = []

    def fn_gemini(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        raise TimeoutError('lento')

    def fn_qwen(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        return _resposta_final('Qwen OK.')

    m1 = FunctionModel(fn_gemini, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen, model_name=MODELO_FALLBACK_1)
    cap = construir_capability_fallback([m1, m2], cooldown_s=0.0)
    agente = Agent(output_type=RespostaAgente, retries=0, capabilities=[cap])

    agente.run_sync('p1')
    agente.run_sync('p2')
    assert chamadas == ['gemini', 'qwen', 'gemini', 'qwen']


def test_circuit_breaker_nao_abre_em_schema_rejeitado_pontual():
    chamadas: list[str] = []
    falhar_primeira = [True]

    def fn_gemini(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('gemini')
        if falhar_primeira[0]:
            falhar_primeira[0] = False
            return ModelResponse(
                parts=[ToolCallPart(tool_name='final_result', args={'fontes': 'x'})]
            )
        return _resposta_final('Gemini OK na segunda.')

    def fn_qwen(messages, info: AgentInfo) -> ModelResponse:
        chamadas.append('qwen')
        return _resposta_final('Qwen fallback.')

    m1 = FunctionModel(fn_gemini, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_qwen, model_name=MODELO_FALLBACK_1)
    cap = construir_capability_fallback([m1, m2], cooldown_s=180.0)
    agente = Agent(output_type=RespostaAgente, retries=0, capabilities=[cap])

    assert agente.run_sync('p1').output.texto == 'Qwen fallback.'
    assert agente.run_sync('p2').output.texto == 'Gemini OK na segunda.'
    assert chamadas == ['gemini', 'qwen', 'gemini']


