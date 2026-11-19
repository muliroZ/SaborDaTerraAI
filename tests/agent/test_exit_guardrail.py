"""Testes unitários da Etapa 4 (Guardrail de Saída: 4a e 4b) com banco real e mocks."""

from __future__ import annotations

from datetime import date
import pytest

from sabor.agent.guardrail_exit import processar_guardrail_saida
from sabor.agent.schemas import Rascunho, RespostaAgente
from sabor.db import conectar_readonly


def test_guardrail_saida_4a_bloqueia_restricao_sanitaria_dinamica():
    """Valida que menção a comercialização de item sob interdição sanitária registrada no banco é bloqueada."""
    resposta_mock = RespostaAgente(
        texto="Sim, podemos confirmar a venda e entrega do frango caipira para amanhã.",
        rascunhos=[Rascunho(destinatario="Cliente", conteudo="Confirmado frango", canal="whatsapp")],
    )
    with conectar_readonly() as conn:
        res_proc, regras = processar_guardrail_saida(resposta_mock, conn=conn, data_referencia=date(2026, 10, 30))
        assert any("4A_BLOQUEIO_SANITARIO" in r for r in regras)
        assert len(res_proc.rascunhos) == 0


def test_guardrail_saida_4a_bloqueia_organico_vencido_dinamico():
    """Valida que produtor com certificação orgânica vencida no banco não pode ser confirmado como orgânico."""
    resposta_mock = RespostaAgente(
        texto="O produtor P008 possui certificação orgânica vigente e válida para fornecimento.",
    )
    with conectar_readonly() as conn:
        res_proc, regras = processar_guardrail_saida(resposta_mock, conn=conn, data_referencia=date(2026, 10, 30))
        assert any("4A_BLOQUEIO_SANITARIO" in r for r in regras)


def test_guardrail_saida_4b_exige_fonte_factual():
    """Afirmação factual sobre estoque/capacidade sem fonte oficial é rebaixada para 'não encontrada'."""
    resposta_mock = RespostaAgente(
        texto="O estoque atual de queijo coalho é de 250 kg disponíveis.",
        fontes=[],
    )
    res_proc, regras = processar_guardrail_saida(resposta_mock, fontes_retornadas_no_turno=[])
    assert any("4B_SEM_FONTE_FACTUAL" in r for r in regras)
    assert "não encontrada" in res_proc.texto.lower()


def test_guardrail_saida_4b_aceita_fonte_formatada_com_ponto_medio():
    """Regressão: fonte 'arquivo · referencia' deve casar com RespostaAgente.fontes."""
    from sabor.agent.schemas import Fonte

    resposta_mock = RespostaAgente(
        texto="Produtor P001 vendeu R$ 1.090,00.",
        fontes=[Fonte(arquivo="financeiro.csv", referencia="FIN-2026-001")],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        fontes_retornadas_no_turno=["financeiro.csv · FIN-2026-001"],
    )
    assert regras == []
    assert res_proc.texto == "Produtor P001 vendeu R$ 1.090,00."
    assert len(res_proc.fontes) == 1


def test_guardrail_saida_4b_rejeita_fonte_nao_retornada_no_turno():
    """Fonte citada na resposta que não veio nas ferramentas do turno continua sendo barrada."""
    from sabor.agent.schemas import Fonte

    resposta_mock = RespostaAgente(
        texto="O preço do mel orgânico é R$ 42,00.",
        fontes=[Fonte(arquivo="tabela_precos_outubro_2026.csv", referencia="Mel")],
    )
    res_proc, regras = processar_guardrail_saida(
        resposta_mock,
        fontes_retornadas_no_turno=["financeiro.csv · FIN-2026-001"],
    )
    assert any("4B_SEM_FONTE_FACTUAL" in r for r in regras)
    assert res_proc.fontes == []