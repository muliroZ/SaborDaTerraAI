# `lotes.csv` — Rastreabilidade de lotes

[← Visão geral](../README.md)

Registro dos lotes entregues entre **02/09 e 25/10/2026**: origem (produtor, data de colheita ou fabricação), transporte, validade e inspeção visual. É a base para responder a exigências de rastreabilidade (Hospital, PNAE, ADAGRO).

| | |
|---|---|
| Arquivo | `dados/Facepe/lotes.csv` |
| Formato | CSV UTF-8 |
| Linhas | 20 (`LOT-2026-0089` a `LOT-2026-0108`) |
| Chave primária | `lote_id` |
| Chave estrangeira | `produtor_id` → `produtores.id` |
| Sensibilidade | Média (CPF do produtor) |

## Schema

| Coluna | Tipo | Exemplo | Observações |
|---|---|---|---|
| `lote_id` | string `LOT-2026-\d{4}` | `LOT-2026-0100` | Sequencial a partir de 0089, o que indica lotes anteriores fora do arquivo. |
| `data_colheita` | data ISO | `2026-09-18` | Para processados (queijo, mel, farinha), é a data de **fabricação ou envase**. |
| `data_entrega` | data ISO | `2026-09-18` | |
| `produtor_id`, `produtor_nome`, `produtor_cpf` | string | | Denormalizados; batem com o cadastro. |
| `produto` | string | `queijo coalho` | Mesmo vocabulário de `financeiro.produto`. |
| `quantidade_kg` | número | `300` | ⚠️ Mesmo problema do financeiro: alface "300 unidades", coentro "150 maços", couve "40 maços (kg)". |
| `comprador` | string | | |
| `pedido_id` | string `PED-2026-<MES>-\d{3}` | `PED-2026-SEP-018` | ⚠️ Formato **diferente** de `pedidos.json` (`PED-2026-001`). Referencia pedidos que não estão no conjunto. |
| `temperatura_transporte_c` | número **ou** `ambiente` | `7`, `ambiente` | Coluna mista. 11 lotes `ambiente`; os refrigerados vão de 4 a 12 °C. |
| `validade` | data ISO | `2026-10-18` | |
| `inspecao_visual` | string | `aprovado` | Único valor presente. |
| `observacao` | string ou vazio | `Certificação orgânica anexa` | Unidade real, certificação e notas de manuseio. |

```python
lotes = pd.read_csv("dados/Facepe/lotes.csv",
                    parse_dates=["data_colheita", "data_entrega", "validade"])
lotes["refrigerado"] = lotes.temperatura_transporte_c != "ambiente"
lotes["temp_c"] = pd.to_numeric(lotes.temperatura_transporte_c, errors="coerce")
```

## Conteúdo

| Produto | Lotes | IDs |
|---|---|---|
| queijo coalho | **3** | 0089 (P003), 0100 (P003), 0108 (P007) |
| mel silvestre | **3** | 0093 (P002), 0104 (P008), 0107 (P018) |
| goiaba paluma | 2 | 0091 (P001), 0105 (P010) |
| alface | 2 | 0094 (P004), 0098 (P009) |
| manteiga de garrafa, manga tommy, coentro, macaxeira, inhame, couve, queijo manteiga, tomate, frango caipira, farinha de mandioca | 1 cada | |

**Tempo entre colheita e entrega:** 0 dias na maioria, 1–3 dias em alguns casos (mel 3 dias, folhosas do PNAE 2 dias).

**Vida útil registrada (validade − colheita):**

| Faixa | Produtos |
|---|---|
| 3–8 dias | frango (3), coentro, alface, couve (5), goiaba, tomate, alface do PNAE (7), manga (8) |
| 11–15 dias | macaxeira (11), inhame (15) |
| 30–31 dias | queijos (coerente com a ficha técnica: 30 dias) |
| ~6 meses | manteiga de garrafa, farinha |
| 24 meses | mel (coerente com a ficha) |

**Temperatura de transporte em relação às fichas técnicas:**

| Produto | Transporte | Faixa recomendada | Situação |
|---|---|---|---|
| Queijos | 6–8 °C | 2–8 °C | ✅ |
| Frango | 4 °C | — | ✅ |
| Folhosas | 10–12 °C | 2–5 °C (ficha de hortaliças) | ⚠️ Acima da faixa |
| Tomate | ambiente | 8–12 °C | ⚠️ Fora da faixa |

## Cobertura

`financeiro.csv` e `lotes.csv` não compartilham chave. Casando por `(data, produtor, produto, quantidade, comprador)`:

- **18 de 20 lotes** têm transação correspondente.
- **LOT-0089 e LOT-0090** (Restaurante Raízes, entregues em 02/09) correspondem a FIN-003/004, que estão datadas de **05/09**. A data diverge.
- **12 transações não têm lote:** FIN-003, 004 (pela divergência de data), 010 (banana), 015 (goiaba), 017 (pimentão), 019 (ovos), 020 (morango), 022 (geleia), 023 (rapadura), 025 (manga), 026 (queijo de P011) e 029 (pólen).

Para a **ADAGRO (PED-2026-014)**, que pede os "últimos 3 lotes de cada produto", o arquivo só atende queijo coalho e mel silvestre. Os outros produtos vendidos não têm histórico suficiente.

## Qualidade e conformidade

| Achado | Lote | Gravidade |
|---|---|---|
| Mel de **P008** entregue com "Certificação orgânica anexa" em 01/10/2026, **depois do vencimento** (30/09) | LOT-0104 | 🔴 |
| Frango de **P017 sem SIE** entregue em 25/09, depois da NC-001 da ADAGRO | LOT-0103 | 🔴 |
| `temperatura_transporte_c` mistura texto e número | todos | 🟡 |
| `quantidade_kg` com unidades e maços | 0094, 0095, 0098, 0099 | 🟡 |
| `inspecao_visual` sempre `aprovado`, sem registro de reprovação | todos | ℹ️ Pode indicar viés de registro |
| Folhosas transportadas acima da faixa da ficha | 0094, 0095, 0098, 0099 | 🟡 |

## Referências a lotes em linguagem natural

O enunciado do projeto usa como exemplo "o queijo que vocês entregaram no **lote 2024-089**". Nos dados, o lote correspondente é **`LOT-2026-0089`** (queijo coalho de P003): o número bate, mas o ano (2024 × 2026) e o preenchimento com zeros (089 × 0089) não. Usuários vão citar lotes de forma livre, e o assistente deve:

1. Extrair o número sequencial (`089` → `0089`) e procurar por ele, tolerando prefixo ausente e ano divergente.
2. Se houver um único candidato, responder e **avisar sobre a divergência** ("não encontrei 2024-089; o lote mais próximo é LOT-2026-0089, de 02/09/2026").
3. Se não houver candidato, dizer que o lote não existe nos registros, sem inventar.

```python
import re

def resolver_lote(texto, lotes):
    m = re.search(r"(\d{3,4})\s*$", texto.strip())
    if not m:
        return None
    seq = m.group(1).zfill(4)
    return lotes[lotes.lote_id.str.endswith(seq)]
```

## Uso pelo assistente

- Para montar um **dossiê de rastreabilidade** (hospital, ADAGRO), junte `lotes` + `produtores` (sem CPF para terceiros) + o certificado ou laudo do produtor.
- Não invente lotes para produtos sem registro. Informe a lacuna.
- Valide a certificação **na data de entrega do lote**, não na data atual.
