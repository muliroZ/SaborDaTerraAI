"""Testes da ferramenta consultar_dados (Issue #7).

Duas camadas:

1. Testes UNITÁRIOS (sem banco): perfil/permissão, Fonte, regex de
   rastrear_lote, mascarador no-op. Rodam sempre, em qualquer máquina.

2. Testes de INTEGRAÇÃO (com banco): as perguntas de referência 2, 3, 4, 5
   e 8 de docs/projeto.md, batendo contra os dados carregados pelo
   pipeline (Issue #4). Usam a fixture `conn` de conftest.py (conexão real,
   só leitura, sem banco de teste isolado — ver conftest.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from sabor.agent.deps import AgentDeps
from sabor.tools.base import SemPermissao, Usuario, _aplicar_filtro_produtor
from sabor.tools.consultar_dados import (
    _PADRAO_LOTE_ID,
    _PADRAO_NUMERO,
    buscar_produtores,
    consultar_financeiro,
    consultar_preco,
    consultar_safra,
    rastrear_lote,
)

FUNCIONARIO = Usuario(perfil='funcionario')
PRODUTOR_P001 = Usuario(perfil='produtor', produtor_id='P001')
PRODUTOR_P003 = Usuario(perfil='produtor', produtor_id='P003')

DATA_REF = date(2026, 10, 30)


@dataclass
class _Ctx:
    """Contexto mínimo compatível com `RunContext[AgentDeps]` nos testes de integração."""

    deps: AgentDeps


def _ctx_funcionario(conn) -> _Ctx:
    return _Ctx(deps=AgentDeps(conn=conn, usuario='Dona Lúcia', data_referencia=DATA_REF))


def _ctx_produtor(conn, produtor_id: str) -> _Ctx:
    return _Ctx(deps=AgentDeps(conn=conn, usuario=produtor_id, data_referencia=DATA_REF))


# =============================================================================
# 1. Unitários — perfil e permissão (sem banco)
# =============================================================================

def test_funcionario_sem_filtro_ve_todo_mundo():
    assert _aplicar_filtro_produtor(FUNCIONARIO, None) is None


def test_funcionario_pode_filtrar_qualquer_produtor():
    assert _aplicar_filtro_produtor(FUNCIONARIO, 'P003') == 'P003'


def test_produtor_sem_filtro_trava_no_proprio():
    assert _aplicar_filtro_produtor(PRODUTOR_P001, None) == 'P001'


def test_produtor_pedindo_o_proprio_funciona():
    assert _aplicar_filtro_produtor(PRODUTOR_P001, 'P001') == 'P001'


def test_produtor_pedindo_outro_produtor_e_negado():
    with pytest.raises(SemPermissao):
        _aplicar_filtro_produtor(PRODUTOR_P001, 'P003')


def test_usuario_produtor_sem_produtor_id_e_erro_de_configuracao():
    with pytest.raises(ValueError):
        _aplicar_filtro_produtor(Usuario(perfil='produtor'), None)


# =============================================================================
# 2. Unitários — regex de rastrear_lote (sem banco)
# =============================================================================

@pytest.mark.parametrize('texto', ['LOT-2026-0089', 'lot-2026-0089'])
def test_padrao_lote_id_aceita_id_exato_case_insensitive(texto):
    assert _PADRAO_LOTE_ID.match(texto)


@pytest.mark.parametrize('texto', ['lote 2024-089', 'LOT 2026 089', '2024-089'])
def test_padrao_lote_id_rejeita_referencia_aproximada(texto):
    assert not _PADRAO_LOTE_ID.match(texto)


def test_padrao_numero_extrai_ano_e_sufixo_da_referencia_aproximada():
    m = _PADRAO_NUMERO.search('qual a rastreabilidade do lote 2024-089?')
    assert m is not None
    assert m.group(1) == '2024'
    assert m.group(2) == '089'


def test_padrao_numero_nao_acha_nada_em_texto_sem_numero_de_lote():
    assert _PADRAO_NUMERO.search('me manda o queijo de sempre') is None


# =============================================================================
# 3. Integração — perguntas de referência (docs/projeto.md)
# =============================================================================

def test_pergunta_2_rastreabilidade_lote_2024_089(conn):
    resultado = rastrear_lote(_ctx_funcionario(conn), texto='lote 2024-089')
    assert resultado.encontrado
    lote = resultado.lote
    assert lote.lote_id == 'LOT-2026-0089'
    assert lote.produto == 'queijo coalho'
    assert lote.produtor_id == 'P003'
    assert lote.quantidade == 45
    assert lote.data_colheita == date(2026, 9, 1)
    assert lote.data_entrega == date(2026, 9, 2)
    assert lote.comprador == 'Restaurante Raízes'
    assert lote.temperatura_transporte_c == 8
    assert lote.validade == date(2026, 10, 2)
    assert resultado.mensagem is not None


def test_pergunta_3_vendas_maria_das_gracas(conn):
    resumo = consultar_financeiro(_ctx_funcionario(conn), produtor_id='P001')
    assert resumo.total_vendido == pytest.approx(1090.00)
    assert resumo.total_pendente == pytest.approx(0.00)
    assert all(item.status_pagamento == 'pago' for item in resumo.itens)

    outubro = consultar_financeiro(
        _ctx_funcionario(conn),
        produtor_id='P001',
        periodo_inicio=date(2026, 10, 1),
        periodo_fim=date(2026, 10, 31),
    )
    assert outubro.itens == []


def test_pergunta_3_perfil_produtor_so_ve_o_proprio_saldo(conn):
    resumo = consultar_financeiro(_ctx_produtor(conn, 'P001'))
    assert resumo.total_vendido == pytest.approx(1090.00)
    with pytest.raises(SemPermissao):
        consultar_financeiro(_ctx_produtor(conn, 'P001'), produtor_id='P003')


def test_pergunta_4_superbom_devendo_p003(conn):
    resumo = consultar_financeiro(_ctx_funcionario(conn), comprador='SuperBom', status='vencido')
    assert resumo.total_vencido == pytest.approx(6020.00)
    assert {item.produtor_id for item in resumo.itens} == {'P003'}
    assert all(item.data_vencimento < DATA_REF for item in resumo.itens)


def test_pergunta_5_caja_novembro_disponibilidade_baixa(conn):
    safra = consultar_safra(_ctx_funcionario(conn), 'cajá', mes=11)
    assert safra.nivel == 'baixa'
    assert safra.produtores == ['P010']

    preco = consultar_preco(_ctx_funcionario(conn), 'cajá')
    assert preco.preco_aplicavel == pytest.approx(8.00)
    assert preco.volume_minimo_atacado == 0


def test_pergunta_8_mel_de_jandaira(conn):
    produtores = buscar_produtores(_ctx_funcionario(conn), produto='mel de jandaíra')
    assert [p.id for p in produtores] == ['P002']

    preco = consultar_preco(_ctx_funcionario(conn), 'mel de abelha jandaíra', organico=True)
    assert preco.preco_aplicavel == pytest.approx(140.00)
    assert preco.organico_disponivel is True


# =============================================================================
# 4. Integração — mascaramento de PII ativo (Issue #33)
# =============================================================================

def test_dados_pessoais_sao_mascarados_por_tokens(conn):
    """Com a Issue #33 ativa em AgentDeps, CPF, telefone e PIX saem como tokens."""
    produtores = buscar_produtores(_ctx_funcionario(conn), municipio='Garanhuns')
    assert produtores
    assert all(p.cpf is None or p.cpf.startswith('[CPF_') for p in produtores)
    assert all(p.chave_pix is None or p.chave_pix.startswith('[PIX_') for p in produtores)