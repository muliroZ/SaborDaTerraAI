# Deploy: ambiente local (Docker) e Google Cloud (#17)

Seção de deploy do documento de arquitetura (#18). A aplicação roda no **Google Cloud**: Streamlit e agente no Cloud Run, banco no Cloud SQL (PostgreSQL + pgvector), Gemini pelo Google AI Studio e traces no Cloud Trace. O deploy foi feito em **19/11/2026** seguindo os passos da seção [Deploy no GCP](#deploy-no-gcp). O ambiente Docker local continua como **Plano B** da demonstração.

## Mapeamento local → GCP

| Componente | Local (Plano B) | GCP |
|---|---|---|
| Streamlit + agente (`processar_turno`, ferramentas, guardrails) | `uv run sabor` ou container `app` do `docker-compose` | **Cloud Run** (serviço `sabor-app`), com a mesma imagem do `Dockerfile` |
| PostgreSQL + pgvector: tabelas operacionais, `chunks` (`vector(768)`, HNSW), cache `UNLOGGED`, sessões/mensagens e auditoria | container `db` (`pgvector/pgvector:pg17`) | **Cloud SQL for PostgreSQL 17** (`sabor-db`) com a extensão `vector` |
| LLM (Gemini 3.5 Flash-Lite + fallback OpenRouter) e embeddings (`gemini-embedding-001`) | Google AI Studio (`GEMINI_API_KEY`) | Google AI Studio, o mesmo (Vertex AI fica como evolução) |
| Segredos (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, senha do banco) | `.env` (fora do git) | **Secret Manager**, injetado como variável de ambiente no Cloud Run |
| Observabilidade (etapa 5c, #39) | Desligada ou `OBSERVABILIDADE=console` | **Cloud Trace** pela Telemetry API (`OBSERVABILIDADE=gcp`), com os logs correlacionados no Cloud Logging |
| Ingestão e carga (`init_db`, `etl`, `ingest_docs`, `triagem_lote`) | `scripts/` via `uv run` ou `docker compose run` | **Cloud Run Job** (`sabor-carga`) com a mesma imagem |
| Imagem da aplicação | `docker compose build` | **Artifact Registry**, com build local (`docker build` + `docker push`) |

### Por que esses serviços

- **Cloud Run** roda o mesmo container da demo local sem gerenciar servidor e cobra por uso. Suporta WebSocket, que o Streamlit usa entre o navegador e o servidor.
- **Cloud SQL for PostgreSQL** mantém o banco único da arquitetura: dados operacionais, vetores (pgvector), cache e auditoria no mesmo lugar. O Cloud SQL suporta a extensão `vector`, então o `schema.sql` roda sem alteração. Um banco vetorial separado (ex.: Vertex AI Vector Search) não compensa com ~30 documentos.
- **Secret Manager** tira as chaves do `.env`. O Cloud Run monta cada segredo como variável de ambiente com o mesmo nome que o código já lê (`os.getenv`), então o código não muda.
- **AI Studio × Vertex AI**: o AI Studio é o que o protótipo usa e basta para a demo. O Vertex AI autentica pela service account do Cloud Run (sem chave) e mantém os dados na região escolhida, mas exige trocar o provider do PydanticAI e do cliente de embeddings.
- **Região**: `southamerica-east1` (São Paulo), a mais próxima de Garanhuns.

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


## Deploy no GCP

> Executado em 19/11/2026 no projeto do Google AI Studio (o mesmo da `GEMINI_API_KEY`). Os comandos abaixo são os que funcionaram. `PROJETO` é o ID do projeto.

### Recursos criados

| Recurso | Nome | Configuração |
|---|---|---|
| Cloud Run (serviço) | `sabor-app` | 1 vCPU, 1 GiB, no máximo 1 instância, afinidade de sessão, timeout de 3600 s, privado (IAM) |
| Cloud Run Job | `sabor-carga` | mesma imagem; `init_db` → `etl` → `ingest_docs`; timeout de 20 min, sem nova tentativa |
| Cloud SQL | `sabor-db` | PostgreSQL 17, edição Enterprise, `db-g1-small`, zonal; flags `timezone=America/Recife` e `cloudsql.iam_authentication=on` |
| Banco e usuário | `sabor` / `sabor` | role somente leitura `sabor_ro` concedida ao usuário `sabor` |
| Secret Manager | `sabor-db-password`, `gemini-api-key`, `openrouter-api-key` | lidos pelo serviço e pelo job como variáveis de ambiente |
| Service account | `sabor-run` | `roles/cloudsql.client`, `roles/secretmanager.secretAccessor`, `roles/telemetry.tracesWriter` |
| Artifact Registry | `sabor` (Docker) | imagem `southamerica-east1-docker.pkg.dev/PROJETO/sabor/app:vN` |

### 0. Preparação

```bash
# Arch: yay -S google-cloud-cli (outras distros: instalador oficial do Google Cloud CLI)
gcloud auth login

export PROJETO=...                                   # ID do projeto
export REGIAO=southamerica-east1
export INSTANCIA="${PROJETO}:${REGIAO}:sabor-db"
export IMAGEM="${REGIAO}-docker.pkg.dev/${PROJETO}/sabor/app"
export SA="sabor-run@${PROJETO}.iam.gserviceaccount.com"
gcloud config set project "$PROJETO"
```

No zsh, use sempre `${VAR}` com chaves antes de `:`. Em `$REGIAO:sabor-db`, o zsh lê `:s` como modificador e falha com `bad substitution`.

### 1. APIs

```bash
gcloud services enable run.googleapis.com sqladmin.googleapis.com sql-component.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com \
  telemetry.googleapis.com cloudtrace.googleapis.com
```

Sem a `sql-component.googleapis.com`, o `gcloud run jobs create --set-cloudsql-instances` pergunta se pode ativá-la. Numa execução não interativa, a pergunta fica sem resposta e o comando termina com `Aborted by user`.

### 2. Banco (Cloud SQL)

```bash
gcloud sql instances create sabor-db \
  --database-version=POSTGRES_17 --edition=ENTERPRISE --tier=db-g1-small \
  --region="$REGIAO" --database-flags=timezone=America/Recife
gcloud sql databases create sabor --instance=sabor-db
```

- A criação leva de 5 a 10 minutos (`PENDING_CREATE` → `RUNNABLE`).
- `--edition=ENTERPRISE`: a máquina compartilhada `db-g1-small` só existe na edição Enterprise.
- Criada pelo console, a instância vem sem a flag `timezone` e com `cloudsql.iam_authentication=on`. Como `--database-flags` substitui a lista inteira, repita as flags que já existem (a instância reinicia):

  ```bash
  gcloud sql instances patch sabor-db \
    --database-flags=cloudsql.iam_authentication=on,timezone=America/Recife
  ```

### 3. Segredos e usuário do banco

A senha do banco é gerada direto no Secret Manager e nunca aparece no terminal. A instância criada pelo console tem política de senha: exige letra maiúscula, letra minúscula, número e caractere especial. Daí o sufixo fixo `aZ9-_`.

```bash
printf '%s' "$(openssl rand -base64 32 | tr -d '/+=\n' | head -c 28)aZ9-_" \
  | gcloud secrets create sabor-db-password --data-file=-
grep -E '^GEMINI_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n' \
  | gcloud secrets create gemini-api-key --data-file=-
grep -E '^OPENROUTER_API_KEY=' .env | cut -d= -f2- | tr -d '\r\n' \
  | gcloud secrets create openrouter-api-key --data-file=-

gcloud sql users create sabor --instance=sabor-db \
  --password="$(gcloud secrets versions access latest --secret=sabor-db-password)"
```

Para ver a senha (ex.: no Cloud SQL Studio): `gcloud secrets versions access latest --secret=sabor-db-password`.

### 4. Service account

```bash
gcloud iam service-accounts create sabor-run --display-name="SaborDaTerraAI Cloud Run"
for ROLE in roles/cloudsql.client roles/secretmanager.secretAccessor roles/telemetry.tracesWriter; do
  gcloud projects add-iam-policy-binding "$PROJETO" \
    --member="serviceAccount:${SA}" --role="$ROLE" --condition=None
done
```

A conta padrão do Compute tem permissões demais. A `sabor-run` só lê os segredos, conecta no Cloud SQL e grava traces.

### 5. Imagem (Artifact Registry)

```bash
gcloud artifacts repositories create sabor --repository-format=docker --location="$REGIAO"
gcloud auth configure-docker "${REGIAO}-docker.pkg.dev"

docker build --platform linux/amd64 -t "${IMAGEM}:v1" .
docker push "${IMAGEM}:v1"
```

- **Build local, e não Cloud Build:** o `Dockerfile` usa `RUN --mount=type=cache`, que exige BuildKit, e o Docker local garante isso. O `gcloud builds submit` não foi testado.
- **`--platform linux/amd64`:** o Cloud Run roda amd64. Isso importa em máquinas ARM, como o Apple Silicon.
- **Uma tag por versão** (`v1`, `v2`, ...) em vez de `latest`, para o rollback ser só apontar para a tag anterior.

### 6. Carga do banco (Cloud Run Job)

O job usa a mesma imagem e o mesmo `sabor.db.database_url()` do app. Por isso acessa o Cloud SQL pelo socket, sem Cloud SQL Auth Proxy.

```bash
gcloud run jobs create sabor-carga \
  --image="${IMAGEM}:v1" --region="$REGIAO" --service-account="$SA" \
  --set-cloudsql-instances="$INSTANCIA" \
  --set-env-vars="DATABASE_HOST=/cloudsql/${INSTANCIA},DATA_REF_DATE=2026-10-30" \
  --set-secrets=DATABASE_PASSWORD=sabor-db-password:latest,GEMINI_API_KEY=gemini-api-key:latest,OPENROUTER_API_KEY=openrouter-api-key:latest \
  --task-timeout=20m --max-retries=0 \
  --command=sh --args=-c,"python scripts/init_db.py && python scripts/etl.py && python scripts/ingest_docs.py"

gcloud run jobs execute sabor-carga --region="$REGIAO" --wait
```

O log termina com `Ingestão concluída com sucesso.` e um teste de busca vetorial. Para ler:

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="sabor-carga"' \
  --limit=50 --freshness=30m --format='value(textPayload)'
```

A carga é idempotente. Para uma nova versão do dataset, basta executar o job de novo.

### 7. Liberar a role somente leitura (obrigatório)

No Cloud SQL o usuário `sabor` não é superusuário. Desde o PostgreSQL 16, quem cria uma role não ganha `SET ROLE` nela automaticamente. Sem o `GRANT`, toda consulta do agente falha com `permission denied to set role "sabor_ro"`.

O `GRANT` roda pelo próprio job, trocando os argumentos só nesta execução (sem `psql` nem proxy):

```bash
gcloud run jobs execute sabor-carga --region="$REGIAO" --wait \
  --args=-c,"python -c \"from sabor.db import conectar; c = conectar(); c.execute('GRANT sabor_ro TO sabor'); c.commit()\""

# Conferência: a role sabor_ro lê os chunks (82 no dataset atual)
gcloud run jobs execute sabor-carga --region="$REGIAO" --wait \
  --args=-c,"python -c \"from sabor.db import conectar_readonly; print(conectar_readonly().execute('SELECT count(*) FROM chunks').fetchone()[0])\""
```

O `gcloud` separa `--args` por vírgula, então o comando Python não pode ter vírgulas. A alternativa é rodar o `GRANT` no **Cloud SQL Studio** (Console → SQL → `sabor-db`), entrando com o usuário `sabor`.

### 8. Aplicação (Cloud Run)

```bash
gcloud run deploy sabor-app \
  --image="${IMAGEM}:v1" --region="$REGIAO" --service-account="$SA" \
  --add-cloudsql-instances="$INSTANCIA" \
  --set-env-vars="DATABASE_HOST=/cloudsql/${INSTANCIA},DATA_REF_DATE=2026-10-30,OBSERVABILIDADE=gcp" \
  --set-secrets=DATABASE_PASSWORD=sabor-db-password:latest,GEMINI_API_KEY=gemini-api-key:latest,OPENROUTER_API_KEY=openrouter-api-key:latest \
  --session-affinity --timeout=3600 \
  --min-instances=0 --max-instances=1 --memory=1Gi \
  --no-allow-unauthenticated
```

- **Conexão com o banco:** o Cloud Run monta o Cloud SQL como socket Unix em `/cloudsql/PROJETO:REGIAO:sabor-db`. Quando `DATABASE_HOST` começa com `/`, `sabor.db.database_url()` monta a URL com `?host=<socket>`. Assim a senha vem do Secret Manager e não fica escrita numa `DATABASE_URL`.
- **`--session-affinity` e `--max-instances=1`:** o estado do Streamlit e a trava de turno por sessão (#13) ficam na memória do processo. Com mais de uma instância, uma reconexão pode cair em outra instância. Para a escala da cooperativa (2 pessoas da equipe), uma instância basta. Para escalar, a trava de turno teria que ir para o banco (ex.: `pg_advisory_lock` por sessão).
- **`--timeout=3600`:** a conexão WebSocket do Streamlit dura o tempo da requisição. Com o timeout padrão de 5 min, a página reconectaria no meio da conversa.
- **`--no-allow-unauthenticated`:** a autenticação do protótipo é simulada (seletor de usuário). Por isso o serviço não fica público e o acesso passa pelo IAM.
- **`OBSERVABILIDADE=gcp`:** traces do turno no Cloud Trace (#39). Ver [observabilidade.md](../dev/observabilidade.md).

### 9. Verificação

```bash
URL=$(gcloud run services describe sabor-app --region="$REGIAO" --format='value(status.url)')
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/_stcore/health"   # 200
curl -s -o /dev/null -w '%{http_code}\n' "$URL/_stcore/health"                          # 403: serviço privado

gcloud run services proxy sabor-app --region="$REGIAO" --port=8501   # abra http://localhost:8501
```

O chat usa WebSocket, então só dá para testá-lo no navegador, pelo proxy. Para testar um turno completo sem a interface (agente, ferramentas, Cloud SQL e Cloud Trace), rode `processar_turno` pelo job. O script vai em base64 numa variável de ambiente porque não tem vírgulas. Sem os hooks de memória e auditoria, o turno não grava nada no banco.

```bash
SMOKE=$(base64 -w0 <<'EOF'
from opentelemetry import trace
from sabor.observabilidade import configurar_observabilidade
from sabor.agent.runner import processar_turno
configurar_observabilidade()
t = processar_turno('smoke', 'Qual o preço do queijo coalho?', 'Dona Lúcia')
print('TRACE', t.trace_id, t.modelo_efetivo, t.falha_segura, t.ferramentas, t.latencia_ms)
trace.get_tracer_provider().force_flush()
EOF
)
gcloud run jobs execute sabor-carga --region="$REGIAO" --wait \
  --update-env-vars="OBSERVABILIDADE=gcp,SMOKE=${SMOKE}" \
  --args=-c,'echo $SMOKE | base64 -d | python'
```

No teste de 19/11, o turno chamou `consultar_preco` e respondeu em cerca de 5 s. O trace apareceu no Cloud Trace com o span `turno do agente` e, dentro dele, as chamadas ao Gemini e à ferramenta.

### 10. Operação

- **Nova versão do código:**

  ```bash
  docker build --platform linux/amd64 -t "${IMAGEM}:v3" . && docker push "${IMAGEM}:v3"
  gcloud run services update sabor-app --region="$REGIAO" --image="${IMAGEM}:v3"
  gcloud run jobs update sabor-carga --region="$REGIAO" --image="${IMAGEM}:v3"
  ```

  As outras flags e variáveis continuam como estavam. Para voltar atrás, use `--image` com a tag anterior.
- **Apresentação ao vivo:** `gcloud run services update sabor-app --region="$REGIAO" --min-instances=1` evita a espera da primeira abertura (cold start). Volte para `0` depois.
- **Custo:** o Cloud SQL cobra 24 h por dia, e o Cloud Run fica em zero sem uso. Fora do uso, desligue a instância:

  ```bash
  gcloud sql instances patch sabor-db --activation-policy=NEVER    # desliga
  gcloud sql instances patch sabor-db --activation-policy=ALWAYS   # religa
  ```

- **Acesso sem `gcloud`** (ex.: banca): ainda não foi feito. O caminho é o IAP no Cloud Run (`gcloud run services update sabor-app --region="$REGIAO" --iap`) e o papel `roles/iap.httpsResourceAccessor` para cada e-mail.

### Problemas encontrados

| Sintoma | Causa | Solução |
|---|---|---|
| `bad substitution` no zsh | `$REGIAO:sabor-db`: o zsh lê `:s` como modificador | `${REGIAO}` com chaves (passo 0) |
| `Aborted by user` no `gcloud run jobs create` | API `sql-component.googleapis.com` desativada e pergunta sem resposta | Ativar a API antes (passo 1) |
| `INVALID_PASSWORD: password should contain ... non-alphanumeric` | Política de senha da instância criada pelo console | Senha com caractere especial (passo 3) |
| Instância sem fuso `America/Recife` | O console não aplica a flag `timezone` | `patch` repetindo todas as flags (passo 2) |
| `permission denied to set role "sabor_ro"` (se o passo 7 for pulado) | O usuário `sabor` não tem `SET ROLE` na role que criou | `GRANT sabor_ro TO sabor` (passo 7) |
| `Failed to export spans batch code: 403` nos logs | Cloud Trace API desativada e IAM ainda propagando | Ativar `telemetry` e `cloudtrace` (passo 1), dar o papel (passo 4) e esperar alguns minutos |

### Cuidados específicos da nuvem

- **Cache `UNLOGGED`** (#36): no Cloud SQL a tabela continua sem WAL. Ela é esvaziada após crash ou failover e não vai para réplicas de leitura. Isso não é problema, porque o cache é descartável e a chave inclui a versão do dataset.
- **Cold start**: com `min-instances=0`, a primeira requisição depois de um tempo parado leva alguns segundos (ver Operação).
- **Observabilidade** (#39): cada turno vira um trace no Cloud Trace, exportado por OTLP para a Telemetry API. A decisão em relação ao Logfire e os detalhes estão em [docs/dev/observabilidade.md](../dev/observabilidade.md).
