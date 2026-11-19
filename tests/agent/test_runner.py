"""Testes unitários do Passo 6: `processar_turno` (etapas 1 a 5 e retornos antecipados)."""

import time
from unittest.mock import MagicMock

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sabor.agent.deps import AgentDeps
from sabor.agent.fallback import MODELO_FALLBACK_1, MODELO_PRIMARIO
from sabor.agent.runner import (
    FOLGA_ENCERRAMENTO_S,
    MENSAGEM_ENTRADA_VAZIA,
    MENSAGEM_FALHA_SEGURA,
    TIMEOUT_TURNO_S,
    criar_agente,
    processar_turno,
)
from sabor.agent.schemas import RespostaTurno


class _MascaradorFake:
    """Simula o mascarador da Issue #33 para testar a restauração na Etapa 4."""

    def mascarar(self, texto: str) -> str:
        return texto.replace('111.222.333-44', '[CPF_MASCARADO]')

    def restaurar(self, texto: str) -> str:
        return texto.replace('[CPF_MASCARADO]', '111.222.333-44')


def test_timeout_padrao_turno_e_20_segundos():
    assert TIMEOUT_TURNO_S == 20.0


def test_processar_turno_fluxo_completo_etapas_1_a_5():
    etapas_executadas: list[str] = []

    def fn_memoria(sessao_id: str):
        etapas_executadas.append(f'1_memoria:{sessao_id}')
        return []

    def fn_guardrail_entrada(msg: str) -> str | None:
        etapas_executadas.append('2_guardrail_entrada')
        return None

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        etapas_executadas.append('3_pydantic_ai')
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Produtor P001 (CPF [CPF_MASCARADO]) vendeu R$ 1.090,00.',
                        'fontes': [{'arquivo': 'financeiro.csv', 'referencia': 'FIN-2026-001'}],
                        'flags': [],
                        'rascunhos': [
                            {
                                'destinatario': 'P001 - [CPF_MASCARADO]',
                                'canal': 'whatsapp',
                                'conteudo': 'Resumo pronto.',
                            }
                        ],
                    },
                )
            ]
        )

    def fn_auditoria(turno: RespostaTurno) -> None:
        etapas_executadas.append(f'5a_auditoria:{turno.modelo_efetivo}')

    def fn_salvar_memoria(sessao_id: str, msg: str, turno: RespostaTurno) -> None:
        etapas_executadas.append(f'5b_salvar_memoria:{sessao_id}')

    agente = criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)])
    deps = AgentDeps(
        conn=MagicMock(),
        usuario='Dona Lúcia',
        mascarador=_MascaradorFake(),
    )

    turno = processar_turno(
        sessao_id='sess-100',
        mensagem='Quanto P001 vendeu?',
        usuario='Dona Lúcia',
        agente=agente,
        deps=deps,
        carregar_memoria=fn_memoria,
        guardrail_entrada=fn_guardrail_entrada,
        registrar_auditoria=fn_auditoria,
        salvar_memoria=fn_salvar_memoria,
    )

    assert etapas_executadas == [
        '1_memoria:sess-100',
        '2_guardrail_entrada',
        '3_pydantic_ai',
        f'5a_auditoria:{MODELO_PRIMARIO}',
        '5b_salvar_memoria:sess-100',
    ]
    assert turno.bloqueado_guardrail is False
    assert turno.falha_segura is False
    assert turno.modelo_efetivo == MODELO_PRIMARIO
    assert turno.provedor_efetivo == 'function'
    # Etapa 4c (#33): restaura PII no texto para o operador, mas NUNCA nos rascunhos para terceiros!
    assert '111.222.333-44' in turno.texto
    assert '111.222.333-44' not in turno.rascunhos[0].destinatario
    assert '[CPF_MASCARADO]' in turno.rascunhos[0].destinatario
    assert len(turno.fontes) == 1


