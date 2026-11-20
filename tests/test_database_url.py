"""Montagem da URL do banco a partir das variáveis de ambiente (#17)."""

import psycopg
import pytest

from sabor import db

VARIAVEIS = (
    'DATABASE_URL',
    'DATABASE_HOST',
    'DATABASE_PORT',
    'DATABASE_USER',
    'DATABASE_PASSWORD',
    'DATABASE_NAME',
)


@pytest.fixture(autouse=True)
def ambiente_limpo(monkeypatch):
    monkeypatch.setattr(db, 'load_dotenv', lambda: None)
    for var in VARIAVEIS:
        monkeypatch.delenv(var, raising=False)


def test_padroes_do_docker_compose_local():
    assert db.database_url() == 'postgresql://sabor:sabor@localhost:5432/sabor'


def test_database_url_tem_precedencia(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://a:b@remoto:6543/x')
    monkeypatch.setenv('DATABASE_HOST', 'db')
    assert db.database_url() == 'postgresql://a:b@remoto:6543/x'


def test_host_do_servico_no_compose(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', '')
    monkeypatch.setenv('DATABASE_HOST', 'db')
    monkeypatch.setenv('DATABASE_PASSWORD', 's3nh@:/#')
    url = db.database_url()
    assert url == 'postgresql://sabor:s3nh%40%3A%2F%23@db:5432/sabor'
    assert psycopg.conninfo.conninfo_to_dict(url)['password'] == 's3nh@:/#'


def test_socket_unix_do_cloud_sql(monkeypatch):
    monkeypatch.setenv('DATABASE_HOST', '/cloudsql/projeto:southamerica-east1:sabor-db')
    monkeypatch.setenv('DATABASE_PASSWORD', 'x')
    partes = psycopg.conninfo.conninfo_to_dict(db.database_url())
    assert partes['host'] == '/cloudsql/projeto:southamerica-east1:sabor-db'
    assert partes['port'] == '5432'
    assert partes['dbname'] == 'sabor'
    assert partes['user'] == 'sabor'
    assert partes['password'] == 'x'
