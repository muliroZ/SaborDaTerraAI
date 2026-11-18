"""Relatório somente leitura de uma sessão de chat, em Markdown (#38).

Montagem determinística (sem LLM) a partir das `mensagens` gravadas pela interface (#13):
perguntas, respostas, fontes citadas, flags e rascunhos de cada turno. As respostas trazem o
texto que o próprio usuário viu na tela; os rascunhos continuam com os dados pessoais
mascarados (#33).
"""

from collections.abc import Sequence
from datetime import date, datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

import psycopg

from sabor.agent.deps import obter_data_referencia
from sabor.agent.schemas import RespostaTurno
from sabor.apresentacao import destaque_do_turno
from sabor.sessao import Mensagem, Sessao, listar_mensagens, obter_sessao

FUSO = ZoneInfo('America/Recife')

_ROTULO_PERFIL = {'funcionario': 'funcionário(a)', 'produtor': 'produtor(a)'}


def _hora(momento: datetime | None, formato: str = '%d/%m/%Y %H:%M') -> str:
    if momento is None:
        return 'em andamento'
    if momento.tzinfo is not None:
        momento = momento.astimezone(FUSO)
    return momento.strftime(formato)


def _bloco_texto(texto: str) -> str:
    """Bloco cercado que não quebra se o texto tiver crases."""
    maior = max((len(m) for m in re.findall(r'`+', texto)), default=0)
    cerca = '`' * max(3, maior + 1)
    return f'{cerca}text\n{texto.strip()}\n{cerca}'


def _secao_turno(
    numero: int, pergunta: Mensagem | None, resposta: RespostaTurno | None, momento: datetime | None
) -> list[str]:
    linhas = [f'## Turno {numero} · {_hora(momento, "%H:%M")}', '']
    if pergunta is not None:
        linhas += ['**Pergunta**', '', *[f'> {ln}' for ln in pergunta.texto.splitlines()], '']
    if resposta is None:
        linhas += ['_Sem resposta registrada._', '']
        return linhas

    destaque = destaque_do_turno(resposta)
    linhas += ['**Resposta**', '']
    if destaque is not None:
        linhas += [f'_{destaque.titulo}._', '']
    linhas += [resposta.texto.strip(), '']

    if resposta.fontes:
        linhas += ['**Fontes**', '', *[f'- `{f.formatar()}`' for f in resposta.fontes], '']
    if resposta.flags:
        linhas += ['**Sinalizações**', '']
        linhas += [f'- **{f.tipo.replace("_", " ")}:** {f.descricao}' for f in resposta.flags]
        linhas.append('')
    if resposta.relatorios:
        linhas += ['**Relatórios gerados**', '']
        linhas += [f'- {r.titulo} (`{r.nome_arquivo}`)' for r in resposta.relatorios]
        linhas.append('')
    if resposta.rascunhos:
        linhas += ['**Rascunhos**', '']
        for r in resposta.rascunhos:
            linhas += [f'Para **{r.destinatario}** ({r.canal}):', '', _bloco_texto(r.conteudo), '']

    meta = []
    if resposta.modelo_efetivo:
        meta.append(f'modelo {resposta.modelo_efetivo}')
    if resposta.latencia_ms:
        meta.append(f'{resposta.latencia_ms} ms')
    if meta:
        linhas += [f'<sub>{" · ".join(meta)}</sub>', '']
    return linhas


def gerar_relatorio(
    sessao: Sessao, mensagens: Sequence[Mensagem], *, data_referencia: date | None = None
) -> str:
    data_ref = data_referencia or obter_data_referencia()
    perfil = _ROTULO_PERFIL.get(sessao.perfil, sessao.perfil)

    turnos: list[tuple[Mensagem | None, RespostaTurno | None, datetime | None]] = []
    pergunta: Mensagem | None = None
    for msg in mensagens:
        if msg.papel == 'usuario':
            if pergunta is not None:
                turnos.append((pergunta, None, pergunta.criada_em))
            pergunta = msg
        else:
            momento = pergunta.criada_em if pergunta else msg.criada_em
            turnos.append((pergunta, msg.turno, momento))
            pergunta = None
    if pergunta is not None:
        turnos.append((pergunta, None, pergunta.criada_em))

    linhas = [
        '# Relatório da conversa · Cooperativa Sabor da Terra',
        '',
        f'- **Usuário:** {sessao.usuario_id} ({perfil})',
        f'- **Início:** {_hora(sessao.criada_em)}',
        f'- **Encerramento:** {_hora(sessao.encerrada_em)}',
        f'- **Data de referência dos dados:** {data_ref.strftime("%d/%m/%Y")}',
        f'- **Turnos:** {len(turnos)}',
        f'- **Sessão:** `{sessao.id}`',
        '',
        '> Relatório somente leitura, montado sem IA a partir do histórico salvo da sessão. '
        'Esta conversa não volta para o contexto do assistente. Rascunhos mantêm dados '
        'pessoais mascarados e devem ser revisados antes do envio.',
        '',
    ]
    if not turnos:
        linhas += ['_Nenhuma mensagem nesta sessão._', '']
    for numero, (perg, resp, momento) in enumerate(turnos, start=1):
        linhas += _secao_turno(numero, perg, resp, momento)
    return '\n'.join(linhas).rstrip() + '\n'


def nome_arquivo_relatorio(sessao: Sessao) -> str:
    ascii_ = unicodedata.normalize('NFKD', sessao.usuario_id).encode('ascii', 'ignore').decode()
    usuario = re.sub(r'[^A-Za-z0-9]+', '-', ascii_).strip('-').lower() or 'usuario'
    return f'relatorio_{usuario}_{_hora(sessao.criada_em, "%Y%m%d-%H%M")}.md'


def relatorio_da_sessao(conn: psycopg.Connection, sessao_id: str) -> tuple[str, str]:
    """Retorna `(nome_do_arquivo, markdown)` da sessão."""
    sessao = obter_sessao(conn, sessao_id)
    if sessao is None:
        raise LookupError(f'Sessão {sessao_id} não encontrada.')
    markdown = gerar_relatorio(sessao, listar_mensagens(conn, sessao_id))
    return nome_arquivo_relatorio(sessao), markdown
