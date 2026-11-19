"""
Guardrail de saída determinístico da etapa 4 issue 34: aplica regras dinâmicas de conformidade sanitária (4a)
consultando o PostgreSQL e validação de afirmações factuais com fontes (4b) sobre a RespostaAgente;
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from sabor.agent.schemas import Flag, RespostaAgente
from sabor.tools.regras import _norm

TERMOS_FACTUAIS = (
    "estoque", "capacidade", "saldo", "valor", "vigência", "vigencia",
    "preço", "preco", "kg", "reais", "r$", "vencimento", "certificado",
)

PADROES_NEGACAO = (
    r"\bnao\s+(podemos|ha|tem|possui|esta|e|sera|foi)\b",
    r"\bnão\s+(podemos|há|tem|possui|está|é|será|foi)\b",
    r"\bsem\s+(registro|certificacao|certificação|validade|estoque|autorizacao|autorização)\b",
    r"\b(vencid[ao]|expirad[ao]|interditad[ao]|proibid[ao]|suspens[ao]|bloquead[ao]|irregular|inativ[ao])\b",
    r"\bnao\s+vigente\b|\bnão\s+vigente\b|\bnao\s+valid[ao]\b|\bnão\s+válid[ao]\b",
)

PADROES_AFIRMACAO_COMERCIAL = (
    r"\b(confirmamos|confirmar|confirmada|disponivel|disponível|pronta\s+entrega|fornecimento\s+liberado)\b",
    r"\b(podemos|vamos|iremos)\s+(vender|entregar|fornecer|disponibilizar|atender)\b",
)

PADROES_AFIRMACAO_ORGANICO_VIGENTE = (
    r"\bcertificaca[oõ]\s+organica\s+(vigente|valida|válida|ativa)\b",
    r"\borganic[oa]\s+(vigente|valid[ao]|válid[ao]|ativ[ao])\b",
    r"\b(possui|tem|com)\s+certificaca[oõ]\s+organica\b",
)

STOPWORDS_INSPECAO = {
    "cooperativa", "sabor", "terra", "geral", "periodo", "período",
    "relatorio", "relatório", "inspecao", "inspeção", "produtor",
    "produtores", "documento", "laudo", "norma", "item", "itens",
}

MENSAGEM_BLOQUEIO_SANITARIO = (
    "Bloqueio operacional de segurança sanitária: A resposta gerada continha "
    "referência ou confirmação de comercialização de item ou produtor sob restrição, "
    "interdição sanitária ou com certificação orgânica vencida registrada no banco de dados."
)

MENSAGEM_SEM_FONTE = (
    "Informação não encontrada ou sem comprovação documental nas fontes oficiais consultadas."
)

def _possui_negacao_explicita(texto_norm: str) -> bool:
    return any(re.search(padrao, texto_norm) for padrao in PADROES_NEGACAO)


def _verificar_regra_4a_sanitaria(
    conn: Any,
    resposta: RespostaAgente,
    data_ref: date,
) -> tuple[bool, str | None]:
    """4a: Bloqueia apenas afirmações/confirmações indevidas de vigência orgânica ou
    venda/entrega de itens sob restrição sanitária, preservando alertas e negações válidas.
    """
    if not conn or not type(conn).__module__.startswith("psycopg"):
        return False, None

    texto_completo_norm = _norm(
        f"{resposta.texto} " + " ".join(r.conteudo for r in resposta.rascunhos)
    )

    texto_sem_negacao_comercial = re.sub(
        r"\b(?:nao|não|nunca|jamais)\s+(?:podemos|vamos|iremos)\s+(?:vender|entregar|fornecer|disponibilizar|atender)\b",
        " ",
        texto_completo_norm,
    )

    afirma_comercial = any(
        re.search(p, texto_sem_negacao_comercial) for p in PADROES_AFIRMACAO_COMERCIAL
    )

    texto_sem_negacao = re.sub(
        r"\b(?:nao|não|nunca|jamais)\s+(?:tem|possui|ha|há|esta|está|e|é|será|sera|foi)\b[^.?!;\n]*",
        " ",
        texto_completo_norm,
    )
    
    afirma_vigencia = any(
        re.search(p, texto_sem_negacao) for p in PADROES_AFIRMACAO_ORGANICO_VIGENTE
    )

    # Resposta apenas alerta/nega restrição sem confirmar venda ou vigência
    if _possui_negacao_explicita(texto_completo_norm) and not afirma_comercial and not afirma_vigencia:
        return False, None

    # 1. Produtores com certificação orgânica vencida
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
        pid_norm = _norm(str(pid))
        pnome_norm = _norm(str(pnome)) if pnome else ""
        menciona_produtor = pid_norm in texto_completo_norm or (
            bool(pnome_norm) and pnome_norm in texto_completo_norm
        )
        if menciona_produtor and afirma_vigencia:
            return True, f"Bloqueio: Produtor {pid} ({pnome}) com certificação orgânica vencida em {pval}"

    # 2. Restrições/interdições em documentos de inspeção sanitária
    if not afirma_comercial:
        return False, None

    rows_inspecao = conn.execute(
        """
        SELECT c.texto, d.produtor_id
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE d.tipo_doc = 'inspecao'
        """,
    ).fetchall()

    for texto_insp, prod_id_insp in rows_inspecao:
        t_insp_norm = _norm(texto_insp or "")
        if not any(
            w in t_insp_norm
            for w in ("proib", "interdit", "suspens", "bloquead", "irregular", "nc-", "nao conform")
        ):
            continue

        menciona_prod_restrito = bool(
            prod_id_insp and _norm(str(prod_id_insp)) in texto_completo_norm
        )
        tokens_insp = {
            w for w in t_insp_norm.split()
            if len(w) >= 4 and w not in STOPWORDS_INSPECAO
        }
        menciona_termo_insp = any(tok in texto_completo_norm for tok in tokens_insp)

        if menciona_prod_restrito or menciona_termo_insp:
            return (
                True,
                "Bloqueio: Tentativa de comercialização de item sob restrição sanitária (Inspeção / NC)",
            )

    return False, None


def _normalizar_fonte(texto: str) -> str:
    """Normaliza citação de fonte ignorando separadores visuais (ex.: '·' e espaços extras)."""
    return " ".join(_norm(texto).replace("·", " ").split())


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

    if not fontes_retornadas_no_turno:
        return True, "Afirmação factual sem fontes retornadas pelas ferramentas no turno"

    fontes_turn_norm = {_normalizar_fonte(f) for f in fontes_retornadas_no_turno if f and f.strip()}
    for f_resp in resposta.fontes:
        chave_resp = _normalizar_fonte(f"{f_resp.arquivo} {f_resp.referencia}")
        if not any(f_t in chave_resp or chave_resp in f_t for f_t in fontes_turn_norm):
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

    bloqueio_4a, motivo_4a = _verificar_regra_4a_sanitaria(conn, resposta, data_referencia)
    if bloqueio_4a:
        msg_motivo = motivo_4a or "Bloqueio sanitário"
        regras_disparadas.append(f"4A_BLOQUEIO_SANITARIO: {msg_motivo}")
        resposta.texto = f"{MENSAGEM_BLOQUEIO_SANITARIO} ({msg_motivo})"
        resposta.rascunhos = []
        if not any(f.tipo == "conformidade" for f in resposta.flags):
            resposta.flags.append(Flag(tipo="conformidade", descricao=msg_motivo))
        return resposta, regras_disparadas

    fontes_turno = fontes_retornadas_no_turno or []
    bloqueio_4b, motivo_4b = _verificar_regra_4b_fontes(resposta, fontes_turno)
    if bloqueio_4b:
        msg_motivo = motivo_4b or "Fonte factual ausente"
        regras_disparadas.append(f"4B_SEM_FONTE_FACTUAL: {msg_motivo}")
        resposta.texto = MENSAGEM_SEM_FONTE
        resposta.fontes = []
        if not any(f.tipo == "ambiguidade" for f in resposta.flags):
            resposta.flags.append(Flag(tipo="ambiguidade", descricao=msg_motivo))
        return resposta, regras_disparadas

    return resposta, regras_disparadas