def test_processar_turno_retorno_antecipado_guardrail_entrada():
    chamou_llm = False

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        nonlocal chamou_llm
        chamou_llm = True
        return ModelResponse(parts=[])

    agente = criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)])
    deps = AgentDeps(conn=MagicMock())

    # Caso 1: Mensagem em branco
    turno_vazio = processar_turno('sess-1', '   ', 'Dona Lúcia', agente=agente, deps=deps)
    assert turno_vazio.bloqueado_guardrail is True
    assert turno_vazio.texto == MENSAGEM_ENTRADA_VAZIA
    assert chamou_llm is False

    # Caso 2: Guardrail customizado reprova escopo
    turno_fora_escopo = processar_turno(
        'sess-1',
        'Qual a previsão do tempo em Tóquio?',
        'Dona Lúcia',
        agente=agente,
        deps=deps,
        guardrail_entrada=lambda m: 'Pergunta fora do escopo da cooperativa.',
    )
    assert turno_fora_escopo.bloqueado_guardrail is True
    assert turno_fora_escopo.texto == 'Pergunta fora do escopo da cooperativa.'
    assert chamou_llm is False


def test_processar_turno_falha_segura_quando_todos_provedores_falham():
    def fn_erro_503(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name='m', body='down')

    m1 = FunctionModel(fn_erro_503, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_erro_503, model_name=MODELO_FALLBACK_1)
    agente = criar_agente([m1, m2])
    deps = AgentDeps(conn=MagicMock())

    auditoria_recebida: list[RespostaTurno] = []
    memoria_salva: list[tuple[str, str, RespostaTurno]] = []
    turno = processar_turno(
        'sess-err',
        'Qual o estoque de mel?',
        'Seu Antônio',
        agente=agente,
        deps=deps,
        registrar_auditoria=auditoria_recebida.append,
        salvar_memoria=lambda sid, msg, t: memoria_salva.append((sid, msg, t)),
    )

    assert turno.falha_segura is True
    assert turno.bloqueado_guardrail is False
    assert turno.texto == MENSAGEM_FALHA_SEGURA
    assert [t.modelo for t in turno.tentativas_fallback] == [MODELO_PRIMARIO, MODELO_FALLBACK_1]
    assert len(auditoria_recebida) == 1
    assert memoria_salva == [('sess-err', 'Qual o estoque de mel?', turno)]


def test_processar_turno_falha_segura_quando_excede_timeout_global():
    def fn_lenta(messages, info: AgentInfo) -> ModelResponse:
        time.sleep(0.25)
        return ModelResponse(
            parts=[ToolCallPart(tool_name='final_result', args={'texto': 'Tarde demais'})]
        )

    agente = criar_agente([FunctionModel(fn_lenta, model_name=MODELO_PRIMARIO)])
    deps = AgentDeps(conn=MagicMock())

    turno = processar_turno(
        'sess-timeout',
        'Pergunta demorada',
        'Dona Lúcia',
        agente=agente,
        deps=deps,
        timeout_turno_s=0.05,
    )

    assert turno.falha_segura is True
    assert turno.texto == MENSAGEM_FALHA_SEGURA


# =============================================================================
# #49: prazo do turno sem esperar a thread e timeout por modelo respeitado
# =============================================================================


def test_falha_segura_no_prazo_mesmo_com_modelo_que_bloqueia_a_thread():
    """Modelo síncrono que trava 3 s: a falha segura volta em ~prazo + folga, não em 3 s."""

    def fn_travada(messages, info: AgentInfo) -> ModelResponse:
        time.sleep(3.0)
        return ModelResponse(
            parts=[ToolCallPart(tool_name='final_result', args={'texto': 'Tarde demais'})]
        )

    agente = criar_agente([FunctionModel(fn_travada, model_name=MODELO_PRIMARIO)])
    inicio = time.perf_counter()
    turno = processar_turno(
        'sess-travada',
        'Pergunta',
        agente=agente,
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _m: None,
        timeout_turno_s=0.2,
    )
    decorrido = time.perf_counter() - inicio

    assert turno.falha_segura is True
    assert turno.texto == MENSAGEM_FALHA_SEGURA
    assert decorrido < 0.2 + FOLGA_ENCERRAMENTO_S + 0.5
    assert turno.latencia_ms < 2000


