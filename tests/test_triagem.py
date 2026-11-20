"""Triagem em lote da caixa de entrada (#14) com banco temporário e modelo simulado (sem LLM)."""

import asyncio
import csv
from datetime import date, datetime
import io
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
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
        'entrega': {'data_texto': 'terça 03/11'},
        'itens': [{'produto_texto': 'mel', 'quantidade': 10, 'unidade': 'kg'}],
    },
}


class ModeloFalso:
    """Devolve a extração conforme a palavra-chave da mensagem e conta as chamadas."""

    def __init__(self):
        self.chamadas: list[str] = []
        self.responde_texto: set[str] = set()  # chaves que respondem fora do formato

    def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        prompt = messages[0].parts[-1].content
        chave = next(k for k in EXTRACOES if k in prompt)
        self.chamadas.append(chave)
        if chave in self.responde_texto:
            return ModelResponse(parts=[TextPart('não consegui extrair')])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, EXTRACOES[chave])])


@pytest.fixture
def conn(conn_temporaria):
    conn_temporaria.execute('TRUNCATE pedidos, financeiro, produtores, produtos CASCADE')
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


def test_regras_rodam_de_novo_sem_tokens_quando_o_banco_muda(conn, modelo):
    _triar(conn)
    assert 'inadimplencia' not in listar_fila(conn)[1]['sinalizacoes']

    # Dívida vencida registrada depois da triagem (cenário da revisão do PR).
    conn.execute(
        'INSERT INTO produtores (id, nome_completo, municipio, status) '
        "VALUES ('P001', 'Produtor Teste', 'Garanhuns', 'ativo')"
    )
    conn.execute(
        """
        INSERT INTO financeiro (id_transacao, data, produtor_id, comprador, produto, quantidade,
                                unidade, valor_unitario, valor_total, status_pagamento,
                                data_vencimento)
        VALUES ('TX-T-1', '2026-09-01', 'P001', 'Mercado Bom Preço', 'Mel', 10, 'kg', 50, 500,
                'pendente', '2026-09-30')
        """
    )
    segundo = _triar(conn)

    assert len(segundo.reaproveitados) == 3 and not segundo.processados
    assert len(modelo.chamadas) == 3 and segundo.uso.total_tokens == 0  # sem LLM
    compra = next(f for f in listar_fila(conn) if f['pedido_id'] == 'PED-T-003')
    assert {'inadimplencia', 'credito'} <= set(compra['sinalizacoes'])


def test_mensagem_alterada_aparece_desatualizada_ate_ser_triada(conn, modelo):
    _triar(conn)
    conn.execute(
        "UPDATE pedidos SET mensagem_original = 'NOTIFICACAO urgente' WHERE id = 'PED-T-001'"
    )

    # Sem rodar o lote (ou sem orçamento para retriar), a classificação antiga some da fila.
    for _ in range(2):
        alterado = next(f for f in listar_fila(conn) if f['pedido_id'] == 'PED-T-001')
        assert alterado['situacao'] == 'desatualizado'
        assert alterado['urgencia'] is None and alterado['sinalizacoes'] == []
        assert listar_fila(conn)[-1]['pedido_id'] == 'PED-T-001'
        resultado = _triar(conn, limite=0)
        assert resultado.nao_processados == ['PED-T-001']

    _triar(conn)
    retriado = next(f for f in listar_fila(conn) if f['pedido_id'] == 'PED-T-001')
    assert (retriado['situacao'], retriado['urgencia']) == ('ok', 'critica')


def test_limite_de_chamadas_do_pedido_nao_interrompe_o_lote(conn, modelo):
    # Teto do lote abaixo do teto do pedido: antes, qualquer UsageLimitExceeded parava tudo.
    modelo.responde_texto.add('COTACAO')
    orcamento = Orcamento(tokens_lote=10_000, tokens_pedido=12_000, requisicoes_pedido=1)
    resultado = _triar(conn, orcamento=orcamento)

    assert not resultado.orcamento_esgotado and not resultado.nao_processados
    assert 'chamadas' in resultado.erros['PED-T-001']
    assert sorted(resultado.processados) == ['PED-T-002', 'PED-T-003']


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
    original = triagem.extrair_pedido
    falhas = {'PED-T-001': 1}
    esperas = []

    async def instavel(deps, mensagem, **kwargs):
        if falhas.get(mensagem.pedido_id):
            falhas[mensagem.pedido_id] -= 1
            raise ModelRateLimitError('modelo', 'limite por minuto', retry_after=53)
        return await original(deps, mensagem, **kwargs)

    async def dormir(segundos):
        esperas.append(segundos)

    monkeypatch.setattr(triagem, 'extrair_pedido', instavel)
    resultado = _triar(conn, esperas_s=(5,), dormir=dormir)

    assert len(resultado.processados) == 3 and not resultado.erros
    assert esperas == [53]  # respeita o tempo pedido pelo provedor


