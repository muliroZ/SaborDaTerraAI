"""Pacote do agente único SaborDaTerraAI (schemas, dependências, fallback e orquestração)."""

from sabor.agent.deps import DATA_REF_PADRAO, AgentDeps, MascaradorNoOp, obter_data_referencia
from sabor.agent.fallback import (
    MODELO_FALLBACK_1,
    MODELO_FALLBACK_2,
    MODELO_PRIMARIO,
    TIMEOUT_POR_MODELO_S,
    configuracao_padrao_modelo,
    construir_capability_fallback,
    construir_modelos_padrao,
    deve_fazer_fallback_excecao,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
    resposta_invalida_para_fallback,
)
from sabor.agent.schemas import (
    Flag,
    Fonte,
    Rascunho,
    RespostaAgente,
    RespostaTurno,
    TentativaFallback,
)

__all__ = [
    'DATA_REF_PADRAO',
    'MODELO_FALLBACK_1',
    'MODELO_FALLBACK_2',
    'MODELO_PRIMARIO',
    'TIMEOUT_POR_MODELO_S',
    'AgentDeps',
    'Flag',
    'Fonte',
    'MascaradorNoOp',
    'Rascunho',
    'RespostaAgente',
    'RespostaTurno',
    'TentativaFallback',
    'configuracao_padrao_modelo',
    'construir_capability_fallback',
    'construir_modelos_padrao',
    'deve_fazer_fallback_excecao',
    'extrair_metadados_resposta',
    'extrair_tentativas_de_excecao',
    'obter_data_referencia',
    'resposta_invalida_para_fallback',
]
