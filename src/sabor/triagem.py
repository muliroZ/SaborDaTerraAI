"""Triagem em lote da caixa de entrada (#14).

Roda `extrair_e_classificar_pedido` sobre todas as mensagens da tabela `pedidos`, fora do
chat, e grava o resultado em `triagem_pedidos`. A fila sai ordenada por urgência e pode ser
exportada em CSV ou JSON (scripts/triagem_lote.py e a "Caixa de entrada" da interface).

- Reaproveitamento: a extração do LLM fica em `extracao_llm`. Com o mesmo `hash_entrada`
  (mensagem, recebimento, modelo e versão da triagem), o pedido não volta ao modelo, mas as
  regras (#10) rodam de novo sobre o banco atual, sem tokens: uma dívida vencida registrada
  depois da última triagem já aparece no lote seguinte.
- Resultado desatualizado: se a mensagem mudou desde a extração, a fila mostra o pedido como
  `desatualizado` (sem a classificação antiga) até ele ser triado de novo.
- Orçamento (#32): cada pedido roda com `UsageLimits` (mesmos limites do turno do chat) e o
  lote inteiro tem um teto de tokens; ao esgotar, o lote para e o restante fica pendente
  para a próxima execução. Estourar o limite de chamadas de um pedido não para o lote.
- Erros temporários do provedor (limite por minuto, 503, conexão) são tentados de novo após
  uma espera; outra falha num pedido não derruba o lote: ele fica com `status = 'erro'` e é
  tentado de novo na próxima execução.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Sequence
import csv
from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import io
import json
import logging
import os
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelRateLimitError,
    ModelServerError,
    ModelUnavailableError,
    UsageLimitExceeded,
)
from pydantic_ai.usage import RunUsage, UsageLimits

from sabor.agent.deps import AgentDeps
from sabor.agent.guardrails import obter_limites_configurados
from sabor.tools.pedidos import (
    MensagemPedido,
    Pedido,
    classificar_extracao,
    extrair_pedido,
    modelo_extracao,
)

logger = logging.getLogger(__name__)

VERSAO_TRIAGEM = '2'
"""Suba quando o prompt de extração mudar: força nova chamada ao LLM para todos os pedidos.
Mudanças nas regras (#10) não precisam disso: elas rodam de novo a cada lote."""

TOKENS_LOTE_PADRAO = 150_000
"""Teto de tokens do lote (`TRIAGEM_TOKENS_LIMIT`): ~100 pedidos/dia a ~1,2 mil tokens cada."""

ESPERAS_RETENTATIVA_S = (20.0, 40.0, 60.0)
"""Esperas antes de repetir um pedido após erro temporário (o plano gratuito do Gemini aceita
15 chamadas por minuto)."""

ERROS_TEMPORARIOS = (
    ModelRateLimitError,
    ModelUnavailableError,
    ModelServerError,
    ModelConnectionError,
)

ESPERA_MAXIMA_S = 120.0

MAX_CARACTERES_ERRO = 300

URGENCIAS = ('critica', 'alta', 'normal', 'baixa')
"""Ordem da fila; pedidos sem triagem válida vêm depois, na ordem de `SITUACOES`."""

SITUACOES = ('ok', 'erro', 'desatualizado', 'pendente')
"""`desatualizado`: a mensagem mudou depois da extração; `pendente`: nunca foi triado."""


@dataclass(frozen=True)
class Orcamento:
    """Limites de consumo do lote. `None` desliga o limite."""

    tokens_lote: int | None
    tokens_pedido: int | None
    requisicoes_pedido: int | None

    @classmethod
    def configurado(cls) -> Orcamento:
        """Teto do lote em `TRIAGEM_TOKENS_LIMIT`; por pedido, os limites do turno (#32)."""
        limites = obter_limites_configurados()
        return cls(
            tokens_lote=int(os.getenv('TRIAGEM_TOKENS_LIMIT') or TOKENS_LOTE_PADRAO),
            tokens_pedido=limites['turno_tokens_limit'],
            requisicoes_pedido=limites['turno_request_limit'],
        )


@dataclass
class ResultadoLote:
    processados: list[str] = field(default_factory=list)
    reaproveitados: list[str] = field(default_factory=list)
    """Extração do LLM reaproveitada e regras reaplicadas, sem tokens."""
    erros: dict[str, str] = field(default_factory=dict)
    nao_processados: list[str] = field(default_factory=list)
    orcamento_esgotado: bool = False
    uso: RunUsage = field(default_factory=RunUsage)


def hash_entrada(mensagem: str, recebido_em: datetime, modelo: str) -> str:
    """Identifica a entrada do LLM. A data de referência global fica de fora: só as regras
    usam, e elas rodam de novo a cada lote."""
    chave = '\x1f'.join((VERSAO_TRIAGEM, modelo, recebido_em.isoformat(), mensagem))
    return hashlib.sha256(chave.encode('utf-8')).hexdigest()


def _gravar_ok(
    conn: psycopg.Connection,
    pedido_id: str,
    hash_: str,
    extracao: Pedido,
    pedido: Pedido,
    modelo: str,
    uso: RunUsage,
) -> None:
    conn.execute(
        """
        INSERT INTO triagem_pedidos (
            pedido_id, hash_entrada, status, tipo, urgencia, comprador, data_entrega,
            sinalizacoes, extracao_llm, resultado, erro, modelo, requisicoes, tokens_entrada,
            tokens_saida, processado_em, classificado_em)
        VALUES (%s, %s, 'ok', %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, now(), now())
        ON CONFLICT (pedido_id) DO UPDATE SET
            hash_entrada = EXCLUDED.hash_entrada, status = 'ok', tipo = EXCLUDED.tipo,
            urgencia = EXCLUDED.urgencia, comprador = EXCLUDED.comprador,
            data_entrega = EXCLUDED.data_entrega, sinalizacoes = EXCLUDED.sinalizacoes,
            extracao_llm = EXCLUDED.extracao_llm, resultado = EXCLUDED.resultado, erro = NULL,
            modelo = EXCLUDED.modelo, requisicoes = EXCLUDED.requisicoes,
            tokens_entrada = EXCLUDED.tokens_entrada, tokens_saida = EXCLUDED.tokens_saida,
            processado_em = now(), classificado_em = now()
        """,
        (
            pedido_id,
            hash_,
            pedido.tipo,
            pedido.urgencia,
            pedido.comprador.nome,
            pedido.entrega.data,
            pedido.sinalizacoes,
            Jsonb(extracao.model_dump(mode='json')),
            Jsonb(pedido.model_dump(mode='json')),
            modelo,
            uso.requests,
            uso.input_tokens,
            uso.output_tokens,
        ),
    )


def _gravar_reclassificacao(conn: psycopg.Connection, pedido_id: str, pedido: Pedido) -> None:
    """Novo resultado das regras sobre a extração gravada; consumo e modelo não mudam."""
    conn.execute(
        """
        UPDATE triagem_pedidos SET
            status = 'ok', tipo = %s, urgencia = %s, comprador = %s, data_entrega = %s,
            sinalizacoes = %s, resultado = %s, erro = NULL, classificado_em = now()
        WHERE pedido_id = %s
        """,
        (
            pedido.tipo,
            pedido.urgencia,
            pedido.comprador.nome,
            pedido.entrega.data,
            pedido.sinalizacoes,
            Jsonb(pedido.model_dump(mode='json')),
            pedido_id,
        ),
    )


def _gravar_erro_extracao(
    conn: psycopg.Connection, pedido_id: str, hash_: str, erro: str, modelo: str, uso: RunUsage
) -> None:
    # Um erro substitui o resultado anterior: ele não corresponde mais ao `hash_entrada`.
    conn.execute(
        """
        INSERT INTO triagem_pedidos (
            pedido_id, hash_entrada, status, erro, modelo, requisicoes, tokens_entrada,
            tokens_saida, processado_em)
        VALUES (%s, %s, 'erro', %s, %s, %s, %s, %s, now())
        ON CONFLICT (pedido_id) DO UPDATE SET
            hash_entrada = EXCLUDED.hash_entrada, status = 'erro', tipo = NULL, urgencia = NULL,
            comprador = NULL, data_entrega = NULL, sinalizacoes = '{}', extracao_llm = NULL,
            resultado = NULL, erro = EXCLUDED.erro, modelo = EXCLUDED.modelo,
            requisicoes = EXCLUDED.requisicoes, tokens_entrada = EXCLUDED.tokens_entrada,
            tokens_saida = EXCLUDED.tokens_saida, processado_em = now(), classificado_em = NULL
        """,
        (pedido_id, hash_, erro, modelo, uso.requests, uso.input_tokens, uso.output_tokens),
    )


def _gravar_erro_regras(conn: psycopg.Connection, pedido_id: str, erro: str) -> None:
    # Mantém `extracao_llm`: o próximo lote tenta as regras de novo sem chamar o modelo.
    conn.execute(
        """
        UPDATE triagem_pedidos SET
            status = 'erro', tipo = NULL, urgencia = NULL, comprador = NULL, data_entrega = NULL,
            sinalizacoes = '{}', resultado = NULL, erro = %s, classificado_em = now()
        WHERE pedido_id = %s
        """,
        (erro, pedido_id),
    )


def _descrever_erro(exc: Exception) -> str:
    return f'{type(exc).__name__}: {exc}'[:MAX_CARACTERES_ERRO]


async def triar_lote(
    deps: AgentDeps,
    *,
    orcamento: Orcamento | None = None,
    reprocessar: bool = False,
    limite: int | None = None,
    ao_processar: Callable[[str, str], None] | None = None,
    esperas_s: Sequence[float] = ESPERAS_RETENTATIVA_S,
    dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ResultadoLote:
    """Tria a caixa de entrada, gravando um a um em `triagem_pedidos`.

    Pedidos com extração válida só passam pelas regras de novo (sem tokens); os demais vão
    ao LLM, dentro do orçamento.

    `deps.conn` precisa de escrita (autocommit): cada pedido é gravado assim que termina, então
    interromper o lote não perde o que já foi feito. `limite` conta só pedidos enviados ao
    modelo. `ao_processar(pedido_id, situacao)` acompanha o progresso. Cada espera de
    `esperas_s` dá uma nova tentativa ao pedido após um erro temporário do provedor.
    """
    orcamento = orcamento or Orcamento.configurado()
    conn = deps.conn
    modelo = modelo_extracao()
    resultado = ResultadoLote()
    avisar = ao_processar or (lambda _id, _situacao: None)
    chamadas_com_erro = 0  # para `limite`: erros das regras na reclassificação não contam

    linhas = conn.execute(
        """
        SELECT p.id, p.data_recebimento, p.mensagem_original, t.hash_entrada, t.extracao_llm
        FROM pedidos p
        LEFT JOIN triagem_pedidos t ON t.pedido_id = p.id
        ORDER BY p.data_recebimento, p.id
        """
    ).fetchall()

    fila: list[tuple[MensagemPedido, str]] = []
    for pedido_id, recebido_em, texto, hash_gravado, extracao_gravada in linhas:
        mensagem = MensagemPedido(
            texto=texto, data_referencia=recebido_em.date(), pedido_id=pedido_id
        )
        hash_ = hash_entrada(texto, recebido_em, modelo)
        if reprocessar or extracao_gravada is None or hash_gravado != hash_:
            fila.append((mensagem, hash_))
            continue
        # Extração válida: só as regras rodam de novo, sobre o banco atual (sem tokens).
        try:
            pedido = classificar_extracao(deps, mensagem, Pedido.model_validate(extracao_gravada))
        except Exception as exc:
            logger.exception('Falha nas regras ao reclassificar %s', pedido_id)
            erro = _descrever_erro(exc)
            _gravar_erro_regras(conn, pedido_id, erro)
            resultado.erros[pedido_id] = erro
            avisar(pedido_id, 'erro')
        else:
            _gravar_reclassificacao(conn, pedido_id, pedido)
            resultado.reaproveitados.append(pedido_id)
            avisar(pedido_id, f'{pedido.urgencia} (extração reaproveitada)')

    for posicao, (mensagem, hash_) in enumerate(fila):
        pedido_id = mensagem.pedido_id
        restante = None
        if orcamento.tokens_lote is not None:
            restante = orcamento.tokens_lote - resultado.uso.total_tokens
        if (limite is not None and len(resultado.processados) + chamadas_com_erro >= limite) or (
            restante is not None and restante <= 0
        ):
            resultado.orcamento_esgotado = restante is not None and restante <= 0
            resultado.nao_processados = [m.pedido_id for m, _ in fila[posicao:]]
            break

        teto_pedido = orcamento.tokens_pedido
        limitado_pelo_lote = restante is not None and (
            teto_pedido is None or restante < teto_pedido
        )
        uso = RunUsage()
        limites = UsageLimits(
            request_limit=orcamento.requisicoes_pedido,
            total_tokens_limit=restante if limitado_pelo_lote else teto_pedido,
        )
        try:
            extracao = await _extrair_com_retentativa(
                deps, mensagem, uso, limites, esperas_s, dormir, avisar
            )
            pedido = classificar_extracao(deps, mensagem, extracao)
        except UsageLimitExceeded:
            teto_tokens = limites.total_tokens_limit
            por_tokens = teto_tokens is not None and uso.total_tokens > teto_tokens
            if por_tokens:
                escopo = 'lote' if limitado_pelo_lote else 'pedido'
                erro = (
                    f'Orçamento de tokens do {escopo} esgotado '
                    f'({uso.total_tokens} de {teto_tokens}).'
                )
            else:
                erro = (
                    'Limite de chamadas ao modelo do pedido atingido '
                    f'({uso.requests} de {limites.request_limit}).'
                )
            _gravar_erro_extracao(conn, pedido_id, hash_, erro, modelo, uso)
            resultado.erros[pedido_id] = erro
            chamadas_com_erro += 1
            avisar(pedido_id, 'erro')
            # Só a falta de tokens do lote o interrompe; o limite de chamadas é do pedido.
            if por_tokens and limitado_pelo_lote:
                resultado.uso.incr(uso)
                resultado.orcamento_esgotado = True
                resultado.nao_processados = [m.pedido_id for m, _ in fila[posicao + 1 :]]
                break
        except Exception as exc:  # um pedido com problema não derruba o lote
            if isinstance(exc, ERROS_TEMPORARIOS):
                logger.warning('Provedor indisponível ao triar %s: %s', pedido_id, exc)
            else:
                logger.exception('Falha ao triar %s', pedido_id)
            erro = _descrever_erro(exc)
            _gravar_erro_extracao(conn, pedido_id, hash_, erro, modelo, uso)
            resultado.erros[pedido_id] = erro
            chamadas_com_erro += 1
            avisar(pedido_id, 'erro')
        else:
            _gravar_ok(conn, pedido_id, hash_, extracao, pedido, modelo, uso)
            resultado.processados.append(pedido_id)
            avisar(pedido_id, pedido.urgencia or 'ok')
        resultado.uso.incr(uso)

    return resultado


async def _extrair_com_retentativa(
    deps: AgentDeps,
    mensagem: MensagemPedido,
    uso: RunUsage,
    limites: UsageLimits,
    esperas_s: Sequence[float],
    dormir: Callable[[float], Awaitable[None]],
    avisar: Callable[[str, str], None],
) -> Pedido:
    # O mesmo `uso` atravessa as tentativas: o orçamento do pedido vale para todas juntas.
    for espera in (*esperas_s, None):
        try:
            return await extrair_pedido(deps, mensagem, usage=uso, usage_limits=limites)
        except ERROS_TEMPORARIOS as exc:
            if espera is None:
                raise
            # O Gemini informa quanto esperar no 429 ("retry in 53s"); respeita, com teto.
            espera = min(max(espera, exc.retry_after or 0.0), ESPERA_MAXIMA_S)
            avisar(mensagem.pedido_id, f'provedor indisponível, nova tentativa em {espera:.0f}s')
            await dormir(espera)
    raise AssertionError('inalcançável')


# ---------------------------------------------------------------------------
# Fila ordenada por urgência e exportação
# ---------------------------------------------------------------------------

COLUNAS = (
    'posicao',
    'pedido_id',
    'recebido_em',
    'canal',
    'situacao',
    'urgencia',
    'tipo',
    'comprador',
    'data_entrega',
    'sinalizacoes',
    'itens',
    'alerta',
    'erro',
)
"""Colunas exportadas. `remetente` fica de fora: guarda e-mail e telefone (LGPD)."""


def _resumo_item(item: dict[str, Any]) -> str:
    produto = item.get('produto') or item.get('produto_texto') or '?'
    quantidade = item.get('quantidade')
    texto = f'{quantidade:g} {item.get("unidade") or ""} {produto}' if quantidade else produto
    marcas = [m for m in ('ambiguo', 'opcional') if item.get(m)]
    marcas = ['ambíguo' if m == 'ambiguo' else m for m in marcas]
    return ' '.join(texto.split()) + (f' ({", ".join(marcas)})' if marcas else '')


def listar_fila(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Caixa de entrada inteira: triados por urgência e entrega; depois erros, desatualizados
    e pendentes.

    Um resultado cuja mensagem mudou depois da extração aparece como `desatualizado`, sem a
    classificação antiga, mesmo que o lote ainda não tenha rodado de novo.
    """
    modelo = modelo_extracao()
    linhas = conn.execute(
        """
        SELECT p.id, p.data_recebimento, p.canal, p.mensagem_original, t.hash_entrada,
               t.status, t.urgencia, t.tipo, t.comprador, t.data_entrega, t.sinalizacoes,
               t.resultado, t.erro
        FROM pedidos p
        LEFT JOIN triagem_pedidos t ON t.pedido_id = p.id
        """
    ).fetchall()
    fila = []
    for linha in linhas:
        (pid, recebido_em, canal, texto, hash_gravado, status, *triagem, erro) = linha
        if status is None:
            situacao = 'pendente'
        elif hash_gravado != hash_entrada(texto, recebido_em, modelo):
            situacao = 'desatualizado'
        else:
            situacao = status
        if situacao in ('pendente', 'desatualizado'):
            triagem, erro = [None] * 6, None
        urgencia, tipo, comprador, entrega, sinais, res = triagem
        res = res or {}
        fila.append(
            {
                'pedido_id': pid,
                'recebido_em': recebido_em,
                'canal': canal,
                'situacao': situacao,
                'urgencia': urgencia,
                'tipo': tipo,
                'comprador': comprador,
                'data_entrega': entrega,
                'sinalizacoes': list(sinais or []),
                'itens': [_resumo_item(i) for i in res.get('itens', [])],
                'alerta': (res.get('entrega') or {}).get('alerta'),
                'erro': erro,
                'extracao': res or None,
            }
        )
    fila.sort(
        key=lambda f: (
            URGENCIAS.index(f['urgencia']) if f['situacao'] == 'ok' else len(URGENCIAS),
            SITUACOES.index(f['situacao']),
            f['data_entrega'] or date.max,
            f['recebido_em'],
            f['pedido_id'],
        )
    )
    return [{'posicao': posicao, **f} for posicao, f in enumerate(fila, start=1)]


_INICIO_DE_FORMULA = ('=', '+', '-', '@', '\t', '\r', '＝', '＋', '－', '＠')


def _celula_segura(valor: Any) -> Any:
    """Neutraliza CSV injection: texto vindo das mensagens não pode virar fórmula no Excel."""
    if isinstance(valor, str) and valor.startswith(_INICIO_DE_FORMULA):
        return "'" + valor
    return valor


def para_csv(fila: Iterable[dict[str, Any]]) -> str:
    """CSV com `;` (abre direto no Excel em português); listas viram texto separado por `|`.

    Células que começariam com `=`, `+`, `-`, `@`, tab ou CR ganham um `'` na frente.
    """
    saida = io.StringIO()
    escritor = csv.DictWriter(saida, fieldnames=COLUNAS, delimiter=';', extrasaction='ignore')
    escritor.writeheader()
    for linha in fila:
        celulas = {
            **linha,
            'recebido_em': linha['recebido_em'].isoformat(sep=' ', timespec='minutes'),
            'sinalizacoes': ' | '.join(linha['sinalizacoes']),
            'itens': ' | '.join(linha['itens']),
        }
        escritor.writerow({c: _celula_segura(celulas.get(c)) for c in COLUNAS})
    return saida.getvalue()


def para_json(fila: Iterable[dict[str, Any]]) -> str:
    """JSON com as colunas da fila e a extração completa (`extracao`) de cada pedido."""
    campos = (*COLUNAS, 'extracao')
    return json.dumps(
        [{c: linha[c] for c in campos} for linha in fila],
        ensure_ascii=False,
        indent=2,
        default=lambda v: v.isoformat() if isinstance(v, date) else str(v),
    )
