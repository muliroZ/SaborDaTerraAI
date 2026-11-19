"""Ponto de entrada das ferramentas (tools) read-only do agente SaborDaTerraAI."""

from sabor.tools.documentos import buscar_documentos
from sabor.tools.pedidos import extrair_e_classificar_pedido
from sabor.tools.consultar_dados import (
    buscar_produtores,
    consultar_financeiro,
    consultar_preco,
    consultar_safra,
    listar_produtos_catalogo,
    rastrear_lote,
)
from sabor.tools.relatorios import gerar_relatorio
from sabor.tools.resumir_produtor import resumir_produtor
from sabor.tools.rascunhar_notificacao import rascunhar_notificacao

__all__ = [
    'buscar_documentos',
    'buscar_produtores',
    'consultar_financeiro',
    'consultar_preco',
    'consultar_safra',
    'listar_produtos_catalogo',
    'extrair_e_classificar_pedido',
    'gerar_relatorio',
    'rastrear_lote',
    'resumir_produtor',
    'rascunhar_notificacao'
]
