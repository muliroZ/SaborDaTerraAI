# Núcleo do Agente Único, Orquestração de Turno e Fallback (`#37`)

[← Índice de Desenvolvimento](README.md)

Este documento detalha tudo o que foi implementado na construção do **Agente Único PydanticAI** e na política de **Fallback de Provedores LLM e Falha Segura (`#37`)**, além de especificar exatamente como os demais integrantes do grupo devem conectar suas respectivas issues.

---

## 1. Visão Geral do que foi Implementado

Seguindo a decisão arquitetural consolidada (ADR v2.1 e reunião de 16/11), o sistema adota a topologia de **Agente Único com Ferramentas Tipadas** via `pydantic-ai` (`2.68.0`), sem multiagentes nem orquestrador LLM intermediário.

Para evitar acoplamento e manter o código limpo (Princípio da Responsabilidade Única), o agente foi estruturado como um pacote modular em [`src/sabor/agent/`](../../src/sabor/agent/) com suíte de testes dedicada em [`tests/agent/`](../../tests/agent/).

### Estrutura de Arquivos Criada/Atualizada

```text

scripts/
├── init_db.py      #Iniciar banco
├── etl.py          #Dados dos csv. para o banco
├── ingest_docs.py  #Dados dos txt. para o banco (embeddings)
├── limpar_cache.py #Limpa o cache de respostas estáticas (--expirados: só as vencidas) (#36)

src/sabor/
├── __init__.py              # `uv run sabor` abre a interface Streamlit
├── db.py                    # Adicionada role sabor_ro e função conectar_readonly()
├── ui/                      # Interface Streamlit (#13)
│   ├── app.py               # App: chat, trava de turno, seletor de usuário, fontes, flags, rascunhos e nova sessão
│   └── apresentacao.py      # Regras de exibição sem Streamlit: usuários simulados, destaques, badges e trecho das fontes
├── tools/                   # Pacote modular das ferramentas read-only do agente
│   ├── __init__.py          # Exporta buscar_produtores, consultar_financeiro, rastrear_lote, consultar_preco, consultar_safra, buscar_documentos, extrair_e_classificar_pedido, resumir_produtor e gerar_relatorio
│   ├── base.py              # Contratos e utilitários compartilhados (Usuario, SemPermissao, filtros e mascaramento)
│   ├── catalogo.py          # Mapeamento de nomes livres ao catálogo canônico (produtos + produto_sinonimos) (#9)
│   ├── consultar_dados.py   # Ferramentas de consulta SQL read-only (#7)
│   ├── datas.py             # Resolução determinística da data de entrega e conflito data × dia da semana (#9)
│   ├── documentos.py        # Ferramenta buscar_documentos com busca híbrida lexical + vetorial (#8)
│   ├── pedidos.py           # Ferramenta extrair_e_classificar_pedido: modelos, extração pelo LLM e normalização (#9)
│   ├── relatorios.py        # Ferramenta gerar_relatorio: contas a receber, orgânicos vigentes e dossiê ADAGRO em Markdown (#15)
│   ├── regras.py            # Motor de regras determinísticas de classificação de pedidos, urgência e elegibilidade (#10, reutilizável na #34)
│   └── resumir_produtor.py  # Ferramenta resumir_produtor com vendas, pendências, pedidos elegíveis, alertas e rascunho (#11)
├── sql/
│   └── schema.sql           # Criação idempotente da role sabor_ro (NOLOGIN, apenas SELECT)
└── agent/
    ├── __init__.py          # Ponto de entrada enxuto (AgentDeps, RespostaAgente, RespostaTurno, criar_agente, processar_turno)
    ├── schemas.py           # Modelos Pydantic: Fonte, Flag, Rascunho, RespostaAgente, TentativaFallback, RespostaTurno, RetornoAntecipado
    ├── deps.py              # Contexto injetado: AgentDeps, obter_data_referencia() e MascaradorNoOp
    ├── fallback.py          # Cadeia ModelFallback (Gemini -> Mistral -> Qwen3), gatilhos e metadados reais (#37)
    ├── guardrails.py        # Guardrail de entrada determinístico: escopo, tamanho, prompt injection, cache hook e limites (Etapa 2)
    ├── cache.py             # Cache de respostas estáticas em tabela UNLOGGED: chave, elegibilidade e CacheRespostas (2b/5b, #36)
    ├── prompts.py           # SYSTEM_PROMPT_BASE alinhado aos 15 pedidos do case e contexto dinâmico (Etapa 3.1)
    ├── memoria.py           # Sessões e mensagens no PostgreSQL + message_history da sessão (camada mínima de #31)
    ├── relatorio_conversa.py # Relatório Markdown somente leitura da sessão (mensagens + auditoria), sem LLM (#38)
    └── runner.py            # Fábrica criar_agente(), descoberta dinâmica de tools e processar_turno() (Etapas 1 a 5)

tests/
├── conftest.py              # Fixture de banco temporário com schema vazio (sessões, UI e relatório)
├── test_schema.py           # Testes da role sabor_ro e bloqueio de INSERT/UPDATE/DELETE/DROP
├── ui/
│   ├── test_app.py          # Interface com AppTest e agente simulado: turno, persistência, memória, trava (#13)
│   └── test_apresentacao.py # Destaques de retorno antecipado, badges de flags e trecho/RBAC das fontes (#13)
├── agent/
│   ├── test_schemas.py      # Testes de Fonte, Flag, Rascunho, RespostaAgente e RespostaTurno
│   ├── test_deps.py         # Testes de AgentDeps, DATA_REF_DATE e MascaradorNoOp
│   ├── test_entry_guardrail.py # Testes do guardrail de entrada (20k chars, fora de escopo, injection, cota e UsageLimits)
│   ├── test_fallback.py     # Testes de ModelFallback, gatilhos, não-gatilhos, tools e output_type (#37)
│   ├── test_prompts_tools.py # Testes do system prompt, contexto dinâmico e descoberta snake_case de tools
│   ├── test_memoria.py      # Sessão ativa, memória dos últimos N turnos, isolamento por "Nova sessão" (#31)
│   ├── test_relatorio_conversa.py # Relatório da conversa: conteúdo, auditoria, PII por perfil e critério de aceite (#38)
│   ├── test_cache.py        # Cache de respostas: chave, elegibilidade, critério de aceite e tabela UNLOGGED (#36)
│   └── test_runner.py       # Testes ponta a ponta do processar_turno (etapas 1 a 5, guardrail e falha segura de 20s)
└── tools/
    ├── conftest.py              # Fixtures compartilhadas do pytest (conexão/banco populado para os testes das tools)
    ├── test_consulta_dados.py   # Testes unitários e de integração das tools SQL da Issue #7 (produtores, financeiro, ...)
    ├── test_buscar_documentos.py # Testes unitários e de integração da busca híbrida lexical + vetorial (#8)
    ├── test_pedidos.py          # Extração com modelo simulado sobre PED-001, 003, 005 e 011, schema, mascaramento e orçamento (#9)
    ├── test_datas.py            # Datas relativas, intervalos, mês sem dia e conflito data × dia da semana (#9)
    ├── test_catalogo.py         # Mapeamento ao catálogo sem adivinhar nomes ambíguos (#9)
    ├── test_relatorios.py       # Relatórios sob demanda: valores, RBAC, dossiê PED-2026-014, inventário e acervo indisponível (#15)
    ├── test_regras_pedidos.py   # Testes parametrizados das 15 mensagens contra a matriz de pedidos.md e elegibilidade PNAE/SIE (#10)
    └── test_resumir_produtor.py # Testes da tool resumir_produtor: P001 (set/out/pendente), RBAC e alertas P004/P008/P017 (#11)
```

