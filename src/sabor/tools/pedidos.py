"""Ferramenta `extrair_e_classificar_pedido` (#9, etapa 3.3 do diagrama *Agent System*).

Transforma uma mensagem livre (pedido salvo na caixa de entrada ou texto colado no chat) num
`Pedido` validado e já triado, em três etapas:

1. `carregar_mensagem`: texto e data de recebimento, que ancora as datas relativas;
2. `extrair_pedido`: o LLM preenche só o que exige leitura (tipo, itens como escritos,
   comprador, a expressão da data);
3. `classificar_extracao`: sem LLM, resolve a data (`sabor.tools.datas`), mapeia os produtos ao
   catálogo (`sabor.tools.catalogo`) e aplica as regras de #10 (`sabor.tools.regras`).

Os campos calculados nas etapas determinísticas ficam fora do schema enviado ao modelo
(`SkipJsonSchema`) e são sempre refeitos a partir da extração, então a mesma extração pode ser
reclassificada quando o banco mudar (triagem em lote, #14).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic.json_schema import SkipJsonSchema
from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import RunUsage, UsageLimits

from sabor.agent.deps import AgentDeps
from sabor.tools.catalogo import Catalogo
from sabor.tools.datas import normalizar, resolver_data
from sabor.tools.regras import classificar_pedido

TipoMensagem = Literal[
    'compra',
    'compra_recorrente',
    'cotacao',
    'consulta',
    'parceria',
    'consulta_financeira',
    'notificacao_oficial',
]
Unidade = Literal['kg', 'unidade', 'dúzia', 'maço', 'litro']
Urgencia = Literal['baixa', 'normal', 'alta', 'critica']

_UNIDADES: dict[str, Unidade] = {
    **dict.fromkeys(['kg', 'kgs', 'quilo', 'quilos'], 'kg'),
    **dict.fromkeys(['unidade', 'unidades', 'un', 'und', 'unid'], 'unidade'),
    **dict.fromkeys(['duzia', 'duzias', 'dz'], 'dúzia'),
    **dict.fromkeys(['maco', 'macos'], 'maço'),
    **dict.fromkeys(['litro', 'litros', 'l'], 'litro'),
}
"""Grafias de unidade aceitas do modelo, já sem acento (ver `Item._normalizar_unidade`)."""


class Item(BaseModel):
    """Item pedido. O LLM preenche o texto; o catálogo e os produtores vêm das regras."""

    produto_texto: str = Field(description='Produto exatamente como escrito na mensagem.')
    produto: str | None = Field(
        default=None,
        description=(
            'Nome do catálogo que corresponde ao produto, copiado da lista enviada. Nulo se '
            'nenhum nome servir ou se mais de um servir.'
        ),
    )
    quantidade: float | None = Field(
        default=None, description='Quantidade pedida, sem somar itens.'
    )
    unidade: Unidade | None = None
    organico: bool | None = Field(
        default=None, description='true se o comprador pede orgânico para este item.'
    )
    opcional: bool = Field(
        default=False, description='true para itens condicionais ("se tiver", "se der").'
    )
    ambiguo: bool = Field(
        default=False,
        description=(
            'true quando não dá para saber qual produto é sem perguntar: referência a pedido '
            'anterior ("o de sempre", "o da última vez"), categoria genérica ("frutas '
            'variadas", "mel variado") ou nome que serve para mais de um produto ("manteiga").'
        ),
    )
    observacao: str | None = Field(
        default=None, description='Motivo da ambiguidade ou outra ressalva curta sobre o item.'
    )

    produto_id: SkipJsonSchema[int | None] = None
    """Id em `produtos`, preenchido por `normalizar_pedido` quando o mapeamento é único."""
    produtores_elegiveis: SkipJsonSchema[list[str]] = Field(default_factory=list)
    """Preenchido pelas regras (#10)."""

    @field_validator('unidade', mode='before')
    @classmethod
    def _normalizar_unidade(cls, valor: Any) -> Any:
        if isinstance(valor, str):
            return _UNIDADES.get(normalizar(valor).strip().rstrip('.'), valor)
        return valor


class Comprador(BaseModel):
    nome: str | None = Field(
        default=None, description='Empresa ou pessoa que envia o pedido, como se apresenta.'
    )
    contrato: str | None = Field(
        default=None, description='Número do contrato citado, ex.: "045/2026".'
    )
    contato: str | None = Field(
        default=None,
        description='Nome da pessoa de contato. Nunca telefone, e-mail ou documento.',
    )


class Entrega(BaseModel):
    """Entrega pedida. O LLM copia a expressão; as datas são calculadas por `resolver_data`."""

    data_texto: str | None = Field(
        default=None,
        description=(
            'Trecho da mensagem com a data ou o prazo de entrega, copiado literalmente e com o '
            'dia da semana, se houver. Ex.: "essa sexta (01/nov)", "pra amanha", "jan/2027".'
        ),
    )
    janela: str | None = Field(default=None, description='Horário de entrega, ex.: "até 10h".')

    data: SkipJsonSchema[date | None] = None
    """Data firme (início, num intervalo). Vazia quando precisa de confirmação."""
    data_fim: SkipJsonSchema[date | None] = None
    """Último dia, quando a entrega é um intervalo ("04 a 08 de novembro")."""
    data_estimada: SkipJsonSchema[date | None] = None
    """Melhor estimativa mesmo sem data firme; é a que as regras de urgência e safra usam."""
    alerta: SkipJsonSchema[str | None] = None
    """Conflito a confirmar com o comprador (ex.: data e dia da semana incompatíveis)."""


class Pedido(BaseModel):
    """Mensagem da caixa de entrada interpretada e triada."""

    tipo: TipoMensagem = Field(
        description=(
            'compra: quantidades e entrega definidas. compra_recorrente: compra que se repete '
            '(semanal, sob contrato de fornecimento contínuo). cotacao: pede preço, '
            'disponibilidade ou documentos antes de comprar. consulta: produtor cooperado '
            'oferecendo produção ou tirando dúvida operacional. parceria: proposta de '
            'fornecimento ou parceria comercial sem itens fechados. consulta_financeira: '
            'pergunta sobre vendas, pagamentos ou saldo. notificacao_oficial: comunicado de '
            'órgão público (fiscalização, inspeção).'
        )
    )
    comprador: Comprador = Field(default_factory=Comprador)
    entrega: Entrega = Field(default_factory=Entrega)
    itens: list[Item] = Field(default_factory=list)
    pagamento: str | None = Field(
        default=None, description='Forma ou condição de pagamento citada, ex.: "boleto 30 dias".'
    )

    pedido_id: SkipJsonSchema[str | None] = None
    sinalizacoes: SkipJsonSchema[list[str]] = Field(default_factory=list)
    motivos_sinalizacoes: SkipJsonSchema[dict[str, list[str]]] = Field(default_factory=dict)
    """Por que cada sinalização foi dada (ex.: `conformidade` → "P017 sem registro SIE
    (frango caipira)"), preenchido pelas regras (#10). Sinalizações sem motivo registrado
    ficam fora do dicionário."""
    urgencia: SkipJsonSchema[Urgencia | None] = None


_CAMPOS_CALCULADOS: dict[str, Any] = {
    'sinalizacoes': True,
    'motivos_sinalizacoes': True,
    'urgencia': True,
    'entrega': {'data', 'data_fim', 'data_estimada', 'alerta'},
    'itens': {'__all__': {'produto_id', 'produtores_elegiveis'}},
}
"""Campos das etapas determinísticas: descartados antes de reclassificar uma extração."""


INSTRUCOES_EXTRACAO = """\
Você extrai dados de mensagens recebidas pela cooperativa Sabor da Terra (agricultura familiar, \
Garanhuns/PE). A mensagem vem de e-mail, WhatsApp ou formulário, em linguagem informal ou em \
outro idioma.

- Extraia só o que está escrito. Não invente quantidades, datas, preços nem produtos.
- `produto_texto` é o produto como o cliente escreveu, no idioma original.
- `produto` é um nome da lista do catálogo enviada junto com a mensagem, copiado como está. \
Se nenhum nome servir ou se mais de um servir, deixe nulo e marque `ambiguo`.
- Nunca resolva referências ao histórico ("o de sempre", "da última vez"): marque `ambiguo` e \
explique em `observacao`.
- Um item por produto. Quantidades por período ("50 kg por semana", "500kg/month") ficam como \
estão, com o período em `observacao`.
- Em `entrega.data_texto`, copie o trecho que fala da data ou do prazo, sem calcular a data.
- Trechos como [CPF_1] ou [TELEFONE_1] substituem dados pessoais: não os copie para a saída.
"""


def modelo_extracao() -> str:
    """Modelo usado na extração de pedidos (`SABOR_PEDIDOS_MODEL`)."""
    return os.getenv('SABOR_PEDIDOS_MODEL', 'google:gemini-3.5-flash-lite')


def _agente_extracao() -> Agent[None, Pedido]:
    return Agent(modelo_extracao(), output_type=Pedido, instructions=INSTRUCOES_EXTRACAO)


class EntradaInvalida(ValueError):
    """A chamada não identifica uma mensagem a extrair."""


class PedidoNaoEncontrado(EntradaInvalida):
    """O ID informado não existe na tabela `pedidos`."""


@dataclass(frozen=True)
class MensagemPedido:
    """Mensagem a extrair e a data que ancora as datas relativas dela."""

    texto: str
    data_referencia: date
    pedido_id: str | None = None


def carregar_mensagem(
    deps: AgentDeps, *, pedido_id: str | None = None, texto: str | None = None
) -> MensagemPedido:
    """Pedido salvo (datas relativas ao recebimento) ou texto colado (data de referência)."""
    pedido_id = (pedido_id or '').strip()
    texto = (texto or '').strip()
    if bool(pedido_id) == bool(texto):
        raise EntradaInvalida('Informe exatamente um entre pedido_id e texto.')
    if not pedido_id:
        return MensagemPedido(texto=texto, data_referencia=deps.data_referencia)

    row = deps.conn.execute(
        'SELECT id, data_recebimento, mensagem_original FROM pedidos WHERE upper(id) = upper(%s)',
        (pedido_id,),
    ).fetchone()
    if row is None:
        raise PedidoNaoEncontrado(f'Pedido {pedido_id} não encontrado na caixa de entrada.')
    id_salvo, recebido_em, mensagem_original = row
    return MensagemPedido(
        texto=mensagem_original or '',
        data_referencia=recebido_em.date(),
        pedido_id=id_salvo,
    )


async def extrair_pedido(
    deps: AgentDeps,
    mensagem: MensagemPedido,
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> Pedido:
    """Etapa do LLM: extração bruta, antes do catálogo, das datas e das regras.

    A mensagem vai mascarada (#33). `usage` acumula o consumo (no chat, é o do turno, para o
    orçamento de #32 valer também para esta chamada) e `usage_limits` aplica um teto próprio
    (triagem em lote, #14).
    """
    catalogo = Catalogo.carregar(deps.conn)
    prompt = (
        f'Catálogo: {"; ".join(catalogo.nomes())}\n\n'
        f'Mensagem:\n"""\n{deps.mascarador.mascarar(mensagem.texto)}\n"""'
    )
    resposta = await _agente_extracao().run(prompt, usage=usage, usage_limits=usage_limits)
    return resposta.output.model_copy(update={'pedido_id': mensagem.pedido_id})


def normalizar_pedido(pedido: Pedido, catalogo: Catalogo, recebido_em: date) -> Pedido:
    """Etapa determinística da extração (#9): data de entrega e catálogo. Idempotente."""
    entrega = pedido.entrega
    if entrega.data_texto:
        resolvida = resolver_data(entrega.data_texto, recebido_em)
        entrega.data = resolvida.data
        entrega.data_fim = resolvida.data_fim
        entrega.data_estimada = resolvida.estimada
        entrega.alerta = resolvida.alerta
        if resolvida.inconsistente and 'data_inconsistente' not in pedido.sinalizacoes:
            pedido.sinalizacoes.append('data_inconsistente')
    elif entrega.data_estimada is None:
        entrega.data_estimada = entrega.data

    for item in pedido.itens:
        if item.produto_id is not None:
            continue
        if item.ambiguo:
            # O LLM às vezes marca como ambíguo um nome que está no catálogo, como "banana
            # prata" (sinônimo de banana). Só o nome ou sinônimo exato desfaz a marcação:
            # "manteiga", "mel variado" e "o de sempre" continuam ambíguos.
            produto = catalogo.resolver_exato(item.produto_texto) if item.produto is None else None
            if produto is not None:
                item.produto_id, item.produto, item.ambiguo = produto.id, produto.nome, False
                # A observação da extração pode ter outra informação ("fornecimento semanal").
                nota = f'Identificado no catálogo como {produto.nome}.'
                if item.observacao:
                    nota = f'{nota} Nota da extração: {item.observacao}'
                item.observacao = nota
            continue
        produto = catalogo.resolver(item.produto, item.produto_texto)
        if produto is None:
            item.ambiguo = True
            item.observacao = item.observacao or 'Produto fora do catálogo; confirme qual é.'
        else:
            item.produto_id, item.produto = produto.id, produto.nome
    return pedido


def classificar_extracao(deps: AgentDeps, mensagem: MensagemPedido, extracao: Pedido) -> Pedido:
    """Etapas sem LLM sobre o banco atual: normalização (#9) e regras (#10).

    Não altera `extracao`: parte de uma cópia só com os campos do modelo.
    """
    pedido = Pedido.model_validate(extracao.model_dump(exclude=_CAMPOS_CALCULADOS))
    pedido.pedido_id = mensagem.pedido_id
    normalizar_pedido(pedido, Catalogo.carregar(deps.conn), mensagem.data_referencia)
    return classificar_pedido(
        pedido,
        conn=deps.conn,
        mensagem_original=mensagem.texto,
        data_referencia=mensagem.data_referencia,
        data_global=deps.data_referencia,
    )


async def extrair_e_classificar(
    deps: AgentDeps,
    *,
    pedido_id: str | None = None,
    texto: str | None = None,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> Pedido:
    """Núcleo da tool, chamável fora do turno: extração pelo LLM seguida das regras.

    Levanta `EntradaInvalida` (ou `PedidoNaoEncontrado`) se a entrada não identificar um pedido.
    """
    mensagem = carregar_mensagem(deps, pedido_id=pedido_id, texto=texto)
    extracao = await extrair_pedido(deps, mensagem, usage=usage, usage_limits=usage_limits)
    return classificar_extracao(deps, mensagem, extracao)


async def extrair_e_classificar_pedido(
    ctx: RunContext[AgentDeps],
    pedido_id: str | None = None,
    texto: str | None = None,
) -> Pedido | str:
    """Interpreta e tria um pedido: itens, comprador, entrega, urgência e sinalizações.

    Mapeia os produtos ao catálogo, calcula a data de entrega a partir do recebimento, marca
    itens ambíguos e datas que não batem com o dia da semana (para confirmar com o comprador)
    e aplica as regras de safra, capacidade, conformidade, crédito e privacidade.

    Args:
        pedido_id: ID de um pedido da caixa de entrada, ex.: "PED-2026-003".
        texto: Mensagem colada no chat, quando não houver ID. Informe só um dos dois.
    """
    try:
        return await extrair_e_classificar(
            ctx.deps, pedido_id=pedido_id, texto=texto, usage=ctx.usage
        )
    except EntradaInvalida as exc:
        # Devolvido ao modelo, que pode pedir o ID certo ou o texto ao operador.
        return str(exc)
