# `certificacoes/` — Certificados, laudos e inspeção

[← Visão geral](../README.md)

Documentos regulatórios em texto livre (TXT com layout de formulário). Definem **o que pode ser vendido como orgânico** e **o que pode ser vendido a terceiros**, que são as duas restrições de maior risco para o assistente.

| Arquivo | Tipo | Sujeito |
|---|---|---|
| `cert_organica_maria_silva_P001.txt` | Certificado orgânico (IBD) | P001 |
| `cert_organica_jose_santos_P002.txt` | Certificado orgânico (IBD) | P002 |
| `cert_organica_ana_bezerra_P004.txt` | Certificado orgânico (Ecocert) | P004 |
| `cert_organica_antonio_lima_P007.txt` | Certificado orgânico (IBD) | P007 |
| `cert_organica_josefa_souza_P008.txt` | Certificado orgânico (IBD), **vencido** | P008 |
| `cert_organica_marcos_rocha_P009.txt` | Certificado orgânico (Ecocert) | P009 |
| `cert_organica_luciana_gomes_P012.txt` | Certificado orgânico (Ecocert) | P012 |
| `cert_organica_mariana_cordeiro_P014.txt` | Certificado orgânico (IBD) | P014 |
| `cert_organica_debora_melo_P018.txt` | Certificado orgânico (IBD) | P018 |
| `laudo_sanitario_apiario_jose_P002.txt` | Laudo da Vigilância Sanitária de Brejão | Casa do mel de P002 |
| `laudo_sanitario_queijaria_cicero_P003.txt` | Laudo da Vigilância Sanitária de Caetés | Queijaria de P003 |
| `inspecao_adagro_2026.txt` | Relatório de inspeção da ADAGRO | Cooperativa e 5 produtores |

**Como ligar ao cadastro:** o sufixo `_P0XX` no nome do arquivo + o CPF no corpo. O relatório da ADAGRO cita os produtores por ID.

**Como extrair:** os arquivos seguem o padrão `Rótulo: valor` em blocos com títulos em maiúsculas (`DADOS DO PRODUTOR:`, `PRODUTOS CERTIFICADOS:`, `RESULTADO DA AUDITORIA`). O layout varia entre certificadoras e entre documentos. Para RAG, divida em chunks por bloco e mantenha o ID do produtor como metadado.

## Certificados orgânicos

| ID | Nº certificado | Certificadora | Emissão | Validade | Status em 09/11/2026 | Produtos certificados |
|---|---|---|---|---|---|---|
| P001 | ORG-PE-2025-004721 | IBD | 15/06/2025 | 15/06/2027 | ✅ Vigente | goiaba paluma, acerola, manga tommy |
| P002 | ORG-PE-2025-003198 | IBD | 20/03/2025 | 20/03/2027 | ✅ Vigente | mel silvestre, mel de jandaíra, própolis verde |
| P004 | ECO-BR-2024-PE-0891 | Ecocert | 01/12/2024 | **01/12/2026** | ⚠️ **Vence em 22 dias** | alface, coentro, cebolinha, rúcula, tomate cereja |
| P007 | ORG-PE-2025-005432 | IBD | 10/08/2025 | 10/08/2027 | ✅ Vigente | queijo coalho, manteiga de garrafa, nata |
| P008 | ORG-PE-2024-003876 | IBD | 30/09/2024 | **30/09/2026** | ❌ **Vencido** | ~~mel silvestre, geleia, doce de goiaba~~ |
| P009 | ECO-BR-2025-PE-1102 | Ecocert | 22/04/2025 | 22/04/2027 | ✅ Vigente | alface, rúcula, espinafre, couve, hortelã, manjericão |
| P012 | ECO-BR-2025-PE-0998 | Ecocert | 15/01/2025 | 15/01/2027 | ✅ Vigente (vence em ~2 meses) | tomate, pimentão, pepino, berinjela, quiabo |
| P014 | ORG-PE-2025-006789 | IBD | 01/07/2025 | 01/07/2027 | ✅ Vigente | morango, amora, mirtilo, framboesa |
| P018 | ORG-PE-2025-002345 | IBD | 28/02/2025 | 28/02/2027 | ✅ Vigente | mel silvestre, cera, pólen |

