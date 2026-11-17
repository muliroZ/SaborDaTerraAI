"""Testes unitários dos schemas Pydantic de saída do agente e do turno."""

from pydantic import ValidationError
import pytest

from sabor.agent.schemas import Flag, Fonte, Rascunho, RespostaAgente, RespostaTurno


def test_fonte_formatar_segue_convencao():
    fonte = Fonte(arquivo='lotes.csv', referencia='LOT-2026-0089')
    assert fonte.formatar() == '[lotes.csv · LOT-2026-0089]'


def test_resposta_agente_valores_padrao_e_validacao():
    resp = RespostaAgente(texto='Não encontrei essa informação nos dados.')
    assert resp.texto == 'Não encontrei essa informação nos dados.'
    assert resp.fontes == []
    assert resp.flags == []
    assert resp.rascunhos == []

    with pytest.raises(ValidationError):
        RespostaAgente()  # type: ignore[call-arg]


def test_resposta_turno_de_resposta_agente():
    resposta_llm = RespostaAgente(
        texto='O lote LOT-2026-0089 é de Cícero (P003).',
        fontes=[Fonte(arquivo='lotes.csv', referencia='LOT-2026-0089')],
        flags=[Flag(tipo='conformidade', descricao='Queijaria com SIE ativo')],
        rascunhos=[
            Rascunho(
                destinatario='P003 - Cícero',
                canal='whatsapp',
                conteudo='Olá Seu Cícero, novo pedido de queijo coalho.',
            )
        ],
    )
    turno = RespostaTurno.de_resposta_agente(
        sessao_id='sessao-01',
        usuario='Dona Lúcia',
        resposta=resposta_llm,
        modelo_efetivo='gemini-3.5-flash-lite',
        provedor_efetivo='google-gla',
        latencia_ms=420,
    )
    assert turno.sessao_id == 'sessao-01'
    assert turno.usuario == 'Dona Lúcia'
    assert turno.texto == resposta_llm.texto
    assert len(turno.fontes) == 1
    assert turno.fontes[0].formatar() == '[lotes.csv · LOT-2026-0089]'
    assert len(turno.flags) == 1
    assert len(turno.rascunhos) == 1
    assert turno.modelo_efetivo == 'gemini-3.5-flash-lite'
    assert turno.provedor_efetivo == 'google-gla'
    assert turno.tentativas_fallback == []
    assert turno.latencia_ms == 420
    assert turno.bloqueado_guardrail is False
    assert turno.falha_segura is False
