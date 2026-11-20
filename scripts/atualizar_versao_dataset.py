"""Calcula e publica a versão SHA-256 dos dados já carregados no PostgreSQL.

Use após atualizar o schema em uma base existente ou após alterações manuais de dados.
ETL e ingest_docs já atualizam essa versão automaticamente ao concluir.
"""

from sabor.agent.deps import atualizar_versao_dataset
from sabor.db import conectar, criar_schema


def main() -> None:
    with conectar() as conn:
        criar_schema(conn)
        versao = atualizar_versao_dataset(conn)
    print(f'Versão do dataset publicada: {versao}')


if __name__ == '__main__':
    main()
