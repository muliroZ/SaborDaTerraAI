"""Testes unitários do Passo 6: `processar_turno` (etapas 1 a 5 e retornos antecipados)."""

import time
from unittest.mock import MagicMock

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sabor.agent.deps import AgentDeps
from sabor.agent.fallback import MODELO_FALLBACK_1, MODELO_PRIMARIO
from sabor.agent.runner import (
    MENSAGEM_ENTRADA_VAZIA,
    MENSAGEM_FALHA_SEGURA,
    TIMEOUT_TURNO_S,
    criar_agente,
    processar_turno,
)
from sabor.agent.schemas import RespostaTurno


class _MascaradorFake:
    """Simula o mascarador da Issue #33 para testar a restauração na Etapa 4."""

    def mascarar(self, texto: str) -> str:
        return texto.replace('111.222.333-44', '[CPF_MASCARADO]')

    def restaurar(self, texto: str) -> str:
        return texto.replace('[CPF_MASCARADO]', '111.222.333-44')


def test_timeout_padrao_turno_e_20_segundos():
    assert TIMEOUT_TURNO_S == 20.0


def test_processar_turno_fluxo_completo_etapas_1_a_5():
    etapas_executadas: list[str] = []

    def fn_memoria(sessao_id: str):
        etapas_executadas.append(f'1_memoria:{sessao_id}')
        return []

    def fn_guardrail_entrada(msg: str) -> str | None:
        etapas_executadas.append('2_guardrail_entrada')
        return None

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        etapas_executadas.append('3_pydantic_ai')
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Produtor P001 (CPF [CPF_MASCARADO]) vendeu R$ 1.090,00.',
                        'fontes': [{'arquivo': 'financeiro.csv', 'referencia': 'FIN-2026-001'}],
                        'flags': [],
                        'rascunhos': [
                            {
                                'destinatario': 'P001 - [CPF_MASCARADO]',
                                'canal': 'whatsapp',
                                'conteudo': 'Resumo pronto.',
                            }
                        ],
                    },
                )
            ]
        )

    def fn_auditoria(turno: RespostaTurno) -> None:
        etapas_executadas.append(f'5a_auditoria:{turno.modelo_efetivo}')

    def fn_salvar_memoria(sessao_id: str, msg: str, turno: RespostaTurno) -> None:
        etapas_executadas.append(f'5b_salvar_memoria:{sessao_id}')

    agente = criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)])
    deps = AgentDeps(
        conn=MagicMock(),
        usuario='Dona Lúcia',
        mascarador=_MascaradorFake(),
    )

    turno = processar_turno(
        sessao_id='sess-100',
        mensagem='Quanto P001 vendeu?',
        usuario='Dona Lúcia',
        agente=agente,
        deps=deps,
        carregar_memoria=fn_memoria,
        guardrail_entrada=fn_guardrail_entrada,
        registrar_auditoria=fn_auditoria,
        salvar_memoria=fn_salvar_memoria,
    )

    assert etapas_executadas == [
        '1_memoria:sess-100',
        '2_guardrail_entrada',
        '3_pydantic_ai',
        f'5a_auditoria:{MODELO_PRIMARIO}',
        '5b_salvar_memoria:sess-100',
    ]
    assert turno.bloqueado_guardrail is False
    assert turno.falha_segura is False
    assert turno.modelo_efetivo == MODELO_PRIMARIO
    assert turno.provedor_efetivo == 'function'
    # Etapa 4c (#33): restaura PII no texto para o operador, mas NUNCA nos rascunhos para terceiros!
    assert '111.222.333-44' in turno.texto
    assert '111.222.333-44' not in turno.rascunhos[0].destinatario
    assert '[CPF_MASCARADO]' in turno.rascunhos[0].destinatario
    assert len(turno.fontes) == 1


def test_processar_turno_retorno_antecipado_guardrail_entrada():
    chamou_llm = False

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        nonlocal chamou_llm
        chamou_llm = True
        return ModelResponse(parts=[])

    agente = criar_agente([FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)])
    deps = AgentDeps(conn=MagicMock())

    # Caso 1: Mensagem em branco
    turno_vazio = processar_turno('sess-1', '   ', 'Dona Lúcia', agente=agente, deps=deps)
    assert turno_vazio.bloqueado_guardrail is True
    assert turno_vazio.texto == MENSAGEM_ENTRADA_VAZIA
    assert chamou_llm is False

    # Caso 2: Guardrail customizado reprova escopo
    turno_fora_escopo = processar_turno(
        'sess-1',
        'Qual a previsão do tempo em Tóquio?',
        'Dona Lúcia',
        agente=agente,
        deps=deps,
        guardrail_entrada=lambda m: 'Pergunta fora do escopo da cooperativa.',
    )
    assert turno_fora_escopo.bloqueado_guardrail is True
    assert turno_fora_escopo.texto == 'Pergunta fora do escopo da cooperativa.'
    assert chamou_llm is False


def test_processar_turno_falha_segura_quando_todos_provedores_falham():
    def fn_erro_503(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name='m', body='down')

    m1 = FunctionModel(fn_erro_503, model_name=MODELO_PRIMARIO)
    m2 = FunctionModel(fn_erro_503, model_name=MODELO_FALLBACK_1)
    agente = criar_agente([m1, m2])
    deps = AgentDeps(conn=MagicMock())

    auditoria_recebida: list[RespostaTurno] = []
    turno = processar_turno(
        'sess-err',
        'Qual o estoque de mel?',
        'Seu Antônio',
        agente=agente,
        deps=deps,
        registrar_auditoria=auditoria_recebida.append,
    )

    assert turno.falha_segura is True
    assert turno.bloqueado_guardrail is False
    assert turno.texto == MENSAGEM_FALHA_SEGURA
    assert [t.modelo for t in turno.tentativas_fallback] == [MODELO_PRIMARIO, MODELO_FALLBACK_1]
    assert len(auditoria_recebida) == 1


def test_processar_turno_falha_segura_quando_excede_timeout_global():
    def fn_lenta(messages, info: AgentInfo) -> ModelResponse:
        time.sleep(0.25)
        return ModelResponse(
            parts=[ToolCallPart(tool_name='final_result', args={'texto': 'Tarde demais'})]
        )

    agente = criar_agente([FunctionModel(fn_lenta, model_name=MODELO_PRIMARIO)])
    deps = AgentDeps(conn=MagicMock())

    turno = processar_turno(
        'sess-timeout',
        'Pergunta demorada',
        'Dona Lúcia',
        agente=agente,
        deps=deps,
        timeout_turno_s=0.05,
    )

    assert turno.falha_segura is True
    assert turno.texto == MENSAGEM_FALHA_SEGURA