def test_falha_definitiva_nao_derruba_o_lote(conn, modelo, monkeypatch):
    original = triagem.extrair_pedido

    async def quebra_um(deps, mensagem, **kwargs):
        if mensagem.pedido_id == 'PED-T-002':
            raise ModelRateLimitError('modelo', 'limite por minuto')
        return await original(deps, mensagem, **kwargs)

    async def dormir(_segundos):
        pass

    monkeypatch.setattr(triagem, 'extrair_pedido', quebra_um)
    resultado = _triar(conn, esperas_s=(1, 1), dormir=dormir)

    assert sorted(resultado.processados) == ['PED-T-001', 'PED-T-003']
    assert 'ModelRateLimitError' in resultado.erros['PED-T-002']
    assert [f['pedido_id'] for f in listar_fila(conn)][-1] == 'PED-T-002'  # erro no fim da fila


def _itens(conn, pedido_id):
    return conn.execute(
        'SELECT ordem, produto_texto, produto_id, quantidade, unidade, opcional, ambiguo '
        'FROM pedido_itens WHERE pedido_id = %s ORDER BY ordem',
        (pedido_id,),
    ).fetchall()


def test_itens_da_triagem_vao_para_pedido_itens_e_saem_com_erro(conn, modelo, monkeypatch):
    _triar(conn)
    # Catálogo vazio: o item fica ambíguo e sem produto_id, mas é gravado.
    assert _itens(conn, 'PED-T-003') == [(1, 'mel', None, 10, 'kg', False, True)]
    assert _itens(conn, 'PED-T-002') == []  # notificação sem itens

    _triar(conn)  # reclassificação sem LLM substitui os itens, sem duplicar
    assert len(_itens(conn, 'PED-T-003')) == 1

    async def quebra(deps, mensagem, **kwargs):
        raise ValueError('extração inválida')

    monkeypatch.setattr(triagem, 'extrair_pedido', quebra)
    resultado = _triar(conn, reprocessar=True)

    assert 'PED-T-003' in resultado.erros
    assert _itens(conn, 'PED-T-003') == []  # sem triagem válida, sem itens


