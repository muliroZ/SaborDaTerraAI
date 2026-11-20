"""Fixtures compartilhadas dos testes do SaborDaTerraAI.

`conn`: conexão com o banco já populado pelo pipeline (`uv run python
scripts/etl.py` + `ingest_docs.py`) — o mesmo banco de desenvolvimento, não
um banco de teste isolado. As ferramentas de consultar_dados (Issue #7) só
fazem SELECT, então rodar os testes aqui não arrisca sujar dado nenhum; por
isso não precisamos de transação com rollback nem de um Postgres separado
só para teste.

Pré-requisito para os testes de integração passarem: o pipeline já ter
rodado contra os dados de `dados/Facepe/` sem alterações, porque os valores
esperados em test_consultar_dados.py são os de docs/projeto.md (perguntas
de referência), fixos para esse dataset.

Se/quando quisermos isolar um banco só de teste, troca-se apenas a URL
usada aqui (ex.: uma env var DATABASE_URL_TESTE lida antes de chamar
`conectar()`), sem precisar tocar em nenhum teste.
"""

import psycopg
import pytest

from sabor.db import conectar


@pytest.fixture
def conn():
    try:
        conexao = conectar(tentativas=1)
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')
    try:
        yield conexao
    finally:
        conexao.close()