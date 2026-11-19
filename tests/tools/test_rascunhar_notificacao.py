from __future__ import annotations

from datetime import date, datetime
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sabor.agent.mascarador import Mascarador
from sabor.tools.base import SemPermissao, Usuario
from sabor.tools.pedidos import Comprador, Entrega, Item, Pedido
from sabor.tools.rascunhar_notificacao import rascunhar_notificacao


class _CursorFalso:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None):
        self._rows = list(rows or [])

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _ConexaoPsycopgFalsa:
    """Conexão fake com `__module__ = 'psycopg.falsa'` para exercitar `classificar_pedido` real."""

    __module__ = "psycopg.falsa"

    def __init__(
        self,
        *,
        pedidos_rows: dict[str, tuple[Any, ...]] | None = None,
        triagem_rows: dict[str, tuple[Any, ...]] | None = None,
        produtos_catalogo: dict[str, tuple[int, str, bool]] | None = None,
        produtores_por_produto: dict[int, list[tuple[Any, ...]]] | None = None,
        nomes_produtores: dict[str, str] | None = None,
        financeiro_vencido: list[tuple[Any, ...]] | None = None,
        falhar_triagem: bool = False,
    ) -> None:
        self.autocommit = False
        self.rollbacks = 0
        self.sqls_executados: list[str] = []
        self._pedidos = pedidos_rows or {}
        self._triagem = triagem_rows or {}
        self._produtos = produtos_catalogo or {}
        self._produtores_por_produto = produtores_por_produto or {}
        self._nomes = nomes_produtores or {}
        self._financeiro = financeiro_vencido or []
        self._falhar_triagem = falhar_triagem
        self._abortada = False

    def rollback(self) -> None:
        self.rollbacks += 1
        self._abortada = False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _CursorFalso:
        if self._abortada:
            raise RuntimeError("InFailedSqlTransaction: transação abortada sem rollback")

        self.sqls_executados.append(sql)
        sql_norm = " ".join(sql.split()).lower()
        params = params or ()

        if "from triagem_pedidos" in sql_norm:
            if "order by id" in sql_norm:
                self._abortada = True
                raise RuntimeError("UndefinedColumn: column 'id' does not exist")
            if self._falhar_triagem:
                self._abortada = True
                raise RuntimeError("erro simulado em triagem_pedidos")
            pid = params[0] if params else ""
            row = self._triagem.get(pid)
            return _CursorFalso([row] if row else [])

        if "from pedidos" in sql_norm and "where id =" in sql_norm:
            pid = params[0] if params else ""
            row = self._pedidos.get(pid)
            return _CursorFalso([row] if row else [])

        if "from pedidos" in sql_norm and "status = 'pendente'" in sql_norm:
            return _CursorFalso([])

        if "from produtos where lower(nome)" in sql_norm:
            chave = str(params[0]).lower()
            if chave in self._produtos:
                return _CursorFalso([(self._produtos[chave][0],)])
            return _CursorFalso([])

        if "from produto_sinonimos" in sql_norm:
            return _CursorFalso([])

        if "select nome from produtos where id =" in sql_norm:
            prod_id = params[0]
            for pid, nome, _orig in self._produtos.values():
                if pid == prod_id:
                    return _CursorFalso([(nome,)])
            return _CursorFalso([])

        if "from produtor_produtos" in sql_norm:
            prod_id = params[0]
            return _CursorFalso(self._produtores_por_produto.get(prod_id, []))

        if "select nome_completo from produtores where id =" in sql_norm:
            pid = params[0]
            nome = self._nomes.get(pid)
            return _CursorFalso([(nome,)] if nome else [])

        if "from produtores" in sql_norm:
            if "capacidade_" in sql_norm:
                self._abortada = True
                raise RuntimeError("UndefinedColumn: coluna de capacidade não existe em produtores")
            return _CursorFalso([])

        if "from financeiro" in sql_norm and "status_pagamento = 'pendente'" in sql_norm:
            return _CursorFalso(self._financeiro)

        return _CursorFalso([])


