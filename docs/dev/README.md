# Documentação de Desenvolvimento (`docs/dev/`)

Esta pasta concentra a documentação técnica de desenvolvimento, arquitetura interna de código, contratos entre módulos e **pontos de integração pendentes por issue** para orientar os membros da equipe à medida que cada funcionalidade for implementada.

## Índice

- **[Núcleo do Agente Único, Orquestração de Turno e Fallback (#37)](agente_e_fallback.md)**
  - Estrutura modular do pacote `src/sabor/agent/` e conexão read-only (`sabor_ro`)
  - Fluxo completo do `processar_turno` (Etapas 1 a 5) e retornos antecipados
  - Cadeia `ModelFallback` (`Gemini 3.5 Flash-Lite` → `Qwen3 32B` → `Mistral Small 3.2`) e falha segura (20s)
  - **Mapa de Pendências e Integração por Issue (`#9`, `#11`, `#12`, `#13`, `#31`, `#33`, `#35`)**
  - Limitações conhecidas dos modelos de fallback para a apresentação de 10 minutos