---

## 2. Segurança de Banco de Dados (Conexão Read-Only)

Para garantir que o agente e suas ferramentas jamais alterem ou apaguem registros das tabelas operacionais ou documentais:

1. **No PostgreSQL ([`src/sabor/sql/schema.sql`](../../src/sabor/sql/schema.sql)):**
   - Foi criada de forma idempotente a role `sabor_ro` com `NOLOGIN` (**sem senha exposta no código ou no SQL**) e permissão exclusiva `GRANT SELECT ON ALL TABLES IN SCHEMA public TO sabor_ro`.
2. **No Python ([`src/sabor/db.py`](../../src/sabor/db.py)):**
   - A função `conectar_readonly()` abre a conexão, executa `SET ROLE sabor_ro` usando `psycopg.sql.Identifier` (sem interpolação insegura de strings) e ativa `conn.read_only = True` na sessão do `psycopg`.
   - Qualquer tentativa de `INSERT`, `UPDATE`, `DELETE` ou `DROP TABLE` dentro da conexão do agente falha imediatamente no banco com `ReadOnlySqlTransaction` / `InsufficientPrivilege`.

---

## 3. Contratos de Dados (`schemas.py` e `deps.py`)

### 3.1. Dependências do Agente (`AgentDeps`)
Injetado automaticamente nas ferramentas e no prompt dinâmico via `RunContext[AgentDeps]`:
- `conn: psycopg.Connection` — Conexão *read-only* (`conectar_readonly()`).
- `data_referencia: date` — Lida de `DATA_REF_DATE` no `.env` (padrão `2026-10-30`).
- `dataset_version: str` — Versão/hash da carga de dados (padrão `'v1'`).
- `usuario: str` — Operador ativo selecionado na interface (padrão `'Dona Lúcia'`).
- `mascarador: Any` — Objeto com `.mascarar(str) -> str` e `.restaurar(str) -> str` (padrão `MascaradorNoOp()`).
- `relatorios: list[Relatorio]` — Relatórios gerados pelas tools no turno (`#15`); o runner limpa no início e anexa à resposta na Etapa 4.

