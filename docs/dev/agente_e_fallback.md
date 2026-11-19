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
│   ├── apresentacao.py      # Regras de exibição sem Streamlit: usuários simulados, destaques, badges e trecho das fontes
│   └── relatorio.py         # Relatório Markdown somente leitura da conversa, sem LLM (#38)
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
    ├── fallback.py          # Cadeia ModelFallback (Gemini -> Qwen3 -> Mistral), gatilhos e metadados reais (#37)
    ├── guardrails.py        # Guardrail de entrada determinístico: escopo, tamanho, prompt injection, cache hook e limites (Etapa 2)
    ├── cache.py             # Cache de respostas estáticas em tabela UNLOGGED: chave, elegibilidade e CacheRespostas (2b/5b, #36)
    ├── prompts.py           # SYSTEM_PROMPT_BASE alinhado aos 15 pedidos do case e contexto dinâmico (Etapa 3.1)
    ├── memoria.py           # Sessões e mensagens no PostgreSQL + message_history da sessão (camada mínima de #31)
    └── runner.py            # Fábrica criar_agente(), descoberta dinâmica de tools e processar_turno() (Etapas 1 a 5)

tests/
├── conftest.py              # Fixture de banco temporário com schema vazio (sessões, UI e relatório)
├── test_schema.py           # Testes da role sabor_ro e bloqueio de INSERT/UPDATE/DELETE/DROP
├── ui/
│   ├── test_app.py          # Interface com AppTest e agente simulado: turno, persistência, memória, trava (#13)
│   ├── test_apresentacao.py # Destaques de retorno antecipado, badges de flags e trecho/RBAC das fontes (#13)
│   └── test_relatorio.py    # Relatório da conversa (#38)
├── agent/
│   ├── test_schemas.py      # Testes de Fonte, Flag, Rascunho, RespostaAgente e RespostaTurno
│   ├── test_deps.py         # Testes de AgentDeps, DATA_REF_DATE e MascaradorNoOp
│   ├── test_entry_guardrail.py # Testes do guardrail de entrada (20k chars, fora de escopo, injection, cota e UsageLimits)
│   ├── test_fallback.py     # Testes de ModelFallback, gatilhos, não-gatilhos, tools e output_type (#37)
│   ├── test_prompts_tools.py # Testes do system prompt, contexto dinâmico e descoberta snake_case de tools
│   ├── test_memoria.py      # Sessão ativa, memória dos últimos N turnos, isolamento por "Nova sessão" (#31)
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

## 5. Mapa de Pendências e Guia de Integração para as Demais Issues

O núcleo do agente e o `processar_turno` já deixaram todos os pontos de extensão prontos. Abaixo está o guia exato do que cada issue pendente deve fazer quando for implementada:

### 🔧 Issues `, `#8`, `#9`, `#11` e `#12` — Ferramentas do Agente (`src/sabor/tools.py`)

- **Onde implementar:** Diretamente em [`src/sabor/tools.py`](../../src/sabor/tools.py).
- **Como o agente descobre:** A função `carregar_ferramentas_registradas()` em [`src/sabor/agent/runner.py`](../../src/sabor/agent/runner.py) inspeciona `sabor.tools` automaticamente e registra no `Agent` todas as funções públicas em `snake_case` (sem `_` no início).
- **Nomes de referência esperados:**
  - `extrair_e_classificar_pedido(ctx: RunContext[AgentDeps], ...)` — **Issue `#9`** (Pilar 4.1)
  - `consultar_dados(ctx: RunContext[AgentDeps], ...)` — **Issue `#7`** (SQL parametrizada e read-only)
  - `buscar_documentos(ctx: RunContext[AgentDeps], ...)` — **Issue `#8`** (Pilar 4.2: busca híbrida lexical + vetorial com citação)
  - `resumir_produtor(ctx: RunContext[AgentDeps], ...)` — **Issue `#11`** (Pilar 4.3: resumo financeiro/operacional)
  - `rascunhar_notificacao(ctx: RunContext[AgentDeps], ...)` — **Issue `#12`** (Pilar 4.3: notificação e proposta de alocação)
