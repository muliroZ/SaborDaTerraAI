"""Ferramenta `rascunhar_notificacao` (Issue #12): sugere propostas de alocação de pedidos
aos produtores elegíveis e redige rascunhos de notificação sem envio automático.
"""

from __future__ import annotations

from datetime import date
import inspect
import json
from typing import TYPE_CHECKING, Any

from sabor.agent.schemas import Flag, Fonte, Rascunho, RespostaAgente
from sabor.tools.base import SemPermissao, _resolver_usuario
from sabor.tools.pedidos import Comprador, Entrega, Item, Pedido, extrair_e_classificar
from sabor.tools.regras import classificar_pedido

if TYPE_CHECKING:
    import psycopg


def _mascarar_se_possivel(ctx: Any, texto: str) -> str:
    """Aplica `ctx.deps.mascarador.mascarar(texto)` quando disponível."""
    if not texto:
        return texto
    deps = getattr(ctx, "deps", None)
    mascarador = getattr(deps, "mascarador", None)
    if mascarador is not None and callable(getattr(mascarador, "mascarar", None)):
        return mascarador.mascarar(texto)
    return texto


def _formatar_quantidade_item(item: Item) -> str:
    """Formata quantidade e unidade sem inventar número quando ausente."""
    qtd = getattr(item, "quantidade", None)
    unidade = getattr(item, "unidade", None)
    if qtd is None:
        return "quantidade a confirmar"
    if isinstance(qtd, float) and qtd.is_integer():
        qtd_str = str(int(qtd))
    else:
        qtd_str = str(qtd)
    if unidade:
        return f"{qtd_str} {unidade}"
    return qtd_str


def _chamar_extrair_e_classificar(
    ctx: Any,
    mensagem_original: str,
    tipo_pedido: str,
    comprador_nome: str,
    data_ref: date,
) -> Pedido | None:
    """Reaproveita `extrair_e_classificar` de `sabor.tools.pedidos` com adaptação segura."""
    if not mensagem_original.strip():
        return None
    try:
        sig = inspect.signature(extrair_e_classificar)
        params = sig.parameters
        kwargs: dict[str, Any] = {}
        if "ctx" in params:
            kwargs["ctx"] = ctx
        if "mensagem" in params:
            kwargs["mensagem"] = mensagem_original
        elif "texto" in params:
            kwargs["texto"] = mensagem_original
        elif "mensagem_original" in params:
            kwargs["mensagem_original"] = mensagem_original
        if "tipo" in params:
            kwargs["tipo"] = tipo_pedido
        if "comprador" in params:
            kwargs["comprador"] = comprador_nome
        if "data_referencia" in params:
            kwargs["data_referencia"] = data_ref
        elif "data_ref" in params:
            kwargs["data_ref"] = data_ref

        resultado = extrair_e_classificar(**kwargs)
        if inspect.isawaitable(resultado):
            return None
        if isinstance(resultado, Pedido):
            return resultado
        if isinstance(resultado, dict):
            return Pedido.model_validate(resultado)
    except Exception:
        return None
    return None


def _pedido_de_triagem_salva(conn: Any, pedido_id: str, data_ref: date) -> Pedido | None:
    """Busca resultado pré-classificado em `triagem_pedidos` quando `status = 'ok'`."""
    if not conn or not pedido_id:
        return None
    try:
        row = conn.execute(
            """
            SELECT resultado
            FROM triagem_pedidos
            WHERE pedido_id = %s AND status = 'ok'
            ORDER BY id DESC
            LIMIT 1
            """,
            (pedido_id,),
        ).fetchone()
    except Exception:
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


def _capacidades_produtores(conn: Any) -> dict[str, float | None]:
    """Lê capacidade semanal/produtiva dos produtores quando a coluna existir."""
    if not conn:
        return {}
    for col in ("capacidade_semanal_kg", "capacidade_kg", "capacidade_semanal"):
        try:
            rows = conn.execute(f"SELECT id, {col} FROM produtores").fetchall()
            return {
                str(pid): (float(cap) if cap is not None else None)
                for pid, cap in rows
            }
        except Exception:
            continue
    return {}