### 3.2. Saída Estruturada do LLM (`RespostaAgente` — Etapa 3.4)
- `texto: str` — Resposta em PT-BR para o operador interno.
- `fontes: list[Fonte]` — Lista de citações (`arquivo` + `referencia`), formatáveis via `.formatar()` como `[arquivo · referencia]`.
- `flags: list[Flag]` — Alertas identificados (`fora_de_safra`, `acima_capacidade`, `conformidade`, `inadimplencia`, `ambiguidade`, `privacidade`).
- `rascunhos: list[Rascunho]` — Mensagens prontas (`destinatario`, `conteudo` em formato padrão limpo e `canal: str | None = None`) para revisão da equipe.
- `relatorios: list[Relatorio]` — **Fora do JSON Schema enviado ao LLM** (`SkipJsonSchema`): preenchido pelo runner com os relatórios das tools (`#15`), para o modelo não reescrever o arquivo.

### 3.3. Retorno do Turno (`RespostaTurno`)
Consolida a `RespostaAgente` + metadados técnicos e de auditoria:
- `sessao_id: str`, `usuario: str`
- `texto: str`, `fontes: list[Fonte]`, `flags: list[Flag]`, `rascunhos: list[Rascunho]`, `relatorios: list[Relatorio]`
- `bloqueado_guardrail: bool` — `True` quando o turno retorna antecipadamente por bloqueio na Etapa 2 (ou quando estoura `UsageLimits`).
- `cache_hit: bool` — `True` quando a resposta veio do cache de respostas estáticas (2b, `#36`), sem chamar o LLM; a auditoria (`#35`) grava esse valor na coluna `auditoria_mensagens.cache_hit`.
- `retorno_antecipado: RetornoAntecipado | None` — Objeto padronizado com `motivo` (`ENTRADA_VAZIA`, `TAMANHO_EXCEDIDO`, `FORA_DE_ESCOPO`, `PROMPT_INJECTION`, `CACHE_HIT`, `COTA_SESSAO_ESGOTADA`, `ORCAMENTO_TURNO_ESGOTADO`), `mensagem`, `fontes` e `flags` (as duas últimas preenchidas no `CACHE_HIT`).

---

## 4. Política de Fallback e Falha Segura — Issue `#37` (`fallback.py` e `runner.py`)

Implementada com a capability oficial `ModelFallback` do PydanticAI `2.68.0`:

| Ordem | Provedor | Modelo | Constante em `fallback.py` |
|---|---|---|---|
| **Primário** | Google AI Studio (`GoogleProvider`) | `gemini-3.5-flash-lite` | `MODELO_PRIMARIO` |
| **Fallback 1** | OpenRouter (`OpenRouterProvider`) | `qwen/qwen3-32b` | `MODELO_FALLBACK_1` |
| **Fallback 2** | OpenRouter (`OpenRouterProvider`) | `mistralai/mistral-small-3.2-24b-instruct` | `MODELO_FALLBACK_2` |

### Gatilhos que ACIONAM troca de provedor:
1. **Timeout ou erro de transporte HTTP:** `TimeoutError`, `httpx.TimeoutException`, `httpx.TransportError` (diretos ou em `ModelAPIError.__cause__`).
2. **Indisponibilidade ou Rate Limit:** `ModelHTTPError` com `status_code == 429` ou `status_code >= 500`.
3. **Falha de validação no `output_type` (`RespostaAgente`):** Validada preventivamente no hook `after_model_request` por `resposta_invalida_para_fallback(response: ModelResponse)`. Se o modelo devolver resposta vazia, JSON malformado ou argumentos que não validam em `RespostaAgente`, a tentativa é marcada como `outcome='rejected'` e o `ModelFallback` avança imediatamente para o próximo modelo sem desperdiçar retries no mesmo provedor.
4. **Recusa por filtro ou corte por limite de tokens:** `ContentFilterError` e `OutputTokenLimitExceeded`.

