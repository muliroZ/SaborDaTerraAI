"""Regras de apresentação da interface (#13): usuários, destaques, badges e trechos."""

import psycopg
import pytest

from sabor.ui import apresentacao as ap
from sabor.agent.schemas import Flag, Fonte, RespostaTurno, RetornoAntecipado
from sabor.db import conectar_readonly


def _turno(**kwargs) -> RespostaTurno:
    return RespostaTurno(sessao_id='s', usuario='Dona Lúcia', texto='t', **kwargs)


def test_usuarios_simulados_cobrem_funcionarios_e_os_18_produtores():
    assert ap.USUARIOS_SIMULADOS[:2] == ('Dona Lúcia', 'Seu Antônio')
    assert ap.PRODUTORES_IDS[0] == 'P001' and ap.PRODUTORES_IDS[-1] == 'P018'
    assert len(ap.PRODUTORES_IDS) == 18


def test_rotulo_e_sugestoes_por_perfil():
    assert ap.rotulo_usuario('P001', {'P001': 'Maria das Graças'}) == (
        'P001 · Maria das Graças · produtor'
    )
    assert ap.rotulo_usuario('P002') == 'P002 · produtor'
    assert ap.rotulo_usuario('Dona Lúcia') == 'Dona Lúcia · equipe da cooperativa'
    assert ap.sugestoes_para('P005') == ap.SUGESTOES_PRODUTOR
    assert ap.sugestoes_para('Seu Antônio') == ap.SUGESTOES_FUNCIONARIO


@pytest.mark.parametrize(
    ('motivo', 'nivel', 'substitui'),
    [
        ('FORA_DE_ESCOPO', 'info', True),
        ('PROMPT_INJECTION', 'warning', True),
        ('COTA_SESSAO_ESGOTADA', 'warning', True),
        ('ORCAMENTO_TURNO_ESGOTADO', 'warning', True),
        ('CACHE_HIT', 'success', False),
    ],
)
def test_destaque_de_retornos_antecipados(motivo, nivel, substitui):
    destaque = ap.destaque_do_turno(
        _turno(retorno_antecipado=RetornoAntecipado(motivo=motivo, mensagem='m'))
    )
    assert destaque.nivel == nivel
    assert destaque.substitui_resposta is substitui


def test_destaque_falha_segura_e_resposta_normal():
    assert ap.destaque_do_turno(_turno(falha_segura=True)).nivel == 'error'
    assert ap.destaque_do_turno(_turno()) is None


@pytest.mark.parametrize(
    ('tipo', 'cor'),
    [
        ('conformidade', 'red'),
        ('inadimplencia', 'red'),
        ('bloqueio_sanitário', 'red'),
        ('urgencia', 'orange'),
        ('fora_de_safra', 'orange'),
        ('acima_capacidade', 'orange'),
        ('ambiguidade', 'blue'),
        ('privacidade', 'violet'),
        ('outro', 'gray'),
    ],
)
def test_estilo_das_flags(tipo, cor):
    assert ap.estilo_flag(Flag(tipo=tipo, descricao='d')).cor == cor


def test_badge_markdown():
    badge = ap.badge_markdown(Flag(tipo='fora_de_safra', descricao='d'))
    assert badge == ':orange-badge[:material/schedule: Fora de safra]'


# ---------------------------------------------------------------------------
# Trecho das fontes no banco de desenvolvimento populado (pula se fora do ar ou vazio)
# ---------------------------------------------------------------------------


@pytest.fixture
def conn_dados():
    try:
        conn = conectar_readonly(tentativas=1)
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')
    if conn.execute('SELECT count(*) FROM produtores').fetchone()[0] == 0:
        conn.close()
        pytest.skip('Banco sem dados (rode `scripts/etl.py`)')
    try:
        yield conn
    finally:
        conn.close()


def test_trecho_completo_para_a_equipe_interna(conn_dados):
    """Mesma regra do texto da resposta (etapa 4c): a equipe vê os dados pessoais."""
    cpf = conn_dados.execute("SELECT cpf FROM produtores WHERE id = 'P001'").fetchone()[0]
    trecho = ap.buscar_trecho(
        conn_dados, Fonte(arquivo='produtores.csv', referencia='P001'), 'Dona Lúcia'
    )
    assert 'id: P001' in trecho
    assert f'cpf: {cpf}' in trecho
    assert 'chave_pix:' in trecho
    assert '[CPF_' not in trecho
    assert 'produto_id' not in trecho and 'extracao' not in trecho


def test_trecho_oculta_dados_pessoais_para_o_perfil_produtor(conn_dados):
    cpf = conn_dados.execute("SELECT cpf FROM produtores WHERE id = 'P001'").fetchone()[0]
    fonte = Fonte(arquivo='produtores.csv', referencia='P001')
    trecho = ap.buscar_trecho(conn_dados, fonte, 'P001')
    assert 'id: P001' in trecho
    assert cpf not in trecho
    assert 'cpf' not in trecho and 'chave_pix' not in trecho and 'conta_corrente' not in trecho


def test_trecho_de_pedido_mascara_terceiros_so_para_produtor(conn_dados):
    """PED-2026-015 traz o CPF de um terceiro na mensagem original."""
    fonte = Fonte(arquivo='pedidos.json', referencia='PED-2026-015')
    equipe = ap.buscar_trecho(conn_dados, fonte, 'Seu Antônio')
    produtor = ap.buscar_trecho(conn_dados, fonte, 'P001')
    assert equipe is not None and produtor is not None
    assert '[CPF_' not in equipe
    assert '[CPF_' in produtor


def test_trecho_respeita_perfil_produtor(conn_dados):
    fonte = Fonte(arquivo='produtores.csv', referencia='P001')
    assert ap.buscar_trecho(conn_dados, fonte, 'P001') is not None
    assert ap.buscar_trecho(conn_dados, fonte, 'P002') is None


def test_trecho_inexistente(conn_dados):
    fonte = Fonte(arquivo='lotes.csv', referencia='LOT-NAO-EXISTE')
    assert ap.buscar_trecho(conn_dados, fonte, 'Dona Lúcia') is None
    assert (
        ap.buscar_trecho(conn_dados, Fonte(arquivo='x.txt', referencia='y'), 'Dona Lúcia') is None
    )
