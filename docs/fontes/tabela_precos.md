# `tabela_precos_outubro_2026.csv` — Tabela de preços

[← Visão geral](../README.md)

Preços de referência da cooperativa em **outubro/2026**, com a versão convencional e a orgânica de cada produto e as regras de desconto no atacado. É a base para cotações. Contratos com preço próprio (o PNAE) **têm precedência** sobre esta tabela.

| | |
|---|---|
| Arquivo | `dados/Facepe/tabela_precos_outubro_2026.csv` |
| Formato | CSV UTF-8, decimal `.` |
| Linhas | 53 produtos |
| Chave | `produto` (texto) |
| Sensibilidade | Baixa; é informação comercial que pode ser compartilhada |

## Schema

| Coluna | Tipo | Exemplo | Observações |
|---|---|---|---|
| `produto` | string | `goiaba paluma extra` | Às vezes inclui classificação ou variedade (`extra`/`especial`, `santa cruz`/`cereja`, `verde`/`colorido`). |
| `unidade` | string | `kg` | `kg` (40), `maço` (7), `litro` (3), `unidade` (2), `dúzia` (1). |
| `preco_convencional` | decimal ou `n/d` | `4.00` | R$ por `unidade`. |
| `preco_organico` | decimal ou `n/d` | `5.50` | `n/d` = não existe produtor orgânico. |
| `desconto_atacado_pct` | inteiro | `10` | `0` = sem desconto de atacado (16 produtos). |
| `volume_minimo_atacado_kg` | inteiro | `100` | ⚠️ Apesar do nome, está na **unidade do produto** (maços, dúzias, unidades). `0` quando não há atacado. |
| `observacao` | string ou vazio | | Restrições importantes: produtor único, safra, ausência de SIE. |

```python
precos = pd.read_csv("dados/Facepe/tabela_precos_outubro_2026.csv", na_values=["n/d"])

def preco(produto, qtd, organico=False):
    r = precos.set_index("produto").loc[produto]
    p = r.preco_organico if organico else r.preco_convencional
    if pd.isna(p):
        raise ValueError("versão indisponível")
    if r.desconto_atacado_pct and qtd >= r.volume_minimo_atacado_kg:
        p *= 1 - r.desconto_atacado_pct / 100
    return round(p * qtd, 2)
```

> **Regra de atacado:** a tabela não diz se o mínimo é `>` ou `>=`. As fichas técnicas escrevem "Atacado (>100kg)". O exemplo acima usa `>=`; defina a regra com a gestão.

## Composição

| Disponibilidade | Qtde | Exemplos |
|---|---|---|
| Convencional e orgânico | 30 | goiaba, manga, acerola, berries, queijo coalho, mel silvestre, todas as hortaliças |
| Só convencional | 19 | banana, maracujá, cajá, umbu, graviola, coco, queijo manteiga, ricota, iogurte, raízes, farinhas, derivados de cana, frango, ovos |
| Só orgânico | 4 | mel de jandaíra (R$ 140), própolis (R$ 180), pólen (R$ 85), cera (R$ 65) |

**Prêmio orgânico:** vai de +9% (quiabo) a +38% (goiaba paluma extra); na maioria dos produtos fica entre +15% e +20%.

### Restrições que a tabela registra em `observacao`

| Produto | Restrição |
|---|---|
| frango caipira | "ATENÇÃO: P017 sem SIE - venda direta apenas". **Na prática está bloqueado** para intermediação (NC-001 da ADAGRO). |
| iogurte natural | "Apenas P011 (sem SIE para revenda)" |
| manga tommy | Só de outubro a março |
| morango, mirtilo, framboesa, amora | Safra curta ou produção pequena |
| cajá, umbu, graviola | Safra curta ou irregular, sem atacado |
| produtor único | queijo manteiga e ricota (P003), tomate cereja (P004), jandaíra e própolis (P002), pólen e cera (P018), rapadura, mascavo e melado (P016), geleia e doce de goiaba (P008), ovos (P017) |

## Inconsistências com outras fontes

| # | Inconsistência | Onde |
|---|---|---|
| 1 | **Preço orgânico de geleia (R$ 35) e doce de goiaba (R$ 28)**, que só P008 produz, e o certificado de P008 **venceu**. Hoje esses preços não podem ser oferecidos. | tabela × certificados |
| 2 | **Leite orgânico (R$ 5,50)** sem produtor orgânico de leite: P011 é convencional, e o certificado de P007 cobre queijo, manteiga e nata, não leite. | tabela × produtores |
| 3 | **Preço convencional de produtos que só têm produtor orgânico**: acerola, berries e hortaliças. Na prática a versão convencional não existe. | tabela × produtores |
| 4 | **Mel convencional (R$ 48)** só existe enquanto P008 está sem certificação. | |
| 5 | **Desconto de atacado nas fichas difere da tabela**: na ficha de tubérculos, batata doce R$ 3,50 (pela tabela daria 3,60), farinha R$ 7,00 (7,36) e tapioca R$ 5,80 (5,98). Na ficha de hortaliças, "Atacado (>50kg)" vale para todas, mas a tabela usa mínimo de 30 para várias. | [fichas_tecnicas.md](fichas_tecnicas.md) |
| 6 | **Preços do contrato PNAE são diferentes** e em geral maiores (ex.: banana 3,80 contra 3,50; queijo 34,00 contra 32,00). | [contratos.md](contratos.md) |
| 7 | O certificado de P002 fala em mel de jandaíra a "R$ 120-150/kg"; a tabela fixa R$ 140,00. | certificados |
| 8 | **Frango caipira** continua com preço e com atacado (5% a partir de 30 kg), apesar do bloqueio. | |
| 9 | **Feijão, milho e fava** (P015) não constam. O produtor está inativo. | |

## Normalização de nomes de produto

As fontes usam nomes diferentes para o mesmo produto. Mapeamento sugerido para o catálogo canônico (nome da tabela de preços):

| Cadastro (`produtores.produtos`) | Pedidos (texto livre) | Sazonalidade | Tabela de preços |
|---|---|---|---|
| `goiaba` | goiaba, guava | `goiaba paluma` | `goiaba paluma extra` / `goiaba paluma especial` |
| `manga` | manga, mango | `manga tommy` | `manga tommy` |
| `banana` | banana prata, banana | `banana prata` | `banana prata` |
| `maracujá` | maracuja, passion fruit | `maracujá` | `maracujá` |
| `tomate` | tomate | `tomate` | `tomate santa cruz` |
| `tomate cereja` | — | *(ausente)* | `tomate cereja` |
| `pimentão` | — | `pimentão` | `pimentão verde` / `pimentão colorido` |
| `mel de abelha jandaíra` | mel de jandaíra | `mel de jandaíra` | `mel de jandaíra` |
| `tapioca`, `goma` | tapioca/goma | `tapioca/goma` | `tapioca/goma` |
| `mel silvestre` | mel, mel puro, mel variado, mel (aquele silvestre mesmo) | `mel silvestre` | `mel silvestre` |
| `queijo coalho` | queijo, "o mais artesanal" | `queijo coalho` | `queijo coalho` |

Quando o texto for ambíguo ("queijo", "manteiga", "mel variado"), o assistente deve **perguntar** em vez de escolher sozinho.