def test_prazo_do_turno_cancela_a_chamada_ao_modelo():
    """Com modelo assíncrono (como os reais, via httpx), o prazo cancela a chamada em curso."""
    import asyncio

    estado = {'cancelada': False, 'terminou': False}

    async def fn_lenta(messages, info: AgentInfo) -> ModelResponse:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            estado['cancelada'] = True
            raise
        estado['terminou'] = True
        return ModelResponse(
            parts=[ToolCallPart(tool_name='final_result', args={'texto': 'Tarde demais'})]
        )

    agente = criar_agente([FunctionModel(fn_lenta, model_name=MODELO_PRIMARIO)])
    inicio = time.perf_counter()
    turno = processar_turno(
        'sess-cancelada',
        'Pergunta',
        agente=agente,
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _m: None,
        timeout_turno_s=0.2,
    )

    assert turno.falha_segura is True
    assert time.perf_counter() - inicio < 1.0
    assert estado == {'cancelada': True, 'terminou': False}


def test_turno_respeita_timeout_por_modelo_do_agente():
    """O turno não sobrescreve mais o timeout de cada chamada com um valor fixo."""
    from pydantic_ai.settings import ModelSettings

    recebidos: list[float | None] = []

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        recebidos.append((info.model_settings or {}).get('timeout'))
        return ModelResponse(parts=[ToolCallPart(tool_name='final_result', args={'texto': 'ok'})])

    modelo = FunctionModel(
        fn_modelo, model_name=MODELO_PRIMARIO, settings=ModelSettings(timeout=33.0)
    )
    turno = processar_turno(
        'sess-timeout-modelo',
        'Pergunta',
        agente=criar_agente([modelo]),
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _m: None,
    )

    assert turno.falha_segura is False
    assert recebidos == [33.0]


class _ConexaoFalsa:
    """Imita o que importa do psycopg: `lock` seguro durante cada consulta."""

    def __init__(self):
        import threading

        self.lock = threading.Lock()
        self.eventos: list[str] = []

    def cancel_safe(self, timeout: float = 30.0) -> None:
        self.eventos.append('cancel')

    def close(self) -> None:
        self.eventos.append('close')


def test_turno_abandonado_cancela_consulta_e_so_fecha_conexao_ao_fim_da_thread(monkeypatch):
    """Fechar a conexão psycopg com a tool ainda usando-a em outra thread não é seguro."""
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolReturnPart

    import sabor.agent.runner as runner

    conn = _ConexaoFalsa()
    monkeypatch.setattr(runner, 'conectar_readonly', lambda: conn)

    def consulta_lenta(ctx: RunContext[AgentDeps]) -> str:
        # Tool síncrona no meio de uma consulta: o cancelamento do asyncio não a interrompe.
        with conn.lock:
            time.sleep(1.8)
            conn.eventos.append('tool_terminou')
        return 'ok'

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart(tool_name='consulta_lenta', args={})])
        return ModelResponse(parts=[ToolCallPart(tool_name='final_result', args={'texto': 'ok'})])

    agente = criar_agente(
        [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[consulta_lenta]
    )
    inicio = time.perf_counter()
    turno = processar_turno(
        'sess-abandonada',
        'Pergunta',
        agente=agente,
        guardrail_entrada=lambda _m: None,
        timeout_turno_s=0.1,
    )

    assert turno.falha_segura is True
    assert time.perf_counter() - inicio < 1.6  # não esperou a tool (1,8 s)
    assert conn.eventos == ['cancel']  # cancelou a consulta e não fechou com ela em uso

    limite = time.perf_counter() + 5
    while 'close' not in conn.eventos and time.perf_counter() < limite:
        time.sleep(0.05)
    assert conn.eventos == ['cancel', 'tool_terminou', 'close']


def test_turno_conclui_e_fecha_a_conexao_propria(monkeypatch):
    import sabor.agent.runner as runner

    conn = _ConexaoFalsa()
    monkeypatch.setattr(runner, 'conectar_readonly', lambda: conn)
    estado_no_guardrail: list[list[str]] = []
    guardrail_original = runner.processar_guardrail_saida

    def spy_guardrail(resposta, **kwargs):
        # Na Etapa 4, `conn_propria` precisa ainda estar aberta (não fechada antes do guardrail).
        estado_no_guardrail.append(list(kwargs['conn'].eventos))
        return guardrail_original(resposta, **kwargs)

    monkeypatch.setattr(runner, 'processar_guardrail_saida', spy_guardrail)

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(tool_name='final_result', args={'texto': 'ok'})])

    turno = processar_turno(
        'sess-ok',
        'Pergunta',
        agente=criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[]),
        guardrail_entrada=lambda _m: None,
    )

    assert turno.falha_segura is False
    assert turno.texto == 'ok'
    assert estado_no_guardrail == [[]]
    assert conn.eventos == ['close']


