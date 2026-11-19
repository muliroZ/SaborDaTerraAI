"""Limpa o cache de respostas estáticas (tabela UNLOGGED `cache_respostas`, #36).

Uso:
    uv run python scripts/limpar_cache.py               # remove todas as respostas em cache
    uv run python scripts/limpar_cache.py --expirados   # remove só as que passaram do TTL

Não é preciso rodar após uma nova carga de dados: a versão do dataset faz parte da chave,
então as respostas antigas já deixam de ser servidas.
"""

import argparse
import sys

import psycopg

from sabor.agent.cache import CacheRespostas
from sabor.db import conectar, criar_schema, database_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--expirados',
        action='store_true',
        help='remove só as respostas que já passaram do TTL (CACHE_TTL_S)',
    )
    args = parser.parse_args()

    try:
        with conectar(tentativas=1) as conn:
            criar_schema(conn)  # garante a tabela em bancos criados antes da #36
    except psycopg.OperationalError as e:
        host = database_url().rsplit('@', 1)[-1]
        print(f'Não foi possível conectar em {host}: {e}', file=sys.stderr)
        print('O banco está no ar? Rode `docker compose up -d`.', file=sys.stderr)
        return 1

    removidas = CacheRespostas().limpar(apenas_expirados=args.expirados)
    tipo = 'expiradas ' if args.expirados else ''
    print(f'{removidas} resposta(s) {tipo}removida(s) do cache.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
