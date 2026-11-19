"""Conexão com o PostgreSQL e criação/reset do schema."""

import os
import time
from importlib.resources import files
from urllib.parse import quote

import psycopg
from psycopg import sql
from dotenv import load_dotenv

EMBEDDING_DIM = 768
"""Dimensão dos embeddings (gemini-embedding-001 com MRL); igual a `vector(768)` no schema."""

READONLY_ROLE = 'sabor_ro'
"""Role PostgreSQL com permissão exclusiva de SELECT para uso pelo agente."""

# Ordem de remoção: dependentes antes das tabelas referenciadas.
TABELAS = (
    'mensagens',
    'sessoes',
    'chunks',
    'documentos',
    'pedido_itens',
    'triagem_pedidos',
    'pedidos',
    'lotes',
    'financeiro',
    'sazonalidade_mensal',
    'sazonalidade',
    'precos',
    'produtor_produtos',
    'produto_sinonimos',
    'produtos',
    'produtores',
    "auditoria_mensagens",
    'dataset_metadata',
)
VIEWS = ('chunks_busca',)


def database_url() -> str:
    """URL do banco: `DATABASE_URL` ou, se vazia, a do docker-compose local."""
    load_dotenv()
    if url := os.getenv('DATABASE_URL'):
        return url
    senha = quote(os.getenv('DATABASE_PASSWORD') or 'sabor', safe='')
    porta = os.getenv('DATABASE_PORT') or '5432'
    return f'postgresql://sabor:{senha}@localhost:{porta}/sabor'


def conectar(url: str | None = None, tentativas: int = 30, **kwargs) -> psycopg.Connection:
    """Abre uma conexão, aguardando o banco subir (útil logo após `docker compose up -d`)."""
    url = url or database_url()
    for tentativa in range(1, tentativas + 1):
        try:
            return psycopg.connect(url, connect_timeout=3, **kwargs)
        except psycopg.OperationalError:
            if tentativa == tentativas:
                raise
            time.sleep(1)
    raise AssertionError('inalcançável')


def conectar_readonly(url: str | None = None, tentativas: int = 30, **kwargs) -> psycopg.Connection:
    """Abre uma conexão somente leitura restrita à role `sabor_ro` (apenas SELECT)."""
    conn = conectar(url, tentativas=tentativas, **kwargs)
    with conn.transaction():
        conn.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(READONLY_ROLE)))
    conn.read_only = True
    return conn


def schema_sql() -> str:
    return files('sabor').joinpath('sql/schema.sql').read_text(encoding='utf-8')


def criar_schema(conn: psycopg.Connection) -> None:
    """Cria as tabelas, índices e views que ainda não existem (idempotente)."""
    with conn.transaction():
        conn.execute(schema_sql())


def resetar_schema(conn: psycopg.Connection) -> None:
    """Remove todas as tabelas do projeto (e os dados) e recria o schema vazio."""
    with conn.transaction():
        for view in VIEWS:
            conn.execute(f'DROP VIEW IF EXISTS {view} CASCADE')
        for tabela in TABELAS:
            conn.execute(f'DROP TABLE IF EXISTS {tabela} CASCADE')
        conn.execute(schema_sql())


def tabelas_existentes(conn: psycopg.Connection) -> list[str]:
    rows = conn.execute(
        'SELECT table_name FROM information_schema.tables '
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]
