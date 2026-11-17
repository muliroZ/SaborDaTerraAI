"""Contexto de dependências (`AgentDeps`) e configuração de data de referência."""

from dataclasses import Field, dataclass, field
from datetime import date
import os
from typing import Any

from dotenv import load_dotenv
import psycopg
from pydantic import ConfigDict, BaseModel, Field

DATA_REF_PADRAO = date(2026, 10, 30)
"""Retrato padrão dos dados operacionais e documentais (30/10/2026)."""


def obter_data_referencia() -> date:
    """Retorna a data de referência configurada em `DATA_REF_DATE` ou `2026-10-30`."""
    load_dotenv()
    if valor := os.getenv('DATA_REF_DATE'):
        return date.fromisoformat(valor.strip())
    return DATA_REF_PADRAO


class MascaradorNoOp:
    """Implementação padrão (identidade) até a conexão com o mascarador de PII (#33)."""

    def mascarar(self, texto: str) -> str:
        return texto

    def restaurar(self, texto: str) -> str:
        return texto


@dataclass
class AgentDeps:
    """Contexto de execução injetado nas ferramentas e no system prompt do agente."""

    conn: psycopg.Connection
    data_referencia: date = field(default_factory=obter_data_referencia)
    dataset_version: str = 'v1'
    usuario: str = 'Dona Lúcia'
    mascarador: Any = field(default_factory=MascaradorNoOp)