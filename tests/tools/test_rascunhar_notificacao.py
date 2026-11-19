from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sabor.tools.base import SemPermissao
from sabor.tools.pedidos import Comprador, Entrega, Item, Pedido
from sabor.tools.rascunhar_notificacao import rascunhar_notificacao


class _MascaradorTeste:
    def mascarar(self, texto: str) -> str:
        import re

        texto = re.sub(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", "[CPF]", texto)
        texto = re.sub(r"\(\d{2}\)\s*9?\d{4}-\d{4}", "[TEL]", texto)
        texto = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[EMAIL]", texto)
        return texto


def _ctx(usuario: str = "Ana Equipe", mascarador: Any | None = None) -> SimpleNamespace:
    deps = SimpleNamespace(
        conn=MagicMock(),
        data_referencia=date(2026, 10, 30),
        usuario=usuario,
        mascarador=mascarador,
    )
    return SimpleNamespace(deps=deps)


@pytest.mark.anyio
async def test_produtor_recebe_sem_permissao():
    ctx = _ctx(usuario="P001")
    with pytest.raises(SemPermissao):
        await rascunhar_notificacao(ctx, pedido_id="PED-2026-004")


@pytest.mark.anyio
async def test_nao_inventa_quantidade_nem_usa_data_recebimento_como_entrega():
    pedido_extraido = Pedido(
        pedido_id="PED-2026-010",
        tipo="compra",
        comprador=Comprador(nome="Mercado Central"),
        entrega=Entrega(data_texto=None),
        itens=[
            Item(
                produto_texto="Queijo coalho artesanal",
                produto="Queijo coalho artesanal",
                quantidade=None,
                unidade=None,
                produtores_elegiveis=["P001"],
            )
        ],
        sinalizacoes=[],
    )

    ctx = _ctx(usuario="Carla Operadora")
    ctx.deps.conn.execute.return_value.fetchone.side_effect = [
        ("PED-2026-010", "compra", "Mercado Central", "Quero queijo coalho artesanal"),
        ("Maria Silva",),
    ]

    with (
        patch("sabor.tools.rascunhar_notificacao._pedido_de_triagem_salva", return_value=None),
        patch(
            "sabor.tools.rascunhar_notificacao._chamar_extrair_e_classificar",
            return_value=pedido_extraido,
        ),
        patch("sabor.tools.rascunhar_notificacao.classificar_pedido"),
    ):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-010")

    assert "10.0" not in res.texto
    assert "a confirmar" in res.rascunhos[0].conteudo.lower()
    assert "A combinar" in res.rascunhos[0].conteudo
    assert any(f.arquivo == "pedidos.json" and f.referencia == "PED-2026-010" for f in res.fontes)
    assert any(f.arquivo == "produtores.csv" and f.referencia == "P001" for f in res.fontes)


@pytest.mark.anyio
async def test_matriz_elegiveis_exclui_p017_e_p008_e_sinaliza_divisao_e_inadimplencia():
    pedido_simulado = Pedido(
        pedido_id="PED-2026-020",
        tipo="compra",
        comprador=Comprador(nome="Rede SuperBom"),
        entrega=Entrega(data_texto="próxima sexta"),
        itens=[
            Item(
                produto_texto="Mel silvestre",
                produto="Mel silvestre",
                quantidade=100.0,
                unidade="kg",
                produtores_elegiveis=["P001", "P002"],
            )
        ],
        sinalizacoes=["inadimplencia"],
    )

    nomes_produtores = {
        "P001": ("Produtor 001",),
        "P002": ("Produtor 002",),
    }

    def _exec_side_effect(sql: str, params: tuple | None = None):
        m = MagicMock()
        if "FROM pedidos" in sql:
            m.fetchone.return_value = (
                "PED-2026-020",
                "compra",
                "Rede SuperBom",
                "100 kg de mel silvestre",
            )
        elif "FROM produtores" in sql and params:
            m.fetchone.return_value = nomes_produtores.get(params[0], (params[0],))
        else:
            m.fetchall.return_value = []
        return m

    ctx = _ctx(usuario="Gestor Cooperativa")
    ctx.deps.conn.execute.side_effect = _exec_side_effect

    with (
        patch("sabor.tools.rascunhar_notificacao._pedido_de_triagem_salva", return_value=None),
        patch(
            "sabor.tools.rascunhar_notificacao._chamar_extrair_e_classificar",
            return_value=pedido_simulado,
        ),
        patch("sabor.tools.rascunhar_notificacao.classificar_pedido"),
    ):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-020")

    destinatarios = [r.destinatario for r in res.rascunhos]
    assert any("P001" in d for d in destinatarios)
    assert any("P002" in d for d in destinatarios)
    assert not any("P017" in d for d in destinatarios)
    assert not any("P008" in d for d in destinatarios)

    # Não deve duplicar 100.0 kg cegamente sem indicar divisão/disponibilidade
    for r in res.rascunhos:
        assert "divisão" in r.conteudo.lower() or "disponibilidade" in r.conteudo.lower()
        assert "validação da gestão" in r.conteudo.lower()

    assert "validação da gestão" in res.texto.lower()


@pytest.mark.anyio
async def test_mascara_pii_no_rascunho_e_texto():
    pedido_pii = Pedido(
        pedido_id="PED-2026-030",
        tipo="compra",
        comprador=Comprador(nome="João Silva CPF 123.456.789-01 tel (81) 99876-5432 joao@exemplo.com"),
        entrega=Entrega(data_texto="amanhã"),
        itens=[
            Item(
                produto_texto="Banana comprida",
                produto="Banana comprida",
                quantidade=30.0,
                unidade="kg",
                produtores_elegiveis=["P003"],
            )
        ],
        sinalizacoes=[],
    )

    ctx = _ctx(usuario="Operador", mascarador=_MascaradorTeste())
    ctx.deps.conn.execute.return_value.fetchone.side_effect = [
        (
            "PED-2026-030",
            "compra",
            "João Silva CPF 123.456.789-01 tel (81) 99876-5432 joao@exemplo.com",
            "30 kg de banana comprida",
        ),
        ("Produtor P003",),
    ]

    with (
        patch("sabor.tools.rascunhar_notificacao._pedido_de_triagem_salva", return_value=None),
        patch(
            "sabor.tools.rascunhar_notificacao._chamar_extrair_e_classificar",
            return_value=pedido_pii,
        ),
        patch("sabor.tools.rascunhar_notificacao.classificar_pedido"),
    ):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-030")

    conteudo = res.rascunhos[0].conteudo
    assert "123.456.789-01" not in conteudo
    assert "99876-5432" not in conteudo
    assert "joao@exemplo.com" not in conteudo
    assert "[CPF]" in conteudo
    assert "[TEL]" in conteudo
    assert "[EMAIL]" in conteudo