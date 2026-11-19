"""Persistência da auditoria por resposta do agente (Etapa 5a, issue #35)."""

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from sabor.agent.mascarador import Mascarador
from sabor.agent.schemas import RespostaTurno, UserProfile


def _mascarar_valor(valor: Any, mascarador: Mascarador) -> Any:
    if isinstance(valor, str):
        return mascarador.mascarar(valor)
    if isinstance(valor, list):
        return [_mascarar_valor(item, mascarador) for item in valor]
    if isinstance(valor, dict):
        return {chave: _mascarar_valor(item, mascarador) for chave, item in valor.items()}
    return valor


def registrar_auditoria(
    conn: psycopg.Connection,
    *,
    message_id: int,
    usuario_id: str,
    perfil: UserProfile,
    turno: RespostaTurno,
) -> None:
    """Grava metadados do turno, sem persistir o texto da pergunta ou da resposta.

    `message_id` é o id da mensagem do assistente criada pela Etapa 5b. A FK e o índice
    único no schema garantem a relação 1–1 entre `mensagens` e `auditoria_mensagens`.
    """
    if message_id is None:
        raise ValueError('A auditoria exige o id persistido da mensagem do assistente.')

    mascarador = Mascarador()
    fontes = _mascarar_valor(
        [fonte.model_dump(mode='json') for fonte in turno.fontes], mascarador
    )
    ferramentas = _mascarar_valor(turno.ferramentas, mascarador)
    regras = _mascarar_valor(turno.regras_disparadas, mascarador)
    conn.execute(
        """
        INSERT INTO auditoria_mensagens (
            message_id, sessao_id, usuario_id, perfil, modelo, provedor,
            ferramentas, fontes, regras_disparadas, retorno_antecipado, cache_hit,
            tokens_entrada, tokens_saida, latencia_ms, trace_id
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            message_id,
            turno.sessao_id,
            usuario_id,
            perfil,
            turno.modelo_efetivo,
            turno.provedor_efetivo,
            Jsonb(ferramentas),
            Jsonb(fontes),
            Jsonb(regras),
            turno.retorno_antecipado.motivo if turno.retorno_antecipado else None,
            turno.cache_hit,
            turno.tokens_entrada,
            turno.tokens_saida,
            turno.latencia_ms,
            turno.trace_id,
        ),
    )