### Erros que NÃO acionam fallback (Política de Não-Fallback):
- Erros de autenticação, permissão ou requisição inválida (`HTTP 400`, `401`, `403`, `404`, `422`).
- Erros de banco de dados (`psycopg.Error`) ou exceções de regra de negócio.

### Orçamento de Tempo e Falha Segura:
- **Prazo por chamada de modelo (`TIMEOUT_POR_MODELO_S`, `.env`: `MODELO_TIMEOUT_S`):** `12.0s`, aplicado como `request_timeout` (prazo da requisição inteira, garantido pelo PydanticAI em qualquer modelo) e `timeout` (por tentativa), com `max_retries=0`. Ao estourar, o `ModelFallback` passa para o próximo modelo (`ModelRequestTimeout` é gatilho de fallback). Ajustes da `#49`:
  - o `timeout` do cliente HTTP só limita a espera entre bytes; o OpenRouter manda keep-alives enquanto processa, e uma chamada ao Qwen chegou a ~35 s com `timeout=8`/`12`. Por isso o prazo total é `request_timeout`;
  - sem `max_retries=0`, o cliente OpenAI-compatível repete cada requisição 2 vezes. Quem tenta de novo é a cadeia de fallback, trocando de provedor;
  - em 18/11 o Gemini 3.5 Flash-Lite levou ~6–9 s por chamada, inclusive numa mensagem trivial, e o limite antigo de 8 s derrubava os turnos.
- **Teto do turno (`TIMEOUT_TURNO_S`, `.env`: `TURNO_TIMEOUT_S`):** `20.0s` (`#37`). O prazo é aplicado dentro da thread do turno com `asyncio.wait_for`, que cancela a requisição em curso. Se a execução não encerrar logo (ex.: tool síncrona no meio de uma consulta), o turno devolve a falha segura em até `TIMEOUT_TURNO_S + FOLGA_ENCERRAMENTO_S` (1 s) sem esperar a thread.
- **Conexão do turno:** `psycopg` não pega o lock em `close()`, então a conexão aberta pelo turno só é fechada quando livre (`_fechar_quando_livre`). Num turno abandonado, a consulta em andamento é cancelada no servidor (`cancel_safe`).
- `criar_agente(timeout_s=...)` define o prazo de cada modelo; `processar_turno` não sobrescreve mais esse valor.
- Se todos os provedores da cadeia falharem (`FallbackExceptionGroup`) ou se o turno ultrapassar o teto, `processar_turno` captura a falha sem quebrar a aplicação e devolve `RespostaTurno(falha_segura=True, texto=MENSAGEM_FALHA_SEGURA, tentativas_fallback=...)`.

---

## 5. Limitações Observadas nos Modelos de Fallback (Notas para a Apresentação de 10 min)

Conforme exigido no último item da Issue `#37` (*"Testar se os modelos de fallback seguem as ferramentas e o `output_type` (registrar limitações para a apresentação)"*) e medido nos benchmarks da equipe (Seção 4.1 do PDF de Arquitetura):

1. **Conformidade com `output_type` (`RespostaAgente`) e Ferramentas:**
   - **Gemini 3.5 Flash-Lite (Primário):** Suporte nativo a *Structured Outputs* e *Tool Calling* com baixa latência.
   - **Qwen3 32B (Fallback 1 via OpenRouter):** Excelente capacidade de raciocínio e esclarecimento de ambiguidades (`13/15` nos testes de triagem e `3/3` em escolha de ferramenta), porém apresentou **1 saída com schema inválido** (`2/3` em decisão com fonte) e **latência superior a 10s em 4 de 21 chamadas**. Por isso:
     - Implementamos o `resposta_invalida_para_fallback` (Response Handler no `ModelFallback`) para rejeitar automaticamente respostas com schema quebrado e acionar o Fallback 2 (`Mistral Small 3.2`);
     - Ajustamos o teto global do turno (`TIMEOUT_TURNO_S`) para **20 segundos** (hoje `12s` de prazo total por chamada, ver `#49`), evitando que o Qwen3 32B sofra corte prematuro quando acionado.
   - **Mistral Small 3.2 (Fallback 2 via OpenRouter):** Latência muito baixa (`0/21` acima de 10s) e menor custo, mas menor rigor proativo em regras sanitárias sutis (ex.: não sinalizou sozinho a suspensão sanitária de `P017` sem apoio de regra determinística). Isso justifica a manutenção das travas determinísticas em código Python na Etapa 4 fora do prompt.
