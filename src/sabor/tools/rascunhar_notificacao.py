"""Ferramenta `rascunhar_notificacao` (Issue #12): sugere propostas de alocação de pedidos
aos produtores elegíveis e redige rascunhos de notificação sem envio automático.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any
import re

from sabor.agent.schemas import Flag, Rascunho, RespostaAgente
from sabor.tools.base import Usuario
from sabor.tools.pedidos import Comprador, Entrega, Item, Pedido
from sabor.tools.regras import classificar_pedido

if TYPE_CHECKING:
    import psycopg


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
    conn: psycopg.Connection = ctx.deps.conn
    data_ref: date = ctx.deps.data_referencia

    _usuario_obj: Usuario = getattr(ctx.deps, 'usuario', None) or Usuario(perfil="funcionario")

    mensagem_original = texto_pedido or ""
    tipo_pedido = "compra"
    comprador_nome = "Comprador Geral"
    data_rec_str = str(data_ref)

    if pedido_id and conn:
        row = conn.execute(
            "SELECT id, tipo, comprador, data_recebimento, mensagem_original FROM pedidos WHERE id = %s",
            (pedido_id,),
        ).fetchone()
        if row:
            p_id, p_tipo, p_comp, p_rec, p_msg = row
            pedido_id = p_id
            tipo_pedido = p_tipo or "compra"
            comprador_nome = p_comp or "Comprador Geral"
            data_rec_str = str(p_rec) if p_rec else str(data_ref)
            mensagem_original = p_msg or texto_pedido or ""

    if not mensagem_original and not pedido_id:
        return RespostaAgente(
            texto="Não foi possível identificar o pedido ou a mensagem para rascunhar notificações. Forneça um ID válido (ex: PED-2026-004) ou o texto do pedido.",
        )

    # Identifica itens do pedido cruzando o catálogo de produtos com a mensagem original
    itens_encontrados: list[Item] = []
    if conn:
        produtos_db = conn.execute("SELECT id, nome FROM produtos").fetchall()
        msg_lower = mensagem_original.lower()
        
        for prod_id, prod_nome in produtos_db:
            prod_limpo = prod_nome.lower()
            # Se o nome do produto aparece na mensagem, extrai uma quantidade aproximada se houver padrão numérico próximo
            if prod_limpo in msg_lower or any(p in msg_lower for p in prod_limpo.split() if len(p) > 4):
                # Tenta extrair quantidade na mesma frase (ex: "200kg de banana")
                qtd = 10.0 # Padrão seguro
                unidade = "kg"
                padrao_qtd = re.search(rf'(\d+(?:[.,]\d+)?)\s*(kg|g|un|dz|dúzias?|litros?|l)?\s*(?:de\s*)?{re.escape(prod_limpo)}', msg_lower)
                if not padrao_qtd:
                    padrao_qtd = re.search(rf'{re.escape(prod_limpo)}[^0-9]{{0,20}}(\d+(?:[.,]\d+)?)\s*(kg|g|un|dz|dúzias?|litros?|l)?', msg_lower)
                
                if padrao_qtd:
                    try:
                        g1 = padrao_qtd.group(1).replace(',', '.')
                        qtd = float(g1)
                        if padrao_qtd.lastindex is not None and padrao_qtd.lastindex >= 2 and padrao_qtd.group(2):
                            raw_un = padrao_qtd.group(2).lower()
                            if "dz" in raw_un or "dúzia" in raw_un:
                                unidade = "dúzia"
                            elif "maço" in raw_un or "mac" in raw_un:
                                unidade = "maço"
                            elif "litro" in raw_un or raw_un == "l":
                                unidade = "litro"
                            elif "un" in raw_un:
                                unidade = "unidade"
                            else:
                                unidade = "kg"
                    except Exception:
                        pass

                organico = "organico" in msg_lower or "orgânico" in msg_lower
                itens_encontrados.append(
                    Item(
                        produto_texto=prod_nome,
                        produto=prod_nome,
                        quantidade=qtd,
                        unidade=unidade,  # type: ignore[arg-type]
                        organico=organico,
                    )
                )

    # Se nenhum item foi detectado automaticamente mas temos o ID, cria um item genérico baseado no contexto
    if not itens_encontrados:
        itens_encontrados.append(
            Item(produto_texto="Diversos", produto="Diversos", quantidade=1.0, unidade="unidade")
        )

    pedido = Pedido(
        pedido_id=pedido_id or "PED-CUSTOM",
        tipo=tipo_pedido,
        comprador=Comprador(nome=comprador_nome),
        entrega=Entrega(data_texto=data_rec_str),
        itens=itens_encontrados,
    )

    # Aplica o motor determinístico oficial (Issue #10)
    classificar_pedido(
        pedido,
        conn=conn,
        mensagem_original=mensagem_original,
        data_referencia=data_ref,
    )

    flags_obj = [Flag(tipo=s, descricao=f"Sinalização regulatória/operacional: {s}") for s in pedido.sinalizacoes]

    alocacoes_por_produtor: dict[str, list[dict[str, Any]]] = {}
    for item in pedido.itens:
        if item.ambiguo:
            continue
        for pid in item.produtores_elegiveis:
            if pid not in alocacoes_por_produtor:
                alocacoes_por_produtor[pid] = []
            alocacoes_por_produtor[pid].append({
                "produto": item.produto or item.produto_texto,
                "quantidade": item.quantidade,
                "unidade": item.unidade,
            })

    if not alocacoes_por_produtor:
        return RespostaAgente(
            texto=f"O pedido {pedido.pedido_id} ({pedido.comprador.nome}) não possui produtores elegíveis ativos que atendam aos critérios sanitários e de safra exigidos.",
            flags=flags_obj,
        )

    rascunhos: list[Rascunho] = []
    resumo_partes: list[str] = [
        f"**Proposta de Alocação e Notificações — Pedido {pedido.pedido_id}**\n"
        f"Comprador: {pedido.comprador.nome} | Entrega: {pedido.entrega.data_texto or 'A definir'}\n"
    ]

    for pid, itens_alocados in alocacoes_por_produtor.items():
        p_row = conn.execute("SELECT nome_completo FROM produtores WHERE id = %s", (pid,)).fetchone()
        p_nome = p_row[0] if p_row else pid

        itens_txt = ", ".join(f"{i['quantidade']} {i['unidade']} de {i['produto']}" for i in itens_alocados)
        
        conteudo_notificacao = (
            f"Olá {p_nome} ({pid}), temos uma demanda de alocação para o seu perfil:\n"
            f"- Itens: {itens_txt}\n"
            f"- Comprador: {pedido.comprador.nome}\n"
            f"- Prazo/Entrega: {pedido.entrega.data_texto or 'A combinar'}\n"
            f"Por favor, confirme a disponibilidade de entrega."
        )

        rascunhos.append(
            Rascunho(
                destinatario=f"{p_nome} ({pid})",
                canal="whatsapp",
                conteudo=conteudo_notificacao,
            )
        )
        resumo_partes.append(f"- **{p_nome} ({pid})**: {itens_txt}")

    texto_final = "\n".join(resumo_partes) + "\n\nOs rascunhos de notificação foram gerados abaixo para revisão da equipe (sistema read-only, nenhum envio automático foi realizado)."

    return RespostaAgente(
        texto=texto_final,
        flags=flags_obj,
        rascunhos=rascunhos,
    )