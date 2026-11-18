"""Testes parametrizados da Issue #10: validação das 15 mensagens contra a matriz de pedidos.md."""

from __future__ import annotations

from datetime import date
import pytest

from sabor.db import conectar_readonly
from sabor.tools.pedidos import Comprador, Entrega, Item, Pedido, _validar_dia_e_data
from sabor.tools.regras import classificar_pedido

# Matriz oficial das 15 mensagens conforme docs/pedidos.md
MATRIZ_15_PEDIDOS = [
    (
        'PED-2026-001',
        'compra',
        'Restaurante Raízes',
        'sexta (01/nov)',
        [
            Item(produto_texto='queijo coalho', produto='Queijo coalho', quantidade=30, unidade='kg', organico=True),
            Item(produto_texto='ricota', produto='Ricota', quantidade=5, unidade='kg', opcional=True),
        ],
        'alta',
        {'ambiguidade', 'data_inconsistente'},
    ),
    (
        'PED-2026-002',
        'compra_recorrente',
        'Mercado Bom Preço',
        'terça 04/nov',
        [
            Item(produto_texto='goiaba paluma', produto='Goiaba Paluma', quantidade=50, unidade='kg'),
            Item(produto_texto='banana prata', produto='Banana Prata', quantidade=40, unidade='kg'),
        ],
        'alta',
        {'conformidade', 'ambiguidade', 'data_inconsistente'},
    ),
    (
        'PED-2026-003',
        'compra',
        'Chef Amanda - Bistrô Sertão Gourmet',
        'amanhã',
        [
            Item(produto_texto='aquele queijo da última vez', quantidade=15, unidade='kg', ambiguo=True),
            Item(produto_texto='mel de jandaíra', produto='Mel de jandaíra', quantidade=3, unidade='kg'),
        ],
        'critica',
        {'acima_capacidade', 'ambiguidade', 'privacidade'},
    ),
    (
        'PED-2026-004',
        'compra',
        'Prefeitura de Garanhuns (PNAE)',
        '04/11/2026',
        [
            Item(produto_texto='banana prata', produto='Banana Prata', quantidade=200, unidade='kg'),
            Item(produto_texto='mel silvestre orgânico', produto='Mel silvestre', quantidade=20, unidade='kg', organico=True),
            Item(produto_texto='ovos caipira', produto='Ovos de galinha caipira', quantidade=100, unidade='dúzia'),
        ],
        'alta',
        {'acima_capacidade', 'conformidade', 'ambiguidade', 'inadimplencia'},
    ),
    (
        'PED-2026-005',
        'compra',
        'Dona Cremilda - Lanchonete da Feira',
        'sábado 31/10',
        [
            Item(produto_texto='queijo coalho', produto='Queijo coalho', quantidade=10, unidade='kg'),
            Item(produto_texto='manteiga', quantidade=5, unidade='kg', ambiguo=True),
        ],
        'alta',
        {'conformidade', 'ambiguidade'},
    ),
    (
        'PED-2026-006',
        'compra',
        'Rede SuperBom Atacado',
        '29/10/2026',
        [
            Item(produto_texto='queijo coalho', produto='Queijo coalho', quantidade=200, unidade='kg'),
            Item(produto_texto='queijo manteiga', produto='Queijo manteiga', quantidade=50, unidade='kg'),
        ],
        'critica',
        {'acima_capacidade', 'inadimplencia'},
    ),
    (
        'PED-2026-007',
        'cotacao',
        'Hotel Fazenda Serra Negra',
        '08/11/2026',
        [
            Item(produto_texto='cajá', produto='Cajá', quantidade=10, unidade='kg'),
            Item(produto_texto='frango caipira', produto='Frango caipira', quantidade=40, unidade='kg'),
            Item(produto_texto='ovos caipira', produto='Ovos de galinha caipira', quantidade=30, unidade='dúzia'),
            Item(produto_texto='mel variado', quantidade=8, unidade='kg', ambiguo=True),
        ],
        'alta',
        {'fora_de_safra', 'acima_capacidade', 'conformidade', 'ambiguidade'},
    ),
    (
        'PED-2026-008',
        'cotacao',
        'Sorveteria Tropical',
        None,
        [
            Item(produto_texto='morango', produto='Morango', quantidade=50, unidade='kg', organico=True),
            Item(produto_texto='amora', produto='Amora', quantidade=50, unidade='kg', organico=True),
        ],
        'normal',
        {'acima_capacidade', 'conformidade'},
    ),
    (
        'PED-2026-009',
        'cotacao',
        'Hospital São Francisco',
        None,
        [
            Item(produto_texto='banana', produto='Banana Prata', quantidade=25, unidade='kg', organico=True),
            Item(produto_texto='ovos', produto='Ovos de galinha caipira', quantidade=50, unidade='dúzia'),
        ],
        'normal',
        {'acima_capacidade', 'conformidade', 'privacidade'},
    ),
    (
        'PED-2026-010',
        'parceria',
        'Feira Orgânica do Recife',
        None,
        [],
        'normal',
        {'conformidade'},
    ),
    (
        'PED-2026-011',
        'cotacao',
        'Tropical Fruits Exportadora',
        None,
        [
            Item(produto_texto='manga', produto='Manga Tommy', quantidade=500, unidade='kg', organico=True),
            Item(produto_texto='maracujá', produto='Maracujá', quantidade=200, unidade='kg', organico=True),
        ],
        'baixa',
        {'conformidade', 'privacidade'},
    ),
    (
        'PED-2026-012',
        'consulta_financeira',
        'Maria das Graças (P001)',
        None,
        [],
        'normal',
        {'ambiguidade', 'privacidade'},
    ),
    (
        'PED-2026-013',
        'cotacao',
        'SESC Garanhuns',
        'janeiro/2027',
        [
            Item(produto_texto='frutas variadas', quantidade=500, unidade='kg', ambiguo=True),
            Item(produto_texto='ovos', produto='Ovos de galinha caipira', quantidade=200, unidade='dúzia'),
            Item(produto_texto='frango caipira', produto='Frango caipira', quantidade=100, unidade='kg'),
        ],
        'baixa',
        {'fora_de_safra', 'acima_capacidade', 'conformidade', 'ambiguidade', 'privacidade'},
    ),
    (
        'PED-2026-014',
        'notificacao_oficial',
        'ADAGRO',
        '11/11/2026',
        [],
        'critica',
        {'conformidade'},
    ),
    (
        'PED-2026-015',
        'consulta',
        'Cícero (P003)',
        '31/10/2026',
        [
            Item(produto_texto='queijo coalho', produto='Queijo coalho', quantidade=200, unidade='kg'),
            Item(produto_texto='manteiga', quantidade=30, unidade='kg', ambiguo=True),
        ],
        'critica',
        {'ambiguidade', 'privacidade'},
    ),
]