- **Regra caso criem ferramentas extras:** Qualquer função pública adicional criada em `src/sabor/tools.py` usando `snake_case` será registrada automaticamente pelo agente. Funções auxiliares internas devem começar com `_` (ex.: `_montar_filtro_sql`) para não serem expostas como tool ao LLM.
- **Acesso ao banco nas tools:** Usar sempre `ctx.deps.conn` (que já é `sabor_ro` *read-only*) e `ctx.deps.data_referencia`. Se a tool ler dados pessoais (`produtores`, `financeiro`), passar pelo `ctx.deps.mascarador.mascarar(...)` antes de devolver ao LLM.

### ✅ Issue `#7` — Ferramentas de Consulta SQL Read-Only (`src/sabor/tools.py`) — *Concluída*
- **Ferramentas já registradas no agente (`ctx: RunContext[AgentDeps]`):**
  - `buscar_produtores(...)` — Filtra produtores por produto/categoria, município, certificação orgânica vigente (`ctx.deps.data_referencia`) e status.
  - `consultar_financeiro(...)` — Consulta vendas, pagamentos, pendências, inadimplência (`vencido`) e alertas de divergência.
  - `rastrear_lote(...)` — Rastreia lote por ID exato (`LOT-2026-0089`) ou referência aproximada (`lote 2024-089`).
  - `consultar_preco(...)` — Consulta preço vigente (convencional/orgânico) e faixa de desconto de atacado.
  - `consultar_safra(...)` — Consulta disponibilidade sazonal por produto e mês (`1–12` ou abreviação).
- **Filtro de permissão por perfil (`_resolver_usuario`):** Converte `ctx.deps.usuario` (`str`) automaticamente: se vier no formato de ID de produtor (`P001`, `P003`, etc.), trava a consulta nos dados daquele produtor e bloqueia tentativas de acesso a terceiros com `SemPermissao`; qualquer outro nome (ex.: `'Dona Lúcia'`) opera com perfil `'funcionario'`.
- **Mascaramento PII:** Já integrado a `ctx.deps.mascarador.mascarar(...)` nos campos sensíveis de `produtores` e `financeiro`.
### -----------------------------------------------------------------------------------------


### ✅ **Issue `#8` (Busca híbrida lexical + vetorial sobre `chunks_busca`):** — *Concluída*
    - `buscar_documentos(ctx, pergunta, tipo_doc=..., produtor_id=..., apenas_vigentes=..., k=5)` — Combina similaridade vetorial (`gemini-embedding-001`, 768 dims, `RETRIEVAL_QUERY`) e busca lexical (`texto_busca @@ websearch_to_tsquery('portuguese', ...)`) via **RRF** (`k=60`), filtrando metadados antes do ranqueamento, aplicando limiar de relevância (`"sem resultado relevante"` abaixo do piso), cruzando validade documental com `ctx.deps.data_referencia` e devolvendo trechos mascarados com citação `[arquivo · seção]`.
- **Controle de acesso e privacidade:** Todas as consultas respeitam o perfil resolvido a partir de `ctx.deps.usuario` (`funcionario` vs. `produtor` restrito ao próprio `produtor_id`, lançando `SemPermissao` em tentativas de acesso a terceiros) e mascaram campos sensíveis via `ctx.deps.mascarador.mascarar(...)`.
### -----------------------------------------------------------------------------------------

