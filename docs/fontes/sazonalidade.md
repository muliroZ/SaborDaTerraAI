# `sazonalidade.csv` — Calendário de disponibilidade

[← Visão geral](../README.md)

Matriz de disponibilidade de cada produto por mês. Serve para dizer se um produto pode ser fornecido numa data e para ajustar expectativas de volume. É qualitativa: não informa quantidade. A capacidade em kg está nas [fichas técnicas](fichas_tecnicas.md).

| | |
|---|---|
| Arquivo | `dados/Facepe/sazonalidade.csv` |
| Formato | CSV UTF-8, formato largo (um mês por coluna) |
| Linhas | 50 produtos |
| Chave | `produto` (texto) |

## Schema

| Coluna | Tipo | Valores |
|---|---|---|
| `produto` | string | Nome do produto. Ver a [tabela de normalização](tabela_precos.md#normalização-de-nomes-de-produto). |
| `jan` … `dez` | categórico | `alta` · `media` (sem acento) · `baixa` · `nenhuma` |
| `observacao` | string ou vazio | Resumo da safra e, às vezes, **capacidade** (frango ~80 kg/semana, ovos ~120 dz/semana). |

Escala ordinal sugerida: `nenhuma = 0`, `baixa = 1`, `media = 2`, `alta = 3`.

```python
MESES = ["jan","fev","mar","abr","mai","jun","jul","ago","set","out","nov","dez"]
NIVEL = {"nenhuma": 0, "baixa": 1, "media": 2, "alta": 3}

saz = pd.read_csv("dados/Facepe/sazonalidade.csv")
saz_long = saz.melt(id_vars=["produto", "observacao"], value_vars=MESES,
                    var_name="mes", value_name="nivel")
saz_long["nivel_num"] = saz_long.nivel.map(NIVEL)
```

## Panorama

- **26 produtos com `alta` o ano todo:** hortaliças folhosas, laticínios, macaxeira, batata doce, processados (farinha, rapadura, geleia etc.), frango e ovos.
- **Produtos com meses `nenhuma`:**
  - manga (mai–ago)
  - cajá (jan–out, ou seja, só tem em nov e dez)
  - umbu (jun–jan)
  - mirtilo (out–abr)
  - framboesa (out–abr)
- Garanhuns fica a ~850 m de altitude, e o clima ameno permite **morango e berries no inverno** (pico jun–set) e hortaliças o ano todo.

### Novembro (mês dos pedidos pendentes)

| Nível | Produtos |
|---|---|
| `nenhuma` | umbu, mirtilo, framboesa |
| `baixa` | cajá |
| `media` | maracujá, graviola, morango, amora, mel silvestre, mel de jandaíra, própolis, pólen, cera, inhame |
| `alta` | todos os demais (36) |

### Janeiro (cotação do SESC, PED-2026-013)

Frutas com `alta`: goiaba, manga, acerola, banana, coco. Com `media`: maracujá, graviola, morango, amora. Cajá e umbu ficam `nenhuma` em janeiro.

## Inconsistências entre `observacao` e a matriz

| Produto | Observação diz | Matriz mostra |
|---|---|---|
| goiaba paluma | "Pico dez-mar. Fora de safra jun-ago." | `alta` de **out a fev**, `media` em mar; jun–ago estão `baixa`, não `nenhuma`. A ficha técnica fala em "pico out-mar". |
| cajá | "Safra muito curta: nov-jan." | jan = `nenhuma`. Só nov (`baixa`) e dez (`media`). |
| tomate | "Dois picos: jun-jul e out-jan." | jun = `media`, e o `alta` é contínuo de jul a fev. |
| pimentão, pepino | "Similar ao tomate." | Iguais ao tomate, com a mesma ressalva. |

**Regra sugerida:** a **matriz** é a fonte de verdade para o mês, e a `observacao` serve de contexto.

## Lacunas

- Sem linha para `tomate cereja` (P004), `pimentão verde`/`colorido`, nem para as classificações `extra`/`especial` da goiaba. Use a linha genérica.
- Sem linha para feijão, milho e fava (P015, inativo).
- Disponibilidade é por **produto**, não por **produtor**. Um produto `alta` pode ter um único fornecedor com capacidade limitada (ex.: ovos, ~120 dz/semana, todos de P017).
- A versão orgânica não aparece separada. Combine com a certificação vigente do produtor.

## Uso pelo assistente

- Para pedidos com data futura, consulte o **mês da entrega**, não o mês atual.
- `nenhuma` → informe que está indisponível e, se fizer sentido, sugira um substituto. `baixa` → aceite só com volume pequeno e confirmação do produtor.
- Combine sempre com a capacidade das [fichas técnicas](fichas_tecnicas.md) e com a demanda já comprometida (ver [pedidos.md](pedidos.md#demanda-agregada--capacidade)).
