"""Triagem em lote da caixa de entrada (#14) com banco temporário e modelo simulado (sem LLM)."""

import asyncio
import csv
from datetime import date, datetime
import io
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sabor.agent.deps import AgentDeps
from sabor.agent.mascarador import Mascarador
from sabor import triagem
from sabor.tools.pedidos import Pedido
from sabor.triagem import Orcamento, listar_fila, para_csv, para_json, triar_lote

SEM_LIMITE = Orcamento(tokens_lote=None, tokens_pedido=None, requisicoes_pedido=None)

PEDIDOS = (
    # id, recebido_em, canal, remetente, mensagem
    ('PED-T-001', datetime(2026, 10, 28, 9), 'email', 'compras@x.com', 'COTACAO para jan/2027'),
    ('PED-T-002', datetime(2026, 10, 28, 10), 'whatsapp', '(87) 99999-0000', 'NOTIFICACAO ADAGRO'),
    ('PED-T-003', datetime(2026, 10, 28, 11), 'formulario_web', None, 'COMPRA de mel'),
)

EXTRACOES = {
    'COTACAO': {'tipo': 'cotacao', 'entrega': {'data_texto': 'janeiro de 2027'}},
    'NOTIFICACAO': {'tipo': 'notificacao_oficial', 'comprador': {'nome': 'ADAGRO'}},
    'COMPRA': {
        'tipo': 'compra',
        'comprador': {'nome': 'Mercado Bom Preço'},
        'entrega': {'data': '2026-11-03'},
        'itens': [{'produto_texto': 'mel', 'quantidade': 10, 'unidade': 'kg'}],
    },
}


class ModeloFalso:
    """Devolve a extração conforme a palavra-chave da mensagem e conta as chamadas."""

    def __init__(self):
        self.chamadas: list[str] = []

    def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        prompt = messages[-1].parts[-1].content
        chave = next(k for k in EXTRACOES if k in prompt)
        self.chamadas.append(chave)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, EXTRACOES[chave])])


@pytest.fixture
def conn(conn_temporaria):
    conn_temporaria.execute('TRUNCATE pedidos CASCADE')
    conn_temporaria.cursor().executemany(
        'INSERT INTO pedidos (id, data_recebimento, canal, remetente, mensagem_original) '
        'VALUES (%s, %s, %s, %s, %s)',
        PEDIDOS,
    )
    return conn_temporaria


@pytest.fixture
def modelo(monkeypatch):
    falso = ModeloFalso()
    monkeypatch.setattr(
        'sabor.tools.pedidos._agente_extracao',
        lambda: Agent(FunctionModel(falso), output_type=Pedido),
    )
    return falso


def _deps(conn):
    return AgentDeps(conn=conn, data_referencia=date(2026, 10, 30), mascarador=Mascarador())


def _triar(conn, **kwargs):
    kwargs.setdefault('orcamento', SEM_LIMITE)
    return asyncio.run(triar_lote(_deps(conn), **kwargs))


def test_tria_tudo_grava_e_ordena_por_urgencia(conn, modelo):
    resultado = _triar(conn)

    assert sorted(resultado.processados) == ['PED-T-001', 'PED-T-002', 'PED-T-003']
    assert not resultado.erros and not resultado.nao_processados
    assert resultado.uso.requests == 3 and resultado.uso.total_tokens > 0

    fila = listar_fila(conn)
    assert [(f['pedido_id'], f['urgencia']) for f in fila] == [
        ('PED-T-002', 'critica'),  # notificação oficial
        ('PED-T-003', 'alta'),  # entrega em 6 dias
        ('PED-T-001', 'baixa'),  # janeiro/2027
    ]
    # Catálogo vazio no banco temporário: as regras (#10) marcam o item como ambíguo.
    assert fila[1]['itens'] == ['10 kg mel (ambíguo)']
    assert fila[1]['comprador'] == 'Mercado Bom Preço'
    gravado = conn.execute(
        "SELECT status, tokens_entrada > 0, resultado->>'tipo' FROM triagem_pedidos "
        "WHERE pedido_id = 'PED-T-003'"
    ).fetchone()
    assert gravado == ('ok', True, 'compra')


def test_reaproveita_resultados_e_so_retria_o_que_mudou(conn, modelo):
    _triar(conn)
    assert len(modelo.chamadas) == 3

    segundo = _triar(conn)
    assert len(segundo.reaproveitados) == 3 and not segundo.processados
    assert len(modelo.chamadas) == 3  # nenhuma chamada nova ao modelo
    assert segundo.uso.total_tokens == 0

    conn.execute(
        "UPDATE pedidos SET mensagem_original = 'COMPRA de mel e queijo' WHERE id = 'PED-T-003'"
    )
    terceiro = _triar(conn)
    assert terceiro.processados == ['PED-T-003'] and len(terceiro.reaproveitados) == 2

    quarto = _triar(conn, reprocessar=True)
    assert len(quarto.processados) == 3 and len(modelo.chamadas) == 7