def _ctx(
    *,
    conn: Any = None,
    usuario: Any = "Ana Equipe",
    mascarador: Any | None = None,
    data_referencia: date = date(2026, 10, 30),
) -> SimpleNamespace:
    deps = SimpleNamespace(
        conn=conn if conn is not None else MagicMock(),
        data_referencia=data_referencia,
        usuario=usuario,
        mascarador=mascarador,
    )
    return SimpleNamespace(deps=deps)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "usuario",
    ["P001", "p008", Usuario(perfil="produtor", produtor_id="P001")],
)
async def test_produtor_recebe_sem_permissao(usuario: Any):
    ctx = _ctx(usuario=usuario)
    with pytest.raises(SemPermissao):
        await rascunhar_notificacao(ctx, pedido_id="PED-2026-004")


@pytest.mark.anyio
async def test_usuario_none_nao_quebra_e_atua_como_equipe():
    pedido = Pedido(
        pedido_id="PED-2026-011",
        tipo="compra",
        comprador=Comprador(nome="Mercado Central"),
        entrega=Entrega(data=date(2026, 11, 5), data_texto=None),
        itens=[
            Item(
                produto_texto="Banana Prata",
                produto="Banana Prata",
                quantidade=20.0,
                unidade="kg",
                produtores_elegiveis=["P001"],
            )
        ],
    )
    conn = _ConexaoPsycopgFalsa(
        pedidos_rows={
            "PED-2026-011": (
                "PED-2026-011",
                "compra",
                "Mercado Central",
                "20 kg de banana prata",
                datetime(2026, 10, 28, 9, 0),
            )
        },
        nomes_produtores={"P001": "Maria Silva"},
    )
    ctx = _ctx(conn=conn, usuario=None)

    with patch(
        "sabor.tools.rascunhar_notificacao.extrair_e_classificar",
        new=AsyncMock(return_value=pedido),
    ):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-011")

    assert len(res.rascunhos) == 1
    assert "05/11/2026" in res.rascunhos[0].conteudo
    assert "28/10/2026" not in res.rascunhos[0].conteudo


@pytest.mark.anyio
async def test_reaproveita_triagem_salva_sem_inventar_quantidade_ou_data_e_sem_sql_invalido():
    pedido_salvo = Pedido(
        pedido_id="PED-2026-010",
        tipo="compra",
        comprador=Comprador(nome="Mercado Central"),
        entrega=Entrega(data=None, data_texto=None),
        itens=[
            Item(
                produto_texto="Queijo coalho",
                produto="Queijo coalho",
                quantidade=None,
                unidade=None,
                produtores_elegiveis=["P001"],
            )
        ],
        sinalizacoes=[],
    )
    conn = _ConexaoPsycopgFalsa(
        pedidos_rows={
            "PED-2026-010": (
                "PED-2026-010",
                "compra",
                "Mercado Central",
                "Quero queijo coalho",
                datetime(2026, 10, 27, 14, 30),
            )
        },
        triagem_rows={"PED-2026-010": (json.dumps(pedido_salvo.model_dump(mode="json")),)},
        produtos_catalogo={"queijo coalho": (1, "Queijo coalho", True)},
        produtores_por_produto={
            1: [
                ("P001", "ativo", False, None, "PE-SIE-0412", "CAF-001", True, "Queijo coalho"),
            ]
        },
        nomes_produtores={"P001": "Maria Silva"},
    )
    ctx = _ctx(conn=conn, usuario="Carla Operadora")
    mock_extrair = AsyncMock()

    with patch("sabor.tools.rascunhar_notificacao.extrair_e_classificar", new=mock_extrair):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-010")

    mock_extrair.assert_not_awaited()
    assert not any("order by id" in s.lower() for s in conn.sqls_executados)
    assert not any("capacidade_" in s.lower() for s in conn.sqls_executados)
    assert "10.0" not in res.texto
    assert "10 kg" not in res.texto.lower()
    assert "quantidade a confirmar" in res.rascunhos[0].conteudo.lower()
    assert "A combinar" in res.rascunhos[0].conteudo
    assert "27/10/2026" not in res.rascunhos[0].conteudo
    assert any(f.arquivo == "pedidos.json" and f.referencia == "PED-2026-010" for f in res.fontes)
    assert any(f.arquivo == "produtores.csv" and f.referencia == "P001" for f in res.fontes)


