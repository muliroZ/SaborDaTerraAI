# SaborDaTerraAI

Assistente de IA para a cooperativa Sabor da Terra. A documentação dos dados e do projeto está em [docs/](docs/README.md).

## Banco de dados (PostgreSQL + pgvector)

Requer Docker e [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env              # opcional: os valores vazios usam os padrões
docker compose up -d              # sobe o Postgres 17 com pgvector (volume `pgdata`)
uv run python scripts/init_db.py  # cria o schema vazio (idempotente)
uv run python scripts/etl.py      # insere os dados nas tabelas correspondentes (idempotente)
uv run python scripts/etl.py      # executa o ETL completo, populando as tabelas 
uv run python scripts/ingest_docs.py # ingere documentos, gera embeddings, completa as tabelas
```

- `uv run python scripts/init_db.py --reset` apaga as tabelas do projeto e recria o schema do zero.
- `uv run python scripts/etl.py` apaga os dados das tabelas e reseta os indices seriais.
- `docker compose down -v` derruba o banco e remove o volume.
- O DDL está em [`src/sabor/sql/schema.sql`](src/sabor/sql/schema.sql). As quantidades das fontes (`quantidade_kg`) ficam em `quantidade` + `unidade` (`kg`, `unidade`, `dúzia`, `maço`, `litro`), e os chunks dos TXT têm `embedding vector(768)` com índice HNSW (cosseno) e `tsvector` para busca híbrida.
- `uv run pytest` roda os testes; os de integração usam um banco temporário e são pulados se o Postgres estiver fora do ar.

## Interface (Streamlit)

```bash
uv run python scripts/init_db.py  # cria também as tabelas `sessoes` e `mensagens`
uv run sabor                      # abre http://localhost:8501 (equivale a `streamlit run src/sabor/ui.py`)
```

Escolha um usuário simulado na barra lateral (equipe da cooperativa ou produtor `P001`…`P018`) e converse pelo chat. A conversa fica no banco: recarregar a página retoma a sessão, e "Nova sessão" oferece o relatório da conversa anterior.
