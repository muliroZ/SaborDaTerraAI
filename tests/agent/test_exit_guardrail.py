"""Testes unitários da Etapa 4 (Guardrail de Saída: 4a e 4b) com mocks e banco real."""

from __future__ import annotations

from datetime import date
import psycopg
import pytest

from sabor.agent.guardrail_exit import processar_guardrail_saida
from sabor.agent.schemas import Fonte, Rascunho, RespostaAgente
from sabor.db import conectar_readonly


class _CursorFalso:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _ConexaoPsycopgFalsa:
    """Simula conexão psycopg com dados equivalentes ao dataset (roda mesmo sem Docker)."""

    __module__ = 'psycopg.connection'

    def execute(self, query: str, params=None):
        sql = ' '.join(query.lower().split())
        if 'from produtores' in sql:
            return _CursorFalso([('P008', 'Produtor P008', date(2026, 9, 30))])
        if 'from chunks' in sql:
            return _CursorFalso(
                [
                    (
                        'Relatório de inspeção ADAGRO: NC-001 grave para P017 — venda e entrega '
                        'de frango caipira proibida e suspensa para entrega ao cliente sem SIE.',
                        'P017',
                    )
                ]
            )
        if 'from produtos' in sql:
            return _CursorFalso(
                [('Banana prata',), ('Frango caipira',), ('Mel',), ('Queijo coalho',)]
            )
        return _CursorFalso([])


@pytest.fixture
def conn_banco_ou_pula():
    try:
        conn = conectar_readonly(tentativas=1)
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')
    try:
        yield conn
    finally:
        conn.close()


def test_guardrail_saida_4a_bloqueia_restricao_sanitaria_unit():
    """Confirmação de venda/entrega de item sob interdição sanitária é bloqueada."""
    resposta_mock = RespostaAgente(
        texto='Sim, podemos confirmar a venda e entrega do frango caipira para amanhã.',
        rascunhos=[
            Rascunho(destinatario='Cliente', conteudo='Confirmado frango', canal='whatsapp')
        ],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock, conn=_ConexaoPsycopgFalsa(), data_referencia=date(2026, 10, 30)
    )
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras)
    assert res_proc.rascunhos == []


def test_guardrail_saida_4a_permite_venda_de_produto_regular():
    """Regressão: termos comuns no relatório de inspeção ('para', 'entrega', 'venda') não podem
    bloquear a comercialização de produtos regulares como banana prata."""
    resposta_mock = RespostaAgente(
        texto='Sim, podemos entregar 50 kg de banana prata para o restaurante amanhã.',
        fontes=[Fonte(arquivo='sazonalidade.csv', referencia='Banana prata')],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        conn=_ConexaoPsycopgFalsa(),
        data_referencia=date(2026, 10, 30),
        fontes_retornadas_no_turno=['sazonalidade.csv · Banana prata'],
    )
    assert regras == []
    assert 'banana prata' in res_proc.texto


def test_guardrail_saida_4a_bloqueia_organico_vencido_unit():
    """Produtor com certificação orgânica vencida não pode ser confirmado como orgânico vigente."""
    resposta_mock = RespostaAgente(
        texto='O produtor P008 possui certificação orgânica vigente e válida para fornecimento.',
    )
    _res_proc, regras = processar_guardrail_saida(
        resposta_mock, conn=_ConexaoPsycopgFalsa(), data_referencia=date(2026, 10, 30)
    )
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras)


def test_guardrail_saida_4a_permite_resposta_pergunta_1_com_vigentes_e_p008_vencido():
    """Regressão: citar P002/P004 como vigentes e P008 como vencido na mesma resposta é válido."""
    resposta_mock = RespostaAgente(
        texto=(
            'Os produtores P002 e P004 têm certificação orgânica vigente para mel. '
            'Já o produtor P008 está com a certificação vencida desde 30/09/2026.'
        ),
        fontes=[Fonte(arquivo='produtores.csv', referencia='P002')],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        conn=_ConexaoPsycopgFalsa(),
        data_referencia=date(2026, 10, 30),
        fontes_retornadas_no_turno=['produtores.csv · P002'],
    )
    assert regras == []
    assert 'P002 e P004' in res_proc.texto


def test_guardrail_saida_4a_permite_negacao_p008_vencido_e_frango_nc001_unit():
    """Resposta correta negando/alertando sobre P008 vencido e frango/NC-001 não é bloqueada."""
    resposta_mock = RespostaAgente(
        texto=(
            'Não podemos vender frango caipira devido à restrição da NC-001, e o produtor P008 '
            'não tem certificação orgânica vigente (certificação vencida).'
        ),
        fontes=[],
        rascunhos=[],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        conn=_ConexaoPsycopgFalsa(),
        data_referencia=date(2026, 10, 30),
    )
    assert regras == []
    assert 'Não podemos vender frango caipira' in res_proc.texto


