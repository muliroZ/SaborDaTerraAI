"""Cria (ou recria) o schema vazio do banco.

Uso:
    docker compose up -d
    uv run python scripts/init_db.py            # cria o que faltar (idempotente)
    uv run python scripts/init_db.py --reset    # APAGA as tabelas do projeto e recria
    uv run python scripts/etl.py                # executa o ETL completo, populando as tabelas (produtos, produtores, preços, etc.)
    uv run python scripts/ingest_docs.py        # ingere documentos, gera embeddings, completa as tabelas(certificados, contratos)
"""

import argparse
import sys

import psycopg

from sabor.db import (
    TABELAS,
    conectar,
    criar_schema,
    database_url,
    resetar_schema,
    tabelas_existentes,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--reset',
        action='store_true',
        help='remove todas as tabelas do projeto (e os dados) antes de recriar',
    )
    args = parser.parse_args()

    try:
        conn = conectar()
    except psycopg.OperationalError as e:
        host = database_url().rsplit('@', 1)[-1]
        print(f'Não foi possível conectar em {host}: {e}', file=sys.stderr)
        print('O banco está no ar? Rode `docker compose up -d`.', file=sys.stderr)
        return 1

    with conn:
        if args.reset:
            resetar_schema(conn)
            print('Schema recriado do zero (dados anteriores removidos).')
        else:
            criar_schema(conn)
            print('Schema criado/atualizado.')

        existentes = set(tabelas_existentes(conn))
        faltando = [t for t in TABELAS if t not in existentes]
        if faltando:
            print(f'Tabelas faltando: {", ".join(faltando)}', file=sys.stderr)
            return 1
        print(f'{len(TABELAS)} tabelas prontas: {", ".join(sorted(TABELAS))}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
