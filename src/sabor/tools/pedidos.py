"""Extração estruturada de pedidos (Issue #9)."""

from __future__ import annotations

from datetime import date, datetime
import os
import re
import unicodedata
from typing import Literal

import dateparser
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from sabor.agent.deps import AgentDeps
from sabor.tools.base import _resolver_produto_id
from sabor.tools.regras import classificar_pedido


class Item(BaseModel):
    produto_texto: str
    produto: str | None = None
    quantidade: float | None = None
    unidade: Literal['kg', 'unidade', 'dúzia', 'maço', 'litro'] | None = None
    organico: bool | None = None
    opcional: bool = False
    ambiguo: bool = False
    observacao: str | None = None
    produtores_elegiveis: list[str] = Field(default_factory=list)


class Comprador(BaseModel):
    nome: str | None = None
    contrato: str | None = None
    contato: str | None = None


class Entrega(BaseModel):
    data: date | None = None
    data_texto: str | None = None
    janela: str | None = None
    alerta: str | None = None


class Pedido(BaseModel):
    pedido_id: str | None = None
    tipo: Literal[
        'compra',
        'compra_recorrente',
        'cotacao',
        'consulta',
        'parceria',
        'consulta_financeira',
        'notificacao_oficial',
    ]
    comprador: Comprador = Field(default_factory=Comprador)
    entrega: Entrega = Field(default_factory=Entrega)
    itens: list[Item] = Field(default_factory=list)
    pagamento: str | None = None
    sinalizacoes: list[str] = Field(default_factory=list)
    urgencia: Literal['baixa', 'normal', 'alta', 'critica'] | None = None

_DIAS_SEMANA = {
    'segunda': 0,
    'seg': 0,
    'terca': 1,
    'ter': 1,
    'quarta': 2,
    'qua': 2,
    'quinta': 3,
    'qui': 3,
    'sexta': 4,
    'sex': 4,
    'sabado': 5,
    'sab': 5,
    'domingo': 6,
    'dom': 6,
}

_NOMES_DIAS = (
    'segunda-feira',
    'terça-feira',
    'quarta-feira',
    'quinta-feira',
    'sexta-feira',
    'sábado',
    'domingo',
)


def _agente_extracao() -> Agent[None, Pedido]:
    """Cria o agente de extração com o modelo configurado para o projeto."""
    modelo = os.getenv('SABOR_PEDIDOS_MODEL', 'google:gemini-3.5-flash-lite')
    return Agent(modelo, output_type=Pedido, deps_type=None)


def _normalizar(texto: str) -> str:
    """Remove acentos para reconhecer nomes de dias com ou sem acentuação."""
    return ''.join(
        caractere
        for caractere in unicodedata.normalize('NFKD', texto.lower())
        if not unicodedata.combining(caractere)
    )


def _extrair_dia_semana(texto: str) -> int | None:
    """Retorna o dia da semana explicitamente mencionado, de segunda=0 a domingo=6."""
    normalizado = _normalizar(texto)

    for nome, numero in _DIAS_SEMANA.items():
        if re.search(rf'\b{nome}(?:-feira)?\b', normalizado):
            return numero

    return None


def _parse_data(texto: str, data_referencia: date) -> date | None:
    """Interpreta uma data usando a data de recebimento como referência."""
    # Se houver uma data explícita, parseia somente esse trecho. Assim, em
    # "sexta (01/nov)", o parser não troca 01/nov pela próxima sexta-feira.
    match = re.search(
        r'\b\d{1,2}[/-](?:\d{1,2}|[a-zA-ZçÇ]{3,})(?:[/-]\d{2,4})?\b',
        texto,
    )
    texto_data = match.group(0) if match else texto

    parsed = dateparser.parse(
        texto_data,
        languages=['pt'],
        settings={
            'RELATIVE_BASE': datetime.combine(data_referencia, datetime.min.time()),
            'PREFER_DATES_FROM': 'future',
        },
    )
    return parsed.date() if parsed else None


