"""Interface Streamlit (#13) com `AppTest`, banco temporário e agente simulado (sem LLM)."""

from contextlib import contextmanager
from pathlib import Path
import threading

import pytest
from streamlit.testing.v1 import AppTest

import sabor.agent
from sabor.agent import memoria as sess
from sabor.agent.schemas import Flag, Fonte, Rascunho, RespostaTurno, RetornoAntecipado
from sabor.tools.pedidos import modelo_extracao
from sabor.triagem import hash_entrada

UI = str(Path(__file__).resolve().parents[2] / 'src' / 'sabor' / 'ui' / 'app.py')


class AgenteFalso:
    """Substitui `processar_turno`: registra as chamadas e a memória recebida."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, sessao_id, mensagem, usuario, *, carregar_memoria, salvar_memoria, **kw):
        historico = carregar_memoria(sessao_id)
        turno = self._responder(sessao_id, mensagem, usuario, historico, **kw)
        salvar_memoria(sessao_id, mensagem.strip(), turno)  # Etapa 5b, como o orquestrador
        return turno

    def _responder(self, sessao_id, mensagem, usuario, historico, **kwargs):
        self.chamadas.append(
            {
                'sessao_id': sessao_id,
                'mensagem': mensagem,
                'usuario': usuario,
                'historico': historico,
                **kwargs,
            }
        )
        if 'piada' in mensagem:
            return RespostaTurno(
                sessao_id=sessao_id,
                usuario=usuario,
                texto='Só ajudo com a operação da cooperativa.',
                bloqueado_guardrail=True,
                retorno_antecipado=RetornoAntecipado(motivo='FORA_DE_ESCOPO', mensagem='x'),
            )
        return RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=f'Resposta para: {mensagem}',
            fontes=[Fonte(arquivo='lotes.csv', referencia='LOT-2026-0089')],
            flags=[Flag(tipo='conformidade', descricao='P017 sem SIE')],
            rascunhos=[Rascunho(destinatario='P003', canal='whatsapp', conteudo='Olá, Cícero!')],
            modelo_efetivo='modelo-falso',
            latencia_ms=1200,
        )


@pytest.fixture
def agente_falso(monkeypatch, url_banco_temporario):
    monkeypatch.setenv('DATABASE_URL', url_banco_temporario)
    falso = AgenteFalso()
    monkeypatch.setattr(sabor.agent, 'processar_turno', falso)
    monkeypatch.setattr(sabor.agent, 'criar_agente', lambda: object())
    return falso


def _app(usuario: str) -> AppTest:
    at = AppTest.from_file(UI, default_timeout=30)
    at.query_params['usuario'] = usuario
    return at.run()


def _textos(at: AppTest) -> str:
    return '\n'.join(m.value for m in at.markdown)


def test_pagina_inicial_sem_conversa(agente_falso):
    at = _app('Seu Antônio')

    assert not at.exception
    assert at.title[0].value == 'Assistente Sabor da Terra'
    assert not at.chat_input[0].disabled
    seletor = at.selectbox(key='usuario')
    assert seletor.value == 'Seu Antônio'
    assert len(seletor.options) == 20
    assert 'Data de referência' in _textos(at)
    assert 'Banco conectado' in _textos(at)
    assert at.button(key='sugestao_0') is not None


def test_turno_exibe_resposta_fontes_flags_rascunhos_e_persiste(agente_falso, conn_temporaria):
    at = _app('Dona Lúcia')
    at.chat_input[0].set_value('tem queijo coalho?').run()

    assert not at.exception
    assert agente_falso.chamadas[0]['usuario'] == 'Dona Lúcia'
    assert agente_falso.chamadas[0]['historico'] == []
    assert agente_falso.chamadas[0]['requisicoes_na_sessao'] == 0

    textos = _textos(at)
    assert 'tem queijo coalho?' in textos
    assert 'Resposta para: tem queijo coalho?' in textos
    assert ':red-badge[:material/block: Conformidade] P017 sem SIE' in textos
    assert at.expander[0].label == 'Fontes (1)'
    assert at.code[0].value == 'Olá, Cícero!'
    assert any('modelo-falso · 1.2 s' in c.value for c in at.caption)
    assert not at.chat_input[0].disabled  # destravado ao fim do turno

    # O histórico vem do banco: uma nova AppTest (= recarregar a página) mostra a conversa.
    recarregada = _app('Dona Lúcia')
    assert 'Resposta para: tem queijo coalho?' in _textos(recarregada)

    sessao = sess.sessao_ativa(conn_temporaria, 'Dona Lúcia')
    assert sessao.total_mensagens == 2

    # Segundo turno recebe o primeiro como memória (contexto "e para o hotel?").
    recarregada.chat_input[0].set_value('e para o hotel?').run()
    segunda = agente_falso.chamadas[1]
    assert [m.parts[0].content for m in segunda['historico']] == [
        'tem queijo coalho?',
        'Resposta para: tem queijo coalho?',
    ]
    assert segunda['requisicoes_na_sessao'] == 1


def test_retorno_antecipado_exibido_em_destaque(agente_falso):
    at = _app('P004')
    at.chat_input[0].set_value('conta uma piada').run()

    assert not at.exception
    assert len(at.info) == 1
    assert 'Fora de escopo' in at.info[0].value
    assert 'Só ajudo com a operação da cooperativa.' in at.info[0].value


def test_sugestao_envia_pergunta(agente_falso):
    at = _app('P005')
    at.button(key='sugestao_0').click().run()

    assert not at.exception
    assert (
        agente_falso.chamadas[-1]['mensagem']
        == 'Quanto eu vendi em setembro e o que tenho a receber?'
    )
    assert agente_falso.chamadas[-1]['usuario'] == 'P005'


def test_usuarios_tem_sessoes_isoladas(agente_falso):
    _app('P006').chat_input[0].set_value('pergunta do P006').run()
    outro = _app('P007')
    assert 'pergunta do P006' not in _textos(outro)


def test_trava_impede_dois_turnos_simultaneos_na_mesma_sessao(agente_falso):
    import sabor.ui.app as ui

    trava = ui.trava_da_sessao('sessao-ocupada')
    assert ui.trava_da_sessao('sessao-ocupada') is trava
    assert trava.acquire(blocking=False)
    try:
        with pytest.raises(ui.TurnoEmAndamento):
            ui.executar_turno('sessao-ocupada', 'Dona Lúcia', 'oi')
    finally:
        trava.release()
    assert isinstance(trava, type(threading.Lock()))


def _botao(at: AppTest, rotulo: str):
    return next((b for b in at.button if b.label == rotulo), None)


def test_caixa_de_entrada_so_para_a_equipe_e_ordenada_por_urgencia(agente_falso, conn_temporaria):
    conn_temporaria.execute('TRUNCATE pedidos CASCADE')
    conn_temporaria.execute(
        """
        INSERT INTO pedidos (id, data_recebimento, canal, mensagem_original) VALUES
            ('PED-UI-1', '2026-10-28 09:00', 'email', 'cotação'),
            ('PED-UI-2', '2026-10-28 10:00', 'whatsapp', 'notificação'),
            ('PED-UI-3', '2026-10-28 11:00', 'email', 'falhou')
        """
    )
    for pid, recebido_em, texto in conn_temporaria.execute(
        'SELECT id, data_recebimento, mensagem_original FROM pedidos'
    ).fetchall():
        conn_temporaria.execute(
            """
            INSERT INTO triagem_pedidos (pedido_id, hash_entrada, status, tipo, urgencia,
                                         resultado, sinalizacoes, erro)
            SELECT %s, %s, v.status, v.tipo, v.urgencia, v.resultado::jsonb,
                   v.sinalizacoes::text[], v.erro
            FROM (VALUES
                ('PED-UI-1', 'ok', 'cotacao', 'baixa', '{}', '{}', NULL),
                ('PED-UI-2', 'ok', 'notificacao_oficial', 'critica', '{}', '{conformidade}', NULL),
                ('PED-UI-3', 'erro', NULL, NULL, NULL, '{}', 'timeout')
            ) AS v (pedido_id, status, tipo, urgencia, resultado, sinalizacoes, erro)
            WHERE v.pedido_id = %s
            """,
            (pid, hash_entrada(texto, recebido_em, modelo_extracao()), pid),
        )

    assert _botao(_app('P003'), 'Caixa de entrada') is None  # produtor não vê pedidos

    at = _app('Dona Lúcia')
    _botao(at, 'Caixa de entrada').click().run()

    assert not at.exception
    tabela = at.dataframe[0].value
    assert list(tabela['pedido_id']) == ['PED-UI-2', 'PED-UI-1', 'PED-UI-3']
    assert list(tabela['urgencia']) == ['Crítica', 'Baixa', 'erro']
    assert [m.value for m in at.metric] == ['1', '0', '0', '1']
    assert '1 pedido(s) sem triagem' in at.warning[0].value


# ---------------------------------------------------------------------------
# executar_turno direto (sem AppTest): conexões e gravação pelo hook da etapa 5b
# ---------------------------------------------------------------------------


@pytest.fixture
def app_modulo(monkeypatch, url_banco_temporario):
    import sabor.ui.app as app

    monkeypatch.setenv('DATABASE_URL', url_banco_temporario)
    monkeypatch.setattr(app, 'agente', lambda: object())
    abertas = {'n': 0}
    conexao_original = app.conexao

    @contextmanager
    def conexao_contada():
        abertas['n'] += 1
        try:
            with conexao_original() as conn:
                yield conn
        finally:
            abertas['n'] -= 1

    monkeypatch.setattr(app, 'conexao', conexao_contada)
    return app, abertas


def test_nenhuma_conexao_aberta_durante_a_chamada_ao_modelo(app_modulo, conn_temporaria):
    app, abertas = app_modulo
    falso = AgenteFalso()
    durante_o_modelo: list[int] = []
    responder_original = falso._responder

    def responder(*args, **kwargs):
        durante_o_modelo.append(abertas['n'])
        return responder_original(*args, **kwargs)

    falso._responder = responder
    app.processar_turno = falso
    sessao = sess.criar_sessao(conn_temporaria, 'P010')

    app.executar_turno(sessao.id, 'P010', 'tem cajá em novembro?')

    assert durante_o_modelo == [0]
    assert abertas['n'] == 0
    assert sess.contar_turnos(conn_temporaria, sessao.id) == 1


def test_erro_inesperado_grava_falha_segura_uma_vez(app_modulo, conn_temporaria, monkeypatch):
    app, _ = app_modulo
    sessao = sess.criar_sessao(conn_temporaria, 'P011')

    def explode_antes_de_gravar(*args, **kwargs):
        raise RuntimeError('bug')

    monkeypatch.setattr(app, 'processar_turno', explode_antes_de_gravar)
    turno = app.executar_turno(sessao.id, 'P011', 'pergunta 1')
    assert turno.falha_segura is True

    def explode_depois_de_gravar(sid, msg, usuario, *, salvar_memoria, **kwargs):
        salvar_memoria(sid, msg, RespostaTurno(sessao_id=sid, usuario=usuario, texto='ok'))
        raise RuntimeError('bug depois da 5b')

    monkeypatch.setattr(app, 'processar_turno', explode_depois_de_gravar)
    app.executar_turno(sessao.id, 'P011', 'pergunta 2')

    mensagens = sess.listar_mensagens(conn_temporaria, sessao.id)
    assert [m.texto for m in mensagens] == [
        'pergunta 1',
        app.MENSAGEM_FALHA_SEGURA,
        'pergunta 2',
        'ok',
    ]
