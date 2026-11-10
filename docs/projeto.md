# Contexto do projeto e alinhamento com os dados

[← Visão geral](README.md)

Este documento relaciona o enunciado do **Projeto Intermediário** (UPE Campus Garanhuns + Sauter Tecnologia, disciplina de IA Generativa e Agentes) com o conteúdo de `dados/Facepe/`. Ele resume os requisitos em termos de dados: o que cada requisito exige das fontes, quais perguntas servem de referência para testar e onde os dados **não têm** resposta.

> O enunciado original (PDF, nov/2026) é de uso interno e confidencial. Por isso não está versionado aqui; este documento só o resume.

## Resumo do desafio

- **Cliente:** Cooperativa Sabor da Terra, fictícia, do Agreste Meridional de Pernambuco.
- **Usuários do assistente:** a **equipe da cooperativa**, em especial **Dona Lúcia** e **Seu Antônio**, que hoje gastam o dia inteiro na triagem. O assistente é uma interface conversacional em texto para essa equipe. Ele não fala diretamente com compradores ou produtores: **gera rascunhos, relatórios e notificações** que a equipe revisa e envia.
- **Entregáveis:**
  - Protótipo funcional com demonstração ao vivo.
  - Apresentação de 10 minutos sobre as decisões técnicas, as alternativas e as limitações.
  - Arquitetura.
- **Conteúdo das aulas a aplicar:** Engenharia de Prompt, RAG, Agentes e Google Cloud.

| Data | Evento |
|---|---|
| 05/11/2026 | Formação dos grupos |
| **19/11/2026** | **Entrega obrigatória da arquitetura.** Mudanças depois desta data são desconsideradas. (O PDF traz o erro de digitação "19/111/2026".) |
| 20–21/11/2026 | Apresentação e demonstração do protótipo |

> "Seu Antônio" é da equipe de triagem e não aparece nos dados. Não confundir com o produtor **P007, Antônio Carlos Pereira Lima**.

## Cenário descrito × dados fornecidos

O enunciado descreve a cooperativa inteira; os dados são uma **amostra**. O assistente deve responder com base nos **dados** e nunca extrapolar os números do cenário.

| Aspecto | Cenário (enunciado) | Dados (`dados/Facepe/`) |
|---|---|---|
| Produtores | 180 | 18 (17 ativos) |
| Municípios | 12 | 7 |
| Volume de pedidos | 60–100 por dia | 15 mensagens em 3 dias |
| Canais | e-mail, formulário web, mensagens de texto | e-mail, formulário web, WhatsApp |
| Programas públicos | PNAE **e PAA** | Só PNAE; **não há dados de PAA** |
| Documentos | "centenas" | 20 TXT |
| Exemplo de lote | "lote 2024-089" | `LOT-2026-0089` (ver abaixo) |

A diferença de volume (60–100 pedidos por dia) é um argumento de arquitetura: a triagem precisa ser automatizável em lote, e não só no chat.

## Os três pilares e as fontes de cada um

### 4.1 Interpretação e triagem de pedidos

> Receber pedidos em linguagem natural, mesmo incompletos ou ambíguos; extrair produtos, quantidades, prazos e comprador; classificar por **urgência e tipo**; sinalizar **produto fora de safra** e **quantidade acima da capacidade**.