def test_resumo_do_produtor_mostra_os_itens_triados(conn, modelo, monkeypatch):
    """F1 do Bloco 4: o resumo do produtor lê os pedidos em aberto que a triagem gravou."""
    from pydantic_ai import RunContext
    from pydantic_ai.models.test import TestModel
    from pydantic_ai.usage import RunUsage

    from sabor.tools.resumir_produtor import resumir_produtor

    queijo, ricota = (
        conn.execute(
            'INSERT INTO produtos (nome, unidade, origem_animal) VALUES (%s, %s, true) '
            'RETURNING id',
            (nome, 'kg'),
        ).fetchone()[0]
        for nome in ('queijo coalho', 'ricota')
    )
    conn.cursor().executemany(
        'INSERT INTO produtores (id, nome_completo, municipio, status, registro_sie) '
        'VALUES (%s, %s, %s, %s, %s)',
        [
            ('P003', 'Cícero Teste', 'Caetés', 'ativo', 'PE-SIE-0756'),
            ('P011', 'Sem SIE Teste', 'Garanhuns', 'ativo', None),
        ],
    )
    conn.cursor().executemany(
        'INSERT INTO produtor_produtos (produtor_id, nome_original, produto_id) VALUES (%s, %s, %s)',
        [('P003', 'queijo coalho', queijo), ('P003', 'ricota', ricota), ('P011', 'queijo', queijo)],
    )
    monkeypatch.setitem(EXTRACOES, 'COMPRA', {
        'tipo': 'compra',
        'comprador': {'nome': 'Restaurante Raízes'},
        'entrega': {'data_texto': 'terça 03/11'},
        'itens': [
            {'produto_texto': 'queijo coalho', 'quantidade': 30, 'unidade': 'kg'},
            {'produto_texto': 'ricota', 'quantidade': 5, 'unidade': 'kg', 'opcional': True},
            {'produto_texto': 'aquele queijo da última vez', 'quantidade': 15,
             'unidade': 'kg', 'ambiguo': True},
        ],
    })
    # Restaurante Raízes, comprador do pedido, já deve ao P003 (F5 do Bloco 4).
    conn.execute(
        """
        INSERT INTO financeiro (id_transacao, data, produtor_id, comprador, produto, quantidade,
                                unidade, valor_unitario, valor_total, status_pagamento,
                                data_vencimento)
        VALUES ('TX-T-9', '2026-09-01', 'P003', 'Restaurante Raízes', 'queijo coalho', 10, 'kg',
                30, 300, 'pendente', '2026-10-20')
        """
    )
    _triar(conn)

    deps = _deps(conn)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    cicero = resumir_produtor(ctx, 'P003')

    assert [(p.pedido_id, p.produto, p.quantidade) for p in cicero.pedidos_em_aberto] == [
        ('PED-T-003', 'queijo coalho', 30.0),
        ('PED-T-003', 'ricota', 5.0),
    ]
    assert cicero.pedidos_em_aberto[0].data_entrega == date(2026, 11, 3)
    assert 'PED-T-003: queijo coalho — 30 kg (entrega em 03/11)' in cicero.rascunho.conteudo
    assert deps.rascunhos == [cicero.rascunho]  # registrado para o runner anexar (F3)
    assert {p.comprador_deve_vencido for p in cicero.pedidos_em_aberto} == {300.0}
    assert (
        'PED-T-003 é de Restaurante Raízes, que tem R$ 300,00 vencidos com o produtor; '
        'a alocação depende de validação da gestão.'
    ) in cicero.alertas
    [flag] = [f for f in cicero.flags if f.tipo == 'inadimplencia']
    assert 'Pedidos em aberto desse comprador (PED-T-003)' in flag.descricao
    # Origem animal sem SIE: o pedido não é oferecido ao produtor.
    assert resumir_produtor(ctx, 'P011').pedidos_em_aberto == []


def test_exporta_csv_e_json_sem_remetente(conn, modelo):
    _triar(conn)
    fila = listar_fila(conn)

    texto_csv = para_csv(fila)
    linhas = list(csv.DictReader(io.StringIO(texto_csv), delimiter=';'))
    assert [linha['pedido_id'] for linha in linhas] == ['PED-T-002', 'PED-T-003', 'PED-T-001']
    assert linhas[0]['urgencia'] == 'critica' and linhas[1]['data_entrega'] == '2026-11-03'
    assert 'remetente' not in linhas[0]
    assert 'compras@x.com' not in texto_csv and '99999' not in texto_csv

    conn.execute("UPDATE triagem_pedidos SET comprador = '=1+1' WHERE pedido_id = 'PED-T-003'")
    conn.execute(
        "UPDATE triagem_pedidos SET comprador = '@SUM(A1)', tipo = '-2+3' "
        "WHERE pedido_id = 'PED-T-002'"
    )
    linhas = {
        linha['pedido_id']: linha
        for linha in csv.DictReader(io.StringIO(para_csv(listar_fila(conn))), delimiter=';')
    }
    assert linhas['PED-T-003']['comprador'] == "'=1+1"  # CSV injection neutralizada
    assert linhas['PED-T-002']['comprador'] == "'@SUM(A1)"
    assert linhas['PED-T-002']['tipo'] == "'-2+3"
    assert linhas['PED-T-003']['data_entrega'] == '2026-11-03'  # valores comuns intactos

    dados = json.loads(para_json(fila))
    assert [d['posicao'] for d in dados] == [1, 2, 3]
    assert dados[1]['extracao']['itens'][0]['produto_texto'] == 'mel'
    assert dados[1]['recebido_em'] == '2026-10-28T11:00:00'