def test_guardrail_saida_4a_bloqueia_rascunho_confirmando_frango_unit():
    """Confirmação indevida em rascunho continua sendo bloqueada pela 4a."""
    resposta_rascunho = RespostaAgente(
        texto='Segue o rascunho da mensagem para o cliente.',
        rascunhos=[
            Rascunho(
                destinatario='Cliente',
                conteudo='Confirmamos a venda e entrega de frango caipira para amanhã.',
                canal='whatsapp',
            )
        ],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_rascunho,
        conn=_ConexaoPsycopgFalsa(),
        data_referencia=date(2026, 10, 30),
    )
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras)
    assert res_proc.rascunhos == []


def test_guardrail_saida_4a_bloqueia_restricao_sanitaria_dinamica(conn_banco_ou_pula):
    """Integração: menção a comercialização de item sob interdição registrada no banco é barrada."""
    resposta_mock = RespostaAgente(
        texto='Sim, podemos confirmar a venda e entrega do frango caipira para amanhã.',
        rascunhos=[
            Rascunho(destinatario='Cliente', conteudo='Confirmado frango', canal='whatsapp')
        ],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock, conn=conn_banco_ou_pula, data_referencia=date(2026, 10, 30)
    )
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras)
    assert len(res_proc.rascunhos) == 0


def test_guardrail_saida_4a_bloqueia_organico_vencido_dinamico(conn_banco_ou_pula):
    """Integração: produtor com certificação vencida no banco não pode ser confirmado vigente."""
    resposta_mock = RespostaAgente(
        texto='O produtor P008 possui certificação orgânica vigente e válida para fornecimento.',
    )
    _res_proc, regras = processar_guardrail_saida(
        resposta_mock, conn=conn_banco_ou_pula, data_referencia=date(2026, 10, 30)
    )
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras)


def test_guardrail_saida_4b_exige_fonte_factual():
    """Afirmação factual sobre estoque/capacidade sem fonte é rebaixada para 'não encontrada'."""
    resposta_mock = RespostaAgente(
        texto='O estoque atual de queijo coalho é de 250 kg disponíveis.',
        fontes=[],
    )
    res_proc, regras = processar_guardrail_saida(resposta_mock, fontes_retornadas_no_turno=[])
    assert any('4B_SEM_FONTE_FACTUAL' in r for r in regras)
    assert 'não encontrada' in res_proc.texto.lower()


def test_guardrail_saida_4b_aceita_fonte_formatada_com_ponto_medio():
    """Regressão: fonte 'arquivo · referencia' deve casar com RespostaAgente.fontes."""
    resposta_mock = RespostaAgente(
        texto='Produtor P001 vendeu R$ 1.090,00.',
        fontes=[Fonte(arquivo='financeiro.csv', referencia='FIN-2026-001')],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        fontes_retornadas_no_turno=['financeiro.csv · FIN-2026-001'],
    )
    assert regras == []
    assert res_proc.texto == 'Produtor P001 vendeu R$ 1.090,00.'
    assert len(res_proc.fontes) == 1


def test_guardrail_saida_4b_rejeita_fonte_nao_retornada_no_turno():
    """Fonte citada na resposta que não veio nas ferramentas do turno continua sendo barrada."""
    resposta_mock = RespostaAgente(
        texto='O preço do mel orgânico é R$ 42,00.',
        fontes=[Fonte(arquivo='tabela_precos_outubro_2026.csv', referencia='Mel')],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        fontes_retornadas_no_turno=['financeiro.csv · FIN-2026-001'],
    )
    assert any('4B_SEM_FONTE_FACTUAL' in r for r in regras)
    assert res_proc.fontes == []


def test_guardrail_saida_4a_bloqueia_evasao_lexical_em_rascunho_p017():
    """INJ-15: voz passiva ('serão entregues') e 'separados' em rascunho de P017 são bloqueados."""
    resposta = RespostaAgente(
        texto='Recado preparado.',
        rascunhos=[
            Rascunho(
                destinatario='Hotel Fazenda',
                canal='whatsapp',
                conteudo=(
                    'Prezado Hotel Fazenda, informamos que os 40 kg de frango do produtor P017 '
                    'já se encontram separados e serão entregues no caminhão de domingo.'
                ),
            )
        ],
    )
    res_proc, regras = processar_guardrail_saida(resposta, conn=_ConexaoPsycopgFalsa())
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras)
    assert res_proc.rascunhos == []
    assert 'Bloqueio operacional' in res_proc.texto


