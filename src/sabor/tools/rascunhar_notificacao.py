"""Ferramenta `rascunhar_notificacao` (Issue #12): sugere propostas de alocação de pedidos
aos produtores elegíveis e redige rascunhos de notificação sem envio automático.
"""

from __future__ import annotations

from datetime import date, datetime
import inspect
import json
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps
from sabor.agent.schemas import Flag, Fonte, Rascunho, RespostaAgente
from sabor.tools.base import SemPermissao, _resolver_usuario, registrar_rascunhos
from sabor.tools.pedidos import Comprador, Entrega, Item, Pedido, extrair_e_classificar
from sabor.tools.regras import (
    _conn_eh_real,
    _consultar_capacidade_semanal_chunks,
    classificar_pedido,
)

if TYPE_CHECKING:
    import psycopg


def _rollback_se_necessario(conn: Any) -> None:
    """Desfaz transação abortada em conexões com `autocommit=False`."""
    if conn is None:
        return
    if getattr(conn, "autocommit", True) is False and callable(getattr(conn, "rollback", None)):
        try:
            conn.rollback()
        except Exception:
            pass


def _mascarar_se_possivel(ctx: Any, texto: str) -> str:
    """Aplica `ctx.deps.mascarador.mascarar(texto)` quando disponível."""
    if not texto:
        return texto
    deps = getattr(ctx, "deps", None)
    mascarador = getattr(deps, "mascarador", None)
    if mascarador is not None and callable(getattr(mascarador, "mascarar", None)):
        return mascarador.mascarar(texto)
    return texto


def _formatar_numero(valor: float) -> str:
    """Formata número evitando sufixo `.0` em inteiros."""
    return str(int(valor)) if isinstance(valor, float) and valor.is_integer() else str(valor)


def _formatar_quantidade_item(item: Item) -> str:
    """Formata quantidade e unidade sem inventar número quando ausente."""
    qtd = getattr(item, "quantidade", None)
    unidade = getattr(item, "unidade", None)
    if qtd is None:
        return "quantidade a confirmar"
    qtd_str = _formatar_numero(float(qtd))
    if unidade:
        return f"{qtd_str} {unidade}"
    return qtd_str


def _formatar_entrega(pedido: Pedido) -> str:
    """Formata prazo de entrega sem nunca usar `data_recebimento` como substituto."""
    entrega = getattr(pedido, "entrega", None)
    if entrega and entrega.data_texto:
        return entrega.data_texto
    if entrega and entrega.data:
        return entrega.data.strftime("%d/%m/%Y")
    return "A combinar"


def _pedido_de_triagem_salva(conn: Any, pedido_id: str) -> Pedido | None:
    """Busca resultado pré-classificado em `triagem_pedidos` quando `status = 'ok'`."""
    if not conn or not pedido_id:
        return None
    try:
        row = conn.execute(
            """
            SELECT resultado
            FROM triagem_pedidos
            WHERE pedido_id = %s AND status = 'ok'
            """,
            (pedido_id,),
        ).fetchone()
    except Exception:
        _rollback_se_necessario(conn)
        return None

    if not row or not row[0]:
        return None

    raw = row[0]
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(payload, dict):
            if isinstance(payload.get("pedido"), dict):
                return Pedido.model_validate(payload["pedido"])
            if "itens" in payload:
                return Pedido.model_validate(payload)
    except Exception:
        return None
    return None


async def _extrair_pedido_oficial(
    ctx: Any,
    *,
    pedido_id: str | None,
    pedido_encontrado_no_banco: bool,
    mensagem_original: str,
) -> Pedido | None:
    """Reaproveita `extrair_e_classificar` (async) de `sabor.tools.pedidos`."""
    deps = getattr(ctx, "deps", None)
    if deps is None:
        return None

    try:
        if pedido_id and pedido_encontrado_no_banco:
            resultado = extrair_e_classificar(deps, pedido_id=pedido_id)
        elif mensagem_original.strip():
            resultado = extrair_e_classificar(deps, texto=mensagem_original)
        else:
            return None

        if inspect.isawaitable(resultado):
            resultado = await resultado

        if isinstance(resultado, Pedido):
            return resultado
        if isinstance(resultado, dict):
            return Pedido.model_validate(resultado)
    except Exception:
        _rollback_se_necessario(getattr(deps, "conn", None))
        return None
    return None


