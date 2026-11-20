"""Testes unitários do Passo 6: `processar_turno` (etapas 1 a 5 e retornos antecipados)."""

import time
from unittest.mock import MagicMock

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
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
from sabor.agent.schemas import RespostaTurno, RetornoAntecipado


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

    def fn_tool_fin(ctx: RunContext[AgentDeps]) -> dict:
        return {'arquivo': 'financeiro.csv', 'referencia': 'FIN-2026-001'}

    def fn_memoria(sessao_id: str):
        etapas_executadas.append(f'1_memoria:{sessao_id}')
        return []

    def fn_guardrail_entrada(msg: str) -> str | None:
        etapas_executadas.append('2_guardrail_entrada')
        return None

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        etapas_executadas.append('3_pydantic_ai')
        ja_chamou_fin = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if not ja_chamou_fin:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='fn_tool_fin', args={}, tool_call_id='call_fin')]
            )
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

    agente = criar_agente(
        [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)],
        tools=[fn_tool_fin],
    )
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
        '3_pydantic_ai',
        '5b_salvar_memoria:sess-100',
        f'5a_auditoria:{MODELO_PRIMARIO}',
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


def test_retorno_antecipado_grava_memoria_antes_da_auditoria():
    eventos = []

    def salvar(sid, mensagem, turno):
        eventos.append(('memoria', sid, mensagem))
        return 456

    def auditar(turno, *, message_id):
        eventos.append(('auditoria', message_id, turno.trace_id, turno.cache_hit))

    turno = processar_turno(
        'sess-audit',
        'resposta do cache',
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _: RetornoAntecipado(
            motivo='CACHE_HIT', mensagem='resposta reaproveitada'
        ),
        salvar_memoria=salvar,
        registrar_auditoria=auditar,
    )

    assert eventos[0] == ('memoria', 'sess-audit', 'resposta do cache')
    assert eventos[1][0:2] == ('auditoria', 456)
    assert eventos[1][2] == turno.trace_id
    assert eventos[1][3] is True


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
    assert turno.modelo_efetivo is None
    assert turno.provedor_efetivo is None
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

    def fn_tool_preco(ctx: RunContext[AgentDeps]) -> dict:
        return {'arquivo': 'tabela_precos_outubro_2026.csv', 'referencia': 'Queijo'}
    
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
        ja_chamou_preco = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if not ja_chamou_preco:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='fn_tool_preco', args={}, tool_call_id='call_preco')]
            )
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
        agente=criar_agente(
            [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)],
            tools=[fn_tool_preco],
        ),
        guardrail_entrada=lambda _m: None,
    )

    assert turno.falha_segura is False
    assert turno.texto == 'O preço vigente do queijo coalho é R$ 38,00.'
    assert len(turno.fontes) == 1
    assert len(conexoes) == 1 and conexoes[0].closed


def test_timeouts_configuraveis_pelo_ambiente(monkeypatch):
    import pytest

    from sabor.agent import fallback as modulo_fallback
    from sabor.agent.fallback import (
        TIMEOUT_POR_MODELO_S,
        construir_modelos_padrao,
        obter_timeout_por_modelo,
    )
    from sabor.agent.runner import obter_timeout_turno

    monkeypatch.setattr(modulo_fallback, 'load_dotenv', lambda: None)
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


def test_processar_turno_anexa_relatorios_das_tools_mascarados():
    """#15: o relatório gerado pela tool chega ao turno sem passar pelo LLM e sem PII."""
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolReturnPart

    from sabor.agent.schemas import Relatorio

    schemas_saida: list[dict] = []

    def gerar_relatorio(ctx: RunContext[AgentDeps], tipo: str) -> dict:
        ctx.deps.relatorios.append(
            Relatorio(
                tipo=tipo,
                titulo='Contas a receber',
                nome_arquivo='contas_a_receber_20261030.md',
                markdown='# Contas a receber\n\nCPF 111.222.333-44 [financeiro.csv · FIN-2026-013]',
            )
        )
        return {'gerado': True, 'destaques': ['Vencido: R$ 8.690,00']}

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        schemas_saida.append(info.output_tools[0].parameters_json_schema)
        ja_chamou = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if not ja_chamou:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='gerar_relatorio', args={'tipo': 'contas_a_receber'})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={'texto': 'Relatório pronto para download.', 'fontes': []},
                )
            ]
        )

    agente = criar_agente(
        [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[gerar_relatorio]
    )
    deps = AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake())
    turno = processar_turno(
        'sess-rel',
        'gera o relatório de contas a receber',
        agente=agente,
        deps=deps,
        guardrail_entrada=lambda _msg: None,
    )

    assert turno.falha_segura is False
    [relatorio] = turno.relatorios
    assert relatorio.nome_arquivo == 'contas_a_receber_20261030.md'
    assert '[CPF_MASCARADO]' in relatorio.markdown  # passou pelo mascarador e não foi restaurado
    assert '111.222.333-44' not in relatorio.markdown
    assert deps.relatorios == []  # não vaza para o próximo turno
    assert all('relatorios' not in s.get('properties', {}) for s in schemas_saida)