| Etapa | Fontes | Doc |
|---|---|---|
| Extrair itens e prazos | `pedidos.json` | [pedidos.md](fontes/pedidos.md) |
| Normalizar o nome do produto | catálogo da tabela de preços | [tabela_precos.md](fontes/tabela_precos.md#normalização-de-nomes-de-produto) |
| Checar safra no mês da entrega | `sazonalidade.csv` | [sazonalidade.md](fontes/sazonalidade.md) |
| Checar capacidade por produtor | `fichas_tecnicas/`, certificados, observações da sazonalidade | [fichas_tecnicas.md](fontes/fichas_tecnicas.md#capacidade-por-produtor) |
| Checar conformidade (orgânico, SIE) | `certificacoes/`, `produtores.csv` | [certificacoes.md](fontes/certificacoes.md#regras-de-negócio-derivadas) |
| Checar condições do comprador | `contratos/`, `financeiro.csv` (inadimplência) | [contratos.md](fontes/contratos.md), [financeiro.md](fontes/financeiro.md) |

A classificação de urgência, os alertas e o formato de saída estruturado estão em [pedidos.md](fontes/pedidos.md#classificação-de-urgência-e-sinalizações).

Além das duas sinalizações que o enunciado pede (safra e capacidade), os dados trazem outras que valem como diferencial na demonstração: **conformidade** (frango sem SIE, orgânico vencido), **crédito** (SuperBom inadimplente), **ambiguidade** (qual queijo, qual manteiga, data e dia da semana que não batem) e **privacidade** (pedidos de CPF e dados bancários).

### 4.2 Consulta inteligente à base documental

> Responder a compradores, auditores e equipe sobre certificações, rastreabilidade, sazonalidade e histórico de lotes, **fundamentado nos documentos e citando as fontes**.

- **Fontes estruturadas** (CSV/JSON), boas para consulta exata via ferramenta, SQL ou pandas: produtores, financeiro, lotes, preços, sazonalidade.
- **Fontes não estruturadas** (TXT), boas para RAG: certificações, contratos, fichas.
- Várias perguntas exigem **as duas**. Exemplo: rastrear um lote envolve `lotes.csv` + `produtores.csv` + o laudo ou certificado do produtor.

**Convenção sugerida para citar fontes:** cite o caminho do arquivo e, quando houver, o identificador do registro ou a seção:

```
[lotes.csv · LOT-2026-0089]
[financeiro.csv · FIN-2026-013]
[certificacoes/cert_organica_jose_santos_P002.txt · PRODUTOS CERTIFICADOS]
[contratos/contrato_pnae_garanhuns.txt · CONDIÇÕES OBRIGATÓRIAS]
```

### 4.3 Comunicação com produtores

> Gerar **resumos personalizados por produtor** (vendas, pedidos alocados, pendências financeiras) e redigir **notificações de novos pedidos** para os produtos de cada um.

| Componente do resumo | Fonte |
|---|---|
| Vendas no período | `financeiro.csv`, filtrado por `produtor_id` e `data` |
| Pendências financeiras | `financeiro.csv` com `status_pagamento = pendente`, comparando o vencimento com a data de referência |
| Pedidos para os produtos do produtor | `pedidos.json` × `produtores.produtos`, filtrado por elegibilidade |
| Alertas do produtor | certificação vencendo ou vencida, NC da ADAGRO, laudos |

A matriz de qual produtor é relevante para qual pedido está em [produtores.md](fontes/produtores.md#notificações-produtor--pedidos-pendentes). O saldo por produtor está em [financeiro.md](fontes/financeiro.md#contas-a-receber).

**Restrição: não existe tabela de alocação.** Os dados não dizem qual produtor vai atender qual pedido. A alocação é uma **saída** que o assistente propõe e a equipe confirma, não algo que ele consulta.

## Requisitos funcionais e o que os dados exigem

| Requisito | O que os dados exigem |
|---|---|
| Interface conversacional para a equipe | As respostas são para a equipe interna, que pode ver CPF e dados bancários. Já os **rascunhos para terceiros** precisam omitir esses dados (ver [LGPD](README.md#dados-sensíveis-lgpd)). |
| Português coloquial, abreviações, erros e informação implícita | PED-003, PED-005 e PED-008 são os casos mais informais. PED-011 está em inglês. Há datas relativas ("amanhã", "sábado") e produtos implícitos ("o de sempre", "aquele queijo"). |
| Precisão e indicação do documento de origem | Ver a convenção de citação acima. |
| Relatórios e notificações autônomos quando solicitado | Resumo por produtor, contas a receber, dossiê para a ADAGRO, lista de orgânicos vigentes. |
| Manter contexto na sessão | Ex.: "e para o hotel?" logo depois de discutir disponibilidade de queijo. |
| **Dizer claramente quando não souber, sem inventar** | Ver as [lacunas conhecidas](#lacunas-conhecidas) abaixo, que são bons casos de teste. |

## Perguntas de referência

Perguntas com resposta verificável nos dados, úteis para avaliar o protótipo e para a demonstração. As duas primeiras são exemplos do próprio enunciado.

| # | Pergunta | Resposta esperada | Fontes |
|---|---|---|---|
| 1 | Quais produtores têm certificação orgânica vigente para mel? | **P002** José Antônio dos Santos (até 20/03/2027) e **P018** Débora Cristina de Melo (até 28/02/2027). P008 Josefa está **vencida** desde 30/09/2026, com renovação em andamento e auditoria em 15/11. | certificados P002, P018, P008; `produtores.csv` |
| 2 | Qual a origem e a rastreabilidade do queijo do lote 2024-089? | O ID mais provável é **LOT-2026-0089** (o ano no enunciado diverge; confirmar). Queijo coalho, 45 kg, de **P003** Cícero Romão de Almeida (Caetés). Fabricado em 01/09/2026 e entregue em 02/09/2026 ao Restaurante Raízes (PED-2026-SEP-003), transportado a 8 °C, validade 02/10/2026, inspeção visual aprovada. Queijaria com SIE PE-SIE-0756 e laudo VS-CAE-2026-0034 aprovado. Não é orgânico. | `lotes.csv`, `produtores.csv`, laudo P003 |
| 3 | Quanto a Maria das Graças vendeu e o que tem a receber? | Setembro: R$ 1.090,00 (FIN-001, 002, 015), tudo pago. Outubro: nenhuma venda. Pendente: R$ 0,00. | `financeiro.csv` |
| 4 | Quem a SuperBom está devendo e há quanto tempo? | R$ 6.020,00 (FIN-013/014), vencidos em 18/10/2026. O credor é P003. | `financeiro.csv` |
| 5 | Tem cajá em novembro? | Disponibilidade **baixa**. Só P010, R$ 8,00/kg, sem atacado. | `sazonalidade.csv`, `tabela_precos`, `produtores.csv` |
| 6 | Posso mandar frango caipira para o Hotel Fazenda? | **Não.** P017 não tem SIE, e a NC-001 grave da ADAGRO determinou a suspensão imediata. | `inspecao_adagro_2026.txt`, `tabela_precos` |
| 7 | Qual o prazo de pagamento do PNAE e o preço do queijo no contrato? | Empenho em até 45 dias após o atesto da nutricionista. Queijo coalho a R$ 34,00/kg. | `contrato_pnae_garanhuns.txt` |
| 8 | Quem produz mel de jandaíra, quanto custa e quanto há? | Só P002, R$ 140,00/kg (orgânico), cerca de 180 kg/ano. | certificado P002, `tabela_precos` |
| 9 | Como armazenar queijo coalho e qual a validade? | Entre 2 e 8 °C, a vácuo ou em filme, validade de 30 dias. | `fichas_tecnicas/queijo_coalho.txt` |
| 10 | Quais documentos a ADAGRO pediu e o que falta? | Ver o PED-2026-014 em [pedidos.md](fontes/pedidos.md#ped-2026-014--adagro--e-mail--notificação-de-inspeção-). Faltam laudos e o histórico de 3 lotes da maioria dos produtos. | `pedidos.json`, `lotes.csv`, `certificacoes/` |

## Lacunas conhecidas

Perguntas plausíveis **sem resposta nos dados**. O comportamento correto é dizer que a informação não foi encontrada e, se fizer sentido, sugerir a quem perguntar.

| Pergunta | Por que não há resposta |
|---|---|
| Número da DAP/CAF de P003, P006, P010, P011, P016 e P017 | Não consta em nenhum documento |
| Qual a chave PIX da cooperativa? | Só existe a conta BB 0437 / 55.678-9, nos contratos |
| Certificado fitossanitário ou certificação orgânica internacional (PED-011) | Não existe nos dados |
| Capacidade de banana, maracujá e coco (P006), de manga e cajá (P010) e de rapadura (P016) | Não há ficha nem certificado com capacidade |
| Contrato com a Rede SuperBom | Não há arquivo |
| Qualquer dado sobre o **PAA** | Citado no cenário, ausente nos dados |
| Lote do pimentão de FIN-2026-017 (e outras 11 transações) | Não registrado em `lotes.csv` |
| Preço de feijão, milho e fava | Fora da tabela (P015 inativo) |
| Pedidos `PED-2026-SEP-*` e `PED-2026-OCT-*` citados nos lotes | Não estão em `pedidos.json` |
| Laudo sanitário de P007, P008 e P018 | Não há arquivo |
| A NC-002 (termômetro) foi corrigida? | Sem registro |
