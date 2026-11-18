"""Contexto de dependências (`AgentDeps`) e configuração de data de referência."""

from dataclasses import dataclass, field
from datetime import date
import hashlib
import os
import time
from typing import Any

from dotenv import load_dotenv
import psycopg

from sabor.agent.mascarador import Mascarador

DATA_REF_PADRAO = date(2026, 10, 30)
"""Retrato padrão dos dados operacionais e documentais (30/10/2026)."""


def obter_data_referencia() -> date:
    """Retorna a data de referência configurada em `DATA_REF_DATE` ou `2026-10-30`."""
    load_dotenv()
    if valor := os.getenv('DATA_REF_DATE'):
        return date.fromisoformat(valor.strip())
    return DATA_REF_PADRAO


_TABELAS_DATASET = (
    'produtores', 'produtos', 'produto_sinonimos', 'produtor_produtos', 'precos',
    'sazonalidade', 'sazonalidade_mensal', 'pedidos', 'pedido_itens', 'financeiro',
    'lotes', 'documentos',
)
_CACHE_VERSAO_DATASET: dict[tuple[str, str, str, str], tuple[float, str]] = {}


def obter_versao_dataset(conn: Any) -> str:
    """Fingerprint estável do conteúdo operacional/documental carregado no PostgreSQL.

    `DATASET_VERSION` pode fixar uma versão publicada. Sem override, hashes por tabela
    são combinados; conteúdo e PII nunca são retornados, apenas o SHA-256 final.
    """
    from dotenv import load_dotenv

    load_dotenv()
    if valor := os.getenv('DATASET_VERSION'):
        return valor.strip()
    info = getattr(conn, 'info', None)
    chave_cache = tuple(
        str(getattr(info, campo, '')) for campo in ('host', 'port', 'dbname', 'user')
    )
    agora = time.monotonic()
    if all(chave_cache) and chave_cache in _CACHE_VERSAO_DATASET:
        instante, versao = _CACHE_VERSAO_DATASET[chave_cache]
        if agora - instante < 60:
            return versao
    digest = hashlib.sha256()
    try:
        for tabela in _TABELAS_DATASET:
            hash_tabela = conn.execute(
                f"SELECT md5(coalesce(string_agg(row_to_json(t)::text, '' "
                f'ORDER BY row_to_json(t)::text), \'\')) '
                f'FROM (SELECT * FROM {tabela}) AS t'
            ).fetchone()[0]
            digest.update(tabela.encode())
            digest.update((hash_tabela or '').encode())
        # Embeddings são derivados e grandes; texto, ordem e metadados identificam a carga.
        hash_chunks = conn.execute(
            "SELECT md5(coalesce(string_agg(row_to_json(t)::text, '' "
            "ORDER BY row_to_json(t)::text), '')) FROM "
            '(SELECT arquivo, ordem, secao, texto, metadados FROM chunks) AS t'
        ).fetchone()[0]
        digest.update(b'chunks')
        digest.update((hash_chunks or '').encode())
    except Exception:
        # Conexões mockadas e bancos indisponíveis preservam a execução de teste; em produção,
        # o runner cria a dependência com conexão antes de avaliar o guardrail.
        return 'sha256-unavailable'
    versao = f'sha256:{digest.hexdigest()}'
    if all(chave_cache):
        _CACHE_VERSAO_DATASET[chave_cache] = (agora, versao)
    return versao


class MascaradorNoOp:
    """Implementação padrão (identidade) até a conexão com o mascarador de PII (#33)."""

    def mascarar(self, texto: str) -> str:
        return texto

    def restaurar(self, texto: str) -> str:
        return texto


@dataclass
class AgentDeps:
    """Contexto de execução injetado nas ferramentas e no system prompt do agente."""

    conn: psycopg.Connection
    data_referencia: date = field(default_factory=obter_data_referencia)
    dataset_version: str | None = 'v1'
    usuario: str = 'Dona Lúcia'
    mascarador: Any = field(default_factory=Mascarador)
