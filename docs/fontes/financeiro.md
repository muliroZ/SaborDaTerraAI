# `financeiro.csv` — Transações financeiras

[← Visão geral](../README.md)

Registro das vendas intermediadas pela cooperativa entre **02/09/2026 e 25/10/2026**, com status de recebimento. Cada linha é **um produto vendido por um produtor a um comprador**; um mesmo pedido gera várias linhas.

| | |
|---|---|
| Arquivo | `dados/Facepe/financeiro.csv` |
| Formato | CSV UTF-8, separador `,`, decimal `.` |
| Linhas | 30 (`FIN-2026-001` a `FIN-2026-030`) |
| Chave primária | `id_transacao` |
| Chave estrangeira | `produtor_id` → `produtores.id` (íntegra) |
| Sensibilidade | **Alta**: CPF, chave PIX e faturamento individual |

## Schema

| Coluna | Tipo | Exemplo | Observações |
|---|---|---|---|
| `id_transacao` | string `FIN-2026-\d{3}` | `FIN-2026-013` | Sequencial. |
| `data` | data ISO | `2026-09-18` | Data da venda e entrega. |
| `produtor_id` | string | `P003` | FK. |
| `produtor_nome` | string | | Denormalizado; igual ao cadastro em 100% dos casos. |
| `produtor_cpf` | string | | Denormalizado; igual ao cadastro. |
| `comprador` | string | `Rede SuperBom Atacado` | Nome livre. São 10 compradores distintos. |
| `tipo` | string | `venda` | Único valor presente. |
| `produto` | string | `queijo coalho` | Nome específico (`goiaba paluma`, `manga tommy`, `banana prata`), mais próximo da tabela de preços que do cadastro. |
| `quantidade_kg` | número | `150` | ⚠️ **Nem sempre é kg.** Alface está em unidades, coentro e couve em maços, ovos em dúzias (FIN-019: "Preço por dúzia"). |
| `valor_unitario` | decimal | `30.00` | R$ por unidade de `quantidade_kg`. |
| `valor_total` | decimal | `4500.00` | Sempre igual a `quantidade_kg × valor_unitario`, verificado em todas as linhas. |
| `forma_pagamento` | string | `boleto 30 dias` | `boleto 30 dias` (13), `pix` (10), `empenho 45 dias` (5), `transferência` (2). |
| `status_pagamento` | `pago` \| `pendente` | | Não existe status "atrasado". O atraso se calcula comparando `data_vencimento` com a data de referência. |
| `data_vencimento` | data ISO | | Para PIX, igual a `data`. |
| `data_pagamento` | data ISO ou vazio | | Vazio quando `pendente`. |
| `chave_pix_deposito` | string | | Chave PIX do **produtor** para repasse, igual a `produtores.chave_pix`. O comprador paga à cooperativa (ver contratos). |
| `observacao` | string ou vazio | `ATRASADO - cobrar` | Texto livre: número de empenho, atraso e unidade. |

### Exemplo: títulos vencidos numa data

```python
import pandas as pd

fin = pd.read_csv("dados/Facepe/financeiro.csv",
                  parse_dates=["data", "data_vencimento", "data_pagamento"])
ref = pd.Timestamp("2026-10-30")
vencidos = fin[(fin.status_pagamento == "pendente") & (fin.data_vencimento < ref)]
vencidos["dias_atraso"] = (ref - vencidos.data_vencimento).dt.days
```

## Números do período

| Indicador | Valor |
|---|---|
| Faturamento total | **R$ 24.634,00** |
| Pago | R$ 11.254,00 (20 transações: 12 em PIX/transferência + 8 em boleto) |
| Pendente | **R$ 13.380,00** (10 transações: 5 boletos + 5 empenhos) |
| Setembro / outubro | R$ 17.099,00 / R$ 7.535,00 |

**Por comprador:**

| Comprador | Total | Pendente |
|---|---|---|
| Rede SuperBom Atacado | R$ 6.020,00 | **R$ 6.020,00 (vencido)** |
| Prefeitura de Garanhuns – PNAE | R$ 5.150,00 | R$ 5.150,00 (R$ 2.670,00 vencidos) |
| Mercado Bom Preço | R$ 4.600,00 | R$ 1.810,00 |
| Restaurante Raízes | R$ 3.125,00 | — |
| Hotel Fazenda Serra Negra | R$ 2.120,00 | — |
| Feira Orgânica do Recife | R$ 1.325,00 | — |
| Lanchonete da Feira | R$ 870,00 | — |
| Sorveteria Tropical | R$ 660,00 | — |
| SESC Garanhuns | R$ 400,00 | R$ 400,00 |
| Hospital São Francisco | R$ 364,00 | — |

**Por produtor (top 5):** P003 R$ 7.460,00 · P007 R$ 3.155,00 · P017 R$ 2.120,00 · P002 R$ 1.650,00 · P018 R$ 1.325,00. Só **P015** (inativo) não tem transação.

