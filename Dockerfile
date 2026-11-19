# Imagem da aplicação (Streamlit + agente). A mesma imagem roda localmente pelo
# docker-compose e no Cloud Run; os scripts de carga (init_db, etl, ingest_docs) também
# vão na imagem para rodar como `docker compose run` ou Cloud Run Job (#17).

FROM ghcr.io/astral-sh/uv:0.12.3 AS uv

# ---------------------------------------------------------------------------
# Build: instala as dependências travadas no uv.lock em /app/.venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependências primeiro: a camada só é refeita quando o uv.lock muda.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------
# Runtime: só o Python, o venv e os arquivos que os scripts leem
# ---------------------------------------------------------------------------
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 sabor
WORKDIR /app

COPY --from=build --chown=sabor:sabor /app/.venv /app/.venv
COPY --chown=sabor:sabor scripts ./scripts
COPY --chown=sabor:sabor data ./data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=America/Recife \
    PORT=8501 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PYDANTIC_AI_NO_BANNER=1

USER sabor
EXPOSE 8501

# O Cloud Run injeta PORT (8080); localmente fica 8501.
CMD ["sh", "-c", "exec sabor --server.address=0.0.0.0 --server.port=${PORT}"]
