"""Ferramenta `extrair_e_classificar_pedido` (#9) com modelo simulado e o banco populado.

O `FunctionModel` devolve a extração que se espera do LLM para cada mensagem; o que se testa é
o resto do caminho: prompt mascarado com o catálogo, schema sem os campos calculados, datas,
catálogo e regras (#10) sobre as mensagens reais de PED-001, 003, 005 e 011.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage

from sabor.agent.deps import AgentDeps
from sabor.agent.mascarador import Mascarador
from sabor.tools.pedidos import (
    Item,
    MensagemPedido,
    Pedido,
    classificar_extracao,
    extrair_e_classificar_pedido,
)

EXTRACOES: dict[str, dict[str, Any]] = {
    'PED-2026-001': {
        'tipo': 'compra',
        'comprador': {'nome': 'Restaurante Raízes', 'contato': 'Carlos'},
        'entrega': {'data_texto': 'essa sexta (01/nov)'},
        'itens': [
            {
                'produto_texto': 'queijo coalho',
                'produto': 'queijo coalho',
                'quantidade': 30,
                'unidade': 'kg',
            },
            {
                'produto_texto': 'manteiga de garrafa',
                'produto': 'manteiga de garrafa',
                'quantidade': 10,
                'unidade': 'kg',
            },
            {'produto_texto': 'mel (aquele silvestre mesmo)', 'quantidade': 5, 'unidade': 'kg'},
            {
                'produto_texto': 'coentro',
                'produto': 'coentro',
                'quantidade': 20,
                'unidade': 'maços',
            },
            {'produto_texto': 'cebolinha', 'quantidade': 10, 'unidade': 'maço'},
            {
                'produto_texto': 'ricota fresca',
                'produto': 'ricota',
                'quantidade': 5,
                'unidade': 'kg',
                'opcional': True,
            },
        ],
    },
    'PED-2026-003': {
        'tipo': 'compra',
        'entrega': {'data_texto': 'urgente pra amanha se possivel'},
        'pagamento': 'PIX',
        'itens': [
            {
                'produto_texto': 'aquele queijo q vcs mandaram da ultima vez, o mais artesanal',
                'quantidade': 15,
                'unidade': 'kg',
                'ambiguo': True,
                'observacao': 'Referência a pedido anterior.',
            },
            {
                'produto_texto': 'mel de jandaíra',
                'produto': 'mel de abelha jandaíra',
                'quantidade': 3,
                'unidade': 'kg',
            },
        ],
    },
    'PED-2026-005': {
        'tipo': 'compra',
        'comprador': {'nome': 'Dona Cremilda - Lanchonete da Feira'},
        'entrega': {'data_texto': 'pra sabado'},
        'itens': [
            {
                'produto_texto': 'queijo coalho',
                'produto': 'queijo coalho',
                'quantidade': 10,
                'unidade': 'kg',
            },
            # O modelo deixou passar a ambiguidade: o catálogo tem que pegá-la.
            {'produto_texto': 'manteiga', 'quantidade': 5, 'unidade': 'kg'},
        ],
    },
    'PED-2026-011': {
        'tipo': 'cotacao',
        'comprador': {'nome': 'Tropical Fruits Exportadora', 'contato': 'Heinrich Mueller'},
        'itens': [
            {
                'produto_texto': 'Mango',
                'produto': 'manga',
                'quantidade': 500,
                'unidade': 'kg',
                'organico': True,
                'observacao': 'por mês',
            },
            {
                'produto_texto': 'Guava',
                'quantidade': 300,
                'unidade': 'kg',
                'organico': True,
                'observacao': 'por mês',
            },
            {
                'produto_texto': 'Passion fruit',
                'quantidade': 200,
                'unidade': 'kg',
                'organico': True,
                'observacao': 'por mês',
            },
        ],
    },
}


@dataclass
class ModeloSimulado:
    """Responde com a extração dada e guarda o prompt e o schema que recebeu."""

    extracao: dict[str, Any]
    prompts: list[str] = field(default_factory=list)
    schema: dict[str, Any] | None = None

    def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        self.prompts.append(messages[0].parts[-1].content)
        self.schema = info.output_tools[0].parameters_json_schema
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, self.extracao)])


@pytest.fixture
def simular_llm(monkeypatch):
    def simular(extracao: dict[str, Any]) -> ModeloSimulado:
        modelo = ModeloSimulado(extracao)
        monkeypatch.setattr(
            'sabor.tools.pedidos._agente_extracao',
            lambda: Agent(FunctionModel(modelo), output_type=Pedido),
        )
        return modelo

    return simular


def _ctx(conn) -> SimpleNamespace:
    deps = AgentDeps(conn=conn, data_referencia=date(2026, 10, 30), mascarador=Mascarador())
    return SimpleNamespace(deps=deps, usage=RunUsage())


def _triar(conn, simular_llm, pedido_id: str) -> tuple[Pedido, ModeloSimulado]:
    modelo = simular_llm(EXTRACOES[pedido_id])
    pedido = asyncio.run(extrair_e_classificar_pedido(_ctx(conn), pedido_id=pedido_id))
    assert isinstance(pedido, Pedido)
    return pedido, modelo


def _por_texto(pedido: Pedido) -> dict[str, Item]:
    return {item.produto_texto: item for item in pedido.itens}


# ---------------------------------------------------------------------------
# Mensagens de referência da issue
# ---------------------------------------------------------------------------


def test_ped_001_data_inconsistente_e_itens_no_catalogo(conn, simular_llm):
    pedido, modelo = _triar(conn, simular_llm, 'PED-2026-001')

    assert pedido.pedido_id == 'PED-2026-001'
    assert pedido.entrega.data is None
    assert pedido.entrega.data_estimada == date(2026, 11, 1)
    assert '30/10/2026' in pedido.entrega.alerta
    assert {'data_inconsistente', 'ambiguidade'} <= set(pedido.sinalizacoes)
    assert pedido.urgencia == 'alta'

    itens = _por_texto(pedido)
    assert itens['mel (aquele silvestre mesmo)'].produto == 'mel silvestre'
    assert itens['cebolinha'].produto == 'cebolinha'  # resolvido pelo texto
    assert itens['coentro'].unidade == 'maço'  # "maços" normalizado
    assert itens['ricota fresca'].opcional
    assert not any(item.ambiguo for item in pedido.itens)
    assert all(item.produto_id is not None for item in pedido.itens)
    assert 'P003' in itens['ricota fresca'].produtores_elegiveis

    prompt = modelo.prompts[0]
    assert 'essa sexta (01/nov)' in prompt
    assert 'queijo coalho' in prompt.split('Mensagem:')[0]  # catálogo enviado ao modelo


def test_ped_003_item_do_historico_fica_ambiguo_e_amanha_e_29_10(conn, simular_llm):
    pedido, _ = _triar(conn, simular_llm, 'PED-2026-003')

    queijo, mel = pedido.itens
    assert queijo.ambiguo and queijo.produto is None and queijo.produtores_elegiveis == []
    assert mel.produto == 'mel de abelha jandaíra' and not mel.ambiguo
    assert pedido.entrega.data == date(2026, 10, 29)
    assert pedido.urgencia == 'critica'
    assert {'ambiguidade', 'privacidade'} <= set(pedido.sinalizacoes)


def test_ped_005_manteiga_ambigua_mesmo_se_o_modelo_nao_marcar(conn, simular_llm):
    pedido, _ = _triar(conn, simular_llm, 'PED-2026-005')

    manteiga = _por_texto(pedido)['manteiga']
    assert manteiga.ambiguo and manteiga.produto_id is None and manteiga.observacao
    assert pedido.entrega.data == date(2026, 10, 31)  # "pra sabado", recebido na quarta
    assert pedido.urgencia == 'alta'
    assert 'ambiguidade' in pedido.sinalizacoes


def test_ped_011_em_ingles_mapeia_para_o_catalogo(conn, simular_llm):
    pedido, _ = _triar(conn, simular_llm, 'PED-2026-011')

    assert [item.produto for item in pedido.itens] == ['manga', 'goiaba', 'maracujá']
    assert pedido.tipo == 'cotacao'
    assert pedido.entrega.data is None and pedido.entrega.data_estimada is None
    assert pedido.urgencia == 'baixa'
    assert {'conformidade', 'privacidade'} <= set(pedido.sinalizacoes)


# ---------------------------------------------------------------------------
# Contrato com o modelo
# ---------------------------------------------------------------------------


def test_schema_do_modelo_nao_tem_campos_calculados(conn, simular_llm):
    _, modelo = _triar(conn, simular_llm, 'PED-2026-005')

    schema = json.dumps(modelo.schema)
    for campo in (
        'urgencia',
        'sinalizacoes',
        'pedido_id',
        'produtores_elegiveis',
        'produto_id',
        'data_estimada',
        'data_fim',
        'alerta',
    ):
        assert f'"{campo}"' not in schema, campo
    assert '"data_texto"' in schema and '"ambiguo"' in schema


def test_campos_calculados_vindos_do_modelo_sao_ignorados(conn):
    extracao = Pedido.model_validate(
        {
            **EXTRACOES['PED-2026-001'],
            'urgencia': 'baixa',
            'sinalizacoes': ['inventada'],
            'entrega': {'data_texto': 'essa sexta (01/nov)', 'data': '2026-11-01'},
        }
    )
    mensagem = MensagemPedido(
        texto='Preciso pra essa sexta (01/nov)', data_referencia=date(2026, 10, 28)
    )

    pedido = classificar_extracao(_ctx(conn).deps, mensagem, extracao)

    assert pedido.urgencia == 'alta' and 'inventada' not in pedido.sinalizacoes
    assert pedido.entrega.data is None
    # A extração gravada (triagem em lote, #14) não muda: pode ser reclassificada depois.
    assert extracao.urgencia == 'baixa' and extracao.itens[0].produto_id is None


def test_texto_colado_vai_mascarado_e_conta_no_orcamento_do_turno(conn, simular_llm):
    modelo = simular_llm({'tipo': 'consulta'})
    ctx = _ctx(conn)

    pedido = asyncio.run(
        extrair_e_classificar_pedido(ctx, texto='Meu CPF é 123.456.789-09, tem mel amanhã?')
    )

    assert isinstance(pedido, Pedido) and pedido.pedido_id is None
    assert '[CPF_1]' in modelo.prompts[0] and '123.456.789-09' not in modelo.prompts[0]
    assert ctx.usage.requests == 1  # a chamada interna soma no uso do turno (#32)


@pytest.mark.parametrize(
    'kwargs,trecho',
    [
        ({'pedido_id': 'PED-2026-999'}, 'não encontrado'),
        ({}, 'exatamente um'),
        ({'pedido_id': 'PED-2026-001', 'texto': 'oi'}, 'exatamente um'),
    ],
)
def test_entrada_invalida_vira_mensagem_para_o_modelo(conn, simular_llm, kwargs, trecho):
    modelo = simular_llm({'tipo': 'compra'})

    resposta = asyncio.run(extrair_e_classificar_pedido(_ctx(conn), **kwargs))

    assert isinstance(resposta, str) and trecho in resposta
    assert modelo.prompts == []  # nenhuma chamada ao LLM


def test_id_sem_diferenciar_caixa(conn, simular_llm):
    simular_llm(EXTRACOES['PED-2026-005'])

    pedido = asyncio.run(extrair_e_classificar_pedido(_ctx(conn), pedido_id=' ped-2026-005 '))

    assert pedido.pedido_id == 'PED-2026-005'


@pytest.mark.parametrize(
    'bruta,esperada',
    [('Kg', 'kg'), ('maços', 'maço'), ('dz', 'dúzia'), ('dúzias', 'dúzia'), ('unid.', 'unidade')],
)
def test_normaliza_unidade(bruta, esperada):
    assert Item(produto_texto='x', unidade=bruta).unidade == esperada


def test_item_que_o_modelo_marcou_ambiguo_resolve_por_nome_ou_sinonimo_exato():
    """F2 do Bloco 4: "banana prata" voltava ambígua do LLM e sumia da alocação do PED-002,
    embora seja sinônimo cadastrado de banana."""
    from sabor.tools.catalogo import Catalogo
    from sabor.tools.pedidos import Item, normalizar_pedido

    catalogo = Catalogo(
        [(4, 'banana'), (2, 'queijo manteiga'), (3, 'manteiga de garrafa'), (10, 'mel silvestre'),
         (7, 'frango caipira')],
        [('banana prata', 4), ('mel puro', 10)],
    )
    pedido = Pedido(
        tipo='compra',
        itens=[
            Item(produto_texto='banana prata', quantidade=40, unidade='kg', ambiguo=True,
                 observacao='banana prata não especificada no catálogo'),
            Item(produto_texto='Mel puro', ambiguo=True, observacao='Fornecimento semanal.'),
            Item(produto_texto='manteiga', ambiguo=True, observacao='o de sempre'),
            Item(produto_texto='frango', ambiguo=True),  # só por palavras: continua ambíguo
        ],
    )

    banana, mel, manteiga, frango = normalizar_pedido(pedido, catalogo, date(2026, 10, 27)).itens

    assert (banana.produto_id, banana.produto, banana.ambiguo) == (4, 'banana', False)
    assert banana.observacao.startswith('Identificado no catálogo como banana.')
    assert (mel.produto, mel.ambiguo) == ('mel silvestre', False)
    assert 'Fornecimento semanal.' in mel.observacao  # a nota da extração não se perde
    assert manteiga.ambiguo and manteiga.produto_id is None and manteiga.observacao == 'o de sempre'
    assert frango.ambiguo and frango.produto_id is None
