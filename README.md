# CajAI

Assistente de IA operacional para a cooperativa **Sabor da Terra**, criado para apoiar a equipe e os produtores (`P001`…`P018`) na triagem de pedidos, verificação sanitária e contratual, consulta financeira e redação de notificações sem envio automático.

O sistema combina **extração estruturada + regras determinísticas** com **busca híbrida em documentos** (pgvector + `tsvector`), **guardrails de entrada e saída**, **mascaramento de dados pessoais (LGPD/PII)**, **fallback entre modelos** e **cache de respostas estáticas**.

---

## Mapa da Documentação

| Onde está | O que contém |
| :--- | :--- |
| **[docs/projeto.md](docs/projeto.md)** | Visão geral do desafio, escopo funcional, personas e critérios de negócio do CajAI. |
| **[docs/dados_gerais.md](docs/dados_gerais.md)** | Índice geral da documentação técnica e dos datasets da cooperativa. |
| **[docs/arquitetura/deploy_gcp.md](docs/arquitetura/deploy_gcp.md)** | Arquitetura em nuvem, mapeamento local → GCP (Cloud Run, Cloud SQL, Secret Manager) e passo a passo de deploy. |
| **[docs/Arquitetura de Solução e Decisões Técnicas — SaborDaTerraAI.pdf](docs/Arquitetura%20de%20Solução%20e%20Decisões%20Técnicas%20—%20SaborDaTerraAI.pdf)** | Documento consolidado de arquitetura da solução e justificativas técnicas. |
| **[docs/diagramas/](docs/diagramas/)** | Diagramas de visão geral, arquitetura do agente e modelo Entidade-Relacionamento (`.jpeg` e `.drawio`). |
| **[docs/dev/](docs/dev/)** | Especificações internas de implementação: [agente e fallback](docs/dev/agente_e_fallback.md), [mascaramento LGPD/PII](docs/dev/mascaramento_pii.md), [observabilidade](docs/dev/observabilidade.md) e [triagem em lote](docs/dev/triagem_lote.md). |
| **[docs/fontes/](docs/fontes/)** | Dicionário e regras de negócio das bases de dados (`produtores`, `pedidos`, `certificacoes`, `contratos`, `financeiro`, `lotes`, `fichas_tecnicas`, `sazonalidade`, `tabela_precos`). |

---

## Estrutura do Projeto e da Documentação

```text
.
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── scripts/                  # init_db.py, etl.py, ingest_docs.py, triagem_lote.py, cache, etc;
├── src/sabor/
│   ├── agent/                # runner, guardrails (entrada/saída), schemas, fallback, etc;
│   ├── tools/                # ferramentas de pedidos, regras, relatórios, notificações, etc;
│   ├── sql/                  # schema.sql (PostgreSQL 17 + pgvector + tsvector);
│   └── ui/                   # interface web em Streamlit (uv run sabor);
├── tests/                    # testes unitários e de integração;
└── docs/
    ├── projeto.md
    ├── dados_gerais.md
    ├── arquitetura/          # deploy_gcp.md e PDF de arquitetura
    ├── diagramas/            # Visao_Geral.jpeg, Agent-System.jpeg, ER-Diagram.jpeg e .drawio
    ├── dev/                  # guias técnicos (agente/fallback, PII, observabilidade, triagem)
    └── fontes/               # documentação das tabelas e arquivos de dados da cooperativa
```

---

## Arquitetura e Diagramas

### Visão Geral da Solução
![Visão Geral da Arquitetura](docs/diagramas/Visao_Geral.jpeg)

### Arquitetura do Agente e Fluxo de Turno
![Arquitetura do Sistema de Agente](docs/diagramas/Agent-System.jpeg)

### Modelo de Dados (ER)
![Diagrama Entidade-Relacionamento](docs/diagramas/ER-Diagram.jpeg)

*(Os arquivos editáveis `.drawio` também estão disponíveis em [`docs/diagramas/`](docs/diagramas/).)*

---

## Início Rápido (Local)

Requer **Docker** e **[uv](https://docs.astral.sh/uv/)**.

### 1. Banco de dados e carga inicial (PostgreSQL 17 + pgvector)

```bash
cp .env.example .env                  # opcional: valores vazios usam os padrões
docker compose up -d                  # sobe o Postgres 17 com pgvector (volume `pgdata`)
uv run python scripts/init_db.py      # cria todas as tabelas do schema (idempotente)
uv run python scripts/etl.py          # popula as tabelas estruturadas (reseta dados e índices)
uv run python scripts/ingest_docs.py  # ingere documentos e gera embeddings (vector 768 + tsvector)
```

- **Reset completo do schema:** `uv run python scripts/init_db.py --reset`
- **Derrubar banco e volume:** `docker compose down -v`
- **DDL e busca híbrida:** definidos em [`src/sabor/sql/schema.sql`](src/sabor/sql/schema.sql) (embeddings `vector(768)` com índice HNSW cosseno + `tsvector`).
- **Testes:** `uv run pytest` (testes de integração usam banco temporário e são ignorados se o Postgres estiver parado).

### 2. Interface Web (Streamlit)

```bash
uv run sabor  # abre http://localhost:8501 (equivale a `streamlit run src/sabor/ui/app.py`)
```

- Selecione o perfil na barra lateral (**Equipe da cooperativa** ou produtor **`P001`…`P018`**).
- As sessões e mensagens são persistidas no PostgreSQL; recarregar a página retoma a conversa e **"Nova sessão"** gera o relatório de encerramento.

---

## Operações e Recursos Principais

### Cache de respostas estáticas
Perguntas autocontidas da equipe (ex.: *"Como armazenar queijo coalho?"*) são armazenadas na tabela `UNLOGGED` `cache_respostas` (TTL configurado via `CACHE_TTL_S` no `.env`, padrão `24h`; `0` desativa) e reutilizadas sem custo de LLM. Respostas com PII, rascunhos, relatórios, bloqueios de guardrail ou consultas de perfil produtor não são cacheadas, e qualquer nova carga invalida a versão do dataset automaticamente.

```bash
uv run python scripts/atualizar_versao_dataset.py  # força atualização da versão do dataset
uv run python scripts/limpar_cache.py              # limpa o cache (--expirados remove apenas vencidos)
```

Detalhes em [docs/dev/agente_e_fallback.md](docs/dev/agente_e_fallback.md).

### Triagem em lote da caixa de entrada
Processa pedidos fora do chat via `extrair_e_classificar_pedido`, grava na tabela `triagem_pedidos` e ordena a fila por urgência (`crítica → baixa`, com erros/pendências ao final). Pedidos já extraídos reaproveitam a extração sem gastar tokens, reexecutando apenas as regras determinísticas sobre o banco atual.

```bash
uv run python scripts/triagem_lote.py --csv fila.csv --json fila.json
```

Opções disponíveis: `--limite N`, `--tokens N`, `--reprocessar` e `--somente-exportar` (sem LLM). Na UI, a equipe acompanha a fila em **"Caixa de entrada"**. Detalhes em [docs/dev/triagem_lote.md](docs/dev/triagem_lote.md).

---

## Execução Completa em Docker (Plano B / Demo)

Utiliza a mesma imagem do Cloud Run (serviço `app` isolado no profile `app`, mantendo `docker compose up -d` restrito ao banco):

```bash
docker compose --profile app build
docker compose up -d db
docker compose --profile app run --rm app python scripts/init_db.py
docker compose --profile app run --rm app python scripts/etl.py
docker compose --profile app run --rm app python scripts/ingest_docs.py
docker compose --profile app up -d   # http://localhost:8501 (ajustável via APP_PORT no .env)
```