async def rascunhar_notificacao(
    ctx: Any,
    *,
    pedido_id: str | None = None,
    texto_pedido: str | None = None,
) -> RespostaAgente:
    """Gera propostas de alocação de itens de pedidos aos produtores elegíveis e redige
    rascunhos de notificação formatados para comunicação operacional (sistema read-only, sem envio automático).

    Parâmetros:
- pedido_id: Identificador opcional do pedido já registrado na base (ex: 'PED-2026-004').
- texto_pedido: Texto bruto opcional de um novo pedido caso ele não esteja gravado na base de dados.

    Quando usar:
- Use esta ferramenta SEMPRE que o operador solicitar o rascunho de mensagens, notificações ou propostas de distribuição de um pedido entre os produtores aptos que atendem aos critérios de safra, capacidade e conformidade sanitária.
    """
    usuario_raw = getattr(ctx.deps, "usuario", None)
    usuario_obj = _resolver_usuario(usuario_raw)
    if usuario_obj.perfil == "produtor":
        raise SemPermissao(
            "Apenas membros da equipe podem consultar pedidos e rascunhar notificações para produtores."
        )

    conn: psycopg.Connection = ctx.deps.conn
    data_ref: date = getattr(ctx.deps, "data_referencia", None) or date(2026, 10, 30)

    mensagem_original = (texto_pedido or "").strip()
    tipo_pedido = "compra"
    comprador_nome = "Comprador Geral"
    data_entrega_txt: str | None = None
    fontes: list[Fonte] = []

    if pedido_id and conn:
        row = conn.execute(
            "SELECT id, tipo, comprador, mensagem_original FROM pedidos WHERE id = %s",
            (pedido_id,),
        ).fetchone()
        if row and isinstance(row, (tuple, list)) and len(row) >= 4:
            p_id, p_tipo, p_comp, p_msg = row[:4]
            pedido_id = p_id
            tipo_pedido = p_tipo or "compra"
            comprador_nome = p_comp or "Comprador Geral"
            if p_msg and not mensagem_original:
                mensagem_original = p_msg
            fontes.append(Fonte(arquivo="pedidos.json", referencia=str(p_id)))
            
    if not mensagem_original and not pedido_id:
        return RespostaAgente(
            texto=(
                "Não foi possível identificar o pedido ou a mensagem para rascunhar notificações. "
                "Forneça um ID válido (ex: PED-2026-004) ou o texto do pedido."
            ),
            fontes=fontes,
        )

    pedido: Pedido | None = None
    if pedido_id:
        pedido = _pedido_de_triagem_salva(conn, pedido_id, data_ref)

    if pedido is None:
        pedido = _chamar_extrair_e_classificar(
            ctx=ctx,
            mensagem_original=mensagem_original,
            tipo_pedido=tipo_pedido,
            comprador_nome=comprador_nome,
            data_ref=data_ref,
        )

    if pedido is None:
        pedido = Pedido(
            pedido_id=pedido_id or "PED-CUSTOM",
            tipo=tipo_pedido,
            comprador=Comprador(nome=comprador_nome),
            entrega=Entrega(data_texto=data_entrega_txt),
            itens=[],
        )
    else:
        if pedido_id and not getattr(pedido, "pedido_id", None):
            pedido.pedido_id = pedido_id
        if not getattr(pedido, "comprador", None) or not pedido.comprador.nome:
            pedido.comprador = Comprador(nome=comprador_nome)
        if getattr(pedido, "entrega", None) is None:
            pedido.entrega = Entrega(data_texto=data_entrega_txt)

    # Aplica o motor determinístico oficial (Issue #10)
    classificar_pedido(
        pedido,
        conn=conn,
        mensagem_original=mensagem_original,
        data_referencia=data_ref,
    )

    sinalizacoes = list(getattr(pedido, "sinalizacoes", None) or [])
    tem_inadimplencia = any("inadimpl" in s.lower() for s in sinalizacoes)
    flags_obj = [
        Flag(tipo=s, descricao=f"Sinalização regulatória/operacional: {s}")
        for s in sinalizacoes
    ]

    capacidades = _capacidades_produtores(conn)
    alocacoes_por_produtor: dict[str, list[dict[str, Any]]] = {}

    for item in pedido.itens:
        if getattr(item, "ambiguo", False):
            continue
        elegiveis = list(getattr(item, "produtores_elegiveis", None) or [])
        num_elegiveis = len(elegiveis)
        divisao_ativa = num_elegiveis > 1
        qtd_total = getattr(item, "quantidade", None)
        unidade_item = getattr(item, "unidade", None)

        for pid in elegiveis:
            cap_prod = capacidades.get(pid)
            qtd_alocada: float | None = None
            obs_divisao: str | None = None

            if qtd_total is not None:
                if divisao_ativa:
                    if cap_prod is not None and cap_prod > 0:
                        qtd_alocada = min(float(qtd_total), cap_prod)
                        obs_divisao = (
                            f"divisão entre {num_elegiveis} produtores elegíveis "
                            f"(capacidade cadastrada: {cap_prod} kg; consultar disponibilidade)"
                        )
                    else:
                        qtd_alocada = None
                        obs_divisao = (
                            f"divisão entre {num_elegiveis} produtores elegíveis "
                            f"(volume total do pedido: {qtd_total} {unidade_item or 'un'}; "
                            "consultar disponibilidade/capacidade)"
                        )
                else:
                    if cap_prod is not None and cap_prod > 0 and float(qtd_total) > cap_prod:
                        qtd_alocada = cap_prod
                        obs_divisao = (
                            f"limitado à capacidade cadastrada ({cap_prod} kg) de {qtd_total} {unidade_item or 'un'} solicitados"
                        )
                    else:
                        qtd_alocada = float(qtd_total)

            alocacoes_por_produtor.setdefault(pid, []).append(
                {
                    "produto": item.produto or item.produto_texto,
                    "quantidade": qtd_alocada,
                    "unidade": unidade_item,
                    "divisao": divisao_ativa,
                    "obs": obs_divisao,
                }
            )

    if not alocacoes_por_produtor:
        comprador_seguro = _mascarar_se_possivel(ctx, pedido.comprador.nome)
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
            texto=msg_sem_elegiveis,
            flags=flags_obj,
            fontes=fontes,
        )

    comprador_seguro = _mascarar_se_possivel(ctx, pedido.comprador.nome)
    entrega_txt = (
        pedido.entrega.data_texto
        if getattr(pedido, "entrega", None) and pedido.entrega.data_texto
        else "A combinar"
    )
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
        for idx_item in itens_alocados:
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

        itens_txt = "; ".join(partes_item)
        alerta_gestao_txt = (
            "\n- Observação: Pedido possui sinalização de inadimplência; "
            "sujeito à validação da gestão antes de confirmar alocação."
            if tem_inadimplencia
            else ""
        )

        conteudo_bruto = (
            f"Olá {p_nome} ({pid}), temos uma consulta/proposta de alocação (divisão/disponibilidade):\n"
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

    return RespostaAgente(
        texto=texto_final,
        flags=flags_obj,
        rascunhos=rascunhos,
        fontes=fontes_unicas,
    )