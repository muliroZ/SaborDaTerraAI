import hashlib
import os
import re
from typing import Any, Optional

from pydantic_ai.usage import UsageLimits
from sabor.agent.schemas import RetornoAntecipado

MENSAGEM_ESCOPO_AMIGAVEL = (
    "Olá! Sou o assistente operacional da **Cooperativa Sabor da Terra**. "
    "Posso ajudar apenas com assuntos da nossa operação:\n"
    "- **Triagem e interpretação de pedidos** (quantidades, produtos, prazos);\n"
    "- **Consultas operacionais e documentais** (estoque, saldos, certificações orgânicas e laudos sanitários);\n"
    "- **Comunicação com produtores** (resumos e rascunhos de notificação).\n\n"
    "Como posso ajudar hoje?"
)

PADROES_INJECTION = [
    r"ignore\s+(todas\s+)?(as\s+)?instru[çc][õo]es",
    r"esque[çc]a\s+(tudo|as\s+regras|o\s+prompt)",
    r"aja\s+como\s+(um|uma)\s+(?!assistente|operador)",
    r"system\s*prompt",
    r"modo\s+desenvolvedor",
    r"jailbreak",
]

TERMOS_FORA_ESCOPO = [
    r"\bpiada\b",
    r"\bpoema\b",
    r"\breceita\s+de\s+bolo\b",
    r"\bprogramar\b|\bc[óo]digo\s+python\b|\bjavascript\b",
    r"\bfutebol\b|\bnovela\b|\bfilme\b",
    r"\bhor[óo]scopo\b|\bprevis[ãa]o\s+do\s+tempo\b",
]

MENSAGEM_ORCAMENTO_TURNO = (
    "Aviso operacional: esta solicitação atingiu o limite de chamadas ou tokens "
    "permitido para um único turno. Por favor, reformule ou divida a pergunta."
)

def obter_limites_configurados() -> dict[str, int]:
    return {
        "max_caracteres": int(os.getenv("GUARDRAIL_MAX_CHARS", "4000")),
        "sessao_request_limit": int(os.getenv("SESSAO_REQUEST_LIMIT", "50")),
        "turno_request_limit": int(os.getenv("TURNO_REQUEST_LIMIT", "4")),
        "turno_tokens_limit": int(os.getenv("TURNO_TOKENS_LIMIT", "12000")),
    }


def obter_usage_limits_turno() -> UsageLimits:
    limites = obter_limites_configurados()
    return UsageLimits(
        request_limit=limites["turno_request_limit"],
        total_tokens_limit=limites["turno_tokens_limit"],
    )


def gerar_hash_pergunta(texto: str, dataset_version: str) -> str:
    normalizado = " ".join(texto.lower().strip().split())
    chave = f"{dataset_version}:{normalizado}"
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()


def verificar_escopo_e_tamanho(texto: str) -> Optional[RetornoAntecipado]:
    """2a: Validação determinística de tamanho, injection e escopo;"""
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
) -> Optional[RetornoAntecipado]:
    """2b: Gancho para consulta por hash no cache UNLOGGED;"""
    if cache_repo is None:
        return None

    query_hash = gerar_hash_pergunta(texto, dataset_version)
    hit = cache_repo.buscar_por_hash(query_hash)
    if hit:
        return RetornoAntecipado(
            motivo="CACHE_HIT",
            mensagem=hit.resposta,
            fontes=getattr(hit, "fontes", []),
        )
    return None


def verificar_cota_sessao(requisicoes_na_sessao: int) -> Optional[RetornoAntecipado]:
    """2c: Verifica se a sessão já atingiu o teto de requisições;"""
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
) -> Optional[RetornoAntecipado]:
    """Executa 2a -> 2b -> 2c em sequência. Retorna None se a entrada estiver liberada;"""
    bloqueio = verificar_escopo_e_tamanho(mensagem)
    if bloqueio:
        return bloqueio

    bloqueio = verificar_cache_estatico(mensagem, dataset_version, cache_repo)
    if bloqueio:
        return bloqueio

    bloqueio = verificar_cota_sessao(requisicoes_na_sessao)
    if bloqueio:
        return bloqueio

    return None