## Contas a receber

Situação na data dos dados (30/10) e na data desta documentação (09/11):

| Transação | Comprador | Produtor | Valor | Vencimento | Em 30/10 | Em 09/11 |
|---|---|---|---|---|---|---|
| FIN-2026-013 | SuperBom | P003 | R$ 4.500,00 | 18/10 | 🔴 12 d atraso | 🔴 22 d |
| FIN-2026-014 | SuperBom | P003 | R$ 1.520,00 | 18/10 | 🔴 12 d | 🔴 22 d |
| FIN-2026-005 | PNAE | P002 | R$ 1.650,00 | 23/10 | 🔴 7 d | 🔴 17 d |
| FIN-2026-006 | PNAE | P004 | R$ 750,00 | 23/10 | 🔴 7 d | 🔴 17 d |
| FIN-2026-007 | PNAE | P004 | R$ 270,00 | 23/10 | 🔴 7 d | 🔴 17 d |
| FIN-2026-023 | Mercado Bom Preço | P016 | R$ 1.200,00 | 05/11 | a vencer | 🔴 4 d |
| FIN-2026-024 | Mercado Bom Preço | P010 | R$ 360,00 | 10/11 | a vencer | vence amanhã |
| FIN-2026-025 | Mercado Bom Preço | P010 | R$ 250,00 | 10/11 | a vencer | vence amanhã |
| FIN-2026-027 | SESC | P013 | R$ 400,00 | 30/11 | a vencer | a vencer |
| FIN-2026-030 | PNAE | P007 | R$ 2.480,00 | 09/12 | a vencer | a vencer |

**Vencido em 30/10: R$ 8.690,00. Vencido em 09/11: R$ 9.890,00.**

Atrasos já quitados: FIN-008/009 (Mercado Bom Preço, 2 dias) e FIN-018/019 (Hotel Fazenda, 1 dia).

**Saldo a receber por produtor** (útil para mensagens como PED-2026-012): P003 R$ 6.020,00 · P007 R$ 2.480,00 · P002 R$ 1.650,00 · P016 R$ 1.200,00 · P004 R$ 1.020,00 · P010 R$ 610,00 · P013 R$ 400,00 · demais R$ 0,00.

## Qualidade e conformidade

| # | Achado | Linhas | Gravidade |
|---|---|---|---|
| 1 | **P008 vendeu como orgânico depois do vencimento**: mel a R$ 58,00 (preço orgânico) e geleia a R$ 35,00 (preço orgânico) em 01/10, um dia depois do vencimento do certificado | FIN-021, FIN-022 | 🔴 Alta |
| 2 | **Frango de P017 vendido depois da ordem da ADAGRO** de suspender a intermediação (NC-001, maio/2026) | FIN-018 | 🔴 Alta |
| 3 | **Queijo de P011 (sem SIE) vendido a pessoa jurídica** (Lanchonete da Feira) | FIN-026 | 🔴 Alta |
| 4 | **Preços do PNAE abaixo do contrato 045/2026**: mel 55,00 (contrato 58,00), alface 2,50 (2,80), coentro 1,80 (2,00), queijo coalho 31,00 (34,00) | FIN-005, 006, 007, 030 | 🟠 Média: receita perdida de R$ 450,00 |
| 5 | **Goiaba convencional de P010 a R$ 4,50/kg**, preço da goiaba orgânica especial. A convencional extra custa R$ 4,00 | FIN-024 | 🟡 Baixa |
| 6 | `quantidade_kg` guarda unidades, maços e dúzias | FIN-006, 007, 011, 012, 019 | 🟡 Modelagem |
| 7 | Data da transação diverge da data de entrega do lote: Restaurante Raízes com transação em 05/09 e lotes LOT-0089/0090 entregues em 02/09 | FIN-003, 004 | 🟡 Baixa |
| 8 | 12 transações **sem lote** correspondente em `lotes.csv` | ver [lotes.md](lotes.md#cobertura) | 🟠 Rastreabilidade |
| 9 | O volume do PNAE (R$ 5.150,00 em 2 meses) está muito abaixo do valor mensal estimado no contrato (R$ 40.000,00). Pode ser dado parcial | — | ℹ️ Contexto |

> Os preços de setembro são anteriores à `tabela_precos_outubro_2026.csv`. Diferenças pequenas frente à tabela de outubro (ex.: queijo a 33,00 para a Lanchonete) não são necessariamente erros.

## Uso pelo assistente

- **Saldo de um produtor:** filtre por `produtor_id`, com `status_pagamento == "pendente"`. Responda só ao titular verificado.
- **Antes de aceitar um pedido a prazo,** consulte os títulos vencidos do comprador. A SuperBom é o caso típico.
- **Mensal "deste mês":** use `data` (a data da venda), não `data_pagamento`.
- Não exponha `produtor_cpf` nem `chave_pix_deposito` em respostas a terceiros.
