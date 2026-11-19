"""Testes unitários e de integração para a ferramenta rascunhar_notificacao (Issue #12)."""

from __future__ import annotations

import asyncio
from datetime import date

from sabor.agent.deps import AgentDeps
from sabor.db import conectar_readonly
from sabor.tools.rascunhar_notificacao import rascunhar_notificacao


def test_rascunhar_notificacao_pedido_banco():
    """Valida a sugestão de alocação e rascunhos gerados para o PED-2026-004 (PNAE)."""

    class MockContext:
        def __init__(self, conn):
            self.deps = AgentDeps(
                conn=conn,
                data_referencia=date(2026, 10, 30),
            )

    with conectar_readonly() as conn:
        ctx = MockContext(conn)
        resposta = asyncio.run(rascunhar_notificacao(ctx, pedido_id="PED-2026-004"))

        assert resposta is not None
        assert len(resposta.rascunhos) > 0
        for r in resposta.rascunhos:
            assert r.canal == "whatsapp"
            assert r.destinatario
            assert r.conteudo