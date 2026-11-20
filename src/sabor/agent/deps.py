"""Contexto de dependências (`AgentDeps`) e configuração de data de referência."""

from dataclasses import dataclass, field
from datetime import date
import hashlib
import os
from typing import Any

from dotenv import load_dotenv
import psycopg

from sabor.agent.mascarador import Mascarador
from sabor.agent.schemas import Rascunho, Relatorio

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
def _calcular_versao_dataset(conn: Any) -> str:
    """Calcula o fingerprint completo. Chamado ao fim das cargas, não por turno."""
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
        # o runner mantém uma versão de fallback. Psycopg deixa a transação abortada após erro.
        rollback = getattr(conn, 'rollback', None)
        if callable(rollback):
            try:
                rollback()
            except Exception:
                pass
        return 'sha256-unavailable'
    return f'sha256:{digest.hexdigest()}'


def atualizar_versao_dataset(conn: Any) -> str:
    """Calcula e persiste a versão depois de uma carga bem-sucedida do dataset."""
    versao = _calcular_versao_dataset(conn)
    if versao == 'sha256-unavailable':
        raise RuntimeError('Não foi possível calcular a versão do dataset; versão não atualizada.')
    conn.execute(
        "INSERT INTO dataset_metadata (chave, versao) VALUES ('atual', %s) "
        'ON CONFLICT (chave) DO UPDATE SET versao = EXCLUDED.versao, atualizada_em = now()',
        (versao,),
    )
    return versao


def obter_versao_dataset(conn: Any) -> str:
    """Lê a versão persistida; `DATASET_VERSION` pode sobrescrevê-la explicitamente."""
    load_dotenv()
    if valor := os.getenv('DATASET_VERSION'):
        return valor.strip()
    info = getattr(conn, 'info', None)
    if not isinstance(getattr(info, 'dbname', None), str):
        return 'sha256-unavailable'
    try:
        row = conn.execute(
            "SELECT versao FROM dataset_metadata WHERE chave = 'atual'"
        ).fetchone()
        if row is not None:
            return row[0]
    except Exception:
        rollback = getattr(conn, 'rollback', None)
        if callable(rollback):
            try:
                rollback()
            except Exception:
                pass
        return 'sha256-unavailable'
    return 'sha256-unpublished'


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
    dataset_version: str | None = None
    usuario: str = 'Dona Lúcia'
    mascarador: Any = field(default_factory=Mascarador)
    relatorios: list[Relatorio] = field(default_factory=list)
    """Relatórios gerados pelas tools no turno (#15); o runner os anexa à resposta."""
    rascunhos: list[Rascunho] = field(default_factory=list)
    """Rascunhos montados pelas tools no turno (`resumir_produtor`, `rascunhar_notificacao`).

    O runner os anexa à resposta no lugar dos que o modelo devolver (F3 do Bloco 4).
    """
