# `fichas_tecnicas/` — Fichas técnicas de produto

[← Visão geral](../README.md)

Cinco fichas em texto livre com a descrição comercial e técnica das principais linhas de produto. São a **única fonte de capacidade produtiva por produtor** e de parâmetros de armazenamento, e é o material que compradores como o Hospital São Francisco pedem junto com a proposta.

| Arquivo | Linha | Produtores citados |
|---|---|---|
| `queijo_coalho.txt` | Queijo de coalho artesanal | P003, P007, P011 |
| `mel_silvestre.txt` | Mel silvestre | P002, P008, P018 |
| `goiaba_paluma.txt` | Goiaba paluma in natura | P001, P010 |
| `hortalicas.txt` | Hortaliças (folhosas e frutos) | P004, P009, P012 |
| `tuberculos_graos.txt` | Tubérculos, raízes e derivados de mandioca | P005, P013 |

**Estrutura:** blocos com títulos em maiúsculas (`DENOMINAÇÃO`, `PRODUTORES FORNECEDORES`, `ARMAZENAMENTO`, `PREÇOS VIGENTES`). O layout varia: as de hortaliças e tubérculos são listas de vários produtos. Para RAG, faça um chunk por produto ou por bloco, com metadados de produto e produtor.

## Capacidade por produtor

| Produto | Produtor | Capacidade | Orgânico | Registro sanitário |
|---|---|---|---|---|
| Queijo coalho | P003 | 150–200 kg/semana | não | SIE PE-0756 |
| Queijo coalho | P007 | 150–200 kg/semana | sim (até 08/2027) | SIE PE-1203 |
| Queijo coalho | P011 | 50–80 kg/semana | não | **sem SIE**: só consumidor final |
| Mel silvestre | P002 | ~300 kg/mês | sim (até 03/2027) | SIE PE-0892 |
| Mel silvestre | P008 | ~150 kg/mês | **vencido**: não vender como orgânico | SIE pendente |
| Mel silvestre | P018 | ~200 kg/mês | sim (até 02/2027) | SIE pendente |
| Goiaba paluma | P001 | ~500 kg/sem (safra out–mar), ~100 kg/sem (entressafra) | sim | n/a |
| Goiaba paluma | P010 | ~300 kg/sem (safra), ~80 kg/sem (entressafra) | não | n/a |
| Folhosas | P004 | ~300 kg/semana | sim (⚠️ até 01/12/2026) | n/a |
| Folhosas | P009 | ~800 kg/semana | sim | n/a |
| Frutos de horta | P012 | ~400 kg/semana | sim | n/a |
| Macaxeira | P005 | ~400 kg/semana | não | n/a |
| Inhame | P005 | ~200 kg/semana (pico abr–jul) | não | n/a |
| Batata doce | P005 | ~150 kg/semana | não | n/a |
| Farinha de mandioca | P013 | ~200 kg/semana | não | n/a |
| Tapioca/goma | P013 | ~100 kg/semana | não | n/a |

Capacidades que só aparecem em outras fontes:
- Manteiga de garrafa (40 kg/sem) e nata (30 kg/sem) de P007, berries de P014 e mel de jandaíra de P002 estão nos [certificados](certificacoes.md).
- Frango (~80 kg/sem) e ovos (~120 dz/sem) estão na [sazonalidade](sazonalidade.md).
- **Não há capacidade** para P006 (banana, maracujá, coco), P010 (manga, cajá, umbu, graviola), P016 (derivados de cana) e P017.

## Parâmetros técnicos

| Produto | Armazenamento | Validade | Outros |
|---|---|---|---|
| Queijo coalho | 2–8 °C, vácuo ou filme | 30 dias | Umidade 36–40%; pH 5,2–5,8; porção de 30 g = 110 kcal. Leite cru, amparado pelo Selo Arte. |
| Mel silvestre | Ambiente 15–25 °C, ao abrigo da luz | 24 meses | Umidade ≤ 20%; HMF ≤ 60 mg/kg; acidez ≤ 50 mEq/kg. Cristalização é natural. Potes de 250 g a baldes de 25 kg. |
| Goiaba paluma | Ambiente 25 °C / refrigerado 8–10 °C (UR 85–90%) | 5–7 dias / 15–20 dias | Classificação: extra > 250 g, especial 150–250 g, primeira 100–150 g. |
| Folhosas | 2–5 °C, UR 95% | 3–5 dias | |
| Frutos de horta | 8–12 °C, UR 85–90% | 7–10 dias | |
| Macaxeira | Ambiente / refrigerado | 7 / 15 dias | |
| Farinha de mandioca | Embalagem fechada | 6 meses | |

## Inconsistências

| # | Ficha | Inconsistência |
|---|---|---|
| 1 | `hortalicas.txt` | Os preços listados são os **orgânicos** da tabela, sem opção convencional, coerente com "todos são orgânicos". Mas o atacado aparece como "(>50kg): 5%" para tudo, e a tabela usa mínimo de 30 para rúcula, espinafre, couve, ervas, pimentão, pepino, berinjela e quiabo. |
| 2 | `hortalicas.txt` | "Todos os produtores de hortaliças possuem certificação orgânica vigente": verdade hoje, mas P004 vence em 01/12/2026. |
| 3 | `tuberculos_graos.txt` | O título diz "grãos", mas não há grãos (feijão, milho e fava eram de P015, inativo). |
| 4 | `tuberculos_graos.txt` | Preços de atacado não batem com a tabela. Batata doce 3,50 (pela tabela daria 3,60), farinha 7,00 (7,36), tapioca 5,80 (5,98). Macaxeira e inhame batem com arredondamento. |
| 5 | `tuberculos_graos.txt` | "Todos possuem DAP/CAF vigente", sem número de registro. |
| 6 | `mel_silvestre.txt` | P018 aparece com "SIE pendente" e é orgânico. Pode vender a consumidor final ou a feira orgânica, mas não ao PNAE. |
| 7 | `queijo_coalho.txt` | O preço convencional vale para "P003, P011", mas P011 não pode vender a revenda (sem SIE). |
| 8 | `goiaba_paluma.txt` | A ficha diz "pico de outubro a março"; a sazonalidade diz "pico dez-mar". |

## Uso pelo assistente

- Fonte primária para **checar capacidade** antes de confirmar pedidos e para **montar propostas técnicas** (hospital, SESC, exportadora).
- Ao anexar fichas a uma proposta, **revise o status de certificação** citado, já que P004 vence em breve e P008 está vencido.
- Para produtos sem ficha (banana, berries, derivados de cana, frango, ovos), recorra a certificados, sazonalidade e tabela, ou informe que não há ficha técnica.
