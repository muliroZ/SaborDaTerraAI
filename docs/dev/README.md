# Documentação de Desenvolvimento (`docs/dev/`)

Esta pasta concentra a documentação técnica de desenvolvimento, arquitetura interna de código, contratos entre módulos e **pontos de integração pendentes por issue** para orientar os membros da equipe à medida que cada funcionalidade for implementada.

## Índice

- **[Núcleo do Agente Único, Orquestração de Turno, Fallback (#37) e Mascaramento (#33)](agente_e_fallback.md)**
  - Estrutura modular do pacote `src/sabor/agent/` e conexão read-only (`sabor_ro`)
  - Fluxo completo do `processar_turno` (Etapas 1 a 5) e retornos antecipados
  - Cadeia `ModelFallback` (`Gemini 3.5 Flash-Lite` → `Qwen3 32B` → `Mistral Small 3.2`) e falha segura (20s)
  - **Mapa de Pendências e Integração por Issue (`#9`, `#11`, `#12`, `#13`, `#31`, `#35`)**
  - Limitações conhecidas dos modelos de fallback para a apresentação de 10 minutos

- **[Mascaramento de Dados Pessoais (PII) antes do LLM e Restauração (#33)](mascaramento_pii.md)**
  - Categorias de tokens (`[CPF_1]`, `[RG_1]`, `[TEL_1]`, `[EMAIL_1]`, `[PIX_1]`, `[BANCO_1]`, `[AGENCIA_1]`, `[CONTA_1]`)
  - Mascaramento da mensagem do usuário (`PED-2026-013`, `PED-2026-015`) e dos retornos das ferramentas (`#7`, `#8`)
  - Regra LGPD na Etapa 4c (restauração exclusiva em `texto` para o operador; proteção total em `rascunhos`)