class _ConexaoInspecaoMultiProdutor:
    """Replica a estrutura real de `inspecao_adagro_2026.txt` (P001..P005 regulares + P017)."""

    __module__ = 'psycopg.connection'

    _CHUNK_VISTORIADOS = """\
PRODUTORES VISTORIADOS (amostragem):

  1. P001 - Maria das Graças S. Ferreira (Garanhuns)
     Produtos: Goiaba, acerola, manga
     Situação: REGULAR

  2. P002 - José Antônio dos Santos (Brejão)
     Produtos: Mel silvestre, mel de jandaíra, própolis
     Registro SIE casa do mel: VIGENTE (PE-SIE-0892)
     Situação: REGULAR

  3. P003 - Cícero Romão de Almeida (Caetés)
     Produtos: Queijo coalho, queijo manteiga, ricota
     Registro SIE queijaria: VIGENTE (PE-SIE-0756)
     Situação: REGULAR

  5. P017 - Wellington da Silva Monteiro (Garanhuns)
     Produtos: Frango caipira, ovos caipira
     Registro SIE: NÃO POSSUI
     Situação: IRREGULAR - Produtor de frango caipira sem registro
     no Serviço de Inspeção. Comercialização limitada a venda
     direta ao consumidor final até regularização.
"""

    _CHUNK_NC = """\
NÃO CONFORMIDADES:
  NC-001 (GRAVE): Produtor P017 (frango caipira) sem registro SIE.
    Ação: Cooperativa deve suspender a intermediação de frango caipira
    até que o produtor obtenha o registro junto ao SIE municipal.
    Prazo: Imediato.
    NOTA: Ovos não exigem SIE para venda direta ao consumidor.
"""

    def execute(self, query: str, params=None):
        sql = ' '.join(query.lower().split())
        if 'from produtores' in sql:
            return _CursorFalso([])
        if 'from chunks' in sql:
            return _CursorFalso([(self._CHUNK_VISTORIADOS, None), (self._CHUNK_NC, None)])
        if 'from produtos' in sql:
            return _CursorFalso(
                [
                    ('Queijo coalho',),
                    ('Mel silvestre',),
                    ('Frango caipira',),
                    ('Ovos caipira',),
                ]
            )
        return _CursorFalso([])


def test_guardrail_saida_4a_nao_contamina_produtores_regulares_no_mesmo_chunk():
    """Regressão (PED-2026-015/PED-2026-003): chunk de inspeção com P001..P005 regulares
    e P017 irregular não pode bloquear queijo coalho (P003), mel (P002) ou ovos caipira."""
    conn = _ConexaoInspecaoMultiProdutor()

    for texto_regular, ref in (
        ('Podemos fornecer 200 kg de queijo coalho do produtor P003 (SIE vigente).', 'P003'),
        ('Confirmamos disponibilidade de mel silvestre do produtor P002.', 'P002'),
        ('Podemos entregar ovos caipira sem restrição de SIE.', 'ovos'),
    ):
        resp = RespostaAgente(
            texto=texto_regular,
            fontes=[Fonte(arquivo='produtores.csv', referencia=ref)],
        )
        res_proc, regras = processar_guardrail_saida(
            resp,
            conn=conn,
            data_referencia=date(2026, 10, 30),
            fontes_retornadas_no_turno=[f'produtores.csv · {ref}'],
        )
        assert regras == [], f'Bloqueio indevido para: {texto_regular}'
        assert res_proc.texto == texto_regular

    resp_frango = RespostaAgente(
        texto='Podemos entregar 40 kg de frango caipira do produtor P017 amanhã.',
    )
    _, regras_frango = processar_guardrail_saida(
        resp_frango, conn=conn, data_referencia=date(2026, 10, 30)
    )
    assert any('4A_BLOQUEIO_SANITARIO' in r for r in regras_frango)


def test_guardrail_saida_4a_permite_queijo_p003_no_banco_real(conn_banco_ou_pula):
    """Integração: P003 e queijo coalho (regulares no relatório ADAGRO) não são bloqueados."""
    resp = RespostaAgente(
        texto='Podemos fornecer 200 kg de queijo coalho pelo produtor P003.',
        fontes=[Fonte(arquivo='pedidos.json', referencia='PED-2026-015')],
    )
    res_proc, regras = processar_guardrail_saida(
        resp,
        conn=conn_banco_ou_pula,
        data_referencia=date(2026, 10, 30),
        fontes_retornadas_no_turno=['pedidos.json · PED-2026-015'],
    )
    assert regras == []
    assert 'queijo coalho' in res_proc.texto
