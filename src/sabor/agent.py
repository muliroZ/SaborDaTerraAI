"""Agente único PydanticAI e orquestração de turno do SaborDaTerraAI."""

from dataclasses import dataclass, field
from datetime import date
import os
from typing import Any

from dotenv import load_dotenv
import psycopg
from pydantic import BaseModel, Field

DATA_REF_PADRAO = date(2026, 10, 30)
"""Retrato padrão dos dados operacionais e documentais (30/10/2026)."""


def obter_data_referencia() -> date:
    """Retorna a data de referência configurada em `DATA_REF_DATE` ou `2026-10-30`."""
    load_dotenv()
    if valor := os.getenv('DATA_REF_DATE'):
        return date.fromisoformat(valor.strip())
    return DATA_REF_PADRAO


# ---------------------------------------------------------------------------
# 3.1 Modelos de saída estruturada do LLM (Etapa 3.4: output_type)
# ---------------------------------------------------------------------------


class Fonte(BaseModel):
    """Citação de fonte documental ou operacional no formato `[arquivo · referencia]`."""

    arquivo: str = Field(description='Caminho do arquivo de origem (ex.: lotes.csv)')
    referencia: str = Field(
        description='Identificador do registro ou seção (ex.: LOT-2026-0089, VALIDADE)'
    )

    def formatar(self) -> str:
        return f'[{self.arquivo} · {self.referencia}]'


class Flag(BaseModel):
    """Sinalização de negócio, conformidade, crédito, ambiguidade ou privacidade."""

    tipo: str = Field(
        description=(
            'Categoria do alerta (ex.: fora_de_safra, acima_capacidade, '
            'conformidade, inadimplencia, ambiguidade, privacidade)'
        )
    )
    descricao: str = Field(description='Explicação objetiva do alerta para o operador')


class Rascunho(BaseModel):
    """Rascunho de mensagem ou notificação para revisão pela equipe da cooperativa."""

    destinatario: str = Field(description='Produtor ou comprador destinatário')
    canal: str = Field(description='Canal sugerido (ex.: whatsapp, email)')
    conteudo: str = Field(description='Texto do rascunho sem dados sensíveis de terceiros')


class RespostaAgente(BaseModel):
    """Saída estruturada produzida pelo agente PydanticAI (etapa 3.4)."""

    texto: str = Field(description='Resposta em português brasileiro para o operador')
    fontes: list[Fonte] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    rascunhos: list[Rascunho] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 3.2 Dependências injetadas no Agente (deps_type)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 3.3 Retorno consolidado do turno (processar_turno)
# ---------------------------------------------------------------------------


class RespostaTurno(BaseModel):
    """Resultado completo de `processar_turno` para a interface (#13) e auditoria (#35)."""

    sessao_id: str
    usuario: str
    texto: str
    fontes: list[Fonte] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    rascunhos: list[Rascunho] = Field(default_factory=list)
    bloqueado_guardrail: bool = False
    falha_segura: bool = False
    modelo_efetivo: str | None = None
    provedor_efetivo: str | None = None
    latencia_ms: int = 0

    @classmethod
    def de_resposta_agente(
        cls,
        sessao_id: str,
        usuario: str,
        resposta: RespostaAgente,
        *,
        modelo_efetivo: str | None = None,
        provedor_efetivo: str | None = None,
        latencia_ms: int = 0,
    ) -> 'RespostaTurno':
        return cls(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=resposta.texto,
            fontes=list(resposta.fontes),
            flags=list(resposta.flags),
            rascunhos=list(resposta.rascunhos),
            modelo_efetivo=modelo_efetivo,
            provedor_efetivo=provedor_efetivo,
            latencia_ms=latencia_ms,
        )