@pytest.mark.parametrize(
    'pedido_id,tipo,comprador_nome,data_texto,itens,urgencia_esperada,sinalizacoes_esperadas',
    MATRIZ_15_PEDIDOS,
    ids=[m[0] for m in MATRIZ_15_PEDIDOS],
)
def test_matriz_15_pedidos_sinalizacoes_e_urgencia(
    pedido_id,
    tipo,
    comprador_nome,
    data_texto,
    itens,
    urgencia_esperada,
    sinalizacoes_esperadas,
):
    """Garante que as 15 mensagens produzem todas as sinalizações e urgências da matriz em pedidos.md."""
    with conectar_readonly() as conn:
        row = conn.execute(
            'SELECT data_recebimento, mensagem_original FROM pedidos WHERE id = %s',
            (pedido_id,),
        ).fetchone()
        assert row is not None, f'{pedido_id} deve existir no banco'
        recebido_em, mensagem_original = row

        pedido = Pedido(
            pedido_id=pedido_id,
            tipo=tipo,
            comprador=Comprador(nome=comprador_nome),
            entrega=Entrega(data_texto=data_texto),
            itens=[i.model_copy(deep=True) for i in itens],
        )

        _validar_dia_e_data(pedido, mensagem_original, recebido_em.date())
        classificar_pedido(
            pedido,
            conn=conn,
            mensagem_original=mensagem_original,
            data_referencia=recebido_em.date(),
            data_global=date(2026, 10, 30),
        )

        assert pedido.urgencia == urgencia_esperada
        for flag_esperada in sinalizacoes_esperadas:
            assert flag_esperada in pedido.sinalizacoes, (
                f'{pedido_id}: flag {flag_esperada!r} ausente em {pedido.sinalizacoes}'
            )


def test_produtores_elegiveis_por_item_pnae_e_bloqueios():
    """Verifica que PNAE mel traz só P002 (exclui P008 vencido e P018 sem SIE) e frango exclui P017."""
    with conectar_readonly() as conn:
        pedido = Pedido(
            pedido_id='PED-2026-004',
            tipo='compra',
            comprador=Comprador(nome='Prefeitura de Garanhuns (PNAE)'),
            itens=[
                Item(produto_texto='mel silvestre', produto='Mel silvestre', quantidade=20, unidade='kg', organico=True),
                Item(produto_texto='frango caipira', produto='Frango caipira', quantidade=40, unidade='kg'),
            ],
        )
        classificar_pedido(
            pedido,
            conn=conn,
            mensagem_original='Compra PNAE contrato 045/2026 mel silvestre orgânico e frango caipira',
            data_referencia=date(2026, 10, 28),
        )
        assert pedido.itens[0].produtores_elegiveis == ['P002']
        assert 'P017' not in pedido.itens[1].produtores_elegiveis