Selos: IBD = Sisorg BR-BIO-134; Ecocert = Sisorg BR-BIO-001. A validade de todos os certificados é de 2 anos.

### Informações úteis contidas nos certificados

| ID | Área | Capacidade | Destaques |
|---|---|---|---|
| P001 | 12 ha (8 certificados) | goiaba 25 t/ano, acerola 12 t/ano, manga 18 t/ano | Sequeiro. Próxima auditoria em dez/2026. |
| P002 | 25 ha (6 com apiários) | mel 3.600 kg/ano (120 colmeias), jandaíra 180 kg/ano, própolis 80 kg/ano | SIE PE-SIE-0892. Análise do mel conforme. |
| P004 | 3 ha (2,5 certificados) | — | Auditoria pede ampliar a compostagem. |
| P007 | 35 ha (20 de pastagem) | queijo até 200 kg/sem, manteiga 40, nata 30 | SIE PE-SIE-1203. 45 vacas. "Queijaria modelo". |
| P009 | 1,2 ha (4 estufas) | ~800 kg/semana | Hidroponia orgânica. |
| P012 | 4 ha | — | NC menor (barreira vegetal) corrigida em 28/03/2025. |
| P014 | 1,5 ha | morango ~150 kg/sem (safra), demais ~40 kg/sem cada | "Única produtora de berries orgânicos certificados no Agreste Meridional". |
| P018 | — | mel ~2.400 kg/ano (80 colmeias), cera ~100, pólen ~200 | Contém dados bancários (ver anomalias). |

### P008 — certificação vencida

- Venceu em **30/09/2026**. A renovação foi pedida em 15/09/2026 (protocolo REN-2026-0456), e a auditoria de renovação está marcada para **15/11/2026**.
- Texto do certificado: "*Até a conclusão da renovação, os produtos NÃO podem ser comercializados como orgânicos.*"
- **Divergências:**
  - `produtores.csv` ainda marca `certificacao_organica = sim`.
  - O cabeçalho do certificado diz "validade próxima", mas o corpo diz "VENCIDA".
  - Houve venda ao preço orgânico em 01/10 (FIN-021/022, LOT-0104).

### P004 — renovação não registrada

O certificado diz "*Renovação deve ser solicitada até 01/10/2026*". Não há protocolo de renovação nos dados. Se não renovar, a partir de 02/12/2026 as hortaliças de P004 deixam de ser orgânicas, o que afeta PED-2026-010 (fornecimento mensal) e PED-2026-013 (janeiro/2027).

## Laudos sanitários

| Laudo | Estabelecimento | Inspeção | Resultado | Pendências | Próxima |
|---|---|---|---|---|---|
| VS-BRJ-2026-0012 | Casa do mel de P002 (SIE PE-SIE-0892) | 05/05/2026 | **Aprovado sem ressalvas**. Físico-química do mel dentro dos limites; resíduos não detectados. | — | nov/2026 |
| VS-CAE-2026-0034 | Queijaria de P003 (SIE PE-SIE-0756) | 12/08/2026 | **Aprovado**. Microbiologia conforme (Salmonella e Listeria ausentes). | NC menor: lixeira sem pedal, prazo 12/09/2026, "pendente de verificação" | fev/2027. O laudo vale 6 meses (até ~12/02/2027). |

**Lacuna:** não há laudo para a queijaria de **P007**, embora ela tenha SIE e o certificado cite o alvará nº 2025/0234. Também não há laudo para P008 nem P018 (mel). A ADAGRO pede "laudos dos últimos 12 meses" (PED-2026-014).

## Relatório de inspeção ADAGRO (ADAGRO-AM-2026-0189)

- **Data:** 15–16/05/2026, inspeção anual programada.
- **Resultado:** **APROVADO COM RESTRIÇÕES.** Próxima inspeção programada para maio/2027, mas a ADAGRO já notificou uma visita em **11–12/11/2026** (PED-2026-014).
- **Cooperativa:**
  - CNPJ 45.678.901/0001-23.
  - Sede: Rua do Comércio, 234, Centro, Garanhuns.
  - CD: Rod. BR-423 km 5, Galpão 3.
  - Presidente: Lúcia Helena Barros Cavalcanti.
  - Responsável técnico: João Marcos Ferreira (CREA-PE 14.567-D).