@pytest.mark.anyio
async def test_erro_em_triagem_faz_rollback_e_chama_extrair_e_classificar_async():
    pedido_extraido = Pedido(
        pedido_id="PED-2026-012",
        tipo="compra",
        comprador=Comprador(nome="Armazém do Campo"),
        entrega=Entrega(data_texto="sexta-feira"),
        itens=[
            Item(
                produto_texto="Banana Prata",
                produto="Banana Prata",
                quantidade=40.0,
                unidade="kg",
                produtores_elegiveis=["P003"],
            )
        ],
    )
    conn = _ConexaoPsycopgFalsa(
        pedidos_rows={
            "PED-2026-012": (
                "PED-2026-012",
                "compra",
                "Armazém do Campo",
                "Preciso de 40 kg de banana prata para sexta-feira",
                datetime(2026, 10, 29, 8, 0),
            )
        },
        nomes_produtores={"P003": "João Produtor"},
        falhar_triagem=True,
    )
    ctx = _ctx(conn=conn, usuario="Dona Lúcia")
    mock_extrair = AsyncMock(return_value=pedido_extraido)

    with patch("sabor.tools.rascunhar_notificacao.extrair_e_classificar", new=mock_extrair):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-012")

    assert conn.rollbacks == 1
    mock_extrair.assert_awaited_once_with(ctx.deps, pedido_id="PED-2026-012")
    assert len(res.rascunhos) == 1
    assert "João Produtor (P003)" == res.rascunhos[0].destinatario


@pytest.mark.anyio
async def test_texto_avulso_chama_extrair_e_classificar_com_texto():
    pedido_avulso = Pedido(
        pedido_id=None,
        tipo="compra",
        comprador=Comprador(nome="Cliente Novo"),
        entrega=Entrega(data_texto="amanhã"),
        itens=[
            Item(
                produto_texto="Mel silvestre",
                produto="Mel silvestre",
                quantidade=15.0,
                unidade="kg",
                produtores_elegiveis=["P002"],
            )
        ],
    )
    conn = _ConexaoPsycopgFalsa(nomes_produtores={"P002": "José Santos"})
    ctx = _ctx(conn=conn, usuario="Dona Lúcia")
    mock_extrair = AsyncMock(return_value=pedido_avulso)

    texto_novo = "Quero 15 kg de mel silvestre para amanhã"
    with patch("sabor.tools.rascunhar_notificacao.extrair_e_classificar", new=mock_extrair):
        res = await rascunhar_notificacao(ctx, texto_pedido=texto_novo)

    mock_extrair.assert_awaited_once_with(ctx.deps, texto=texto_novo)
    assert len(res.rascunhos) == 1
    assert "P002" in res.rascunhos[0].destinatario