def test_turno_com_postgres_real_executa_guardrail_saida_antes_de_fechar_conexao(monkeypatch):
    """Regressão: com `deps=None` (como na UI), a conexão própria segue aberta até a Etapa 4
    para o guardrail de saída consultar o banco, e é fechada logo depois."""
    import psycopg
    import pytest

    import sabor.agent.runner as runner
    from sabor.db import conectar_readonly

    try:
        conectar_readonly(tentativas=1).close()
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')

    conexoes: list[psycopg.Connection] = []

    def _abrir():
        conn = conectar_readonly(tentativas=1)
        conexoes.append(conn)
        return conn

    monkeypatch.setattr(runner, 'conectar_readonly', _abrir)

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'O preço vigente do queijo coalho é R$ 38,00.',
                        'fontes': [
                            {'arquivo': 'tabela_precos_outubro_2026.csv', 'referencia': 'Queijo'}
                        ],
                    },
                )
            ]
        )

    turno = processar_turno(
        'sess-pg-guardrail',
        'Qual o preço do queijo coalho?',
        agente=criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[]),
        guardrail_entrada=lambda _m: None,
    )

    assert turno.falha_segura is False
    assert turno.texto == 'O preço vigente do queijo coalho é R$ 38,00.'
    assert len(turno.fontes) == 1
    assert len(conexoes) == 1 and conexoes[0].closed


def test_timeouts_configuraveis_pelo_ambiente(monkeypatch):
    import pytest

    from sabor.agent.fallback import (
        TIMEOUT_POR_MODELO_S,
        construir_modelos_padrao,
        obter_timeout_por_modelo,
    )
    from sabor.agent.runner import obter_timeout_turno

    monkeypatch.delenv('MODELO_TIMEOUT_S', raising=False)
    monkeypatch.delenv('TURNO_TIMEOUT_S', raising=False)
    assert obter_timeout_por_modelo() == TIMEOUT_POR_MODELO_S == 12.0
    assert obter_timeout_turno() == TIMEOUT_TURNO_S

    monkeypatch.setenv('MODELO_TIMEOUT_S', '15')
    monkeypatch.setenv('TURNO_TIMEOUT_S', '30,5')
    assert obter_timeout_por_modelo() == 15.0
    assert obter_timeout_turno() == 30.5
    modelos = construir_modelos_padrao(gemini_api_key='x', openrouter_api_key='y')
    assert {m.settings.get('timeout') for m in modelos} == {15.0}
    assert {m.settings.get('max_retries') for m in modelos} == {0}  # retentativa é do fallback
    assert {m.settings.get('request_timeout') for m in modelos} == {15.0}  # prazo total

    monkeypatch.setenv('TURNO_TIMEOUT_S', 'vinte')
    with pytest.raises(ValueError, match='TURNO_TIMEOUT_S'):
        obter_timeout_turno()
    monkeypatch.setenv('MODELO_TIMEOUT_S', '0')
    with pytest.raises(ValueError, match='MODELO_TIMEOUT_S'):
        obter_timeout_por_modelo()