def test_relatorio_informado_pelo_modelo_e_descartado():
    """Revisão do PR #48: anexos vêm só das tools. Um `relatorios` no `final_result` do modelo
    (fora do schema, mas aceito pelo Pydantic) não chega ao turno."""
    from sabor.agent.schemas import RespostaAgente

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Segue o relatório.',
                        'relatorios': [
                            {
                                'tipo': 'contas_a_receber',
                                'titulo': 'Forjado',
                                'nome_arquivo': 'forjado.md',
                                'markdown': 'CPF 111.222.333-44',
                            }
                        ],
                    },
                )
            ]
        )

    # Sem a sobrescrita no runner, o Pydantic aceitaria o campo vindo do modelo.
    forjado = {'tipo': 't', 'titulo': 't', 'nome_arquivo': 'f.md', 'markdown': 'm'}
    assert RespostaAgente.model_validate({'texto': 'x', 'relatorios': [forjado]}).relatorios

    deps = AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake())
    turno = processar_turno(
        'sess-forjado',
        'gera o relatório',
        agente=criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[]),
        deps=deps,
        guardrail_entrada=lambda _msg: None,
    )

    assert turno.falha_segura is False
    assert turno.relatorios == []


def test_relatorio_continua_anexado_quando_o_guardrail_de_saida_reescreve_o_texto():
    """O guardrail de saída avalia o texto do modelo; o relatório (montado sem LLM) segue anexado
    e mascarado mesmo quando a regra 4B troca o texto por falta de fonte."""
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolReturnPart

    from sabor.agent.schemas import Relatorio

    def gerar_relatorio(ctx: RunContext[AgentDeps], tipo: str) -> dict:
        ctx.deps.relatorios.append(
            Relatorio(
                tipo=tipo,
                titulo='Contas a receber',
                nome_arquivo='contas_a_receber_20261030.md',
                markdown='# Contas\n\nCPF 111.222.333-44',
            )
        )
        return {'gerado': True}

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(
                parts=[ToolCallPart(tool_name='gerar_relatorio', args={'tipo': 'contas_a_receber'})]
            )
        # "vencidos" + valor sem fontes: dispara a regra 4B do guardrail de saída.
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={'texto': 'Relatório gerado: R$ 8.690,00 vencidos.', 'fontes': []},
                )
            ]
        )

    turno = processar_turno(
        'sess-guardrail-saida',
        'gera o relatório de contas a receber',
        agente=criar_agente(
            [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)], tools=[gerar_relatorio]
        ),
        deps=AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake()),
        guardrail_entrada=lambda _msg: None,
    )

    assert turno.texto != 'Relatório gerado: R$ 8.690,00 vencidos.'  # 4B reescreveu o texto
    [relatorio] = turno.relatorios
    assert '[CPF_MASCARADO]' in relatorio.markdown