@pytest.mark.anyio
async def test_matriz_real_exclui_p017_sem_sie_e_sinaliza_divisao_e_inadimplencia():
    """Exercita `classificar_pedido`: exclui P017 sem SIE e sinaliza divisão/inadimplência."""
    pedido_salvo = Pedido(
        pedido_id="PED-2026-006",
        tipo="compra",
        comprador=Comprador(nome="Rede SuperBom Atacado"),
        entrega=Entrega(data=date(2026, 10, 29), data_texto="29/10/2026"),
        itens=[
            Item(
                produto_texto="queijo coalho",
                produto="Queijo coalho",
                quantidade=200.0,
                unidade="kg",
                produtores_elegiveis=[],
            )
        ],
        sinalizacoes=[],
    )
    conn = _ConexaoPsycopgFalsa(
        pedidos_rows={
            "PED-2026-006": (
                "PED-2026-006",
                "compra",
                "Rede SuperBom Atacado",
                "Precisamos de 200 kg de queijo coalho para 29/10/2026",
                datetime(2026, 10, 28, 10, 0),
            )
        },
        triagem_rows={"PED-2026-006": (json.dumps(pedido_salvo.model_dump(mode="json")),)},
        produtos_catalogo={"queijo coalho": (10, "Queijo coalho", True)},
        produtores_por_produto={
            10: [
                (
                    "P001", "ativo", True, date(2027, 6, 30),
                    "PE-SIE-0412", "CAF-001", True, "Queijo coalho",
                ),
                (
                    "P006", "ativo", False, None,
                    "PE-SIE-0756", "CAF-006", True, "Queijo coalho",
                ),
                (
                    "P017", "ativo", False, None,
                    None, "CAF-017", True, "Queijo coalho",
                ),
            ]
        },
        nomes_produtores={
            "P001": "Produtor 001",
            "P006": "Produtor 006",
            "P017": "Produtor 017 Sem SIE",
        },
        financeiro_vencido=[
            ("TX-001", "Rede SuperBom Atacado", "Rede SuperBom", 4500.0),
        ],
    )
    ctx = _ctx(conn=conn, usuario="Gestor Cooperativa")

    res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-006")

    destinatarios = [r.destinatario for r in res.rascunhos]
    assert any("P001" in d for d in destinatarios)
    assert any("P006" in d for d in destinatarios)
    assert not any("P017" in d for d in destinatarios)

    tipos_flags = {f.tipo for f in res.flags}
    assert "inadimplencia" in tipos_flags
    assert "conformidade" in tipos_flags

    for r in res.rascunhos:
        assert "divisão" in r.conteudo.lower()
        assert "quantidade a confirmar" in r.conteudo.lower()
        assert "validação da gestão" in r.conteudo.lower()

    assert "validação da gestão" in res.texto.lower()


@pytest.mark.anyio
async def test_matriz_real_classificar_pedido_exclui_p008_organico_vencido():
    """Exercita `classificar_pedido`: exclui P008 (orgânico vencido) e mantém P002/P004."""
    pedido_salvo = Pedido(
        pedido_id="PED-2026-004",
        tipo="compra",
        comprador=Comprador(nome="Prefeitura de Garanhuns (PNAE)"),
        entrega=Entrega(data=date(2026, 11, 4), data_texto="04/11/2026"),
        itens=[
            Item(
                produto_texto="mel silvestre orgânico",
                produto="Mel silvestre",
                quantidade=20.0,
                unidade="kg",
                organico=True,
                produtores_elegiveis=[],
            )
        ],
        sinalizacoes=[],
    )
    conn = _ConexaoPsycopgFalsa(
        pedidos_rows={
            "PED-2026-004": (
                "PED-2026-004",
                "compra",
                "Prefeitura de Garanhuns (PNAE)",
                "20 kg de mel silvestre orgânico para 04/11/2026",
                datetime(2026, 10, 28, 11, 0),
            )
        },
        triagem_rows={"PED-2026-004": (json.dumps(pedido_salvo.model_dump(mode="json")),)},
        produtos_catalogo={"mel silvestre": (20, "Mel silvestre", True)},
        produtores_por_produto={
            20: [
                (
                    "P002", "ativo", True, date(2027, 6, 30),
                    "PE-SIE-0102", "CAF-002", True, "Mel silvestre",
                ),
                (
                    "P004", "ativo", True, date(2027, 3, 15),
                    "PE-SIE-0104", "CAF-004", True, "Mel silvestre",
                ),
                (
                    "P008", "ativo", True, date(2026, 8, 15),
                    "PE-SIE-0108", "CAF-008", True, "Mel silvestre",
                ),
            ]
        },
        nomes_produtores={
            "P002": "José Santos",
            "P004": "Antônio Lima",
            "P008": "Produtor 008 Vencido",
        },
    )
    ctx = _ctx(conn=conn, usuario="Dona Lúcia")

    res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-004")

    destinatarios = [r.destinatario for r in res.rascunhos]
    assert any("P002" in d for d in destinatarios)
    assert any("P004" in d for d in destinatarios)
    assert not any("P008" in d for d in destinatarios)
    assert any(f.tipo == "conformidade" for f in res.flags)


