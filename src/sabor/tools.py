"""Ferramentas (tools) read-only do agente SaborDaTerraAI.

Convenções para implementação das ferramentas (Issues #7, #8, #9, #11, #12):
1. Nomenclatura: defina as funções públicas em `snake_case` (sem prefixo `_`).
   O agente descobre e registra automaticamente as funções públicas definidas neste módulo.
   Nomes de referência previstos na arquitetura:
   - `extrair_e_classificar_pedido` (Pilar 4.1 - Issue #9)
   - `consultar_dados` (SQL parametrizada - Issue #7)
   - `buscar_documentos` (Pilar 4.2: busca híbrida lexical + vetorial - Issue #8)
   - `resumir_produtor` (Pilar 4.3: resumo do produtor - Issue #11)
   - `rascunhar_notificacao` (Pilar 4.3: notificação e proposta de alocação - Issue #12)
   Caso seja necessário criar ferramentas auxiliares adicionais, basta seguir `snake_case`.

2. Assinatura e Acesso a Dados:
   - Receba `ctx: RunContext[AgentDeps]` como primeiro parâmetro quando precisar de contexto.
   - Utilize exclusivamente `ctx.deps.conn` (conexão somente leitura `sabor_ro`) e
     `ctx.deps.data_referencia` para regras temporais.
"""
