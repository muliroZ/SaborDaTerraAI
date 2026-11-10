# `contratos/` — Contratos com compradores

[← Visão geral](../README.md)

Três contratos em texto livre com os principais compradores. Definem preços (só no PNAE), condições de pagamento, janelas de entrega, exigências documentais e penalidades. Prevalecem sobre a tabela de preços e sobre o que a mensagem do cliente afirmar.

| Arquivo | Nº | Contratante | Tipo | Vigência |
|---|---|---|---|---|
| `contrato_mercado_bom_preco.txt` | 012/2026 | Mercado Bom Preço Ltda. | Fornecimento contínuo (varejo) | 01/01/2026 – **31/12/2026**, renovável |
| `contrato_pnae_garanhuns.txt` | 045/2026-PNAE | Município de Garanhuns (Sec. Educação) | Compra pública da agricultura familiar | 01/03/2026 – 28/02/2027 |
| `contrato_restaurante_raizes.txt` | 008/2026 | Restaurante Raízes – Cozinha Regional | Fornecimento preferencial sob demanda | Indeterminada (aviso prévio de 30 dias) |

**Contratada em todos:** Cooperativa dos Produtores Rurais Sabor da Terra, CNPJ 45.678.901/0001-23, representada pela presidente Lúcia Helena Barros Cavalcanti.

**Conta de recebimento da cooperativa** (Bom Preço e PNAE): Banco do Brasil, ag. 0437, c/c 55.678-9.

## Comparativo

| | Mercado Bom Preço | PNAE Garanhuns | Restaurante Raízes |
|---|---|---|---|
| **Objeto** | Frutas, hortaliças, tubérculos e laticínios para 3 filiais | Gêneros para 45 escolas municipais | Produtos regionais, prioridade para orgânicos e locais |
| **Preço** | "Tabela vigente da cooperativa", reajuste trimestral pelo IPCA | **Preço fixo por item** (tabela abaixo) | Não especificado (tabela vigente) |
| **Pagamento** | Boleto em 30 dias | Empenho em até 45 dias após o atesto da nutricionista | **PIX no ato da entrega** |
| **Entrega** | Terças e sextas, 6h–10h, CD Rod. BR-423 km 3 | Semanal conforme cardápio, depósito central Rua da Educação 500 (7h–12h, segundo PED-004) | No restaurante até as 10h, 2–3x por semana |
| **Exigências** | Padrão de qualidade | DAP/CAF de todos os produtores; certificado orgânico válido; SIE/SIM/SISBI para produtos animais; NF individual por produtor | — |
| **Penalidades** | Multa de 2% ao dia por atraso; devolução de produto fora do padrão; rescisão com 60 dias de aviso | (não especificadas) | Rescisão com 30 dias de aviso |
| **Contato** | Ernesto Cavalcanti de Albuquerque (diretor comercial) | Dr. Fernando Augusto Mendes (ordenador); nutricionista Dra. Carla Patrícia de Souza; fiscais Ana Cláudia Ferreira e Marcos Aurélio Gomes | Chef Carlos Eduardo Ramos |

### Volumes semanais estimados — Mercado Bom Preço

goiaba paluma 50–80 kg · banana prata 40–60 kg · manga (safra) 30–50 kg · maracujá 20–30 kg · macaxeira 50–80 kg · inhame 20–40 kg · tomate 30–50 kg · alface 50–80 un · rúcula 20–40 maços · queijo coalho 30–50 kg · rapadura 20–30 kg.

### Preços contratados — PNAE (comparados à tabela de outubro)

| Item | Contrato | Tabela de out/2026 | Praticado em `financeiro.csv` |
|---|---|---|---|
| Banana prata | R$ 3,80/kg | 3,50 | — |
| Goiaba paluma | R$ 5,20/kg | 3,50–5,50 | — |
| Manga tommy (safra) | R$ 5,50/kg | 5,00 / 5,50 org. | — |
| Maracujá | R$ 6,00/kg | 6,00 | — |
| Macaxeira | R$ 3,50/kg | 3,20 | — |
| Inhame | R$ 5,00/kg | 4,80 | — |
| Batata doce | R$ 4,00/kg | 4,00 | — |
| Alface | R$ 2,80/un | 2,50 / 2,80 org. | **2,50** ⚠️ |
| Coentro | R$ 2,00/maço | 1,80 / 2,00 org. | **1,80** ⚠️ |
| Tomate | R$ 7,00/kg | 6,00 / 7,00 org. | — |
| Queijo coalho | R$ 34,00/kg | 32,00 / 38,00 org. | **31,00** ⚠️ |
| Mel silvestre (orgânico) | R$ 58,00/kg | 58,00 org. | **55,00** ⚠️ |
| Ovos caipira | R$ 18,00/dz | 18,00 | — |
| Frango caipira | R$ 29,00/kg | 28,00 | — (bloqueado) |

O contrato tem valor total estimado de **R$ 480.000,00** (R$ 40.000,00/mês). Base legal: Lei 11.947/2009 e Resolução FNDE 06/2020 (art. 14: mínimo de 30% para a agricultura familiar). Publicado no DOM em 25/02/2026.

## Pontos de atenção

1. **O contrato do Mercado Bom Preço vence em 31/12/2026.** A renovação é por igual período e precisa ser tratada. O contrato não diz como a renovação acontece. Se o prazo de 60 dias do cancelamento unilateral valer também para a não renovação, a janela de aviso terminou em ~01/11/2026.
2. **Preços praticados com o PNAE estão abaixo dos contratados** (ver tabela). É receita que a cooperativa deixa de receber.
3. **Condições do PNAE x produtores:**
   - Ovos e frango de P017 não atendem à condição 3 (falta registro sanitário).
   - O mel orgânico com SIE vem só de P002.
   - Não há DAP/CAF documentada para P003, P006, P010, P011, P016 e P017.
4. **Não há contrato com a Rede SuperBom Atacado,** embora o PED-2026-006 diga "boleto 30 dias conforme contrato". Trate como cliente sem contrato e com títulos vencidos.
5. **Divergência de cargo:** no contrato PNAE, Dr. Fernando Augusto Mendes é "Secretário de Educação"; no PED-2026-004 assina como "Coordenador de Alimentação Escolar".
6. **Dias de entrega:** o Bom Preço recebe terças e sextas, e o PED-2026-002 pede "terça 04/nov", que cai numa quarta. Confirme.
7. **PIX do Restaurante Raízes:** a chave informada (CNPJ do restaurante) é **só para devoluções**. O pagamento vai da Raízes para a cooperativa.

## Dados sensíveis

Os contratos contêm CPF de representantes, testemunhas, nutricionista e fiscais. Não reproduza esses dados em respostas. Para citar um contrato, use número, partes (pessoa jurídica) e cláusulas.

## Uso pelo assistente

- **Cotação para comprador com contrato:** no PNAE, use o preço do contrato. No Bom Preço e no Raízes, use a tabela vigente.
- **Validação de pedido:** confira a janela de entrega, as exigências documentais e se o produtor elegível tem capacidade.
- **Respostas ao PNAE e à ADAGRO:** cite o número do contrato e liste os documentos que acompanham a entrega (NF por produtor, certificados).
