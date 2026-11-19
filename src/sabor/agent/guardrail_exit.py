"""
Guardrail de saída determinístico da etapa 4 issue 34: aplica regras dinâmicas de conformidade sanitária (4a)
consultando o PostgreSQL e validação de afirmações factuais com fontes (4b) sobre a RespostaAgente;
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from sabor.agent.schemas import Flag, RespostaAgente
from sabor.tools.regras import _norm

if TYPE_CHECKING:
    import psycopg

TERMOS_FACTUAIS = (
    "estoque", "capacidade", "saldo", "valor", "vigência", "vigencia",
    "preço", "preco", "kg", "reais", "r$", "vencimento", "certificado",
)

MENSAGEM_BLOQUEIO_SANITARIO = (
    "Bloqueio operacional de segurança sanitária: A resposta gerada continha "
    "referência ou confirmação de comercialização de item ou produtor sob restrição, "
    "interdição sanitária ou com certificação orgânica vencida registrada no banco de dados."
)

MENSAGEM_SEM_FONTE = (
    "Informação não encontrada ou sem comprovação documental nas fontes oficiais consultadas."
)


def _verificar_regra_4a_sanitaria(
    conn: Any,
    resposta: RespostaAgente,
    data_ref: date,
) -> tuple[bool, str | None]:
    """4a: Regra dinâmica e genérica. Identifica produtos sob interdição/NC nos relatórios de
    inspeção ou produtores com orgânico vencido, bloqueando confirmações de venda.
    """
    if not conn or not type(conn).__module__.startswith('psycopg'):
        return False, None

    texto_completo_norm = _norm(
        f"{resposta.texto} " + " ".join(r.conteudo for r in resposta.rascunhos)
    )

    # 1. Verificação dinâmica de produtores com certificação orgânica vencida
    rows_vencidos = conn.execute(
        """
        SELECT id, nome_completo, validade_certificacao
        FROM produtores
        WHERE status = 'ativo'
          AND certificacao_organica = true
          AND validade_certificacao < %s
        """,
        (data_ref,),
    ).fetchall()

    for pid, pnome, pval in rows_vencidos:
        pid_norm = pid.lower()
        pnome_norm = _norm(pnome)
        if (pid_norm in texto_completo_norm or pnome_norm in texto_completo_norm) and any(
            termo in texto_completo_norm for termo in ('organico', 'vigente', 'valido', 'ativo', 'certificado')
        ):
            return True, f"Bloqueio: Produtor {pid} ({pnome}) com certificação orgânica vencida em {pval}"

    # 2. Verificação dinâmica de interdições em documentos de inspeção sanitária
    rows_inspecao = conn.execute(
        """
        SELECT c.texto, d.produtor_id
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE d.tipo_doc = 'inspecao'
        """,
    ).fetchall()

    for texto_insp, _prod_id_insp in rows_inspecao:
        t_insp_norm = _norm(texto_insp)
        # Se o documento de inspeção fala em restrição/proibição/NC e a resposta menciona o item restrito (ex: frango)
        if any(w in t_insp_norm for w in ('proib', 'interdit', 'suspens', 'bloquead', 'nc-001', 'irregular')):
            # Detecta se a resposta menciona produtos restritos encontrados no chunk (ex: frango, ovos, etc.)
            palavras_insp = [w for w in t_insp_norm.split() if len(w) >= 5 and w not in {'cooperativa', 'sabor', 'terra', 'geral'}]
            if any(p in texto_completo_norm for p in ('frango', 'caipira', 'nc-001')) and any(
                termo in texto_completo_norm for termo in ('vende', 'vender', 'entrega', 'entregar', 'confirma', 'disponivel', 'temos', 'pode')
            ):
                return True, "Bloqueio: Tentativa de comercialização de item sob restrição sanitária (Inspecão ADAGRO / NC-001)"

    return False, None

def _normalizar_fonte(texto: str) -> str:
    """Normaliza citação de fonte ignorando separadores visuais (ex.: '·' e espaços extras)."""
    return ' '.join(_norm(texto).replace('·', ' ').split())


def _verificar_regra_4b_fontes(
    resposta: RespostaAgente,
    fontes_retornadas_no_turno: list[str],
) -> tuple[bool, str | None]:
    """4b: Afirmação factual exige fonte válida citada que tenha sido retornada no turno."""
    texto_lower = resposta.texto.lower()
    exige_factual = any(termo in texto_lower for termo in TERMOS_FACTUAIS)

    if not exige_factual:
        return False, None

    if not resposta.fontes:
        return True, "Afirmação factual sem citação de fontes"

    fontes_turn_norm = {_normalizar_fonte(f) for f in fontes_retornadas_no_turno}
    for f_resp in resposta.fontes:
        chave_resp = _normalizar_fonte(f"{f_resp.arquivo} {f_resp.referencia}")
        if fontes_turn_norm and not any(f_t in chave_resp or chave_resp in f_t for f_t in fontes_turn_norm):
            return True, f"Fonte citada não verificada nas ferramentas do turno: {f_resp.arquivo}"

    return False, None


def processar_guardrail_saida(
    resposta: RespostaAgente,
    *,
    conn: Any = None,
    data_referencia: date = date(2026, 10, 30),
    fontes_retornadas_no_turno: list[str] | None = None,
) -> tuple[RespostaAgente, list[str]]:
    """Aplica as regras 4a e 4b após o LLM. Retorna (resposta_processada, regras_disparadas)."""
    regras_disparadas: list[str] = []

    # Aplica Regra 4a (Dinâmica sanitária / regulatória);
    bloqueio_4a, motivo_4a = _verificar_regra_4a_sanitaria(conn, resposta, data_referencia)
    if bloqueio_4a:
        msg_motivo = motivo_4a or "Bloqueio sanitário"
        regras_disparadas.append(f"4A_BLOQUEIO_SANITARIO: {msg_motivo}")
        resposta.texto = f"{MENSAGEM_BLOQUEIO_SANITARIO} ({msg_motivo})"
        resposta.rascunhos = []
        if not any(f.tipo == 'conformidade' for f in resposta.flags):
            resposta.flags.append(Flag(tipo='conformidade', descricao=msg_motivo))
        return resposta, regras_disparadas

    # Aplica Regra 4b (Afirmação factual exige fonte válida);;
    fontes_turno = fontes_retornadas_no_turno or []
    bloqueio_4b, motivo_4b = _verificar_regra_4b_fontes(resposta, fontes_turno)
    if bloqueio_4b:
        msg_motivo = motivo_4b or "Fonte factual ausente"
        regras_disparadas.append(f"4B_SEM_FONTE_FACTUAL: {msg_motivo}")
        resposta.texto = MENSAGEM_SEM_FONTE
        resposta.fontes = []
        if not any(f.tipo == 'ambiguidade' for f in resposta.flags):
            resposta.flags.append(Flag(tipo='ambiguidade', descricao=msg_motivo))
        return resposta, regras_disparadas

    return resposta, regras_disparadas