def _validar_dia_e_data(
    pedido: Pedido,
    mensagem_original: str,
    data_referencia: date,
) -> None:
    """Resolve a data e sinaliza conflito explícito entre data e dia da semana (#9)."""
    data_entrega = pedido.entrega.data

    if data_entrega is None and pedido.entrega.data_texto:
        data_entrega = _parse_data(pedido.entrega.data_texto, data_referencia)

    if data_entrega is None:
        data_entrega = _parse_data(mensagem_original, data_referencia)

    dia_informado = _extrair_dia_semana(mensagem_original)

    if data_entrega is not None and dia_informado is not None:
        if data_entrega.weekday() != dia_informado:
            pedido.entrega.data = None
            pedido.entrega.alerta = (
                f'A mensagem informa { _NOMES_DIAS[dia_informado] }, mas '
                f'{data_entrega:%d/%m/%Y} cai em '
                f'{_NOMES_DIAS[data_entrega.weekday()]}; confirme a data.'
            )
            if 'data_inconsistente' not in pedido.sinalizacoes:
                pedido.sinalizacoes.append('data_inconsistente')
        else:
            pedido.entrega.data = data_entrega
    elif data_entrega is not None and pedido.entrega.alerta is None:
        pedido.entrega.data = data_entrega


async def extrair_e_classificar_pedido(
    ctx: RunContext[AgentDeps],
    *,
    pedido_id: str | None = None,
    texto: str | None = None,
) -> Pedido:
    """Extrai dados de um pedido salvo ou de texto colado."""
    if bool(pedido_id) == bool(texto):
        raise ValueError('Informe exatamente um entre pedido_id e texto.')

    recebido_em: datetime | None = None
    id_resolvido: str | None = None

    if pedido_id:
        row = ctx.deps.conn.execute(
            """
            SELECT id, data_recebimento, mensagem_original
            FROM pedidos
            WHERE id = %s
            """,
            (pedido_id.strip(),),
        ).fetchone()

        if row is None:
            raise ValueError(f'Pedido não encontrado: {pedido_id}')

        id_resolvido, recebido_em, texto = row

    mensagem_original = texto or ''
    data_referencia = (
        recebido_em.date()
        if recebido_em is not None
        else ctx.deps.data_referencia
    )
    mensagem_mascarada = ctx.deps.mascarador.mascarar(mensagem_original)

    instrucao = (
        f'Extraia os dados sem inventar valores. Data de referência: {data_referencia}. '
        'Resolva datas relativas usando essa referência. Extraia em entrega.data_texto '
        'a expressão original da data e preserve o dia da semana mencionado. Se a data '
        'e o dia da semana forem incompatíveis, deixe entrega.data nula. Preserve itens '
        'vagos como ambíguos; não adivinhe o produto. Responda em português, salvo se a '
        'mensagem estiver em outro idioma. Não inclua CPF, telefone, e-mail ou dados '
        'bancários no resultado.'
    )

    resposta = await _agente_extracao().run(
        f'{instrucao}\n\nMensagem:\n{mensagem_mascarada}',
    )
    pedido = resposta.output
    pedido.pedido_id = id_resolvido

    for item in pedido.itens:
        if item.produto is None:
            item.produto = item.produto_texto

        produto_id = _resolver_produto_id(ctx.deps.conn, item.produto)
        if produto_id is not None:
            row = ctx.deps.conn.execute(
                'SELECT nome FROM produtos WHERE id = %s',
                (produto_id,),
            ).fetchone()
            if row:
                item.produto = row[0]

    _validar_dia_e_data(pedido, mensagem_original, data_referencia)

    classificar_pedido(
        pedido,
        conn=ctx.deps.conn,
        mensagem_original=mensagem_original,
        data_referencia=data_referencia,
        data_global=ctx.deps.data_referencia,
    )

    return pedido