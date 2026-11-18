"""Sessões de chat e memória da sessão no PostgreSQL (etapas 1 e 5b do Agent System, #31).

Camada mínima usada pela interface (#13): a conversa vive nas tabelas `sessoes` e
`mensagens`, não em `st.session_state`, então recarregar a página não perde a sessão ativa.

- Cada usuário simulado tem no máximo uma sessão ativa (`encerrada_em IS NULL`).
- "Nova sessão" encerra a atual e abre outra: o contexto anterior não volta para o prompt,
  fica só para o relatório somente leitura (#38).
- `carregar_historico` monta o `message_history` do PydanticAI com os últimos N turnos
  (`MEMORIA_MAX_TURNOS`, padrão 6), só com o texto de pergunta e resposta e com dados
  pessoais mascarados (#33).

Todas as funções recebem uma conexão de escrita (`sabor.db.conectar()`), nunca a `sabor_ro`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import os
from typing import Any, Literal

from dotenv import load_dotenv
import psycopg
from psycopg.types.json import Jsonb
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)

from sabor.agent.mascarador import Mascarador
from sabor.agent.schemas import RespostaTurno, UserProfile
from sabor.tools.base import _resolver_usuario

MAX_TURNOS_MEMORIA_PADRAO = 6
"""Quantidade padrão de turnos (pergunta + resposta) repassados ao modelo como memória."""

Papel = Literal['usuario', 'assistente']


@dataclass(frozen=True)
class Sessao:
    id: str
    usuario_id: str
    perfil: UserProfile
    criada_em: datetime
    encerrada_em: datetime | None = None
    total_mensagens: int = 0

    @property
    def ativa(self) -> bool:
        return self.encerrada_em is None


@dataclass(frozen=True)
class Mensagem:
    id: int
    papel: Papel
    texto: str
    criada_em: datetime
    turno: RespostaTurno | None = None


def max_turnos_memoria() -> int:
    """Lê `MEMORIA_MAX_TURNOS` (padrão 6); valores inválidos ou negativos usam o padrão."""
    load_dotenv()
    try:
        valor = int(os.getenv('MEMORIA_MAX_TURNOS', MAX_TURNOS_MEMORIA_PADRAO))
    except ValueError:
        return MAX_TURNOS_MEMORIA_PADRAO
    return valor if valor >= 0 else MAX_TURNOS_MEMORIA_PADRAO


def perfil_do_usuario(usuario_id: str) -> UserProfile:
    """Mesma regra das ferramentas: `P001`…`P018` é produtor; qualquer outro nome é funcionário."""
    return _resolver_usuario(usuario_id).perfil


_COLUNAS_SESSAO = """
    s.id, s.usuario_id, s.perfil, s.criada_em, s.encerrada_em,
    (SELECT count(*) FROM mensagens m WHERE m.sessao_id = s.id) AS total_mensagens
