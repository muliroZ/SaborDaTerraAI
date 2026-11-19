"""Contratos Pydantic de saída estruturada do LLM, tentativas de fallback e turno."""

from collections.abc import Sequence

from typing import Literal, Optional
from uuid import uuid4
from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema
from pydantic_ai.messages import ModelRequestAttempt


UserProfile = Literal["funcionario", "produtor"]

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


class Relatorio(BaseModel):
    """Relatório Markdown gerado sob demanda no chat (#15), oferecido para download na UI.

    Montado de forma determinística pela tool `gerar_relatorio` a partir das consultas
    (`consultar_dados` e `buscar_documentos`); o LLM recebe só um resumo e não reescreve o
    arquivo, que é anexado ao turno pelo runner.
    """

    tipo: str
    titulo: str
    nome_arquivo: str
    markdown: str
    fontes: list[Fonte] = Field(default_factory=list)


class RespostaAgente(BaseModel):
    """Saída estruturada produzida pelo agente PydanticAI (etapa 3.4)."""

    texto: str = Field(description='Resposta em português brasileiro para o operador')
    fontes: list[Fonte] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    rascunhos: list[Rascunho] = Field(default_factory=list)
    relatorios: SkipJsonSchema[list[Relatorio]] = Field(default_factory=list)
    """Preenchido pelo runner com os relatórios gerados pelas tools; fora do schema do LLM."""


class TentativaFallback(BaseModel):
    """Registro de uma tentativa de modelo que falhou ou teve resposta rejeitada."""

    modelo: str
    provedor: str | None = None
    outcome: str
    erro: str | None = None
    duracao_ms: int = 0

    @classmethod
    def de_attempt(cls, attempt: ModelRequestAttempt) -> 'TentativaFallback':
        return cls(
            modelo=attempt.model_name,
            provedor=attempt.provider_name,
            outcome=attempt.outcome,
            erro=attempt.error,
            duracao_ms=int(attempt.duration.total_seconds() * 1000),
        )

EarlyReturnList = Literal[
    "ENTRADA_VAZIA",
    "TAMANHO_EXCEDIDO",
    "FORA_DE_ESCOPO",
    "PROMPT_INJECTION",
    "CACHE_HIT",
    "COTA_SESSAO_ESGOTADA",
    "ORCAMENTO_TURNO_ESGOTADO",
]

class RetornoAntecipado(BaseModel):
    """Objeto padronizado para retornos antecipados da Etapa 2 (UI e auditoria)."""
    motivo: EarlyReturnList
    mensagem: str
    fontes: list[Fonte] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    """Preenchido só no `CACHE_HIT`: as flags da resposta original guardada no cache (#36)."""

class RespostaTurno(BaseModel):
    """Resultado completo de `processar_turno` para a interface (#13) e auditoria (#35)."""

    sessao_id: str
    usuario: str
    texto: str
    fontes: list[Fonte] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)
    rascunhos: list[Rascunho] = Field(default_factory=list)
    relatorios: list[Relatorio] = Field(default_factory=list)
    bloqueado_guardrail: bool = False
    retorno_antecipado: RetornoAntecipado | None = None
    cache_hit: bool = False
    """Resposta servida pelo cache de respostas estáticas, sem chamar o LLM (2b, #36)."""
    falha_segura: bool = False
    modelo_efetivo: str | None = None
    provedor_efetivo: str | None = None
    tentativas_fallback: list[TentativaFallback] = Field(default_factory=list)
    latencia_ms: int = 0
    regras_disparadas: list[str] = Field(default_factory=list)
    ferramentas: list[str] = Field(default_factory=list)
    tokens_entrada: int | None = None
    tokens_saida: int | None = None
    cache_hit: bool = False
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    
    @classmethod
    def de_resposta_agente(
        cls,
        sessao_id: str,
        usuario: str,
        resposta: RespostaAgente,
        *,
        modelo_efetivo: str | None = None,
        provedor_efetivo: str | None = None,
        tentativas_fallback: Sequence[TentativaFallback] = (),
        latencia_ms: int = 0,
    ) -> 'RespostaTurno':
        return cls(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=resposta.texto,
            fontes=list(resposta.fontes),
            flags=list(resposta.flags),
            rascunhos=list(resposta.rascunhos),
            relatorios=list(resposta.relatorios),
            modelo_efetivo=modelo_efetivo,
            provedor_efetivo=provedor_efetivo,
            tentativas_fallback=list(tentativas_fallback),
            latencia_ms=latencia_ms,
        )
