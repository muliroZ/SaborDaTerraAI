"""Guardrail de saída determinístico da etapa 4 (Issue #34):
aplica regras dinâmicas de conformidade sanitária (4a) consultando o PostgreSQL
e validação de afirmações factuais com fontes (4b) sobre a `RespostaAgente`.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from sabor.agent.schemas import Flag, RespostaAgente
from sabor.tools.regras import _norm

TERMOS_FACTUAIS = (
    'estoque', 'capacidade', 'saldo', 'valor', 'vigência', 'vigencia',
    'preço', 'preco', 'kg', 'reais', 'r$', 'vencimento', 'certificado',
)

PADROES_AFIRMACAO_COMERCIAL = (
    r'\b(confirmamos|confirmar|confirmad[ao]s?|disponive(?:l|is)|pronta\s+entrega|'
    r'fornecimento\s+liberado|separad[oa]s?|despachad[oa]s?|encaminhad[oa]s?|enviad[oa]s?)\b',
    r'\b(podemos|vamos|iremos)\s+'
    r'(vender|entregar|fornecer|disponibilizar|atender|enviar|despachar)\b',
    r'\b(sera|serao|vai|vao|esta|estao)\s+(?:sendo\s+)?'
    r'(entregues?|enviad[oa]s?|fornecid[oa]s?|disponibilizad[oa]s?|'
    r'liberado[s]?|despachad[oa]s?|separad[oa]s?)\b',
    r'\b(segue|seguem|seguira|seguirao)\s+'
    r'(?:no\s+caminhao|para\s+entrega|em\s+transporte|'
    r'amanha|hoje|domingo|segunda|terca|quarta|quinta|sexta|sabado)\b',
)

PADROES_AFIRMACAO_ORGANICO_VIGENTE = (
    r'\bcertificacao\s+organica\s+(vigente|valida|ativa)\b',
    r'\borganic[oa]\s+(vigente|valid[ao]|ativ[ao])\b',
    r'\b(possui|tem|com)\s+certificacao\s+organica\b',
)

_MARCADORES_RESTRICAO_INSPECAO = (
    'proib', 'interdit', 'suspens', 'bloquead', 'irregular',
    'nc-', 'nao conform', 'sem registro sie',
)

_REGEX_NEGACAO_COMERCIAL = re.compile(
    r'\b(?:nao|nunca|jamais)\s+'
    r'(?:podemos|pode|podem|vamos|iremos|ha|tem|esta|estao|e|sao|sera|serao|vai|vao|foi|foram)?\s*'
    r'(?:ser\s+)?'
    r'(?:vender|entregar|fornecer|disponibilizar|atender|enviar|despachar|confirmar|'
    r'confirmad[ao]s?|disponive(?:l|is)|entregues?|enviad[oa]s?|fornecid[oa]s?|'
    r'disponibilizad[oa]s?|liberado[s]?|separad[oa]s?|despachad[oa]s?|encaminhad[oa]s?)\b'
)

_REGEX_NEGACAO_VIGENCIA = re.compile(
    r'\b(?:nao|nunca|jamais)\s+(?:tem|possui|ha|esta|e|sera|foi)\b[^,.;!?\n]*'
)

MENSAGEM_BLOQUEIO_SANITARIO = (
    'Bloqueio operacional de segurança sanitária: A resposta gerada continha '
    'referência ou confirmação de comercialização de item ou produtor sob restrição, '
    'interdição sanitária ou com certificação orgânica vencida registrada no banco de dados.'
)

MENSAGEM_SEM_FONTE = (
    'Informação não encontrada ou sem comprovação documental nas fontes oficiais consultadas.'
)


def _segmentos_norm(resposta: RespostaAgente) -> list[str]:
    """Divide texto e rascunhos em sentenças normalizadas para evitar falsos positivos cruzados."""
    blocos = [resposta.texto, *(r.conteudo for r in resposta.rascunhos)]
    segmentos: list[str] = []
    for bloco in blocos:
        if not bloco:
            continue
        for parte in re.split(r'[.!?\n;]+', bloco):
            norm = _norm(parte).strip()
            if norm:
                segmentos.append(norm)
    return segmentos


def _segmento_afirma_vigencia(seg: str) -> bool:
    limpo = _REGEX_NEGACAO_VIGENCIA.sub(' ', seg)
    if re.search(r'\b(vencid[ao]|expirad[ao]|irregular|inativ[ao])\b', limpo):
        return False
    return any(re.search(p, limpo) for p in PADROES_AFIRMACAO_ORGANICO_VIGENTE)


def _segmento_afirma_comercial(seg: str) -> bool:
    limpo = _REGEX_NEGACAO_COMERCIAL.sub(' ', seg)
    return any(re.search(p, limpo) for p in PADROES_AFIRMACAO_COMERCIAL)


def _termos_produto_restrito(nome_produto: str) -> tuple[str, ...]:
    norm = _norm(nome_produto).strip()
    if not norm:
        return ()
    primeiro = norm.split()[0]
    if len(primeiro) >= 4 and primeiro != norm:
        return (norm, primeiro)
    return (norm,)


def _verificar_regra_4a_sanitaria(
    conn: Any,
    resposta: RespostaAgente,
    data_ref: date,
) -> tuple[bool, str | None]:
    """4a: Bloqueia apenas afirmações/confirmações indevidas de vigência orgânica ou
    venda/entrega de itens sob restrição sanitária, preservando alertas e negações válidas.
    """
    if not conn or not type(conn).__module__.startswith('psycopg'):
        return False, None

    segmentos = _segmentos_norm(resposta)
    if not segmentos:
        return False, None

    segmentos_vigencia = [s for s in segmentos if _segmento_afirma_vigencia(s)]
    segmentos_comerciais = [s for s in segmentos if _segmento_afirma_comercial(s)]

    # 1. Produtores com certificação orgânica vencida (checado na mesma sentença)
    if segmentos_vigencia:
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
            pnome_norm = _norm(str(pnome)) if pnome else ''
            for seg in segmentos_vigencia:
                menciona_prod = pid_norm in seg or (bool(pnome_norm) and pnome_norm in seg)
                if menciona_prod:
                    return (
                        True,
                        f'Bloqueio: Produtor {pid} ({pnome}) com certificação orgânica '
                        f'vencida em {pval}',
                    )

    # 2. Restrições/interdições em documentos de inspeção sanitária (checado na mesma sentença)
    if not segmentos_comerciais:
        return False, None

    rows_inspecao = conn.execute(
        """
        SELECT c.texto, d.produtor_id
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE d.tipo_doc = 'inspecao'
        """,
    ).fetchall()

    # A restrição é avaliada por parágrafo, não pelo chunk inteiro. No relatório da ADAGRO,
    # cada produtor vistoriado tem um parágrafo próprio, e o chunk "ESTABELECIMENTO
    # INSPECIONADO" traz P001 a P005 ("REGULAR") junto com P017 ("IRREGULAR"). Avaliado
    # inteiro, o chunk tornava restritos todos esses produtores e produtos, como a goiaba
    # do P001 e o queijo do P003, e bloqueava rascunhos legítimos (F3 do Bloco 4).
    chunks_restritivos: list[tuple[str, str | None]] = []
    for texto_insp, prod_id_insp in rows_inspecao:
        pid_insp = _norm(str(prod_id_insp)) if prod_id_insp else None
        for paragrafo in re.split(r'\n\s*\n+', texto_insp or ''):
            p_norm = _norm(paragrafo)
            if any(w in p_norm for w in _MARCADORES_RESTRICAO_INSPECAO):
                chunks_restritivos.append((p_norm, pid_insp))

    if not chunks_restritivos:
        return False, None

    # Identifica dinamicamente produtos do catálogo e produtores nos chunks restritivos
    rows_produtos = conn.execute('SELECT nome FROM produtos').fetchall()
    termos_restritos: set[str] = set()
    for (nome_prod,) in rows_produtos:
        termos_prod = _termos_produto_restrito(str(nome_prod))
        if any(
            any(re.search(rf'\b{re.escape(t)}\b', chunk) for t in termos_prod)
            for chunk, _ in chunks_restritivos
        ):
            termos_restritos.update(termos_prod)

    produtores_restritos: set[str] = {pid for _, pid in chunks_restritivos if pid}
    for chunk, _ in chunks_restritivos:
        produtores_restritos.update(re.findall(r'\bp\d{3}\b', chunk))

    for seg in segmentos_comerciais:
        if any(re.search(rf'\b{re.escape(pid)}\b', seg) for pid in produtores_restritos):
            return (
                True,
                'Bloqueio: Tentativa de comercialização de produtor sob restrição sanitária '
                '(Inspeção / NC)',
            )
        if any(re.search(rf'\b{re.escape(termo)}\b', seg) for termo in termos_restritos):
            return (
                True,
                'Bloqueio: Tentativa de comercialização de item sob restrição sanitária '
                '(Inspeção / NC)',
            )

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
        return True, 'Afirmação factual sem citação de fontes'

    if not fontes_retornadas_no_turno:
        return True, 'Afirmação factual sem fontes retornadas pelas ferramentas no turno'

    fontes_turn_norm = {
        _normalizar_fonte(f) for f in fontes_retornadas_no_turno if f and f.strip()
    }
    for f_resp in resposta.fontes:
        chave_resp = _normalizar_fonte(f'{f_resp.arquivo} {f_resp.referencia}')
        if not any(f_t in chave_resp or chave_resp in f_t for f_t in fontes_turn_norm):
            return True, f'Fonte citada não verificada nas ferramentas do turno: {f_resp.arquivo}'

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
        msg_motivo = motivo_4a or 'Bloqueio sanitário'
        regras_disparadas.append(f'4A_BLOQUEIO_SANITARIO: {msg_motivo}')
        resposta.texto = f'{MENSAGEM_BLOQUEIO_SANITARIO} ({msg_motivo})'
        resposta.rascunhos = []
        if not any(f.tipo == 'conformidade' for f in resposta.flags):
            resposta.flags.append(Flag(tipo='conformidade', descricao=msg_motivo))
        return resposta, regras_disparadas

    fontes_turno = fontes_retornadas_no_turno or []
    bloqueio_4b, motivo_4b = _verificar_regra_4b_fontes(resposta, fontes_turno)
    if bloqueio_4b:
        msg_motivo = motivo_4b or 'Fonte factual ausente'
        regras_disparadas.append(f'4B_SEM_FONTE_FACTUAL: {msg_motivo}')
        resposta.texto = MENSAGEM_SEM_FONTE
        resposta.fontes = []
        if not any(f.tipo == 'ambiguidade' for f in resposta.flags):
            resposta.flags.append(Flag(tipo='ambiguidade', descricao=msg_motivo))
        return resposta, regras_disparadas

    return resposta, regras_disparadas