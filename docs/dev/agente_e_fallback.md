# Núcleo do Agente Único, Orquestração de Turno e Fallback (`#37`)

[← Índice de Desenvolvimento](README.md)

Este documento detalha tudo o que foi implementado na construção do **Agente Único PydanticAI** e na política de **Fallback de Provedores LLM e Falha Segura (`#37`)**, além de especificar exatamente como os demais integrantes do grupo devem conectar suas respectivas issues.

---

## 1. Visão Geral do que foi Implementado

Seguindo a decisão arquitetural consolidada (ADR v2.1 e reunião de 16/11), o sistema adota a topologia de **Agente Único com Ferramentas Tipadas** via `pydantic-ai` (`2.68.0`), sem multiagentes nem orquestrador LLM intermediário.

Para evitar acoplamento e manter o código limpo (Princípio da Responsabilidade Única), o agente foi estruturado como um pacote modular em [`src/sabor/agent/`](../../src/sabor/agent/) com suíte de testes dedicada em [`tests/agent/`](../../tests/agent/).

### Estrutura de Arquivos Criada/Atualizada

```text
src/sabor/
├── db.py                    # Adicionada role sabor_ro e função conectar_readonly()
├── tools.py                 # Documentação e convenção snake_case para as tools do time
├── sql/
│   └── schema.sql           # Criação idempotente da role sabor_ro (NOLOGIN, apenas SELECT)
└── agent/
    ├── __init__.py          # Ponto de entrada enxuto (AgentDeps, RespostaAgente, RespostaTurno, criar_agente, processar_turno)
    ├── schemas.py           # Modelos Pydantic: Fonte, Flag, Rascunho, RespostaAgente, TentativaFallback, RespostaTurno
    ├── deps.py              # Contexto injetado: AgentDeps, obter_data_referencia() e MascaradorNoOp
    ├── fallback.py          # Cadeia ModelFallback (Gemini -> Qwen3 -> Mistral), gatilhos e metadados reais (#37)
    ├── prompts.py           # SYSTEM_PROMPT_BASE alinhado aos 15 pedidos do case e contexto dinâmico (Etapa 3.1)
    └── runner.py            # Fábrica criar_agente(), descoberta dinâmica de tools e processar_turno() (Etapas 1 a 5)

tests/
├── test_schema.py           # Testes da role sabor_ro e bloqueio de INSERT/UPDATE/DELETE/DROP
└── agent/
    ├── test_schemas.py      # Testes de Fonte, Flag, Rascunho, RespostaAgente e RespostaTurno
    ├── test_deps.py         # Testes de AgentDeps, DATA_REF_DATE e MascaradorNoOp
    ├── test_fallback.py     # Testes de ModelFallback, gatilhos, não-gatilhos, tools e output_type (#37)
    ├── test_prompts_tools.py # Testes do system prompt, contexto dinâmico e descoberta snake_case de tools
    └── test_runner.py      # Testes ponta a ponta do processar_turno (etapas 1 a 5, guardrail e falha segura de 20s)
└── tools/   
    └── test_consulta_dados.py # Testes unitários e de integração das tools SQL da Issue #7 (produtores, financeiro, ...)
    └── conftest.py # Fixtures compartilhadas do pytest (conexão/banco populado para os testes das tools)
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

### 3.2. Saída Estruturada do LLM (`RespostaAgente` — Etapa 3.4)
- `texto: str` — Resposta em PT-BR para o operador interno.
- `fontes: list[Fonte]` — Lista de citações (`arquivo` + `referencia`), formatáveis via `.formatar()` como `[arquivo · referencia]`.
- `flags: list[Flag]` — Alertas identificados (`fora_de_safra`, `acima_capacidade`, `conformidade`, `inadimplencia`, `ambiguidade`, `privacidade`).
- `rascunhos: list[Rascunho]` — Mensagens prontas (`destinatario`, `canal`, `conteudo`) para revisão da equipe.

### 3.3. Retorno do Turno (`RespostaTurno`)
Consolida a `RespostaAgente` + metadados técnicos e de auditoria:
- `sessao_id: str`, `usuario: str`
- `texto: str`, `fontes: list[Fonte]`, `flags: list[Flag]`, `rascunhos: list[Rascunho]`
- `bloqueado_guardrail: bool` — `True` quando o turno retorna antecipadamente na Etapa 2.
- `falha_segura: bool` — `True` quando todos os provedores LLM falham ou o turno excede o limite global de **20 segundos**.
- `modelo_efetivo: str | None` e `provedor_efetivo: str | None` — Extraídos diretamente dos metadados reais do `ModelResponse` do PydanticAI.
- `tentativas_fallback: list[TentativaFallback]` — Histórico detalhado de tentativas que falharam ou tiveram schema rejeitado antes da resposta final.
- `latencia_ms: int` — Tempo total do turno em milissegundos.

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
- **Timeout por chamada de modelo (`TIMEOUT_POR_MODELO_S`):** `8.0s`.
- **Timeout global do turno (`TIMEOUT_TURNO_S`):** `20.0s`.
- Se todos os provedores da cadeia falharem (`FallbackExceptionGroup`) ou se o turno ultrapassar **20 segundos**, `processar_turno` captura a falha sem quebrar a aplicação e devolve `RespostaTurno(falha_segura=True, texto=MENSAGEM_FALHA_SEGURA, tentativas_fallback=...)`.

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
---

### 🧠 Issue `#31` — Memória de Sessão no PostgreSQL (Etapas 1 e 5b)
- **Pontos de conexão no `processar_turno` ([`src/sabor/agent/runner.py`](../../src/sabor/agent/runner.py)):**
  1. **Etapa 1 (`carregar_memoria`):** Passar (ou conectar como padrão) uma função `(sessao_id: str) -> Sequence[ModelMessage]` que busca os últimos N turnos da sessão no banco.
  2. **Etapa 5b (`salvar_memoria`):** Passar (ou conectar como padrão) uma função `(sessao_id: str, mensagem_usuario: str, turno: RespostaTurno) -> None` que grava a pergunta e a resposta na tabela de sessão usando conexão de escrita (`conectar()`).

