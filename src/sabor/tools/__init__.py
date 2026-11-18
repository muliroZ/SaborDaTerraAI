"""Ponto de entrada das ferramentas (tools) read-only do agente SaborDaTerraAI."""

from sabor.tools.consultar_dados import (
    buscar_produtores,
    consultar_financeiro,
    consultar_preco,
    consultar_safra,
    rastrear_lote,
)
from sabor.tools.documentos import buscar_documentos
from sabor.tools.resumir_produtor import resumir_produtor

__all__ = [
    'buscar_documentos',
    'buscar_produtores',
    'consultar_financeiro',
    'consultar_preco',
    'consultar_safra',
    'rastrear_lote',
    'resumir_produtor',
]