# Mascaramento de Dados Pessoais (PII) e Restauração (`#33`)

[← Índice de Desenvolvimento](README.md)

Este documento descreve a implementação da **Issue `#33`**, responsável pela proteção de dados pessoais (LGPD) entre as ferramentas/entrada do usuário (**Etapa 3.3**) e o provedor de LLM (**Etapa 3.4**), bem como pela restauração controlada para o operador interno (**Etapa 4c**).

---

## 1. Arquitetura e Arquivos Envolvidos

- **[`src/sabor/agent/mascarador.py`](../../src/sabor/agent/mascarador.py):** Implementa a classe `Mascarador` com mapas bidirecionais exclusivamente em memória (`_valor_para_token` e `_token_para_valor`).
- **[`src/sabor/agent/deps.py`](../../src/sabor/agent/deps.py):** Configura `AgentDeps.mascarador` com `default_factory=Mascarador`, garantindo que cada turno receba uma instância isolada em memória.
- **[`src/sabor/tools/`](../../src/sabor/tools/):** Pacote modular das ferramentas:
  - [`base.py`](../../src/sabor/tools/base.py): Fornece `_mascarar_opcional` e `_mascarar_pix`.
  - [`consultar_dados.py`](../../src/sabor/tools/consultar_dados.py) (`#7`) e [`buscar_documentos.py`](../../src/sabor/tools/buscar_documentos.py) (`#8`): Devolvem resultados já mascarados.
  - [`pedidos.py`](../../src/sabor/tools/pedidos.py) (`#9`) e [`comunicacao.py`](../../src/sabor/tools/comunicacao.py) (`#11`, `#12`): Módulos dedicados para as próximas tools.
- **[`src/sabor/agent/runner.py`](../../src/sabor/agent/runner.py):**
  - Mascara a mensagem do usuário **antes** de enviá-la ao `Agent` (`deps_exec.mascarador.mascarar(mensagem_limpa)`).
  - Na **Etapa 4c** (`_restaurar_pii_resposta`), restaura os tokens **exclusivamente em `resposta.texto`** (visão do operador interno) e **jamais em `resposta.rascunhos`** (mensagens destinadas a terceiros).
- **[`tests/agent/test_mascarador.py`](../../tests/agent/test_mascarador.py):** Suíte de 11 testes unitários e de integração cobrindo formatos com/sem pontuação, todos os 18 produtores do CSV, os pedidos reais com PII (`PED-2026-012`, `PED-2026-013`, `PED-2026-015`), ferramentas e proteção de rascunhos.

---

## 2. Categorias de Tokens e Formatos Suportados

| Categoria | Formato do Token | Exemplos Reconhecidos (com e sem pontuação) |
|---|---|---|
| **CPF** | `[CPF_1]`, `[CPF_2]`... | `831.452.674-90`, `CPF: 29418756341`, `72301649855` |
| **RG** | `[RG_1]`, `[RG_2]`... | `RG: 5.432.109 SDS/PE`, `5.432.109 SDS/PE`, `RG 1234567` |
| **Telefone** | `[TEL_1]`, `[TEL_2]`... | `(87) 99812-3456`, `(87) 3761-5000`, `Tel: 87996347812`, `87997451098`, `99812-3456` |
| **E-mail** | `[EMAIL_1]`, `[EMAIL_2]`... | `maria.graca@email.com`, `jose.santos.mel@email.com` |
| **Chave PIX** | `[PIX_1]`, `[PIX_2]`... | Colunas `chave_pix`/`chave_pix_deposito` via `mascarar_campo(v, 'PIX')` e textos `Chave PIX: ...` (CPF, e-mail, telefone ou UUID) |
| **Banco** | `[BANCO_1]`, `[BANCO_2]`... | `Banco do Brasil`, `Banco do Nordeste`, `Caixa Econômica`, `Bradesco`, `Itaú`, `Nubank`, `Sicoob` |
| **Agência** | `[AGENCIA_1]`, `[AGENCIA_2]`... | Valores de coluna (`0437`, `0023`) e texto livre (`Agência 0437`, `Ag. 0023`) |
| **Conta Corrente** | `[CONTA_1]`, `[CONTA_2]`... | `28901-3`, `0073921-7`, `00134578-2`, `55.678-9`, `C/C 63478-2`, `Conta: 28901-3` |

### Prevenção de Falsos Positivos
As expressões regulares foram construídas com *lookarounds* para **não** mascarar identificadores operacionais:
- Códigos de lote (`LOT-2026-0089`), transações (`FIN-2026-013`), pedidos (`PED-2026-001`) e produtores (`P001`–`P018`);
- Registros públicos/regulatórios: `PE-SIE-0756`, `SDR/PE-2019-0047821` e CNPJs de pessoas jurídicas (`10.578.224/0034-56`);
- Datas (`30/10/2026`) e quantidades (`500kg`).

---

## 3. Regra LGPD na Etapa 4c (Operador vs. Terceiros)

Conforme documentado em [`docs/README.md`](../README.md#dados-sensíveis-lgpd):
1. **Para o operador interno (`turno.texto`):** `_restaurar_pii_resposta` chama `mascarador.restaurar(resposta.texto)`, permitindo que Dona Lúcia e Seu Antônio vejam os dados necessários ao trabalho interno sem que esses dados tenham passado pelo provedor de LLM.
2. **Para terceiros (`turno.rascunhos`):** `_restaurar_pii_resposta` **nunca** executa `restaurar()` sobre os itens de `rascunhos`, impedindo vazamento de CPF, RG, telefone ou dados bancários em mensagens para compradores ou terceiros.

---

## 4. Guia para as Issues Pendentes (`#9`, `#11`, `#12`)

Quando as ferramentas de triagem (`#9`), resumo do produtor (`#11`) e rascunho de notificação (`#12`) forem adicionadas a [`src/sabor/tools.py`](../../src/sabor/tools.py):
- Acesse o mascarador do turno via `mascarador = ctx.deps.mascarador`.
- Para textos livres ou observações: use `mascarador.mascarar(texto)`.
- Para campos opcionais de tabela: use `_mascarar_opcional(mascarador, valor)` (ou `_mascarar_pix(mascarador, valor)` para chaves PIX).
