"""Interface Streamlit (#13) com `AppTest`, banco temporário e agente simulado (sem LLM)."""

from pathlib import Path
import threading

import pytest
from streamlit.testing.v1 import AppTest

import sabor.agent
from sabor.agent import memoria as sess
from sabor.agent.schemas import Flag, Fonte, Rascunho, RespostaTurno, RetornoAntecipado

UI = str(Path(__file__).resolve().parents[2] / 'src' / 'sabor' / 'ui' / 'app.py')


class AgenteFalso:
    """Substitui `processar_turno`: registra as chamadas e a memória recebida."""

    def __init__(self):
        self.chamadas: list[dict] = []

    def __call__(self, sessao_id, mensagem, usuario, *, carregar_memoria, **kwargs):
        historico = carregar_memoria(sessao_id)
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