def test_processar_turno_descarta_relatorio_nao_solicitado_e_limpa_citacoes_inline():
    """Pergunta simples descarta relatórios não pedidos e limpa citações inline do texto."""
    from sabor.agent.schemas import Relatorio

    def consultar_financeiro_fake(ctx: RunContext[AgentDeps]) -> dict:
        ctx.deps.relatorios.append(
            Relatorio(
                tipo='contas_a_receber',
                titulo='Contas a receber',
                nome_arquivo='contas_a_receber_20261030.md',
                markdown='# Contas a receber',
            )
        )
        return {
            'arquivo': 'financeiro.csv',
            'referencia': 'FIN-2026-013',
            'valor_total': 6020.0,
        }

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        ja_chamou = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if not ja_chamou:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='consultar_financeiro_fake', args={})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': (
                            'A SuperBom deve R$ 6.020,00 para Cícero Romão '
                            '[financeiro.csv · FIN-2026-013].\n\n'
                            'Fontes: [financeiro.csv · FIN-2026-013]'
                        ),
                        'fontes': [{'arquivo': 'financeiro.csv', 'referencia': 'FIN-2026-013'}],
                    },
                )
            ]
        )

    agente = criar_agente(
        [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)],
        tools=[consultar_financeiro_fake],
    )
    turno = processar_turno(
        'sess-sem-relatorio',
        'Quem a SuperBom está devendo e há quanto tempo?',
        agente=agente,
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _msg: None,
    )

    assert turno.falha_segura is False
    assert turno.relatorios == []
    assert turno.texto == 'A SuperBom deve R$ 6.020,00 para Cícero Romão.'
    assert 'Fontes:' not in turno.texto
    assert [(f.arquivo, f.referencia) for f in turno.fontes] == [
        ('financeiro.csv', 'FIN-2026-013')
    ]


def test_processar_turno_barra_fonte_inventada_pela_regra_4b():
    """Regra 4b (#34): fonte citada pelo modelo sem respaldo nas tools do turno é bloqueada."""

    def consultar_financeiro_fake(ctx: RunContext[AgentDeps]) -> dict:
        return {
            'arquivo': 'financeiro.csv',
            'referencia': 'FIN-2026-001',
            'valor': 'R$ 1.090,00',
            'produtos': ['mel'],
        }

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        ja_chamou_tool = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if not ja_chamou_tool:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='consultar_financeiro_fake', args={})]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'O preço vigente do mel é R$ 45,00 por kg.',
                        'fontes': [
                            {'arquivo': 'fonte_inventada.csv', 'referencia': 'mel'}
                        ],
                    },
                )
            ]
        )

    agente = criar_agente(
        [FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)],
        tools=[consultar_financeiro_fake],
    )
    turno = processar_turno(
        'sess-4b-fonte-inventada',
        'Qual o preço do mel por kg?',
        agente=agente,
        deps=AgentDeps(conn=MagicMock()),
        guardrail_entrada=lambda _msg: None,
    )

    assert turno.bloqueado_guardrail is True
    assert any('4B_SEM_FONTE_FACTUAL' in r for r in turno.regras_disparadas)
    assert turno.fontes == []
    assert 'não encontrada' in turno.texto.lower()


def test_anexar_relatorios_mantem_relatorio_quando_gerar_relatorio_rodou():
    """Se `gerar_relatorio` rodou no turno, o anexo não é descartado (o texto o anuncia)."""
    from sabor.agent.runner import _anexar_relatorios
    from sabor.agent.schemas import RespostaAgente, Relatorio

    def deps_com_relatorio() -> AgentDeps:
        deps = AgentDeps(conn=MagicMock())
        deps.relatorios.append(
            Relatorio(
                tipo='contas_a_receber',
                titulo='Contas a receber',
                nome_arquivo='contas_a_receber_20261030.md',
                markdown='# Contas a receber',
            )
        )
        return deps

    resposta = RespostaAgente(texto='O arquivo está disponível para baixar.')

    mantida = _anexar_relatorios(resposta, deps_com_relatorio(), gerar_relatorio_chamado=True)
    assert [r.nome_arquivo for r in mantida.relatorios] == ['contas_a_receber_20261030.md']

    descartada = _anexar_relatorios(resposta, deps_com_relatorio(), gerar_relatorio_chamado=False)
    assert descartada.relatorios == []


def _modelo_chama_tools_e_responde(chamadas: list[ToolCallPart], final: dict):
    """FunctionModel que faz `chamadas` no primeiro passo e devolve `final` no seguinte."""

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        ja_chamou = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if chamadas and not ja_chamou:
            return ModelResponse(parts=list(chamadas))
        return ModelResponse(parts=[ToolCallPart(tool_name='final_result', args=final)])

    return FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)


RASCUNHO_INVENTADO = {
    'destinatario': 'Maria das Graças (P001)',
    'canal': 'whatsapp',
    'conteudo': 'Olá! Preciso de maracujá/goiaba conforme alocação.',
}