@pytest.mark.anyio
async def test_mascara_pii_com_mascarador_real_no_rascunho_e_texto():
    pedido_pii = Pedido(
        pedido_id="PED-2026-030",
        tipo="compra",
        comprador=Comprador(
            nome="João Silva CPF 123.456.789-01 tel (81) 99876-5432 joao@exemplo.com"
        ),
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
    conn = _ConexaoPsycopgFalsa(
        pedidos_rows={
            "PED-2026-030": (
                "PED-2026-030",
                "compra",
                "João Silva CPF 123.456.789-01 tel (81) 99876-5432 joao@exemplo.com",
                "30 kg de banana comprida",
                datetime(2026, 10, 29, 9, 0),
            )
        },
        nomes_produtores={"P003": "Produtor P003"},
    )
    ctx = _ctx(conn=conn, usuario="Operador", mascarador=Mascarador())

    with patch(
        "sabor.tools.rascunhar_notificacao.extrair_e_classificar",
        new=AsyncMock(return_value=pedido_pii),
    ):
        res = await rascunhar_notificacao(ctx, pedido_id="PED-2026-030")

    conteudo = res.rascunhos[0].conteudo
    for texto in (conteudo, res.texto):
        assert "123.456.789-01" not in texto
        assert "99876-5432" not in texto
        assert "joao@exemplo.com" not in texto
    assert "[CPF_" in conteudo
    assert "[TEL_" in conteudo
    assert "[EMAIL_" in conteudo


@pytest.mark.anyio
async def test_integracao_postgres_matriz_exclui_p008_e_p017(conn):
    """Integração real com Postgres: valida SQL, `classificar_pedido` e exclusão de P008 e P017."""
    ctx = _ctx(conn=conn, usuario="Dona Lúcia", mascarador=Mascarador())

    pedido_mel_org = Pedido(
        pedido_id="PED-2026-004",
        tipo="compra",
        comprador=Comprador(nome="Prefeitura de Garanhuns (PNAE)"),
        entrega=Entrega(data=date(2026, 11, 4), data_texto="04/11/2026"),
        itens=[
            Item(
                produto_texto="mel silvestre orgânico",
                produto="Mel silvestre",
                quantidade=20.0,
                unidade="kg",
                organico=True,
            )
        ],
    )
    with patch(
        "sabor.tools.rascunhar_notificacao._pedido_de_triagem_salva",
        return_value=pedido_mel_org,
    ):
        res_004 = await rascunhar_notificacao(ctx, pedido_id="PED-2026-004")

    dests_004 = [r.destinatario for r in res_004.rascunhos]
    assert dests_004
    assert not any("P008" in d for d in dests_004)
    assert any(
        f.arquivo == "pedidos.json" and f.referencia == "PED-2026-004"
        for f in res_004.fontes
    )

    pedido_queijo = Pedido(
        pedido_id="PED-2026-006",
        tipo="compra",
        comprador=Comprador(nome="Rede SuperBom Atacado"),
        entrega=Entrega(data=date(2026, 10, 29), data_texto="29/10/2026"),
        itens=[
            Item(
                produto_texto="queijo coalho",
                produto="Queijo coalho",
                quantidade=200.0,
                unidade="kg",
            )
        ],
    )
    with patch(
        "sabor.tools.rascunhar_notificacao._pedido_de_triagem_salva",
        return_value=pedido_queijo,
    ):
        res_006 = await rascunhar_notificacao(ctx, pedido_id="PED-2026-006")

    dests_006 = [r.destinatario for r in res_006.rascunhos]
    assert dests_006
    assert not any("P017" in d for d in dests_006)
    assert any(f.tipo == "inadimplencia" for f in res_006.flags)
    assert "validação da gestão" in res_006.texto.lower()
