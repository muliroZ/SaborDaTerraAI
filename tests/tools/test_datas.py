"""Resolução determinística das datas de entrega (#9), sem banco nem LLM."""

from datetime import date

import pytest

from sabor.tools.datas import resolver_data

QUARTA_28_OUT = date(2026, 10, 28)  # recebimento de PED-001 a PED-005


@pytest.mark.parametrize(
    'texto,esperada',
    [
        ('amanhã', date(2026, 10, 29)),
        ('pra amanha se possivel', date(2026, 10, 29)),
        ('HOJE', date(2026, 10, 28)),
        ('depois de amanhã', date(2026, 10, 30)),
        ('pra sabado', date(2026, 10, 31)),
        ('sexta-feira', date(2026, 10, 30)),
        ('quarta', date(2026, 11, 4)),  # o próprio dia do recebimento é a semana que vem
        ('29/10/2026', date(2026, 10, 29)),
        ('08/nov', date(2026, 11, 8)),
        ('8 de novembro', date(2026, 11, 8)),
        ('evento em 08/11 (domingo)', date(2026, 11, 8)),
        ('29.10.26', date(2026, 10, 29)),
        ('04/11 10h', date(2026, 11, 4)),  # "10" é a hora, não o ano
        ('05/01', date(2027, 1, 5)),  # sem ano e já passou: ano seguinte
    ],
)
def test_resolve_datas_relativas_e_explicitas(texto, esperada):
    resolvida = resolver_data(texto, QUARTA_28_OUT)

    assert resolvida.data == esperada
    assert resolvida.estimada == esperada
    assert resolvida.alerta is None


def test_ped_001_sexta_01_nov_cai_num_domingo():
    resolvida = resolver_data('essa sexta (01/nov)', QUARTA_28_OUT)

    assert resolvida.data is None
    assert resolvida.estimada == date(2026, 11, 1)
    assert resolvida.inconsistente
    assert 'sexta-feira' in resolvida.alerta and 'domingo' in resolvida.alerta
    assert '30/10/2026' in resolvida.alerta  # sexta mais próxima


def test_ped_002_terca_04_nov_cai_numa_quarta():
    resolvida = resolver_data('terça 04/nov', QUARTA_28_OUT)

    assert resolvida.data is None
    assert '03/11/2026' in resolvida.alerta


def test_dia_da_semana_compativel_confirma_a_data():
    assert resolver_data('sábado 31/10', QUARTA_28_OUT).data == date(2026, 10, 31)


@pytest.mark.parametrize(
    'texto,inicio,fim',
    [
        ('período de 04 a 08 de novembro de 2026', date(2026, 11, 4), date(2026, 11, 8)),
        ('11 e 12/11/2026 (quarta e quinta)', date(2026, 11, 11), date(2026, 11, 12)),
        ('04-08/11', date(2026, 11, 4), date(2026, 11, 8)),
    ],
)
def test_intervalo_guarda_inicio_e_fim(texto, inicio, fim):
    resolvida = resolver_data(texto, QUARTA_28_OUT)

    assert (resolvida.data, resolvida.data_fim, resolvida.alerta) == (inicio, fim, None)


@pytest.mark.parametrize('texto', ['jan/2027', 'janeiro de 2027', 'em janeiro'])
def test_mes_sem_dia_e_so_estimativa(texto):
    resolvida = resolver_data(texto, QUARTA_28_OUT)

    assert resolvida.data is None
    assert resolvida.estimada == date(2027, 1, 1)
    assert resolvida.alerta is None


@pytest.mark.parametrize('texto', [None, '', 'o quanto antes', 'mar', '31/11', 'set de pratos'])
def test_sem_data_reconhecivel(texto):
    resolvida = resolver_data(texto, QUARTA_28_OUT)

    assert (resolvida.data, resolvida.estimada, resolvida.alerta) == (None, None, None)