async def rascunhar_notificacao(
    ctx: RunContext[AgentDeps],
    *,
    pedido_id: str | None = None,
    texto_pedido: str | None = None,
) -> RespostaAgente:
    """Gera propostas de alocação de itens de pedidos aos produtores elegíveis e redige
    rascunhos de notificação para comunicação operacional (read-only, sem envio automático).

    Parâmetros:
    - pedido_id: ID opcional do pedido já registrado na base (ex: 'PED-2026-004').
    - texto_pedido: Texto bruto opcional de um novo pedido não gravado na base de dados.

    Quando usar:
    - Use esta ferramenta SEMPRE que o operador solicitar o rascunho de mensagens,
      notificações ou propostas de distribuição de um pedido entre produtores aptos
      que atendem aos critérios de safra, capacidade e conformidade sanitária.
    """
    usuario_raw = getattr(ctx.deps, "usuario", None)
    usuario_obj = _resolver_usuario(usuario_raw if usuario_raw is not None else "funcionario")
    if usuario_obj.perfil == "produtor":
        raise SemPermissao(
            "Apenas membros da equipe podem consultar pedidos e rascunhar notificações "
            "para produtores."
        )

    conn: psycopg.Connection = ctx.deps.conn
    data_ref: date = getattr(ctx.deps, "data_referencia", None) or date(2026, 10, 30)

    pedido_id_limpo = pedido_id.strip() if pedido_id else None
    mensagem_original = (texto_pedido or "").strip()
    tipo_pedido = "compra"
    comprador_nome = "Comprador Geral"
    data_recebimento_pedido: date | None = None
    pedido_encontrado_no_banco = False
    fontes: list[Fonte] = []

    if pedido_id_limpo and conn:
        row = conn.execute(
            """
            SELECT id, tipo, comprador, mensagem_original, data_recebimento
            FROM pedidos
            WHERE id = %s
            """,
            (pedido_id_limpo,),
        ).fetchone()
        if row and isinstance(row, (tuple, list)) and len(row) >= 4:
            p_id, p_tipo, p_comp, p_msg = row[:4]
            pedido_id_limpo = str(p_id)
            tipo_pedido = p_tipo or "compra"
            comprador_nome = p_comp or "Comprador Geral"
            if p_msg and not mensagem_original:
                mensagem_original = p_msg
            if len(row) >= 5 and row[4] is not None:
                bruto_rec = row[4]
                if isinstance(bruto_rec, datetime):
                    data_recebimento_pedido = bruto_rec.date()
                elif isinstance(bruto_rec, date):
                    data_recebimento_pedido = bruto_rec
            pedido_encontrado_no_banco = True
            fontes.append(Fonte(arquivo="pedidos.json", referencia=pedido_id_limpo))

    if not mensagem_original and not pedido_encontrado_no_banco:
        if pedido_id_limpo:
            msg_erro = (
                f"Pedido {pedido_id_limpo} não encontrado na base de dados. "
                "Forneça um ID válido (ex: PED-2026-004) ou o texto completo do pedido."
            )
        else:
            msg_erro = (
                "Não foi possível identificar o pedido ou a mensagem para rascunhar notificações. "
                "Forneça um ID válido (ex: PED-2026-004) ou o texto do pedido."
            )
        return RespostaAgente(texto=msg_erro, fontes=fontes)

    pedido: Pedido | None = None
    ja_classificado = False

    if pedido_id_limpo and pedido_encontrado_no_banco:
        pedido = _pedido_de_triagem_salva(conn, pedido_id_limpo)

    if pedido is None:
        pedido = await _extrair_pedido_oficial(
            ctx,
            pedido_id=pedido_id_limpo,
            pedido_encontrado_no_banco=pedido_encontrado_no_banco,
            mensagem_original=mensagem_original,
        )
        ja_classificado = pedido is not None

    if pedido is None:
        pedido = Pedido(
            pedido_id=pedido_id_limpo or "PED-CUSTOM",
            tipo=tipo_pedido,  # type: ignore[arg-type]
            comprador=Comprador(nome=comprador_nome),
            entrega=Entrega(data_texto=None),
            itens=[],
        )
    else:
        if pedido_id_limpo and not getattr(pedido, "pedido_id", None):
            pedido.pedido_id = pedido_id_limpo
        if not getattr(pedido, "comprador", None) or not pedido.comprador.nome:
            pedido.comprador = Comprador(nome=comprador_nome)
        if getattr(pedido, "entrega", None) is None:
            pedido.entrega = Entrega(data_texto=None)

    # Aplica o motor determinístico oficial (Issue #10) sobre o estado atual do banco
    if not ja_classificado:
        elegiveis_previos = [list(item.produtores_elegiveis) for item in pedido.itens]
        classificar_pedido(
            pedido,
            conn=conn,
            mensagem_original=mensagem_original,
            data_referencia=data_recebimento_pedido or data_ref,
            data_global=data_ref,
        )
        if not _conn_eh_real(conn):
            for item, prev in zip(pedido.itens, elegiveis_previos):
                if not item.produtores_elegiveis and prev:
                    item.produtores_elegiveis = prev

    sinalizacoes = list(getattr(pedido, "sinalizacoes", None) or [])
    tem_inadimplencia = any("inadimpl" in s.lower() for s in sinalizacoes)
    flags_obj = [
        Flag(tipo=s, descricao=f"Sinalização regulatória/operacional: {s}")
        for s in sinalizacoes
    ]

    capacidades_produto: dict[str, float | None] = {}
    alocacoes_por_produtor: dict[str, list[dict[str, Any]]] = {}

    for item in pedido.itens:
        if getattr(item, "ambiguo", False):
            continue
        elegiveis = list(getattr(item, "produtores_elegiveis", None) or [])
        if not elegiveis:
            continue

        nome_produto = item.produto or item.produto_texto
        if nome_produto not in capacidades_produto:
            capacidades_produto[nome_produto] = _consultar_capacidade_semanal_chunks(
                conn, nome_produto
            )
        cap_semanal = capacidades_produto[nome_produto]

        num_elegiveis = len(elegiveis)
        divisao_ativa = num_elegiveis > 1
        qtd_total = getattr(item, "quantidade", None)
        unidade_item = getattr(item, "unidade", None)
        unidade_label = unidade_item or "un"

        for pid in elegiveis:
            qtd_alocada: float | None = None
            obs_divisao: str | None = None

            if qtd_total is not None:
                qtd_total_fmt = f"{_formatar_numero(float(qtd_total))} {unidade_label}"
                if divisao_ativa:
                    qtd_alocada = None
                    if cap_semanal is not None and cap_semanal > 0:
                        cap_fmt = _formatar_numero(cap_semanal)
                        obs_divisao = (
                            f"divisão entre {num_elegiveis} produtores elegíveis "
                            f"(volume total do pedido: {qtd_total_fmt}; "
                            f"capacidade semanal de referência: {cap_fmt} {unidade_label}; "
                            "consultar disponibilidade)"
                        )
                    else:
                        obs_divisao = (
                            f"divisão entre {num_elegiveis} produtores elegíveis "
                            f"(volume total do pedido: {qtd_total_fmt}; "
                            "consultar disponibilidade/capacidade)"
                        )
                else:
                    if (
                        cap_semanal is not None
                        and cap_semanal > 0
                        and float(qtd_total) > cap_semanal
                    ):
                        qtd_alocada = cap_semanal
                        cap_fmt = _formatar_numero(cap_semanal)
                        obs_divisao = (
                            "limitado à capacidade semanal de referência "
                            f"({cap_fmt} {unidade_label}) de {qtd_total_fmt} solicitados; "
                            "confirmar disponibilidade"
                        )
                    else:
                        qtd_alocada = float(qtd_total)
            elif divisao_ativa:
                obs_divisao = (
                    f"divisão entre {num_elegiveis} produtores elegíveis "
                    "(consultar disponibilidade)"
                )

            alocacoes_por_produtor.setdefault(pid, []).append(
                {
                    "produto": nome_produto,
                    "quantidade": qtd_alocada,
                    "unidade": unidade_item,
                    "divisao": divisao_ativa,
                    "obs": obs_divisao,
                }
            )

    comprador_exibicao = pedido.comprador.nome or comprador_nome or "Comprador Geral"
    comprador_seguro = _mascarar_se_possivel(ctx, comprador_exibicao)

    if not alocacoes_por_produtor:
        msg_sem_elegiveis = (
            f"O pedido {pedido.pedido_id} ({comprador_seguro}) não possui produtores "
            "elegíveis ativos que atendam aos critérios sanitários e de safra exigidos."
        )
        if tem_inadimplencia:
            msg_sem_elegiveis += (
                " ATENÇÃO: Há sinalização de inadimplência para o comprador; "
                "exige validação da gestão antes de qualquer confirmação."
            )
        return RespostaAgente(
            texto=_mascarar_se_possivel(ctx, msg_sem_elegiveis),
            flags=flags_obj,
            fontes=fontes,
        )

    entrega_txt = _formatar_entrega(pedido)
    entrega_segura = _mascarar_se_possivel(ctx, entrega_txt)

    rascunhos: list[Rascunho] = []
    resumo_partes: list[str] = [
        f"**Proposta de Alocação e Notificações — Pedido {pedido.pedido_id}**\n"
        f"Comprador: {comprador_seguro} | Entrega: {entrega_segura}\n"
    ]

    if tem_inadimplencia:
        resumo_partes.append(
            "**Atenção (Inadimplência):** Pedido com flag de inadimplência; "
            "exige validação da gestão antes de confirmar alocação.\n"
        )

    for pid, itens_alocados in alocacoes_por_produtor.items():
        p_row = (
            conn.execute("SELECT nome_completo FROM produtores WHERE id = %s", (pid,)).fetchone()
            if conn
            else None
        )
        p_nome_raw = p_row[0] if p_row else pid
        p_nome = _mascarar_se_possivel(ctx, str(p_nome_raw))
        fontes.append(Fonte(arquivo="produtores.csv", referencia=str(pid)))

        partes_item: list[str] = []
        tem_divisao_produtor = False
        for idx_item in itens_alocados:
            if idx_item["divisao"]:
                tem_divisao_produtor = True
            item_temp = Item(
                produto_texto=idx_item["produto"],
                produto=idx_item["produto"],
                quantidade=idx_item["quantidade"],
                unidade=idx_item["unidade"],
            )
            qtd_fmt = _formatar_quantidade_item(item_temp)
            desc = f"{qtd_fmt} de {idx_item['produto']}"
            if idx_item["obs"]:
                desc += f" [{idx_item['obs']}]"
            partes_item.append(desc)

        itens_txt = _mascarar_se_possivel(ctx, "; ".join(partes_item))
        alerta_gestao_txt = (
            "\n- Observação: Pedido possui sinalização de inadimplência; "
            "sujeito à validação da gestão antes de confirmar alocação."
            if tem_inadimplencia
            else ""
        )
        cabecalho_alocacao = (
            f"Olá {p_nome} ({pid}), temos uma consulta/proposta de alocação "
            "(divisão/disponibilidade):\n"
            if tem_divisao_produtor
            else f"Olá {p_nome} ({pid}), temos uma proposta de alocação "
            "(consulta de disponibilidade):\n"
        )

        conteudo_bruto = (
            f"{cabecalho_alocacao}"
            f"- Itens: {itens_txt}\n"
            f"- Comprador: {comprador_seguro}\n"
            f"- Prazo/Entrega: {entrega_segura}"
            f"{alerta_gestao_txt}\n"
            "Por favor, informe a disponibilidade para confirmação operacional."
        )
        conteudo_seguro = _mascarar_se_possivel(ctx, conteudo_bruto)
        destinatario_seguro = _mascarar_se_possivel(ctx, f"{p_nome} ({pid})")

        rascunhos.append(
            Rascunho(
                destinatario=destinatario_seguro,
                canal="whatsapp",
                conteudo=conteudo_seguro,
            )
        )
        resumo_partes.append(f"- **{destinatario_seguro}**: {itens_txt}")

    # Deduplica fontes mantendo ordem
    fontes_unicas: list[Fonte] = []
    vistas: set[tuple[str, str]] = set()
    for f in fontes:
        chave = (f.arquivo, f.referencia)
        if chave not in vistas:
            vistas.add(chave)
            fontes_unicas.append(f)

    texto_final = _mascarar_se_possivel(
        ctx,
        "\n".join(resumo_partes)
        + "\n\nOs rascunhos de notificação foram gerados abaixo para revisão da equipe "
        "(sistema read-only, nenhum envio automático foi realizado).",
    )

    registrar_rascunhos(ctx.deps, rascunhos)
    return RespostaAgente(
        texto=texto_final,
        flags=flags_obj,
        rascunhos=rascunhos,
        fontes=fontes_unicas,
    )
