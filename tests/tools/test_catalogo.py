"""Mapeamento de nomes livres ao catálogo canônico (#9), com um catálogo em memória."""

import pytest

from sabor.tools.catalogo import Catalogo

PRODUTOS = [
    (1, 'queijo coalho'),
    (2, 'queijo manteiga'),
    (3, 'manteiga de garrafa'),
    (4, 'mel silvestre'),
    (5, 'mel de abelha jandaíra'),
    (6, 'ovos caipira'),
    (7, 'frango caipira'),
    (8, 'tomate'),
    (9, 'tomate cereja'),
    (10, 'leite'),
    (11, 'manga'),
    (12, 'maracujá'),
    (13, 'ricota'),
]
SINONIMOS = [
    ('mel', 4),
    ('mel de jandaíra', 5),
    ('mango', 11),
    ('manga tommy', 11),
    ('passion fruit', 12),
    ('maracuja', 12),
]


@pytest.fixture(scope='module')
def catalogo():
    return Catalogo(PRODUTOS, SINONIMOS)


@pytest.mark.parametrize(
    'texto,esperado',
    [
        ('Queijo Coalho', 'queijo coalho'),
        ('maracuja', 'maracujá'),  # sinônimo sem acento
        ('Mango', 'manga'),  # PED-011, em inglês
        ('Passion fruit', 'maracujá'),
        ('mel (aquele silvestre mesmo)', 'mel silvestre'),  # PED-001
        ('mel de jandaíra', 'mel de abelha jandaíra'),  # PED-003
        ('Mel silvestre (com certificação orgânica)', 'mel silvestre'),
        ('Ovos de galinha caipira', 'ovos caipira'),
        ('ovos', 'ovos caipira'),
        ('frango', 'frango caipira'),
        ('ricota fresca', 'ricota'),
        ('tomate', 'tomate'),
        ('tomate cereja', 'tomate cereja'),  # o mais específico vence
    ],
)
def test_resolve_correspondencia_unica(catalogo, texto, esperado):
    produto = catalogo.resolver(texto)

    assert produto is not None and produto.nome == esperado


@pytest.mark.parametrize(
    'texto',
    [
        'manteiga',  # PED-005: manteiga de garrafa ou queijo manteiga
        'queijo',
        'aquele queijo q vcs mandaram da ultima vez, o mais artesanal',  # PED-003
        'mel variado',
        'frutas variadas',
        'doce de leite',  # não é leite
        '',
        None,
    ],
)
def test_nao_adivinha(catalogo, texto):
    assert catalogo.resolver(texto) is None


def test_usa_o_primeiro_texto_que_resolve(catalogo):
    assert catalogo.resolver(None, 'mel silvestre').nome == 'mel silvestre'
    assert catalogo.resolver('manteiga de garrafa', 'manteiga').id == 3


def test_ignora_sinonimo_de_produto_inexistente():
    catalogo = Catalogo([(1, 'goiaba')], [('guava', 99)])

    assert catalogo.resolver('guava') is None
    assert catalogo.nomes() == ['goiaba']