def test_rascunhos_das_tools_substituem_os_redigitados_pelo_modelo():
    """F3 do Bloco 4: o rascunho anexado é o da tool, mascarado e sem duplicata."""
    from sabor.agent.schemas import Rascunho
    from sabor.tools.base import registrar_rascunhos

    conteudo_tool = 'Olá, Maria (P001)!\n\nPedidos em aberto:\n- PED-2026-002: goiaba — 50 kg\nCPF 111.222.333-44'

    def resumir_produtor(ctx: RunContext[AgentDeps], produtor: str) -> dict:
        registrar_rascunhos(
            ctx.deps,
            [Rascunho(destinatario='Maria das Graças (P001)', canal='padrao', conteudo=conteudo_tool)],
        )
        return {'produtor_id': 'P001'}

    chamada = ToolCallPart(tool_name='resumir_produtor', args={'produtor': 'P001'})
    agente = criar_agente(
        [
            _modelo_chama_tools_e_responde(
                [chamada, ToolCallPart(tool_name='resumir_produtor', args={'produtor': 'P001'})],
                {'texto': 'Resumo pronto.', 'rascunhos': [RASCUNHO_INVENTADO]},
            )
        ],
        tools=[resumir_produtor],
    )
    deps = AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake())
    turno = processar_turno(
        'sess-rasc', 'resuma a Maria', agente=agente, deps=deps, guardrail_entrada=lambda _m: None
    )

    assert turno.falha_segura is False
    [rascunho] = turno.rascunhos  # a tool rodou duas vezes com o mesmo resultado
    assert rascunho.canal == 'padrao'
    assert 'PED-2026-002: goiaba — 50 kg' in rascunho.conteudo
    assert 'maracujá' not in rascunho.conteudo
    assert '[CPF_MASCARADO]' in rascunho.conteudo  # mascarado e não restaurado
    assert deps.rascunhos == []  # não vaza para o próximo turno


def test_tool_de_rascunho_sem_rascunho_descarta_os_do_modelo():
    """Se `rascunhar_notificacao` não montou rascunho (sem elegíveis), o modelo não inventa um."""

    def rascunhar_notificacao(ctx: RunContext[AgentDeps], pedido_id: str) -> dict:
        return {'texto': 'O pedido não possui produtores elegíveis.'}

    agente = criar_agente(
        [
            _modelo_chama_tools_e_responde(
                [ToolCallPart(tool_name='rascunhar_notificacao', args={'pedido_id': 'PED-X'})],
                {'texto': 'Sem elegíveis.', 'rascunhos': [RASCUNHO_INVENTADO]},
            )
        ],
        tools=[rascunhar_notificacao],
    )
    turno = processar_turno(
        'sess-rasc-vazio',
        'rascunhe para os elegíveis do PED-X',
        agente=agente,
        deps=AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake()),
        guardrail_entrada=lambda _m: None,
    )

    assert turno.falha_segura is False
    assert turno.rascunhos == []


def test_rascunho_do_modelo_sem_tool_tem_quebras_de_linha_corrigidas():
    """Sem tool de rascunho, vale o do modelo, mas `\\n` escapado vira quebra de linha."""
    escapado = dict(RASCUNHO_INVENTADO, conteudo='Prezados,\\n\\nConfirmamos o recebimento.')
    agente = criar_agente(
        [_modelo_chama_tools_e_responde([], {'texto': 'Segue o rascunho.', 'rascunhos': [escapado]})],
        tools=[],
    )
    turno = processar_turno(
        'sess-rasc-modelo',
        'rascunhe uma resposta ao comprador',
        agente=agente,
        deps=AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake()),
        guardrail_entrada=lambda _m: None,
    )

    [rascunho] = turno.rascunhos
    assert rascunho.conteudo == 'Prezados,\n\nConfirmamos o recebimento.'


