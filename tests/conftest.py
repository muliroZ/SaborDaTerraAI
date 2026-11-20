"""Fixtures compartilhadas: banco temporário com o schema vazio (mesmo padrão de test_schema)."""

import uuid

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from sabor.db import conectar, criar_schema, database_url


@pytest.fixture(scope='session')
def url_banco_temporario():
    """Cria um banco `sabor_test_*` com o schema e devolve a URL (pula sem Postgres)."""
    base = database_url()
    try:
        admin = conectar(base, tentativas=1, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')

    nome = f'sabor_test_{uuid.uuid4().hex[:8]}'
    admin.execute(f'CREATE DATABASE {nome}')
    url = make_conninfo(**(conninfo_to_dict(base) | {'dbname': nome}))
    with psycopg.connect(url) as c:
        criar_schema(c)
    try:
        yield url
    finally:
        admin.execute(f'DROP DATABASE {nome} WITH (FORCE)')
        admin.close()


@pytest.fixture
def conn_temporaria(url_banco_temporario):
    with psycopg.connect(url_banco_temporario, autocommit=True) as c:
        yield c