---

### 🛡️ Guardrail de Entrada (Etapa 2) e Pós-Processamento / Guardrail de Saída (Etapa 4)
- **Pontos de conexão no `processar_turno` ([`src/sabor/agent/runner.py`](../../src/sabor/agent/runner.py)):**
  1. **Etapa 2 (`guardrail_entrada`):** Atualmente já bloqueia mensagens vazias. Quando o guardrail de escopo/tamanho/cota for implementado, basta plugar a função `(mensagem: str) -> str | None` (retorna `None` se válida, ou a string de motivo do bloqueio para retorno antecipado com `bloqueado_guardrail=True`).
  2. **Etapa 4 (`pos_processar`):** Hook `(RespostaAgente) -> RespostaAgente` chamado logo após a restauração de PII para aplicar travas determinísticas de saída (ex.: bloqueios obrigatórios de `P017` e `P008`).

---

### 🔒 Issue `#33` — Mascaramento de Dados Pessoais (PII) antes do LLM e Restauração (Etapa 3.3 → 4c)
- **O que já está pronto no agente:**
  - `AgentDeps.mascarador` já existe (usando `MascaradorNoOp` como fallback).
  - Na **Etapa 4c**, `_restaurar_pii_resposta()` em [`src/sabor/agent/runner.py`](../../src/sabor/agent/runner.py) já chama `deps.mascarador.restaurar(...)` sobre `resposta.texto` e sobre `destinatario` e `conteudo` de cada item em `resposta.rascunhos`.
- **O que a Issue `#33` precisa fazer quando ficar pronta:**
  - Substituir `MascaradorNoOp()` pelo mascarador real ao instanciar `AgentDeps` (e chamar `ctx.deps.mascarador.mascarar(...)` na saída das ferramentas que retornam CPF, telefone, endereço ou dados bancários/PIX).

---

### 📋 Issue `#35` — Auditoria por Mensagem: Tabela e Gravação (Etapa 5a)
- **O que já está pronto no agente:**
  - O `RespostaTurno` já entrega prontos: `sessao_id`, `usuario`, `texto`, `fontes`, `flags`, `modelo_efetivo`, `provedor_efetivo`, `tentativas_fallback`, `bloqueado_guardrail`, `falha_segura` e `latencia_ms`.
- **O que a Issue `#35` precisa fazer quando ficar pronta:**
  - Implementar a função `registrar_auditoria(turno: RespostaTurno) -> None` que faz o `INSERT INTO auditoria_mensagens` (usando `conectar()` normal de escrita) e plugá-la no parâmetro `registrar_auditoria` do `processar_turno`.

---

### 💬 Issue `#13` — Interface Streamlit (Chat, Trava de Turno, Seletor de Usuário e Fontes)
- **Como chamar o agente no Streamlit (`src/sabor/ui.py`):**
  ```python
  from sabor.agent import processar_turno

  turno = processar_turno(
      sessao_id=st.session_state.sessao_id,
      mensagem=mensagem_usuario,
      usuario=usuario_selecionado, # ex.: "Dona Lúcia" (funcionário) ou "P001" (trava no produtor P001)
  )
  )
  # Exibir turno.texto, turno.fontes, turno.flags e turno.rascunhos
  ```

---

## 6. Limitações Observadas nos Modelos de Fallback (Notas para a Apresentação de 10 min)

Conforme exigido no último item da Issue `#37` (*"Testar se os modelos de fallback seguem as ferramentas e o `output_type` (registrar limitações para a apresentação)"*) e medido nos benchmarks da equipe (Seção 4.1 do PDF de Arquitetura):

1. **Conformidade com `output_type` (`RespostaAgente`) e Ferramentas:**
   - **Gemini 3.5 Flash-Lite (Primário):** Suporte nativo a *Structured Outputs* e *Tool Calling* com baixa latência.
   - **Qwen3 32B (Fallback 1 via OpenRouter):** Excelente capacidade de raciocínio e esclarecimento de ambiguidades (`13/15` nos testes de triagem e `3/3` em escolha de ferramenta), porém apresentou **1 saída com schema inválido** (`2/3` em decisão com fonte) e **latência superior a 10s em 4 de 21 chamadas**. Por isso:
     - Implementamos o `resposta_invalida_para_fallback` (Response Handler no `ModelFallback`) para rejeitar automaticamente respostas com schema quebrado e acionar o Fallback 2 (`Mistral Small 3.2`);
     - Ajustamos o teto global do turno (`TIMEOUT_TURNO_S`) para **20 segundos** (`8s` por chamada individual), evitando que o Qwen3 32B sofra corte prematuro quando acionado.
   - **Mistral Small 3.2 (Fallback 2 via OpenRouter):** Latência muito baixa (`0/21` acima de 10s) e menor custo, mas menor rigor proativo em regras sanitárias sutis (ex.: não sinalizou sozinho a suspensão sanitária de `P017` sem apoio de regra determinística). Isso justifica a manutenção das travas determinísticas em código Python na Etapa 4 fora do prompt.
