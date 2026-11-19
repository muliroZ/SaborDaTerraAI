# Deploy: ambiente local (Docker) e mapeamento para o Google Cloud (#17)

Seção de deploy do documento de arquitetura (#18). O diagrama *Visão geral* usa ícones do Google Cloud, mas o protótipo da demonstração roda **local**: Gemini via Google AI Studio e PostgreSQL + pgvector em Docker. O deploy no GCP é **opcional** (P2). Esta seção mostra como cada bloco do diagrama corresponde a um serviço gerenciado e o que muda na configuração para chegar lá.

## Mapeamento local → GCP

| Componente | Local (demo) | GCP |
|---|---|---|
| Streamlit + agente (`processar_turno`, ferramentas, guardrails) | `uv run sabor` ou container `app` do `docker-compose` | **Cloud Run** (serviço), com a mesma imagem do `Dockerfile` |
| PostgreSQL + pgvector: tabelas operacionais, `chunks` (`vector(768)`, HNSW), cache `UNLOGGED`, sessões/mensagens e auditoria | container `db` (`pgvector/pgvector:pg17`) | **Cloud SQL for PostgreSQL 17** com a extensão `vector` |
| LLM (Gemini 3.5 Flash-Lite + fallback OpenRouter) e embeddings (`gemini-embedding-001`) | Google AI Studio (`GEMINI_API_KEY`) | Google AI Studio ou **Vertex AI** |
| Segredos (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, senha do banco) | `.env` (fora do git) | **Secret Manager**, injetado como variável de ambiente no Cloud Run |
| Observabilidade (etapa 5c, #39) | Logfire | Logfire ou **Cloud Trace** (mesma instrumentação OpenTelemetry, muda o exportador) |
| Ingestão e carga (`init_db`, `etl`, `ingest_docs`, `triagem_lote`) | `scripts/` via `uv run` ou `docker compose run` | **Cloud Run Job** com a mesma imagem |
| Imagem da aplicação | `docker compose build` | **Artifact Registry** (build com Cloud Build ou `docker push`) |

### Por que esses serviços

- **Cloud Run** roda o mesmo container da demo local sem gerenciar servidor e cobra por uso. Suporta WebSocket, que o Streamlit usa entre o navegador e o servidor.
- **Cloud SQL for PostgreSQL** mantém o banco único da arquitetura: dados operacionais, vetores (pgvector), cache e auditoria no mesmo lugar. O Cloud SQL suporta a extensão `vector`, então o `schema.sql` roda sem alteração. Um banco vetorial separado (ex.: Vertex AI Vector Search) não compensa com ~30 documentos.
- **Secret Manager** tira as chaves do `.env`. O Cloud Run monta cada segredo como variável de ambiente com o mesmo nome que o código já lê (`os.getenv`), então o código não muda.
- **AI Studio × Vertex AI**: o AI Studio é o que o protótipo usa e basta para a demo. O Vertex AI autentica pela service account do Cloud Run (sem chave) e mantém os dados na região escolhida, mas exige trocar o provider do PydanticAI e do cliente de embeddings.
- **Região sugerida**: `southamerica-east1` (São Paulo), a mais próxima de Garanhuns.

## Ambiente local com Docker (Plano B da demo)

O `Dockerfile` gera a imagem da aplicação. A mesma imagem serve para o Streamlit e para os scripts de carga. O `docker-compose.yml` tem dois serviços:

- `db`: PostgreSQL 17 + pgvector (sobe com `docker compose up -d`, como antes);
- `app`: a aplicação, no perfil `app`, para não mudar o fluxo de quem desenvolve com `uv run sabor`.

```bash
cp .env.example .env                                   # preencha GEMINI_API_KEY e OPENROUTER_API_KEY
docker compose --profile app build                     # gera a imagem sabor-app
docker compose up -d db                                # sobe o banco

# Carga (idempotente), rodando os scripts dentro da imagem:
docker compose --profile app run --rm app python scripts/init_db.py
docker compose --profile app run --rm app python scripts/etl.py
docker compose --profile app run --rm app python scripts/ingest_docs.py   # gera embeddings (usa a GEMINI_API_KEY)
docker compose --profile app run --rm app python scripts/triagem_lote.py  # opcional: fila da caixa de entrada

docker compose --profile app up -d                     # abre http://localhost:8501
```

- Dentro da rede do compose o app fala com o serviço `db` na porta 5432. O serviço `app` sobrescreve `DATABASE_URL` (vazia), `DATABASE_HOST=db` e `DATABASE_PORT=5432`, então o mesmo `.env` serve para rodar fora e dentro do container.
- `APP_PORT` no `.env` muda a porta publicada (padrão 8501), útil se o `uv run sabor` já estiver ocupando a 8501.
- A imagem roda como usuário sem privilégios (`sabor`), com `TZ=America/Recife`, e escuta na porta da variável `PORT`. O Cloud Run define `PORT=8080`; localmente ela fica em 8501.
- O `.dockerignore` deixa `.env`, `.venv`, `.git`, `docs/` e `tests/` fora do contexto de build, então nenhuma chave vai para a imagem.

**Plano B:** se o deploy na nuvem não estiver pronto ou a rede da apresentação falhar com o GCP, a demo roda inteira com os comandos acima numa máquina com Docker. Só as chamadas ao LLM e aos embeddings da busca precisam de internet. Sem ela, o agente cai na falha segura (#37) e o cache de respostas estáticas (#36) continua respondendo às perguntas já feitas.

## Deploy no GCP (opcional, P2)

> Passo a passo de referência, **não executado** na entrega do protótipo. `PROJETO`, `REGIAO` e as senhas são placeholders.

### 1. Banco (Cloud SQL)

```bash
gcloud sql instances create sabor-db \
  --database-version=POSTGRES_17 --edition=ENTERPRISE --tier=db-g1-small \
  --region=REGIAO --database-flags=timezone=America/Recife
gcloud sql databases create sabor --instance=sabor-db
gcloud sql users create sabor --instance=sabor-db --password='SENHA'
```

### 2. Segredos (Secret Manager)

```bash
printf '%s' 'SENHA'        | gcloud secrets create sabor-db-password --data-file=-
printf '%s' "$GEMINI_API_KEY"     | gcloud secrets create gemini-api-key --data-file=-
printf '%s' "$OPENROUTER_API_KEY" | gcloud secrets create openrouter-api-key --data-file=-
```

A service account do Cloud Run precisa de `roles/cloudsql.client` e `roles/secretmanager.secretAccessor`.

### 3. Seed do banco na nuvem

A forma mais simples é rodar os scripts da máquina local pelo [Cloud SQL Auth Proxy](https://cloud.google.com/sql/docs/postgres/sql-proxy), sem abrir o banco para a internet:

```bash
cloud-sql-proxy PROJETO:REGIAO:sabor-db --port 5433 &
export DATABASE_URL= DATABASE_HOST=127.0.0.1 DATABASE_PORT=5433 DATABASE_PASSWORD='SENHA'
uv run python scripts/init_db.py
uv run python scripts/etl.py
uv run python scripts/ingest_docs.py
```

No Cloud SQL o usuário `sabor` não é superusuário. Desde o PostgreSQL 16, quem cria uma role não ganha `SET ROLE` nela automaticamente, então depois do `init_db` é preciso liberar a role somente leitura do agente:

```sql
GRANT sabor_ro TO sabor;
```

Para cargas recorrentes (nova versão do dataset, triagem em lote), a mesma imagem vira um **Cloud Run Job**. O `init_db`, o ETL e a ingestão usam a mesma `sabor.db.database_url()` do app, então o job acessa o Cloud SQL pelo mesmo socket:

```bash
gcloud run jobs create sabor-carga \
  --image=REGIAO-docker.pkg.dev/PROJETO/sabor/app:v1 --region=REGIAO \
  --set-cloudsql-instances=PROJETO:REGIAO:sabor-db \
  --set-env-vars=DATABASE_HOST=/cloudsql/PROJETO:REGIAO:sabor-db \
  --set-secrets=DATABASE_PASSWORD=sabor-db-password:latest,GEMINI_API_KEY=gemini-api-key:latest \
  --command=sh --args=-c,"python scripts/init_db.py && python scripts/etl.py && python scripts/ingest_docs.py"
gcloud run jobs execute sabor-carga --region=REGIAO
```

### 4. Aplicação (Artifact Registry + Cloud Run)

```bash
gcloud artifacts repositories create sabor --repository-format=docker --location=REGIAO
gcloud builds submit --tag REGIAO-docker.pkg.dev/PROJETO/sabor/app:v1

gcloud run deploy sabor-app \
  --image=REGIAO-docker.pkg.dev/PROJETO/sabor/app:v1 --region=REGIAO \
  --add-cloudsql-instances=PROJETO:REGIAO:sabor-db \
  --set-env-vars=DATABASE_HOST=/cloudsql/PROJETO:REGIAO:sabor-db,DATA_REF_DATE=2026-10-30 \
  --set-secrets=DATABASE_PASSWORD=sabor-db-password:latest,GEMINI_API_KEY=gemini-api-key:latest,OPENROUTER_API_KEY=openrouter-api-key:latest \
  --session-affinity --timeout=3600 \
  --min-instances=0 --max-instances=1 --memory=1Gi \
  --no-allow-unauthenticated
```

- **Conexão com o banco:** o Cloud Run monta o Cloud SQL como socket Unix em `/cloudsql/PROJETO:REGIAO:sabor-db`. Quando `DATABASE_HOST` começa com `/`, `sabor.db.database_url()` monta a URL com `?host=<socket>`. Assim a senha vem do Secret Manager e não fica escrita numa `DATABASE_URL`.
- **`--session-affinity` e `--max-instances=1`:** o estado do Streamlit e a trava de turno por sessão (#13) ficam na memória do processo. Com mais de uma instância, uma reconexão pode cair em outra instância. Para a escala da cooperativa (2 pessoas da equipe), uma instância basta. Para escalar, a trava de turno teria que ir para o banco (ex.: `pg_advisory_lock` por sessão).
- **`--timeout=3600`:** a conexão WebSocket do Streamlit dura o tempo da requisição. Com o timeout padrão de 5 min, a página reconectaria no meio da conversa.
- **`--no-allow-unauthenticated`:** a autenticação do protótipo é simulada (seletor de usuário). Por isso o serviço não fica público: o acesso passa pelo IAM (`gcloud run services proxy sabor-app --region=REGIAO`) ou pelo Identity-Aware Proxy.

### Cuidados específicos da nuvem

- **Cache `UNLOGGED`** (#36): no Cloud SQL a tabela continua sem WAL. Ela é esvaziada após crash ou failover e não vai para réplicas de leitura. Isso não é problema, porque o cache é descartável e a chave inclui a versão do dataset.
- **Custos**: `db-g1-small` sem alta disponibilidade e Cloud Run com `min-instances=0` mantêm a demo barata, mas a primeira requisição tem cold start de alguns segundos. Para a apresentação ao vivo, `--min-instances=1` evita o cold start.
- **Observabilidade** (#39): o PydanticAI emite OpenTelemetry. Com o Logfire basta o token como segredo. Com o Cloud Trace entra o exportador `opentelemetry-exporter-gcp-trace`, e a service account precisa de `roles/cloudtrace.agent`.