"""


def _sessao(row: Sequence[Any]) -> Sessao:
    return Sessao(
        id=str(row[0]),
        usuario_id=row[1],
        perfil=row[2],
        criada_em=row[3],
        encerrada_em=row[4],
        total_mensagens=int(row[5]),
    )


def criar_sessao(conn: psycopg.Connection, usuario_id: str, sessao_id: str | None = None) -> Sessao:
    """Abre uma sessão. Sem `sessao_id`, o banco gera um UUID (em texto); qualquer string
    é aceita, como no contrato `processar_turno(sessao_id: str)` e na auditoria (#35)."""
    with conn.transaction():
        row = conn.execute(
            'INSERT INTO sessoes (id, usuario_id, perfil) '
            'VALUES (coalesce(%s, gen_random_uuid()::text), %s, %s) '
            'RETURNING id, usuario_id, perfil, criada_em, encerrada_em, 0',
            (sessao_id, usuario_id, perfil_do_usuario(usuario_id)),
        ).fetchone()
    return _sessao(row)


def obter_sessao(conn: psycopg.Connection, sessao_id: str) -> Sessao | None:
    row = conn.execute(
        f'SELECT {_COLUNAS_SESSAO} FROM sessoes s WHERE s.id = %s', (sessao_id,)
    ).fetchone()
    return _sessao(row) if row else None


def sessao_ativa(conn: psycopg.Connection, usuario_id: str) -> Sessao | None:
    row = conn.execute(
        f'SELECT {_COLUNAS_SESSAO} FROM sessoes s '
        'WHERE s.usuario_id = %s AND s.encerrada_em IS NULL '
        'ORDER BY s.criada_em DESC LIMIT 1',
        (usuario_id,),
    ).fetchone()
    return _sessao(row) if row else None


def obter_ou_criar_sessao_ativa(conn: psycopg.Connection, usuario_id: str) -> Sessao:
    """Retoma a sessão ativa do usuário (ex.: após recarregar a página) ou abre uma nova."""
    return sessao_ativa(conn, usuario_id) or criar_sessao(conn, usuario_id)


def encerrar_sessao(conn: psycopg.Connection, sessao_id: str) -> None:
    with conn.transaction():
        conn.execute(
            'UPDATE sessoes SET encerrada_em = now() WHERE id = %s AND encerrada_em IS NULL',
            (sessao_id,),
        )


def nova_sessao(conn: psycopg.Connection, sessao_id: str, usuario_id: str) -> Sessao:
    """Encerra a sessão atual e abre outra para o mesmo usuário."""
    encerrar_sessao(conn, sessao_id)
    return criar_sessao(conn, usuario_id)


def listar_sessoes_encerradas(
    conn: psycopg.Connection, usuario_id: str, limite: int = 10
) -> list[Sessao]:
    """Sessões anteriores do usuário que tiveram ao menos uma mensagem (mais recentes primeiro)."""
    rows = conn.execute(
        f'SELECT {_COLUNAS_SESSAO} FROM sessoes s '
        'WHERE s.usuario_id = %s AND s.encerrada_em IS NOT NULL '
        'AND EXISTS (SELECT 1 FROM mensagens m WHERE m.sessao_id = s.id) '
        'ORDER BY s.encerrada_em DESC LIMIT %s',
        (usuario_id, limite),
    ).fetchall()
    return [_sessao(r) for r in rows]


def registrar_turno(
    conn: psycopg.Connection, sessao_id: str, mensagem: str, turno: RespostaTurno
) -> None:
    """Etapa 5b: grava a pergunta e a resposta do turno, inclusive retornos antecipados e
    falha segura, para o histórico exibido na interface ficar coerente."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                'INSERT INTO mensagens (sessao_id, papel, conteudo) VALUES (%s, %s, %s)',
                [
                    (sessao_id, 'usuario', Jsonb({'texto': mensagem})),
                    (sessao_id, 'assistente', Jsonb(turno.model_dump(mode='json'))),
                ],
            )


def _mensagem(row: Sequence[Any]) -> Mensagem:
    id_, papel, conteudo, criada_em = row
    if papel == 'assistente':
        turno = RespostaTurno.model_validate(conteudo)
        return Mensagem(id=id_, papel=papel, texto=turno.texto, criada_em=criada_em, turno=turno)
    return Mensagem(id=id_, papel=papel, texto=conteudo.get('texto', ''), criada_em=criada_em)


def listar_mensagens(conn: psycopg.Connection, sessao_id: str) -> list[Mensagem]:
    rows = conn.execute(
        'SELECT id, papel, conteudo, criada_em FROM mensagens WHERE sessao_id = %s ORDER BY id',
        (sessao_id,),
    ).fetchall()
    return [_mensagem(r) for r in rows]


def contar_turnos(conn: psycopg.Connection, sessao_id: str) -> int:
    """Quantidade de mensagens do usuário na sessão (base da cota por sessão do guardrail 2c)."""
    return conn.execute(
        "SELECT count(*) FROM mensagens WHERE sessao_id = %s AND papel = 'usuario'",
        (sessao_id,),
    ).fetchone()[0]


def turno_entra_na_memoria(turno: RespostaTurno) -> bool:
    """Só respostas de fato (do modelo ou do cache) voltam ao prompt; mensagens barradas pelo
    guardrail (ex.: prompt injection) e falhas seguras ficam só no histórico exibido."""
    return not (turno.falha_segura or turno.bloqueado_guardrail)


def montar_historico(
    mensagens: Sequence[Mensagem], max_turnos: int, mascarador: Any | None = None
) -> list[ModelMessage]:
    """Converte os pares pergunta/resposta em `ModelRequest`/`ModelResponse` do PydanticAI."""
    if max_turnos <= 0:
        return []
    mascarar = (mascarador or Mascarador()).mascarar

    pares: list[tuple[str, str]] = []
    pergunta: str | None = None
    for msg in mensagens:
        if msg.papel == 'usuario':
            pergunta = msg.texto
        elif pergunta is not None and msg.turno is not None:
            if turno_entra_na_memoria(msg.turno):
                pares.append((pergunta, msg.turno.texto))
            pergunta = None

    historico: list[ModelMessage] = []
    for pergunta, resposta in pares[-max_turnos:]:
        historico.append(ModelRequest(parts=[UserPromptPart(content=mascarar(pergunta))]))
        historico.append(ModelResponse(parts=[TextPart(content=mascarar(resposta))]))
    return historico


def carregar_historico(
    conn: psycopg.Connection, sessao_id: str, max_turnos: int | None = None
) -> list[ModelMessage]:
    """Etapa 1: memória da sessão no formato do `message_history` do `processar_turno`."""
    limite = max_turnos_memoria() if max_turnos is None else max_turnos
    return montar_historico(listar_mensagens(conn, sessao_id), limite)