def test_orcamento_do_lote_esgotado_para_e_retoma_na_proxima_execucao(conn, modelo):
    curto = Orcamento(tokens_lote=1, tokens_pedido=10_000, requisicoes_pedido=4)
    resultado = _triar(conn, orcamento=curto)

    assert resultado.orcamento_esgotado
    assert list(resultado.erros) == ['PED-T-001']
    assert 'lote' in resultado.erros['PED-T-001']
    assert resultado.nao_processados == ['PED-T-002', 'PED-T-003']
    assert len(modelo.chamadas) == 1
    situacoes = {f['pedido_id']: f['situacao'] for f in listar_fila(conn)}
    assert situacoes == {'PED-T-001': 'erro', 'PED-T-002': 'pendente', 'PED-T-003': 'pendente'}

    retomada = _triar(conn)  # orçamento renovado: o pedido com erro volta ao modelo
    assert sorted(retomada.processados) == ['PED-T-001', 'PED-T-002', 'PED-T-003']
    assert {f['situacao'] for f in listar_fila(conn)} == {'ok'}


def test_orcamento_por_pedido_registra_erro_e_segue_o_lote(conn, modelo):
    apertado = Orcamento(tokens_lote=None, tokens_pedido=1, requisicoes_pedido=4)
    resultado = _triar(conn, orcamento=apertado)

    assert not resultado.orcamento_esgotado and not resultado.nao_processados
    assert len(resultado.erros) == 3
    assert all('pedido esgotado' in e for e in resultado.erros.values())


def test_limite_de_pedidos_deixa_o_resto_pendente(conn, modelo):
    resultado = _triar(conn, limite=2)

    assert resultado.processados == ['PED-T-001', 'PED-T-002']
    assert resultado.nao_processados == ['PED-T-003'] and not resultado.orcamento_esgotado


def test_erro_temporario_espera_e_tenta_de_novo(conn, modelo, monkeypatch):
    original = triagem.extrair_e_classificar
    falhas = {'PED-T-001': 1}
    esperas = []

    async def instavel(deps, *, pedido_id, **kwargs):
        if falhas.get(pedido_id):
            falhas[pedido_id] -= 1
            raise ModelRateLimitError('modelo', 'limite por minuto', retry_after=53)
        return await original(deps, pedido_id=pedido_id, **kwargs)

    async def dormir(segundos):
        esperas.append(segundos)

    monkeypatch.setattr(triagem, 'extrair_e_classificar', instavel)
    resultado = _triar(conn, esperas_s=(5,), dormir=dormir)

    assert len(resultado.processados) == 3 and not resultado.erros
    assert esperas == [53]  # respeita o tempo pedido pelo provedor


def test_falha_definitiva_nao_derruba_o_lote(conn, modelo, monkeypatch):
    original = triagem.extrair_e_classificar

    async def quebra_um(deps, *, pedido_id, **kwargs):
        if pedido_id == 'PED-T-002':
            raise ModelRateLimitError('modelo', 'limite por minuto')
        return await original(deps, pedido_id=pedido_id, **kwargs)

    async def dormir(_segundos):
        pass

    monkeypatch.setattr(triagem, 'extrair_e_classificar', quebra_um)
    resultado = _triar(conn, esperas_s=(1, 1), dormir=dormir)

    assert sorted(resultado.processados) == ['PED-T-001', 'PED-T-003']
    assert 'ModelRateLimitError' in resultado.erros['PED-T-002']
    assert [f['pedido_id'] for f in listar_fila(conn)][-1] == 'PED-T-002'  # erro no fim da fila


def test_exporta_csv_e_json_sem_remetente(conn, modelo):
    _triar(conn)
    fila = listar_fila(conn)

    texto_csv = para_csv(fila)
    linhas = list(csv.DictReader(io.StringIO(texto_csv), delimiter=';'))
    assert [linha['pedido_id'] for linha in linhas] == ['PED-T-002', 'PED-T-003', 'PED-T-001']
    assert linhas[0]['urgencia'] == 'critica' and linhas[1]['data_entrega'] == '2026-11-03'
    assert 'remetente' not in linhas[0]
    assert 'compras@x.com' not in texto_csv and '99999' not in texto_csv

    dados = json.loads(para_json(fila))
    assert [d['posicao'] for d in dados] == [1, 2, 3]
    assert dados[1]['extracao']['itens'][0]['produto_texto'] == 'mel'
    assert dados[1]['recebido_em'] == '2026-10-28T11:00:00'