def test_flags_do_resumo_substituem_as_do_modelo_nos_tipos_cobertos():
    """F5/F6 do Bloco 4: conformidade e inadimplência vêm do `resumir_produtor`; o modelo não
    as omite nem marca o produtor credor como inadimplente. Outros tipos do modelo ficam."""
    from sabor.agent.schemas import Flag
    from sabor.tools.base import registrar_flags

    flag_tool = Flag(tipo='inadimplencia', descricao='Rede SuperBom tem R$ 6.020,00 vencidos com P003.')

    def resumir_produtor(ctx: RunContext[AgentDeps], produtor: str) -> dict:
        registrar_flags(ctx.deps, [flag_tool, flag_tool])  # repetida: entra uma vez
        return {'produtor_id': produtor}

    final = {
        'texto': 'Resumo pronto.',
        'flags': [
            {'tipo': 'inadimplencia', 'descricao': 'O produtor P003 possui valores vencidos.'},
            {'tipo': 'ambiguidade', 'descricao': 'Confirmar qual queijo.'},
        ],
    }
    agente = criar_agente(
        [_modelo_chama_tools_e_responde(
            [ToolCallPart(tool_name='resumir_produtor', args={'produtor': 'P003'})], final
        )],
        tools=[resumir_produtor],
    )
    deps = AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake())
    turno = processar_turno(
        'sess-flags', 'resuma o Cícero', agente=agente, deps=deps, guardrail_entrada=lambda _m: None
    )

    assert [(f.tipo, f.descricao) for f in turno.flags] == [
        ('inadimplencia', flag_tool.descricao),
        ('ambiguidade', 'Confirmar qual queijo.'),
    ]
    assert deps.flags == []


def test_flags_do_modelo_ficam_quando_o_resumo_nao_roda():
    final = {'texto': 'ok', 'flags': [{'tipo': 'conformidade', 'descricao': 'P017 suspenso.'}]}
    turno = processar_turno(
        'sess-flags-modelo',
        'o frango do P017 pode ser vendido?',
        agente=criar_agente([_modelo_chama_tools_e_responde([], final)], tools=[]),
        deps=AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake()),
        guardrail_entrada=lambda _m: None,
    )

    assert [f.tipo for f in turno.flags] == ['conformidade']


def test_repara_so_a_extensao_de_fonte_retornada_pela_tool():
    """REL-04 do Bloco 4: o modelo trocou a extensão do laudo, e a 4B bloqueava a resposta."""
    from sabor.agent.runner import _reconciliar_fontes_com_tools
    from sabor.agent.schemas import Fonte, RespostaAgente

    laudo = 'laudos/laudo_sanitario_queijaria_cicero_P003.txt'
    tools = [Fonte(arquivo=laudo, referencia='LAUDO'), Fonte(arquivo='financeiro.csv', referencia='FIN-2026-013')]
    resposta = RespostaAgente(texto='ok', fontes=[
        Fonte(arquivo='laudos/laudo_sanitario_queijaria_cicero_P003.csv', referencia='LAUDO'),
        Fonte(arquivo='laudos/laudo_sanitario_queijaria_cicero_P003.quist.txt', referencia='LAUDO'),
        Fonte(arquivo='financeiro.csv', referencia='FIN-2026-013'),
        Fonte(arquivo='laudos/laudo_sanitario_queijaria_cicero_P003.csv', referencia='VALIDADE'),
        Fonte(arquivo='laudos/laudo_inventado_P003.txt', referencia='LAUDO'),
    ])

    fontes = _reconciliar_fontes_com_tools(resposta, tools).fontes

    assert [(f.arquivo, f.referencia) for f in fontes] == [
        (laudo, 'LAUDO'),  # .csv e .quist.txt viram a mesma fonte, sem duplicar
        ('financeiro.csv', 'FIN-2026-013'),
        # Outra referência ou outro arquivo: fica como veio, e a 4B decide.
        ('laudos/laudo_sanitario_queijaria_cicero_P003.csv', 'VALIDADE'),
        ('laudos/laudo_inventado_P003.txt', 'LAUDO'),
    ]