def test_turno_abandonado_com_postgres_real_cancela_e_fecha(monkeypatch):
    """Integração: consulta longa numa tool, turno estoura o prazo, consulta é cancelada no
    servidor e a conexão do turno fecha sem esperar os 5 s do `pg_sleep`."""
    import psycopg
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolReturnPart

    import sabor.agent.runner as runner
    from sabor.db import conectar_readonly

    import pytest

    try:
        conectar_readonly(tentativas=1).close()
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')

    conexoes: list[psycopg.Connection] = []

    def _abrir():
        conn = conectar_readonly(tentativas=1)
        conexoes.append(conn)
        return conn

    monkeypatch.setattr(runner, 'conectar_readonly', _abrir)
    erros: list[BaseException] = []

    def consulta_longa(ctx: RunContext[AgentDeps]) -> str:
        try:
            ctx.deps.conn.execute('SELECT pg_sleep(5)')
        except BaseException as exc:  # esperado: cancelada pelo turno
            erros.append(exc)
            raise
        return 'ok'

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart(tool_name='consulta_longa', args={})])
        return ModelResponse(parts=[ToolCallPart(tool_name='final_result', args={'texto': 'x'})])

    agente = criar_agente(
        [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[consulta_longa]
    )
    inicio = time.perf_counter()
    turno = processar_turno(
        'sess-pg',
        'Pergunta',
        agente=agente,
        guardrail_entrada=lambda _m: None,
        timeout_turno_s=0.3,
    )

    assert turno.falha_segura is True
    assert time.perf_counter() - inicio < 0.3 + FOLGA_ENCERRAMENTO_S + 0.5

    limite = time.perf_counter() + 4
    while not conexoes[0].closed and time.perf_counter() < limite:
        time.sleep(0.05)
    assert conexoes[0].closed
    assert time.perf_counter() - inicio < 4  # não esperou o pg_sleep(5)
    assert erros and isinstance(erros[0], psycopg.errors.QueryCanceled)


def test_prazo_total_por_modelo_aciona_o_proximo_da_cadeia():
    """`request_timeout` cobre a requisição inteira (inclusive keep-alives do OpenRouter) e,
    ao estourar, o fallback passa para o próximo modelo dentro do mesmo turno."""
    import asyncio

    from pydantic_ai.settings import ModelSettings

    from sabor.agent.fallback import configuracao_padrao_modelo

    async def fn_pendurada(messages, info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5.0)
        raise AssertionError('não deveria terminar')

    def fn_rapida(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(tool_name='final_result', args={'texto': 'pelo fallback'})]
        )

    prazo: ModelSettings = configuracao_padrao_modelo(0.2)
    agente = criar_agente(
        [
            FunctionModel(fn_pendurada, model_name=MODELO_PRIMARIO, settings=prazo),
            FunctionModel(fn_rapida, model_name=MODELO_FALLBACK_1, settings=prazo),
        ]
    )
    inicio = time.perf_counter()
    turno = processar_turno(
        'sess-prazo-modelo',
        'Pergunta',
        agente=agente,
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _m: None,
    )

    assert turno.falha_segura is False
    assert turno.texto == 'pelo fallback'
    assert turno.modelo_efetivo == MODELO_FALLBACK_1
    assert [t.modelo for t in turno.tentativas_fallback] == [MODELO_PRIMARIO]
    assert time.perf_counter() - inicio < 2.0
