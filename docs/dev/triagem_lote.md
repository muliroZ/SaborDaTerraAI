# Triagem em lote da caixa de entrada (#14)

O cenário prevê 60–100 pedidos por dia chegando por e-mail, WhatsApp e formulário. Os pipelines de ingestão gravam cada mensagem em JSON na tabela `pedidos` ("Salvar JSON no Banco" no diagrama *Visão geral*). A triagem em lote roda sobre essa tabela, **fora do chat**, e deixa a fila pronta antes de alguém abrir o assistente.

```
pedidos ──► triar_lote ──► extrair_e_classificar (LLM #9 + regras #10) ──► triagem_pedidos ──► fila por urgência
                │                                                                              ├─ CSV / JSON
                └─ pula o que já tem resultado com o mesmo hash_entrada                        └─ "Caixa de entrada" (UI)
```

## Componentes

| Peça | Onde |
|---|---|
| Núcleo da extração, chamável fora do turno (`usage`, `usage_limits`) | `extrair_e_classificar` em `src/sabor/tools/pedidos.py` (a tool `extrair_e_classificar_pedido` delega para ele) |
| Lote, orçamento, novas tentativas, fila e exportação | `src/sabor/triagem.py` |
| Linha de comando | `scripts/triagem_lote.py` |
| Tabela | `triagem_pedidos` em `src/sabor/sql/schema.sql` |
| Interface | botão "Caixa de entrada" na barra lateral (só equipe) em `src/sabor/ui/app.py` |

## Decisões

- **Tabela própria.** A urgência das regras (#10) tem quatro níveis (`critica`, `alta`, `normal`, `baixa`), e `pedidos.urgencia` usa outra escala. `triagem_pedidos.prioridade` é uma coluna gerada (0 = crítica) para ordenar a fila.
- **Reaproveitamento.** `hash_entrada` = SHA-256 de (versão da triagem, modelo, `DATA_REF_DATE`, data de recebimento, mensagem). Se nada disso mudou e o resultado está `ok`, o pedido não volta ao modelo. Mudou o prompt ou as regras? Suba `VERSAO_TRIAGEM` (ou rode com `--reprocessar`).
- **Orçamento (#32).** Cada pedido roda com `UsageLimits` iguais aos do turno do chat (`TURNO_TOKENS_LIMIT`, `TURNO_REQUEST_LIMIT`). O lote tem teto próprio (`TRIAGEM_TOKENS_LIMIT` ou `--tokens`): quando o restante do lote é menor que o teto do pedido, ele vira o limite do pedido. Ao esgotar, o lote para e o restante fica pendente para a próxima execução. No dataset de referência, cada pedido gasta ~1,2 mil tokens (15 pedidos ≈ 17 mil).
- **Falhas.** Erros temporários do provedor (429 do plano gratuito do Gemini, 503, conexão) esperam e tentam de novo (20 s, 40 s, 60 s, ou o `retry_after` que o provedor informar, até 2 min). Qualquer outra falha marca o pedido como `erro` e o lote segue; a próxima execução tenta de novo.
- **Gravação incremental.** Cada pedido é gravado assim que termina (autocommit): interromper o lote não perde trabalho.
- **LGPD.** A mensagem passa pelo mascarador antes do LLM, como no chat (#33). A exportação não inclui `remetente` (e-mail/telefone).
- **Ordem da fila.** Urgência → data de entrega → recebimento. Pedidos com erro e pendentes vêm no fim, e a interface avisa quantos estão sem triagem.

## Limitações

- O lote é sequencial: com o limite de 15 chamadas/min do plano gratuito, 100 pedidos levam alguns minutos. Paralelizar só compensa num plano pago.
- O modelo às vezes coloca texto livre em `sinalizacoes` além das sinalizações das regras (comportamento da extração, #9).
