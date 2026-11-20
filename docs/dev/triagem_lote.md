# Triagem em lote da caixa de entrada (#14)

O cenário prevê 60–100 pedidos por dia chegando por e-mail, WhatsApp e formulário. Os pipelines de ingestão gravam cada mensagem em JSON na tabela `pedidos` ("Salvar JSON no Banco" no diagrama *Visão geral*). A triagem em lote roda sobre essa tabela, **fora do chat**, e deixa a fila pronta antes de alguém abrir o assistente.

```
pedidos ──► triar_lote ──► extrair_e_classificar (LLM #9 + regras #10) ──► triagem_pedidos ──► fila por urgência
                │                                                                              ├─ CSV / JSON
                └─ mesmo hash_entrada: reaproveita extracao_llm e só reaplica as regras        └─ "Caixa de entrada" (UI)
```

## Componentes

| Peça | Onde |
|---|---|
| Extração separada em etapa do LLM (`extrair_pedido`, com `usage`/`usage_limits`) e etapa determinística (`classificar_extracao`) | `src/sabor/tools/pedidos.py` (a tool `extrair_e_classificar_pedido` chama as duas) |
| Lote, orçamento, novas tentativas, fila e exportação | `src/sabor/triagem.py` |
| Linha de comando | `scripts/triagem_lote.py` |
| Tabelas | `triagem_pedidos` e `pedido_itens` em `src/sabor/sql/schema.sql` |
| Interface | botão "Caixa de entrada" na barra lateral (só equipe) em `src/sabor/ui/app.py` |

## Decisões

- **Tabela própria.** A urgência das regras (#10) tem quatro níveis (`critica`, `alta`, `normal`, `baixa`), e `pedidos.urgencia` usa outra escala. `triagem_pedidos.prioridade` é uma coluna gerada (0 = crítica) para ordenar a fila.
- **Reaproveitamento da extração, regras sempre de novo.** A saída bruta do LLM fica em `extracao_llm`, e `hash_entrada` = SHA-256 de (versão da triagem, modelo, data de recebimento, mensagem). Se o hash não mudou, o pedido não volta ao modelo, mas `classificar_extracao` (catálogo, datas e regras #10) roda de novo sobre o banco atual, sem tokens. Assim, uma dívida vencida, uma certificação vencida ou um produto novo no catálogo aparecem no lote seguinte. Mudou o prompt da extração? Suba `VERSAO_TRIAGEM` (ou rode com `--reprocessar`).
- **Resultado desatualizado.** `listar_fila` recalcula o hash: se a mensagem mudou depois da extração, o pedido aparece como `desatualizado`, sem a classificação antiga, até ser triado de novo (inclusive quando falta orçamento para isso).
- **Orçamento (#32).** Cada pedido roda com `UsageLimits` iguais aos do turno do chat (`TURNO_TOKENS_LIMIT`, `TURNO_REQUEST_LIMIT`). O lote tem teto próprio (`TRIAGEM_TOKENS_LIMIT` ou `--tokens`): quando o restante do lote é menor que o teto do pedido, ele vira o limite do pedido. Ao esgotar, o lote para e o restante fica pendente para a próxima execução. Estourar o limite de **chamadas** de um pedido (ex.: o modelo errou o formato da saída várias vezes) marca só aquele pedido como erro; o lote segue. No dataset de referência, cada pedido gasta ~1,2 mil tokens (15 pedidos ≈ 17 mil).
- **Falhas.** Erros temporários do provedor (429 do plano gratuito do Gemini, 503, conexão) esperam e tentam de novo (20 s, 40 s, 60 s, ou o `retry_after` que o provedor informar, até 2 min). Qualquer outra falha marca o pedido como `erro` e o lote segue; a próxima execução tenta de novo.
- **Gravação incremental.** Cada pedido é gravado assim que termina (autocommit): interromper o lote não perde trabalho.
- **Motivos das sinalizações.** As regras (#10) registram em `resultado->'motivos_sinalizacoes'` por que cada sinalização foi dada, como "P017 interditado em inspeção sanitária (frango caipira)" ou "cajá: safra baixa no mês da entrega (11)". `rascunhar_notificacao` usa esses motivos nas flags. Como as regras rodam de novo a cada lote, uma triagem antiga ganha os motivos com `--limite 0`, sem LLM.
- **Itens em `pedido_itens`.** `pedidos.json` só traz a mensagem em texto livre, então a carga (ETL) não tem como preencher `pedido_itens`. Quem preenche é a triagem: cada gravação de `triagem_pedidos` substitui os itens do pedido na mesma transação, e um erro os apaga. Assim, a tabela só tem itens de pedidos com triagem válida. O `resumir_produtor` (#11) lê dela os pedidos em aberto de cada produtor, sem os itens ambíguos. Como o ETL faz `TRUNCATE pedidos ... CASCADE`, que também esvazia `triagem_pedidos` e `pedido_itens`, rode a triagem depois de cada carga.
- **LGPD.** A mensagem passa pelo mascarador antes do LLM, como no chat (#33). A exportação não inclui `remetente` (e-mail/telefone).
- **CSV injection.** Campos vêm de mensagens de terceiros: no CSV, células que começariam com `=`, `+`, `-`, `@`, tab ou CR ganham um `'` na frente e não viram fórmula no Excel.
- **Ordem da fila.** Urgência → data de entrega → recebimento. Depois vêm os pedidos com erro, os desatualizados e os pendentes, e a interface avisa quantos estão sem triagem válida.

## Limitações

- O lote é sequencial: com o limite de 15 chamadas/min do plano gratuito, 100 pedidos levam alguns minutos. Paralelizar só compensa num plano pago.
