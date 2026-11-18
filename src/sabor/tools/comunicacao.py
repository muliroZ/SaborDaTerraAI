"""Módulo reservado para comunicação com produtores (Issues #11 e #12 — Pilar 4.3).

Funções públicas esperadas (em `snake_case`, recebendo `ctx: RunContext[AgentDeps]`):
- `resumir_produtor(ctx: RunContext[AgentDeps], ...)` (Issue #11)
- `rascunhar_notificacao(ctx: RunContext[AgentDeps], ...)` (Issue #12)

Após implementar aqui, reexporte as funções em `src/sabor/tools/__init__.py`.
"""