def test_divide_fonte_com_registros_juntados_so_se_todos_vieram_das_tools():
    """REL-08 do Bloco 4: `produtores.csv · P001, P004, P005` vira uma fonte por produtor."""
    from sabor.agent.runner import _reconciliar_fontes_com_tools
    from sabor.agent.schemas import Fonte, RespostaAgente

    tools = [Fonte(arquivo='produtores.csv', referencia=pid) for pid in ('P001', 'P004', 'P005')]
    tools.append(Fonte(arquivo='pedidos.json', referencia='PED-2026-002'))
    tools += [Fonte(arquivo=f'certificacoes/cert_{p}.txt', referencia='PRODUTOS CERTIFICADOS')
              for p in ('P001', 'P002')]
    resposta = RespostaAgente(texto='ok', fontes=[
        Fonte(arquivo='pedidos.json', referencia='PED-2026-002'),
        Fonte(arquivo='produtores.csv', referencia='P001, P004 e P005'),
        Fonte(arquivo='produtores.csv', referencia='P001, P099'),  # P099 não veio das tools
        Fonte(arquivo='produtores.csv', referencia='P004'),  # repetida depois da divisão
        # REL-07: citações de arquivos diferentes copiadas do destaque para uma fonte só.
        Fonte(arquivo='certificacoes/cert_P001.txt',
              referencia='PRODUTOS CERTIFICADOS, certificacoes/cert_P002.txt · PRODUTOS CERTIFICADOS'),
    ])

    fontes = _reconciliar_fontes_com_tools(resposta, tools).fontes

    assert [(f.arquivo, f.referencia) for f in fontes] == [
        ('pedidos.json', 'PED-2026-002'),
        ('produtores.csv', 'P001'),
        ('produtores.csv', 'P004'),
        ('produtores.csv', 'P005'),
        ('produtores.csv', 'P001, P099'),  # fica como veio: a 4B decide
        ('certificacoes/cert_P001.txt', 'PRODUTOS CERTIFICADOS'),
        ('certificacoes/cert_P002.txt', 'PRODUTOS CERTIFICADOS'),
    ]


def test_descarta_fonte_cuja_referencia_e_o_proprio_arquivo():
    """REL-07 do Bloco 4: `produtores.csv · produtores.csv` não aponta registro nenhum, e
    `produtores.csv · P017` dentro da referência perde o prefixo do arquivo."""
    from sabor.agent.runner import _reconciliar_fontes_com_tools
    from sabor.agent.schemas import Fonte, RespostaAgente

    resposta = RespostaAgente(texto='ok', fontes=[
        Fonte(arquivo='produtores.csv', referencia='produtores.csv'),
        Fonte(arquivo='produtores.csv', referencia='produtores.csv · P017'),  # formato inteiro
        Fonte(arquivo='produtores.csv', referencia='P017'),
    ])
    tools = [Fonte(arquivo='produtores.csv', referencia='P017')]

    fontes = _reconciliar_fontes_com_tools(resposta, tools).fontes

    assert [(f.arquivo, f.referencia) for f in fontes] == [('produtores.csv', 'P017')]


def test_fontes_dos_rascunhos_entram_mesmo_se_o_modelo_nao_citar():
    """REL-08 do Bloco 4: o modelo citava só o pedido; os produtores dos rascunhos sumiam."""
    from sabor.agent.schemas import Fonte, Rascunho, RespostaAgente
    from sabor.tools.base import registrar_rascunhos

    def rascunhar_notificacao(ctx: RunContext[AgentDeps], pedido_id: str) -> RespostaAgente:
        rascunhos = [Rascunho(destinatario=f'Produtor ({p})', canal='whatsapp', conteudo='Olá')
                     for p in ('P001', 'P010')]
        registrar_rascunhos(ctx.deps, rascunhos)
        return RespostaAgente(
            texto='Proposta de alocação.',
            rascunhos=rascunhos,
            fontes=[Fonte(arquivo='pedidos.json', referencia=pedido_id),
                    Fonte(arquivo='produtores.csv', referencia='P001'),
                    Fonte(arquivo='produtores.csv', referencia='P010')],
        )

    final = {
        'texto': 'Rascunhos prontos.',
        'fontes': [{'arquivo': 'pedidos.json', 'referencia': 'PED-2026-002'}],
    }
    agente = criar_agente(
        [_modelo_chama_tools_e_responde(
            [ToolCallPart(tool_name='rascunhar_notificacao', args={'pedido_id': 'PED-2026-002'})],
            final,
        )],
        tools=[rascunhar_notificacao],
    )
    turno = processar_turno(
        'sess-fontes-rasc',
        'rascunhe para os elegíveis do PED-2026-002',
        agente=agente,
        deps=AgentDeps(conn=MagicMock(), mascarador=_MascaradorFake()),
        guardrail_entrada=lambda _m: None,
    )

    assert [(f.arquivo, f.referencia) for f in turno.fontes] == [
        ('pedidos.json', 'PED-2026-002'),
        ('produtores.csv', 'P001'),
        ('produtores.csv', 'P010'),
    ]
