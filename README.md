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
uv run sabor                      # abre http://localhost:8501 (equivale a `streamlit run src/sabor/ui/app.py`)
```

Escolha um usuário simulado na barra lateral (equipe da cooperativa ou produtor `P001`…`P018`) e converse pelo chat. A conversa fica no banco: recarregar a página retoma a sessão, e "Nova sessão" oferece o relatório da conversa anterior.

### Cache de respostas estáticas

Perguntas autocontidas da equipe (ex.: "Como armazenar queijo coalho?") ficam na tabela `UNLOGGED` `cache_respostas` e, repetidas em outra sessão, voltam sem chamar o LLM (selo "Resposta do cache", mesmas fontes). Respostas com dados pessoais, rascunhos, relatórios, bloqueios do guardrail ou do perfil produtor não entram no cache. A chave inclui a versão do dataset, então uma nova carga de dados invalida o cache sozinha.

```bash
uv run python scripts/atualizar_versao_dataset.py  # publica a versão do dataset (ETL e ingestão já fazem isso)
uv run python scripts/limpar_cache.py              # esvazia o cache (--expirados: só as vencidas)
```

O TTL é `CACHE_TTL_S` no `.env` (padrão 24 h; `0` desliga o cache). Detalhes em [docs/dev/agente_e_fallback.md](docs/dev/agente_e_fallback.md).

## Triagem em lote da caixa de entrada

```bash
uv run python scripts/init_db.py                                  # cria a tabela `triagem_pedidos`
uv run python scripts/triagem_lote.py --csv fila.csv --json fila.json
```

Roda `extrair_e_classificar_pedido` em todos os `pedidos`, fora do chat, grava em `triagem_pedidos` e mostra a fila ordenada por urgência (crítica → baixa; erros, desatualizados e pendentes no fim). Pedidos já extraídos não voltam ao LLM, mas as regras rodam de novo sobre o banco atual sem gastar tokens; cada pedido respeita o orçamento do turno (`TURNO_TOKENS_LIMIT`/`TURNO_REQUEST_LIMIT`) e o lote tem teto próprio (`TRIAGEM_TOKENS_LIMIT`, padrão 150 mil tokens). Opções: `--limite N`, `--tokens N`, `--reprocessar` e `--somente-exportar` (sem LLM). Na interface, a equipe vê a fila em "Caixa de entrada". Detalhes em [docs/dev/triagem_lote.md](docs/dev/triagem_lote.md).

## Tudo em Docker (Plano B da demo)

A aplicação também roda em container, com a mesma imagem usada no Cloud Run. O serviço `app` fica no perfil `app`, então `docker compose up -d` continua subindo só o banco.

```bash
docker compose --profile app build
docker compose up -d db
docker compose --profile app run --rm app python scripts/init_db.py
docker compose --profile app run --rm app python scripts/etl.py
docker compose --profile app run --rm app python scripts/ingest_docs.py
docker compose --profile app up -d   # http://localhost:8501 (APP_PORT no .env muda a porta)
```

O mapeamento local → GCP (Cloud Run + Cloud SQL + Secret Manager) e o passo a passo do deploy opcional estão em [docs/arquitetura/deploy_gcp.md](docs/arquitetura/deploy_gcp.md).
