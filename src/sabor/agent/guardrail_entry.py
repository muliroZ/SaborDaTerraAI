"""Guardrail de entrada determinístico para o agente da Cooperativa Sabor da Terra.
Valida escopo, tamanho, prompt injection, extração de diretrizes, links e gibberish.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic_ai.usage import UsageLimits
from sabor.agent.cache import gerar_chave_cache
from sabor.agent.schemas import RetornoAntecipado

logger = logging.getLogger(__name__)

MENSAGEM_ESCOPO_AMIGAVEL = (
    "Olá! Sou o **CajAI**, assistente operacional da **Cooperativa Sabor da Terra**. "
    "Posso ajudar apenas com assuntos da nossa operação:\n"
    "- **Triagem e interpretação de pedidos** (quantidades, produtos, prazos);\n"
    "- **Consultas operacionais e documentais** (estoque, saldos, certificações orgânicas e laudos sanitários);\n"
    "- **Comunicação com produtores** (resumos e rascunhos de notificação).\n\n"
    "Como posso ajudar hoje?"
)

PADROES_INJECTION = [
    # Tentativas clássicas de override de regras
    r"ignore\s+(todas\s+)?(as\s+)?instru[çc][õo]es",
    r"esque[çc]a\s+(tudo|as\s+regras|o\s+prompt|suas\s+diretrizes)",
    r"aja\s+como\s+(um|uma)\s+(?!assistente|operador)",
    r"system\s*prompt",
    r"modo\s+desenvolvedor",
    r"jailbreak",
    # Tentativas de extração de diretrizes e restrições internas (como o teste relatado)
    r"responda\s+com\s+apenas.*sim.*n[ãa]o",
    r"voc[êe]\s+tem\s+restri[çc][õo]es\s+de\s+conte[úu]do",
    r"quais\s+s[ãa]o\s+as\s+suas\s+instru[çc][õo]es\s+internas",
    r"liste\s+as\s+(suas\s+)?restri[çc][õo]es",
    r"mostre\s+seu\s+system\s+prompt",
]

TERMOS_FORA_ESCOPO = [
    r"\bpiada\b",
    r"\bpoema\b",
    r"\breceita\s+de\s+bolo\b",
    r"\bprogramar\b|\bc[óo]digo\s+python\b|\bjavascript\b",
    r"\bfutebol\b|\bnovela\b|\bfilme\b",
    r"\bhor[óo]scopo\b|\bprevis[ãa]o\s+do\s+tempo\b",
]

# Regex para detectar URLs e links externos
REGEX_URL = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)",
    re.IGNORECASE,
)

MENSAGEM_ORCAMENTO_TURNO = (
    "Aviso operacional: esta solicitação atingiu o limite de chamadas ou tokens "
    "permitido para um único turno. Por favor, reformule ou divida a pergunta."
)

REGEX_DIVISORES_EMAIL = re.compile(r"(?:^|\s)[=\-_*~]{3,}(?:\s|$)")

def obter_limites_configurados() -> dict[str, int]:
    load_dotenv()

    def _parse_env(var_name: str, default_val: str) -> int:
        val = os.getenv(var_name)
        if not val or not val.strip():
            return int(default_val)
        try:
            parsed = int(val.strip())
        except (ValueError, TypeError):
            return int(default_val)
        if parsed <= 0:
            return int(default_val)
        return parsed

    return {
        "max_caracteres": _parse_env("GUARDRAIL_MAX_CHARS", "750"),
        "sessao_request_limit": _parse_env("SESSAO_REQUEST_LIMIT", "50"),
        "turno_request_limit": _parse_env("TURNO_REQUEST_LIMIT", "8"),
        "turno_tokens_limit": _parse_env("TURNO_TOKENS_LIMIT", "32000"),
    }


def obter_usage_limits_turno() -> UsageLimits:
    limites = obter_limites_configurados()
    return UsageLimits(
        request_limit=limites["turno_request_limit"],
        total_tokens_limit=limites["turno_tokens_limit"],
    )


def _eh_gibberish(texto: str) -> bool:
    """Identifica mensagens compostas por caracteres aleatórios sem sentido ou spam de símbolos."""
    texto_limpo = texto.strip()
    if len(texto_limpo) < 4:
        return False

    # Ignora divisores estruturais legítimos antes de checar densidade/ruído de símbolos
    texto_sem_divisores = REGEX_DIVISORES_EMAIL.sub(" ", texto_limpo).strip()
    if not texto_sem_divisores:
        return True

    # 1. Proporção excessiva de caracteres especiais aleatórios consecutivos
    if re.search(r"[^a-zA-Z0-9À-ÿ\s]{4,}", texto_sem_divisores):
        return True

    # 2. Sequências puramente randômicas sem vogais em palavras longas (ex: "asjdhfiuasdfh")
    palavras = texto_sem_divisores.split()
    palavras_sem_sentido = 0
    vogais = set("aeiouáéíóúâêîôûãõàèìòùäëïöü")
    for p in palavras:
        p_limpo = re.sub(r"[^a-zA-ZÀ-ÿ]", "", p)
        if len(p_limpo) >= 6:
            tem_vogal = any(c.lower() in vogais for c in p_limpo)
            if not tem_vogal:
                palavras_sem_sentido += 1

    if len(palavras) > 0 and (palavras_sem_sentido / len(palavras)) > 0.4 and len(palavras) <= 3:
        return True

    return False


def verificar_escopo_e_tamanho(texto: str) -> Optional[RetornoAntecipado]:
    """2a: Validação determinística de tamanho, links, gibberish, injection e escopo."""
    limites = obter_limites_configurados()
    max_chars = limites["max_caracteres"]
    texto_limpo = texto.strip()

    if len(texto_limpo) > max_chars:
        return RetornoAntecipado(
            motivo="TAMANHO_EXCEDIDO",
            mensagem=(
                f"Sua mensagem excedeu o limite permitido ({len(texto_limpo)} caracteres; "
                f"máximo de {max_chars}). Por favor, resuma ou envie em partes menores."
            ),
        )

    # Bloqueio de URLs e Links externos
    if REGEX_URL.search(texto_limpo):
        return RetornoAntecipado(
            motivo="FORA_DE_ESCOPO",
            mensagem=(
                "Links externos não são permitidos por motivos de segurança e política operacional. "
                "Por favor, cole o texto diretamente se necessário."
            ),
        )

    # Bloqueio de caracteres aleatórios / gibberish
    if _eh_gibberish(texto_limpo):
        return RetornoAntecipado(
            motivo="FORA_DE_ESCOPO",
            mensagem=(
                "Não consegui identificar uma solicitação clara em sua mensagem. "
                "Por favor, digite sua dúvida sobre pedidos, estoque ou relatórios operacionais."
            ),
        )

    texto_lower = texto_limpo.lower()

    for padrao in PADROES_INJECTION:
        if re.search(padrao, texto_lower):
            return RetornoAntecipado(
                motivo="PROMPT_INJECTION",
                mensagem=MENSAGEM_ESCOPO_AMIGAVEL,
            )

    for termo in TERMOS_FORA_ESCOPO:
        if re.search(termo, texto_lower):
            return RetornoAntecipado(
                motivo="FORA_DE_ESCOPO",
                mensagem=MENSAGEM_ESCOPO_AMIGAVEL,
            )

    return None


def verificar_cache_estatico(
    texto: str,
    dataset_version: str,
    cache_repo: Optional[Any] = None,
    *,
    perfil: str = "funcionario",
    data_referencia: date | str | None = None,
) -> Optional[RetornoAntecipado]:
    """2b: consulta o cache UNLOGGED (#36) pela chave SHA-256 da pergunta normalizada,
    perfil, versão do dataset e data de referência. Falha no cache não bloqueia o turno."""
    if cache_repo is None:
        return None

    chave = gerar_chave_cache(texto, perfil, dataset_version, data_referencia)
    try:
        hit = cache_repo.buscar_por_hash(chave)
    except Exception:
        logger.warning("Falha ao consultar o cache de respostas; seguindo sem cache", exc_info=True)
        return None
    if hit:
        return RetornoAntecipado(
            motivo="CACHE_HIT",
            mensagem=hit.resposta,
            fontes=list(getattr(hit, "fontes", None) or []),
            flags=list(getattr(hit, "flags", None) or []),
        )
    return None


def verificar_cota_sessao(requisicoes_na_sessao: int) -> Optional[RetornoAntecipado]:
    """2c: Verifica se a sessão já atingiu o teto de requisições."""
    limites = obter_limites_configurados()
    if requisicoes_na_sessao >= limites["sessao_request_limit"]:
        return RetornoAntecipado(
            motivo="COTA_SESSAO_ESGOTADA",
            mensagem=(
                "A cota de solicitações para esta sessão foi atingida. "
                "Inicie uma nova sessão ou aguarde a renovação do limite operacional."
            ),
        )
    return None


def criar_retorno_orcamento_turno() -> RetornoAntecipado:
    """2c: Retorno padronizado quando UsageLimitExceeded é disparado pelo PydanticAI."""
    return RetornoAntecipado(
        motivo="ORCAMENTO_TURNO_ESGOTADO",
        mensagem=MENSAGEM_ORCAMENTO_TURNO,
    )


def executar_guardrail_entrada(
    mensagem: str,
    dataset_version: str = "default",
    requisicoes_na_sessao: int = 0,
    cache_repo: Optional[Any] = None,
    *,
    perfil: str = "funcionario",
    data_referencia: date | str | None = None,
) -> Optional[RetornoAntecipado]:
    """Executa 2a -> 2b -> 2c em sequência. Retorna None se a entrada estiver liberada."""
    bloqueio = verificar_escopo_e_tamanho(mensagem)
    if bloqueio:
        return bloqueio

    bloqueio = verificar_cache_estatico(
        mensagem,
        dataset_version,
        cache_repo,
        perfil=perfil,
        data_referencia=data_referencia,
    )
    if bloqueio:
        return bloqueio

    bloqueio = verificar_cota_sessao(requisicoes_na_sessao)
    if bloqueio:
        return bloqueio

    return None