### ✅ Issues `#9` e `#10` — Extração Estruturada e Regras Determinísticas de Triagem de Pedidos (`src/sabor/tools/pedidos.py` e `regras.py`) — *Concluídas*
- **Ferramenta registrada no agente (`ctx: RunContext[AgentDeps]`):**
  - `extrair_e_classificar_pedido(ctx, pedido_id=None, texto=None) -> Pedido | str` — Recebe o ID de um pedido salvo na caixa de entrada (`PED-2026-001`…`015`, sem diferenciar maiúsculas) ou um texto colado no chat. ID inexistente ou entrada inválida voltam como texto para o modelo, sem derrubar o turno. A chamada ao LLM de extração soma no uso do turno (`usage=ctx.usage`, #32).
- **Etapa 1 — Extração pelo LLM (`#9` — `pedidos.py`, `extrair_pedido`):**
  - Mascara PII antes do envio e manda junto os nomes do catálogo. O modelo preenche só o que exige leitura: `tipo`, itens como escritos (`produto_texto`, `produto` da lista, quantidade, unidade, orgânico, opcional, `ambiguo`), comprador, pagamento e o trecho literal da data (`entrega.data_texto`).
  - Campos calculados (`urgencia`, `sinalizacoes`, `pedido_id`, `entrega.data`/`data_fim`/`data_estimada`/`alerta`, `item.produto_id`/`produtores_elegiveis`) ficam fora do schema enviado ao modelo (`SkipJsonSchema`) e são descartados e refeitos em toda classificação.
- **Etapa 1b — Normalização sem LLM (`#9` — `normalizar_pedido`, `datas.py`, `catalogo.py`):**
  - `resolver_data` calcula a data a partir de `data_recebimento` (ou `deps.data_referencia` para texto colado): "hoje", "amanhã", dia da semana, datas explícitas, intervalos ("04 a 08 de novembro" → `data` e `data_fim`) e mês sem dia ("jan/2027" → só `data_estimada`). Se a expressão cita um dia da semana que não bate com a data (`PED-2026-001`, `PED-2026-002`), `data` fica vazia, `alerta` sugere a data mais próxima e entra `'data_inconsistente'`.
  - `Catalogo.resolver` aceita só correspondência única (nome/sinônimo exato, nome contido no texto a partir da primeira palavra, ou nome que é o único a conter o texto). O que não resolve vira item ambíguo com `observacao`, mesmo que o modelo não tenha marcado (ex.: "manteiga" em `PED-2026-005`).
- **Etapa 2 — Classificação Determinística em Python (`#10` — `regras.py`):**
  - Executa `classificar_pedido(pedido, conn=..., mensagem_original=..., data_referencia=..., data_global=...)` **100% em Python sobre o PostgreSQL** (sem delegar regras de negócio ao LLM e sem constantes *hardcoded* de IDs/datas):
    - **Fora de safra (`fora_de_safra`):** Consulta `sazonalidade_mensal` no mês estimado da entrega (`nivel IN ('nenhuma', 'baixa')`).
    - **Acima da capacidade (`acima_capacidade`):** Extrai dinamicamente a capacidade semanal dos `chunks` (`ficha` / `certificado`) e compara com o volume do item, o prazo de entrega ($\le 1$ dia exigindo $\ge 80\%$ da produção semanal ou carga concentrada $\ge 200\text{ kg}$ nos mesmos produtores) e a demanda agregada pendente na caixa de entrada.
    - **Conformidade (`conformidade`):** Verifica no banco interdições em relatórios de `inspecao` da ADAGRO (`NC-001` no frango de `P017`), produtos de origem animal sem `registro_sie` (`P011`, `P017`) ou com SIE pendente em contrato público (`P018`), falta de `dap_caf` (`P006`, `P010`), certificação orgânica vencida ou a vencer até a data de entrega (`P008`, `P004` em jan/2027), solicitação de versão orgânica inexistente em `precos` (`preco_organico IS NULL` para banana/maracujá) e histórico de compras anteriores do cliente com produtor sem SIE.
    - **Crédito / Inadimplência (`inadimplencia`, `credito`):** Soma faturas `status_pagamento = 'pendente'` com `data_vencimento < data_corte` na tabela `financeiro` (detectando `Rede SuperBom` e `PNAE`).
    - **Privacidade (`privacidade`):** Sinaliza presença ou solicitação de CPF/RG, dados bancários (`IBAN`/`SWIFT`), chave PIX ou consulta de saldo financeiro pessoal.
    - **Ambiguidade (`ambiguidade`):** Combina itens ambíguos (marcados pelo modelo ou pelo catálogo na Etapa 1b), conflito de data, referências temporais abertas (`"esse mês"`) e divergência entre o cargo declarado pelo signatário e o registrado no contrato do banco para o mesmo CPF.
    - **Produtores elegíveis por item (`item.produtores_elegiveis`):** Filtra `produtor_produtos` mantendo apenas produtores ativos que atendem todas as restrições sanitárias (SIE/ADAGRO) e de certificação orgânica vigentes na data da entrega.
    - **Urgência (`critica`, `alta`, `normal`, `baixa`):** Calculada por prazo relativo (`data_estimada - data_recebimento`) e natureza da mensagem, reproduzindo integralmente a matriz das 15 mensagens em `docs/pedidos.md`.
  - **Reuso na Etapa 4a (`#34`):** As funções de `src/sabor/tools/regras.py` (`listar_produtores_elegiveis_item`, `verificar_inadimplencia_comprador`, `_produto_interditado_em_inspecao`) ficam disponíveis para o Guardrail de Saída.
### -----------------------------------------------------------------------------------------

### ✅ Issue `#11` — Resumo Operacional e Financeiro do Produtor (`src/sabor/tools/resumir_produtor.py`) — *Concluída*
- **Ferramenta registrada no agente (`ctx: RunContext[AgentDeps]`):**
  - `resumir_produtor(ctx, produtor)` — Aceita tanto o ID exato (`'P001'`) quanto o nome parcial/completo com ou sem acentos (`'Maria das Graças'`).
- **O que consolida:**
  - **Vendas e Pendências (reaproveitando `consultar_financeiro` da `#7`):** Soma vendas de setembro e outubro do ano de referência (`P001` = `R$ 1.090,00` em setembro, `R$ 0,00` em outubro, `R$ 0,00` pendente), total pendente e total vencido em relação a `ctx.deps.data_referencia`.
  - **Pedidos em aberto elegíveis:** Lista pedidos `status = 'pendente'` que demandam produtos fornecidos pelo produtor, filtrando automaticamente por elegibilidade (pedidos orgânicos exigem certificação vigente; produtos de origem animal exigem registro SIE ativo).
  - **Alertas documentais e sanitários:** Sinaliza certificação orgânica vencendo em até 45 dias (`P004`) ou vencida (`P008`), não conformidades/apontamentos em relatórios de inspeção da ADAGRO (`P017`) e ausência de registro SIE ou laudo sanitário para produtores de origem animal (sem gerar falso positivo para produtores vegetais).
  - **Rascunho na resposta estruturada:** Devolve `Rascunho(destinatario=..., conteudo=...)` com texto em formatação padrão limpa, tom simples e direto, e compradores mascarados via `ctx.deps.mascarador`.
- **Controle de acesso (RBAC):** Funcionário pode consultar qualquer produtor; perfil `produtor` só acessa o próprio resumo (tentativas de consultar terceiros disparam `SemPermissao`).
### -----------------------------------------------------------------------------------------

### ✅ Issue `#15` — Relatórios Autônomos sob Demanda (`src/sabor/tools/relatorios.py`) — *Concluída*
- **Ferramenta registrada no agente:** `gerar_relatorio(ctx, tipo)`, com `tipo` em:
  - `contas_a_receber` — pendências vencidas × a vencer por comprador, com maior atraso e transações (`consultar_financeiro`). Na data de referência: R$ 8.690,00 vencidos (SuperBom R$ 6.020,00, PNAE R$ 2.670,00) e R$ 4.690,00 a vencer.
  - `organicos_vigentes` — produtores com certificação vigente agrupados por produto (`buscar_produtores(organico=True)`), cada produto confirmado só na seção `PRODUTOS CERTIFICADOS` do próprio certificado (`buscar_documentos`, com busca direcionada ao produtor se a ampla não trouxer a seção), numa linha que não o exclua ("não contemplado", "exceto"); observações e outras seções não confirmam produto; renovação em até 90 dias (P004, P012) e certificação vencida ainda marcada no cadastro (P008) em "Atenção".
  - `dossie_adagro` — para a notificação `PED-2026-014`: cita o que foi pedido e, para cada item, o que existe e o que falta (cadastro, certificações, laudos dos últimos 12 meses, últimos 3 lotes por produto, contratos institucionais), além das NCs da inspeção anterior com indício de descumprimento (NC-001: venda de frango em `FIN-2026-018`). Restrito ao perfil funcionário.
- **Como o arquivo chega à UI:** a tool monta o Markdown sem LLM, guarda em `ctx.deps.relatorios` e devolve ao modelo só `destaques` (cada um com as próprias fontes). Na Etapa 4 o runner **sempre sobrescreve** `RespostaAgente.relatorios` com o que as tools geraram (um valor enviado pelo modelo no `final_result` é descartado), passando o Markdown pelo mascarador (sem restaurar, como os rascunhos) antes do guardrail de saída e do `pos_processar`. O guardrail de saída avalia o texto do modelo e não remove os relatórios, montados sem LLM; daí seguem em `RespostaTurno.relatorios` para auditoria, memória (só o texto entra no contexto) e para a UI, que mostra o botão de download e a pré-visualização.
- **Guardrails:** o pedido passa pelo guardrail de entrada e pelos `UsageLimits` como qualquer turno; as consultas herdam o filtro por perfil e o mascaramento das tools `#7`/`#8`. - **Ausência documental:** só é afirmada pelo **inventário do acervo** (`listar_documentos`, tabela `documentos`, com o filtro por perfil); uma busca sem resultado nunca vira "não existe". Documento no inventário cujo trecho não foi lido aparece como "não foi possível confirmar" (lista de produtos do certificado, data do laudo, contratante do contrato). Com a busca fora do ar, a existência vem do inventário e o que depende do texto fica a confirmar.
- **Apoio:** `listar_lotes` (em `consultar_dados.py`) e `listar_documentos` (em `documentos.py`), não exportadas como tools, e `ProdutorResultado.produtos_origem_animal`.
- **Não confundir** com o relatório da conversa (`#38`, `relatorio.py`), que agora apenas lista os relatórios gerados em cada turno.
### -----------------------------------------------------------------------------------------
---

### 🧠 Issue `#31` — Memória de Sessão no PostgreSQL (Etapas 1 e 5b)
> **Implementação da `#31`** em [`src/sabor/agent/memoria.py`](../../src/sabor/agent/memoria.py): tabelas `sessoes`/`mensagens`, `obter_ou_criar_sessao_ativa`, gravação de todos os desfechos do turno, histórico completo serializado com `new_messages_json()` e restaurado por `ModelMessagesTypeAdapter`, limitado por `MEMORIA_MAX_TURNOS` (padrão 6). Turnos barrados/falhas ficam no relatório, mas não voltam ao prompt. A versão SHA-256 é publicada após as cargas em `dataset_metadata` e lida pelo runner para versionar o cache.
>
> **Decisão de contrato:** `sessoes.id` e as FKs permanecem `text`, com UUID gerado por padrão. Isso conserva compatibilidade com `processar_turno(sessao_id: str)`, testes/integrações legadas e auditoria; o requisito literal de tipo `uuid` da #31 fica como desvio documentado a aprovar, pois migrar exige converter IDs legados e coordenar a #35.

- **Pontos de integração:** a interface injeta `carregar_memoria` e `salvar_memoria` no `processar_turno`; o callback de gravação aceita também `historico_modelo_json` para persistir a sequência completa produzida pelo PydanticAI. Registros antigos seguem sendo reconstruídos como pares simples de pergunta/resposta.

---

### ✅ Guardrail de Entrada (Etapa 2) — *Concluída* e 🛡️ Guardrail de Saída (Etapa 4)
- **Onde está implementado (Etapa 2):** [`src/sabor/agent/guardrails.py`](../../src/sabor/agent/guardrails.py), integrado em [`runner.py`](../../src/sabor/agent/runner.py) e [`schemas.py`](../../src/sabor/agent/schemas.py).
- **Como funciona a Etapa 2:**
  1. **2a (Escopo, Tamanho e Prompt Injection):** Bloqueia deterministicamente mensagens vazias, acima do limite (`GUARDRAIL_MAX_CHARS`, padrão `4000`), assuntos fora do domínio da cooperativa e tentativas de *prompt injection* sem consumir tokens do LLM (`modelo_efetivo=None`).
  2. **2b (Cache de Respostas Estáticas — `#36`):** ver a seção da `#36` abaixo. Hit devolve `RetornoAntecipado(motivo="CACHE_HIT")` com `bloqueado_guardrail=False` e `cache_hit=True`, sem chamar o LLM.
  3. **2c (Orçamento de Turno e Cota por Sessão):** Verifica o limite de requisições por sessão (`SESSAO_REQUEST_LIMIT`, padrão `50`) antes do LLM e injeta `UsageLimits(request_limit=..., total_tokens_limit=...)` no `agente.run_sync`. Caso ocorra `UsageLimitExceeded`, captura a exceção sem *stack trace* na UI e devolve `RetornoAntecipado(motivo="ORCAMENTO_TURNO_ESGOTADO")` sem afetar o turno seguinte.
  4. **Pós-processamento garantido:** Todos os retornos antecipados passam pelos hooks de auditoria (`#35`) e memória (`#31`).
- **O que falta para a Etapa 4 (`pos_processar` — `#34`):** Hook `(RespostaAgente) -> RespostaAgente` em `processar_turno` chamado logo após a restauração de PII, reaproveitando `src/sabor/tools/regras.py` (`#10`) para aplicar travas determinísticas de saída (bloqueios obrigatórios de `P017`, `P008` e exigência de fonte factual).

---

### ⚡ Issue `#36` — Cache de Respostas Estáticas em Tabela `UNLOGGED` (Etapas 2b e 5b)
- **Onde está implementado:** [`src/sabor/agent/cache.py`](../../src/sabor/agent/cache.py), tabela `cache_respostas` em [`schema.sql`](../../src/sabor/sql/schema.sql), integrado em `guardrail_entry.verificar_cache_estatico` (2b) e no fim do `processar_turno` (5b). A interface injeta `cache_repo=CacheRespostas(conexao)`.
- **Por que `UNLOGGED`:** Redis foi descartado na reunião de 16/11. A tabela não escreve WAL (mais rápida) e o PostgreSQL a esvazia após um crash, o que é aceitável para cache.
- **Chave:** SHA-256 de pergunta normalizada (minúsculas, sem acento e sem pontuação) + perfil + versão do dataset (`dataset_metadata`) + data de referência (`DATA_REF_DATE`). Nova carga de dados publica outra versão e as chaves antigas deixam de ser encontradas. Com versão `sha256-unavailable` o cache não é usado.
- **2b (leitura):** só para o perfil `funcionario` e fora de contexto multi-turno (primeira pergunta da sessão ou pergunta autocontida, pela heurística `pergunta_autocontida`). Hit conta em `hits`, responde com as mesmas fontes e flags e zero tokens.
- **5b (gravação):** só respostas estáticas e seguras (`resposta_cacheavel`): sem dados pessoais na pergunta, no texto, nas flags e nas fontes (detectados pelo `Mascarador`, `#33`), sem retorno antecipado, sem bloqueio/regras do guardrail de saída, sem falha segura e sem rascunhos ou relatórios; mais as mesmas condições de perfil e contexto da leitura. Falha no banco do cache só gera log: o turno segue normalmente.
- **Configuração e limpeza:** `CACHE_TTL_S` (padrão `86400`, 24 h; `0` desliga). `uv run python scripts/limpar_cache.py` remove tudo; `--expirados` remove só as vencidas (cada gravação também descarta as expiradas).

---

### 🔒 Issue `#33` — Mascaramento de Dados Pessoais (PII) antes do LLM e Restauração (Etapa 3.3 → 4c) — ✅ CONCLUÍDA
- **Onde está implementado:** [`src/sabor/agent/mascarador.py`](../../src/sabor/agent/mascarador.py), integrado em [`deps.py`](../../src/sabor/agent/deps.py), [`runner.py`](../../src/sabor/agent/runner.py) e [`tools.py`](../../src/sabor/tools.py).
- **Como funciona:**
  - `AgentDeps.mascarador` cria uma instância de `Mascarador` por turno (com mapa `token -> valor` exclusivamente em memória).
  - Substitui CPF, RG, telefone, e-mail, banco, agência, conta corrente e chave PIX por tokens (`[CPF_1]`, `[RG_1]`, `[TEL_1]`, `[EMAIL_1]`, `[BANCO_1]`, `[AGENCIA_1]`, `[CONTA_1]`, `[PIX_1]`), aceitando formatos com e sem pontuação.
  - Mascara tanto a **mensagem do usuário antes do prompt** (ex.: `PED-2026-013` e `PED-2026-015`) quanto o retorno das ferramentas (`#7`, `#8`, `#11` e futura `#12`).
  - Na **Etapa 4c**, `_restaurar_pii_resposta()` restaura os tokens **exclusivamente em `resposta.texto`** (para o operador interno) e **nunca em `resposta.rascunhos`** (protegendo rascunhos destinados a terceiros conforme a LGPD).
- **Como a futura tools (`#12`) devem usar:**
  - Basta chamar `ctx.deps.mascarador.mascarar(texto)` (ou `ctx.deps.mascarador.mascarar_campo(valor, 'PIX')`) antes de devolver qualquer string com dados de produtores/compradores.

---

### 📋 Issue `#35` — Auditoria por Mensagem: Tabela e Gravação (Etapa 5a)
- A função [`registrar_auditoria`](../../src/sabor/agent/auditoria.py) grava uma linha por resposta, ligada ao `message_id` da mensagem do assistente. A UI chama o hook após persistir o turno; retornos antecipados, cache hit, orçamento esgotado e falhas seguras também passam pelo hook.
- A auditoria armazena modelo/provedor, ferramentas, fontes, regras, retorno antecipado, `cache_hit` (`#36`), tokens, latência e `trace_id`; não copia texto de pergunta/resposta e mascara PII em valores textuais de fontes/regras.
- `auditoria_mensagens.sessao_id` referencia `sessoes.id` e `(message_id, sessao_id)` referencia a mensagem correspondente. Índice único em `message_id` limita a uma auditoria por mensagem. Constraints `NOT VALID` mantêm linhas históricas válidas e verificam novas gravações.
- A cardinalidade também está representada no ER de [`docs/README.md`](../README.md#modelo-de-dados).

---

### 💬 Issue `#13` — Interface Streamlit (Chat, Trava de Turno, Seletor de Usuário e Fontes)
- **Status: ✅ implementada** em [`src/sabor/ui/app.py`](../../src/sabor/ui/app.py) (rodar com `uv run sabor`).
- **Fluxo do turno:** `st.chat_input` → `executar_turno()` → `processar_turno(sessao_id, mensagem, usuario, carregar_memoria=..., salvar_memoria=..., requisicoes_na_sessao=...)` → `st.rerun()`. Os hooks das etapas 1 e 5b abrem e fecham conexões curtas: nenhuma conexão de escrita fica aberta durante a chamada ao modelo. O `processar_turno` chama `salvar_memoria` em todos os desfechos, inclusive falha segura. O histórico exibido sai sempre de `mensagens` (banco), nunca de `st.session_state`.
- **Trava de turno:** o input e os botões ficam desabilitados enquanto o turno roda (`submit_mode='disable'` + flag `processando`) e há uma `threading.Lock` por `sessao_id` no processo, que impede dois turnos simultâneos na mesma sessão (ex.: duas abas).
- **Seletor de usuário simulado:** `Dona Lúcia`, `Seu Antônio` (perfil funcionário) e `P001`…`P018` (perfil produtor), persistido em `?usuario=` na URL. Cada usuário tem a própria sessão ativa.
- **Relatórios sob demanda (`#15`):** cada `turno.relatorios` vira um bloco com `st.download_button` (`.md`) e pré-visualização, também nas mensagens do histórico.
- **Exibição:** fontes em expander com o trecho (chunk do documento ou registro tabular; completo para a equipe interna, como o texto restaurado na etapa 4c, e com dados pessoais ocultos/mascarados e só os próprios registros para o perfil produtor), flags como badges coloridos (bloqueio em vermelho, urgência em laranja, ambiguidade em azul, privacidade em violeta), rascunhos em `st.code` copiável e retornos antecipados em caixas distintas (`info`/`warning`/`error`; cache hit vira selo verde).
- **Nova sessão:** diálogo com "Baixar relatório da conversa" (`ui/relatorio.py`, `#38`), pré-visualização e confirmação; sessões anteriores ficam listadas na sidebar só para leitura.
- **Sidebar:** data de referência, versão do dataset (com assinatura dos documentos ingeridos) e status do banco.
- **Sugestões de perguntas** da demo por perfil: balões na área do chat, abaixo da saudação do CajAI; com a conversa em andamento, ficam no botão "Sugestões de perguntas" no fim do chat (#56).

---

## 6. Limitações Observadas nos Modelos de Fallback (Notas para a Apresentação de 10 min)

Conforme exigido no último item da Issue `#37` (*"Testar se os modelos de fallback seguem as ferramentas e o `output_type` (registrar limitações para a apresentação)"*) e medido nos benchmarks da equipe (Seção 4.1 do PDF de Arquitetura):

1. **Conformidade com `output_type` (`RespostaAgente`) e Ferramentas:**
   - **Gemini 3.5 Flash-Lite (Primário):** Suporte nativo a *Structured Outputs* e *Tool Calling* com baixa latência.
   - **Qwen3 32B (Fallback 1 via OpenRouter):** Excelente capacidade de raciocínio e esclarecimento de ambiguidades (`13/15` nos testes de triagem e `3/3` em escolha de ferramenta), porém apresentou **1 saída com schema inválido** (`2/3` em decisão com fonte) e **latência superior a 10s em 4 de 21 chamadas**. Por isso:
     - Implementamos o `resposta_invalida_para_fallback` (Response Handler no `ModelFallback`) para rejeitar automaticamente respostas com schema quebrado e acionar o Fallback 2 (`Mistral Small 3.2`);
     - Ajustamos o teto global do turno (`TIMEOUT_TURNO_S`) para **20 segundos** (hoje `12s` de prazo total por chamada, ver `#49`), evitando que o Qwen3 32B sofra corte prematuro quando acionado.
   - **Mistral Small 3.2 (Fallback 2 via OpenRouter):** Latência muito baixa (`0/21` acima de 10s) e menor custo, mas menor rigor proativo em regras sanitárias sutis (ex.: não sinalizou sozinho a suspensão sanitária de `P017` sem apoio de regra determinística). Isso justifica a manutenção das travas determinísticas em código Python na Etapa 4 fora do prompt.