**Produtores vistoriados:**

| ID | Situação | Nota |
|---|---|---|
| P001 | Regular | Orgânico vigente |
| P002 | Regular | Orgânico e SIE vigentes |
| P003 | Regular | SIE vigente, sem orgânico |
| P005 | Regular | DAP/CAF vigente |
| P017 | **IRREGULAR** | Frango sem SIE |

**Depósito central:** recebimento, câmaras frias (laticínios 4 °C, frutas 10 °C), área seca, controle de pragas e rastreabilidade estão conformes. Veículos: conformes com observação.

**Não conformidades:**

| NC | Gravidade | Descrição | Prazo | Situação nos dados |
|---|---|---|---|---|
| NC-001 | **Grave** | P017 sem SIE. A cooperativa deve **suspender a intermediação de frango caipira**. Ovos estão dispensados de SIE **apenas na venda direta ao consumidor**. | Imediato | ❌ **Descumprida**: venda de frango em 25/09 (FIN-018, LOT-0103), frango ainda na tabela de preços, e pedidos pendentes (PED-007, PED-013) pedem frango. |
| NC-002 | Menor | Termômetro descalibrado na caminhonete DEF-5678 | 15 dias (~31/05/2026) | ❓ Sem evidência de correção |

> O relatório manda P017 regularizar "junto ao SIE municipal". O correto seria SIM (municipal) ou SIE (estadual). É uma imprecisão do documento.

## Produtos orgânicos vendáveis

Lista consolidada para responder pedidos como PED-2026-010 e PED-2026-009. Situação em 09/11/2026:

| Linha | Produto | Produtor(es) | Válido até |
|---|---|---|---|
| Frutas | goiaba paluma, acerola, manga tommy | P001 | 15/06/2027 |
| Berries | morango, amora, mirtilo, framboesa | P014 | 01/07/2027 |
| Folhosas | alface, rúcula | P004 ⚠️, P009 | 01/12/2026 / 22/04/2027 |
| Folhosas | coentro, cebolinha | P004 ⚠️ | 01/12/2026 |
| Folhosas | espinafre, couve, hortelã, manjericão | P009 | 22/04/2027 |
| Frutos de horta | tomate cereja | P004 ⚠️ | 01/12/2026 |
| Frutos de horta | tomate, pimentão, pepino, berinjela, quiabo | P012 | 15/01/2027 |
| Laticínios | queijo coalho, manteiga de garrafa, nata | P007 | 10/08/2027 |
| Apícolas | mel silvestre | P002, P018 | 20/03/2027 / 28/02/2027 |
| Apícolas | mel de jandaíra, própolis | P002 | 20/03/2027 |
| Apícolas | pólen, cera | P018 | 28/02/2027 |

**Não orgânicos:**
- Banana, maracujá, coco, cajá, umbu e graviola.
- Queijo de P003 e P011.
- Raízes, farinhas, derivados de cana, frango e ovos.
- **Tudo de P008** até a renovação.

## Regras de negócio derivadas

1. **Orgânico** ⇔ o produtor tem certificado com `validade >= data de entrega` **e** o produto consta da lista certificada. Não basta o campo `certificacao_organica` do cadastro.
2. **Produto de origem animal** (queijo, manteiga, nata, mel, pólen, própolis, frango, ovos, iogurte) vendido a **pessoa jurídica ou órgão público** ⇒ o produtor precisa ter SIE, SIM ou SISBI. Produtores com registro: P002, P003 e P007.
3. **Frango caipira não deve ser intermediado** enquanto P017 não tiver registro.
4. Antes de usar um certificado em proposta ou dossiê, verifique se ele vence antes da data de fornecimento.

## Anomalias e dados sensíveis

- O certificado de **P018** traz "DADOS BANCÁRIOS PARA ROYALTIES DA CERTIFICAÇÃO" (banco, agência, conta, PIX). Certificados não costumam conter isso. Trate o arquivo como sensível e **não use certificados como fonte de dados bancários**.
- Os certificados trazem CPF, CEP e endereço dos produtores. Ao enviar cópias a compradores (PED-009, PED-010, PED-011), avalie se é preciso mascarar o CPF.
- O relatório da ADAGRO traz o CPF da presidente da cooperativa.
