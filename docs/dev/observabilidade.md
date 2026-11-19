# Observabilidade do agente: traces do turno (#39)

Etapa **5c** do diagrama *Agent System*: cada turno vira um trace no **Cloud Trace**, com as chamadas ao modelo, as ferramentas, os tokens e a latência. O código fica em [`src/sabor/observabilidade.py`](../../src/sabor/observabilidade.py).

## Decisão: Cloud Trace (e não Logfire)

O PydanticAI emite OpenTelemetry, então os dois usam a mesma instrumentação e só muda o exportador. A comparação:

| | Cloud Trace | Pydantic Logfire |
|---|---|---|
| Custo | Dentro da cota gratuita para o volume da cooperativa | Plano gratuito também cobre |
| Autenticação | Service account do Cloud Run, sem chave | Mais um token no Secret Manager |
| Dependências | Nenhuma nova (OTLP, SDK e `google-auth` já vêm com o `pydantic-ai`) | Nenhuma nova |
| Visualização das tool calls | Árvore de spans genérica: turno → agente → `chat` / `execute_tool` | Interface feita para LLM, com a conversa e os tokens mais legíveis |
| Logs e alertas | No mesmo lugar (Cloud Logging, Cloud Monitoring) | Separados da infraestrutura |

**Escolhido: Cloud Trace.** O deploy já está no GCP, não precisa de fornecedor nem segredo extra, e os logs aparecem dentro do trace. A vantagem do Logfire é só a interface. Trocar continua barato: `OBSERVABILIDADE=otlp` com as variáveis `OTEL_EXPORTER_OTLP_*` do Logfire manda os mesmos spans para lá.

## Como funciona

```text
turno do agente                    ← span do turno (instrumentar_turno em processar_turno)
└── invoke_agent agent             ← PydanticAI (Agent.instrument_all)
    ├── chat gemini-3.5-flash-lite     modelo, tokens da chamada
    ├── execute_tool consultar_preco   nome da ferramenta
    └── chat gemini-3.5-flash-lite
```

- **Span do turno.** O decorator `instrumentar_turno` envolve `processar_turno` e grava no fim os atributos `sabor.sessao_id`, `sabor.cache_hit`, `sabor.retorno_antecipado`, `sabor.modelo`, `sabor.provedor`, `sabor.tokens` (e `_entrada`/`_saida`), `sabor.latencia_ms`, `sabor.ferramentas`, `sabor.regras_disparadas`, `sabor.tentativas_fallback`, `sabor.falha_segura` e `sabor.bloqueado_guardrail`. Falha segura marca o span como erro, o que permite filtrar esses turnos no Trace Explorer.
- **`trace_id` na auditoria (#35).** `RespostaTurno.trace_id` usa o `trace_id` do span ativo, então a coluna `auditoria_mensagens.trace_id` e o relatório da conversa (#38) apontam para o trace do turno. Com a observabilidade desligada, o campo continua sendo um UUID.
- **Thread do agente.** `_executar_com_prazo` roda o agente noutra thread (#49). A thread recebe uma cópia do contexto (`contextvars.copy_context()`); sem ela, os spans do PydanticAI cairiam num trace separado. O teste `test_spans_do_pydantic_ai_ficam_dentro_do_trace_do_turno` cobre isso.
- **Sem texto da conversa (#33).** O PydanticAI é instrumentado com `include_content=False`: nenhum prompt, resposta, argumento ou retorno de ferramenta vai para o trace. O span do turno só leva metadados. O texto, mascarado, fica só no banco.
- **Logs no trace.** Com `OBSERVABILIDADE=gcp`, os logs do pacote `sabor` saem em JSON com `severity` e `logging.googleapis.com/trace`. No Cloud Trace, as linhas de log de um turno (ex.: `desfecho=sucesso … tokens_total=…`) aparecem dentro do trace.
- **Nunca derruba o app.** Se a configuração falhar (ex.: sem credenciais do GCP na máquina local), o erro vai para o log e o app segue sem traces.

## Configuração

| `OBSERVABILIDADE` | Destino |
|---|---|
| (vazia) | Desligada. Padrão local |
| `gcp` | Cloud Trace pela Telemetry API (`https://telemetry.googleapis.com/v1/traces`), autenticado pelas credenciais padrão (ADC) |
| `otlp` | Qualquer backend OTLP/HTTP, pelas variáveis padrão `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` |
| `console` | Spans impressos no terminal, para depuração |

O nome do serviço vem de `OTEL_SERVICE_NAME`, de `K_SERVICE` (definida pelo Cloud Run) ou é `sabor-app`. Fora do Cloud Run, `gcp` precisa de `gcloud auth application-default login` e, se o projeto não vier das credenciais, de `GOOGLE_CLOUD_PROJECT`.

### No GCP

```bash
gcloud services enable telemetry.googleapis.com cloudtrace.googleapis.com
gcloud projects add-iam-policy-binding PROJETO \
  --member=serviceAccount:sabor-run@PROJETO.iam.gserviceaccount.com \
  --role=roles/telemetry.tracesWriter
gcloud run services update sabor-app --region=REGIAO --update-env-vars=OBSERVABILIDADE=gcp
```

Ative as duas APIs. No deploy de 19/11 a exportação devolveu `403 Forbidden` (no log: `Failed to export spans batch code: 403`) com só a Telemetry API ativa, e o 403 continuou por alguns minutos depois de ativar a Cloud Trace API e dar o papel, até a permissão propagar.

## Onde ver

- **Um turno específico:** pegue o `trace_id` na auditoria ou no relatório da conversa e abra `https://console.cloud.google.com/traces/list?project=PROJETO&tid=<trace_id>`.
- **Todos os turnos:** Trace Explorer, filtrando pelo span `turno do agente`. Os atributos `sabor.*` servem de filtro (ex.: `sabor.falha_segura:true`, `sabor.cache_hit:true`).
