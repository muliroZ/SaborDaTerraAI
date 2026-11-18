# `produtores.csv` — Cadastro de produtores

[← Visão geral](../README.md)

Cadastro mestre dos 18 produtores cooperados. É a **tabela de referência** do conjunto: `financeiro.csv` e `lotes.csv` apontam para ela pelo `id`, e os certificados e laudos citam o mesmo ID no nome do arquivo.

| | |
|---|---|
| Arquivo | `dados/Facepe/produtores.csv` |
| Formato | CSV UTF-8, separador `,`, campos com vírgula entre aspas |
| Linhas | 18 (P001–P018) |
| Chave primária | `id` |
| Sensibilidade | **Alta**: CPF, nascimento, contato, endereço e dados bancários |

## Schema

| Coluna | Tipo | Exemplo | Descrição e observações |
|---|---|---|---|
| `id` | string `P\d{3}` | `P001` | Identificador do produtor. Único. |
| `nome_completo` | string | `Maria das Graças Silva Ferreira` | Nas mensagens os produtores aparecem por apelido ou primeiro nome ("Cícero", "Maria das Graças"). |
| `cpf` | string `000.000.000-00` | `831.***.***-90` | Formatado com pontuação. **Fictício**: 17 de 18 falham no dígito verificador. |
| `data_nascimento` | data ISO | `1975-03-12` | |
| `telefone` | string `(DD) 9XXXX-XXXX` | `(87) 99812-3456` | Todos com DDD 87. Serve para identificar produtores que escrevem por WhatsApp (ver [pedidos.md](pedidos.md)). |
| `email` | string | | Domínios pessoais (gmail, hotmail, email.com). |
| `endereco` | string | `Sítio Boa Vista, Zona Rural s/n` | Nome da propriedade e localidade. Sem CEP; o CEP só aparece em alguns certificados. |
| `municipio` | string | `Garanhuns` | Todos em PE. Ver distribuição abaixo. |
| `produtos` | lista separada por `,` (entre aspas) | `"goiaba,acerola,manga"` | Nomes **genéricos**, que não batem 1:1 com a tabela de preços nem com a sazonalidade. Ver [normalização](tabela_precos.md#normalização-de-nomes-de-produto). |
| `certificacao_organica` | `sim` \| `nao` | `sim` | Sem acento em "nao". **Não reflete vencimento**: P008 está `sim` com o certificado vencido. |
| `validade_certificacao` | data ISO ou vazio | `2027-06-15` | Vazio quando `nao`. Use este campo, e não `certificacao_organica`, para decidir se o produto é orgânico. |
| `chave_pix` | string | CPF, e-mail ou telefone | 12 usam CPF, 4 usam telefone e 2 usam e-mail. Igual a `chave_pix_deposito` em `financeiro.csv`. |
| `banco` | string | `Banco do Brasil` | 7 bancos distintos. |
| `agencia` | string (zeros à esquerda) | `0437` | **Leia como string** para não perder os zeros. |
| `conta_corrente` | string com dígito | `28901-3` | Formatos variados (`0073921-7`, `00134578-2`). Leia como string. |
| `status` | `ativo` \| `inativo` | `ativo` | Só P015 está `inativo`. |

### Exemplo de leitura

```python
import pandas as pd

produtores = pd.read_csv(
    "dados/Facepe/produtores.csv",
    dtype={"agencia": str, "conta_corrente": str, "cpf": str, "telefone": str},
    parse_dates=["data_nascimento", "validade_certificacao"],
)
produtores["produtos"] = produtores["produtos"].str.split(",")

ref = pd.Timestamp("2026-10-30")
produtores["organico_vigente"] = produtores["validade_certificacao"].ge(ref)
```

## Visão dos produtores

| ID | Nome | Município | Produtos | Orgânico | Validade | Status |
|---|---|---|---|---|---|---|
| P001 | Maria das Graças Silva Ferreira | Garanhuns | goiaba, acerola, manga | sim | 15/06/2027 | ativo |
| P002 | José Antônio dos Santos | Brejão | mel silvestre, mel de jandaíra, própolis | sim | 20/03/2027 | ativo |
| P003 | Cícero Romão de Almeida | Caetés | queijo coalho, queijo manteiga, ricota | não | — | ativo |
| P004 | Ana Lúcia Bezerra da Costa | Jucati | alface, coentro, cebolinha, rúcula, tomate cereja | sim | **01/12/2026** ⚠️ | ativo |
| P005 | Francisco de Assis Cordeiro | Garanhuns | macaxeira, inhame, batata doce | não | — | ativo |
| P006 | Severina Maria do Nascimento | Garanhuns | banana, maracujá, coco verde | não | — | ativo |
| P007 | Antônio Carlos Pereira Lima | Lajedo | queijo coalho, manteiga de garrafa, nata | sim | 10/08/2027 | ativo |
| P008 | Josefa Alves de Souza | Águas Belas | mel silvestre, geleia de frutas, doce de goiaba | sim* | **30/09/2026** ❌ | ativo |
| P009 | Marcos Vinícius da Rocha | Garanhuns | alface, rúcula, espinafre, couve, hortelã, manjericão | sim | 22/04/2027 | ativo |
| P010 | Raimunda Nonata de Lima | Bom Conselho | goiaba, manga, cajá, umbu, graviola | não | — | ativo |
| P011 | Pedro Henrique Albuquerque | Caetés | leite, queijo coalho, iogurte natural | não | — | ativo |
| P012 | Luciana Ferreira Gomes | Garanhuns | tomate, pimentão, pepino, berinjela, quiabo | sim | 15/01/2027 | ativo |
| P013 | Edmilson Pereira da Silva | Jucati | farinha de mandioca, tapioca, goma | não | — | ativo |
| P014 | Mariana de Fátima Cordeiro | Garanhuns | morango, amora, mirtilo, framboesa | sim | 01/07/2027 | ativo |
| P015 | Geraldo Manoel Barbosa | Lajedo | macaxeira, feijão, milho, fava | não | — | **inativo** |
| P016 | Terezinha de Jesus Oliveira | Bom Conselho | rapadura, açúcar mascavo, melado | não | — | ativo |
| P017 | Wellington da Silva Monteiro | Garanhuns | frango caipira, ovos caipira | não | — | ativo |
| P018 | Débora Cristina de Melo | Águas Belas | mel silvestre, cera de abelha, pólen | sim | 28/02/2027 | ativo |

\* P008 aparece como `sim` no CSV, mas o certificado venceu. Ver [certificacoes.md](certificacoes.md#p008--certificação-vencida).

**Distribuição:** Garanhuns 7, Caetés 2, Jucati 2, Lajedo 2, Águas Belas 2, Bom Conselho 2, Brejão 1.

**Por linha de produto:**

| Linha | Produtores |
|---|---|
| Frutas | P001, P006, P010, P014 |
| Laticínios | P003, P007, P011 |
| Apícolas | P002, P008, P018 |
| Hortaliças | P004, P009, P012 (todos orgânicos) |
| Raízes e derivados | P005, P013, P015 (inativo) |
| Doces e derivados de cana | P008 (geleia, doce), P016 |
| Aves | P017 |

## Atributos que só existem em outras fontes

O cadastro não tem vários campos exigidos por compradores e fiscalização. Eles estão espalhados nos documentos de texto:

| ID | DAP/CAF | Registro SIE | Fonte |
|---|---|---|---|
| P001 | SDR/PE-2019-0047821 | n/a (vegetal) | certificado |
| P002 | SDR/PE-2018-0031456 | **PE-SIE-0892** (casa do mel) | certificado, laudo, ADAGRO |
| P003 | não informado | **PE-SIE-0756** (queijaria) | laudo, ADAGRO, ficha |
| P004 | SDR/PE-2020-0058934 | n/a | certificado |
| P005 | "vigente" (sem número) | n/a | ADAGRO, ficha |
| P007 | SDR/PE-2017-0022178 | **PE-SIE-1203** (queijaria) | certificado, ficha |
| P008 | SDR/PE-2019-0051098 | pendente (mel) | certificado, ficha |
| P009 | SDR/PE-2021-0072345 | n/a | certificado |
| P011 | não informado | **não possui** | ficha, tabela |
| P012 | SDR/PE-2020-0064512 | n/a | certificado |
| P013 | "vigente" (sem número) | n/a | ficha |
| P014 | SDR/PE-2022-0089012 | n/a | certificado |
| P017 | não informado | **não possui** (NC grave da ADAGRO) | ADAGRO |
| P018 | SDR/PE-2020-0067890 | pendente (mel) | certificado, ficha |

**Sem DAP/CAF documentada:** P003, P006, P010, P011, P015, P016 e P017. Isso afeta o PNAE, que exige DAP/CAF de todos, e os pedidos PED-2026-009 e PED-2026-013, que perguntam pela CAF.

A capacidade produtiva semanal ou mensal aparece só nas [fichas técnicas](fichas_tecnicas.md) e em alguns certificados.

## Notificações: produtor × pedidos pendentes

Apoio ao pilar 4.3 do projeto ([projeto.md](../projeto.md#43-comunicação-com-produtores)): quais mensagens pendentes envolvem produtos de cada produtor, já filtradas por elegibilidade (orgânico vigente, SIE, status). A tabela indica produtores **relevantes**; a alocação final é uma proposta que a equipe confirma.

| Produtor | Pedidos relevantes | Observação para a notificação |
|---|---|---|
| P001 Maria das Graças | 002, 004, 006, 007, 009, 010, 011, 013 | Única fonte de goiaba e manga **orgânicas**. Também tem consulta própria (012). |
| P002 José Antônio | 001, 003, 004, 006, 007, 009, 010, 013 | Único mel elegível para o PNAE (orgânico + SIE). Único com jandaíra (003). |
| P003 Cícero | 001, 003, 005, 006, 007, 013 | Tem 200 kg de queijo parado (015). Único com queijo manteiga e ricota. Credor da SuperBom (006). |
| P004 Ana Lúcia | 001, 002, 004, 009, 010, 013 | ⚠️ Certificado vence em 01/12. Avisar sobre a renovação. |
| P005 Francisco | 002, 004, 013 | Macaxeira e inhame. |
| P006 Severina | 002, 004, 009, 013 | Banana e maracujá. Sem DAP/CAF documentada, o que trava o PNAE. |
| P007 Antônio Carlos | 001, 003, 005, 006, 007, 010, 013 | Queijo, manteiga e nata orgânicos, com SIE. |
| P008 Josefa | 001, 007 (mel só **convencional**) | ❌ Não ofertar como orgânico. Auditoria de renovação em 15/11. |
| P009 Marcos | 002, 004, 009, 010, 013 | Maior capacidade em folhosas (~800 kg/sem). |
| P010 Raimunda | 002, 004, 006, 007, 009, 013 | Goiaba e manga convencionais; cajá (007). |
| P011 Pedro Henrique | — | Sem SIE. Fica fora dos pedidos de revenda e institucionais. |
| P012 Luciana | 002, 004, 009, 010, 013 | Tomate e frutos de horta orgânicos. |
| P013 Edmilson | 007 | Tapioca/goma. |
| P014 Mariana | 008, 010, 013 | Berries orgânicos. A amora não cobre os 50 kg/sem do 008. |
| P015 Geraldo | — | Inativo. |
| P016 Terezinha | 007 | Rapadura. |
| P017 Wellington | ovos: 004, 007, 009, 013 ⚠️ · frango: 007, 013 ❌ | Avisar sobre a NC-001 e a necessidade do SIE. Ovos sem SIE só podem ir a consumidor final. |
| P018 Débora | 001, 006, 007, 009, 010, 013 | Mel orgânico, mas com SIE pendente (fora do PNAE). Pólen e cera. |

## Qualidade dos dados

| Problema | Impacto | Tratamento sugerido |
|---|---|---|
| `certificacao_organica` não considera a validade (P008) | Risco de vender como orgânico sem certificado vigente | Derivar `organico_vigente = validade_certificacao >= data_ref` |
| Nomes de produto genéricos (`goiaba`, `manga`, `banana`, `tomate`, `pimentão`, `tapioca`, `goma`, `mel de abelha jandaíra`) | Join com preço e sazonalidade falha | Tabela de sinônimos (ver [tabela_precos.md](tabela_precos.md#normalização-de-nomes-de-produto)) |
| `feijão`, `milho` e `fava` (P015) não existem na tabela de preços nem na sazonalidade | Sem preço ou disponibilidade | Tratar como fora de catálogo (o produtor está inativo) |
| CPFs com dígito verificador inválido | Validação de CPF rejeitaria 17 de 18 | Não validar o DV com estes dados |
| `agencia` e `conta_corrente` com zeros à esquerda | Perda de dígitos se lidos como número | `dtype=str` |
| Sem DAP/CAF nem SIE no cadastro | Não dá para checar elegibilidade a PNAE ou venda de produto animal | Enriquecer o cadastro a partir dos TXT |

## Uso pelo assistente

- **Identificar o remetente:** o telefone do WhatsApp em PED-2026-012 é o de P001 e o de PED-2026-015 é o de P003. O telefone casar é um indício, **não** uma autenticação suficiente para revelar dados financeiros.
- **Sugerir fornecedores:** filtre `status == "ativo"`, a certificação vigente na data da entrega (quando o pedido exige orgânico) e o SIE (quando o produto é de origem animal e o comprador não é consumidor final).
- **Nunca expor** CPF, data de nascimento, dados bancários ou endereço a compradores. Nome, município e certificação são, em geral, informação comercial que se pode compartilhar.
- **Cadastro de novos cooperados:** PED-2026-015 traz o CPF de um terceiro (Sebastião). Não há fluxo de cadastro nos dados. O assistente deve encaminhar o caso para a presidência e não registrar dados pessoais que vieram de outra pessoa.
