"""Relatório somente leitura de uma sessão de chat, em Markdown (#38).

Funcionalidade de backend, sem Streamlit e sem LLM: lê `mensagens` (#31) e
`auditoria_mensagens` (#35) da sessão e monta o relatório de forma determinística, com
perguntas, respostas, fontes citadas, flags, rascunhos, relatórios gerados e a
rastreabilidade de cada turno (ferramentas, regras, cache, modelo, tokens e `trace_id`).

O relatório não volta para o contexto do assistente: "Nova sessão" encerra a sessão e a
memória (`carregar_historico`) só lê a sessão ativa.

Dados pessoais conforme o perfil (#33):
- `funcionario` (operador): perguntas e respostas como apareceram na tela, com a PII
  restaurada na etapa 4c.
- `produtor`: perguntas e respostas passam pelo `Mascarador` (`[CPF_1]`, `[PIX_1]`…), já que
  o arquivo sai do sistema e pode ser encaminhado; o produtor consulta os próprios dados no chat.
- Rascunhos e relatórios gerados ficam sempre mascarados, como saem do runner, e os metadados
  da auditoria já são gravados mascarados.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
import re
import unicodedata
from zoneinfo import ZoneInfo

import psycopg

from sabor.agent.deps import obter_data_referencia
from sabor.agent.mascarador import Mascarador
from sabor.agent.memoria import Mensagem, Sessao, listar_mensagens, obter_sessao
from sabor.agent.schemas import RespostaTurno

FUSO = ZoneInfo('America/Recife')

_ROTULO_PERFIL = {'funcionario': 'funcionário(a)', 'produtor': 'produtor(a)'}

TITULOS_RETORNO: dict[str, str] = {
    'FORA_DE_ESCOPO': 'Fora de escopo',
    'PROMPT_INJECTION': 'Mensagem bloqueada',
    'ENTRADA_VAZIA': 'Mensagem vazia',
    'TAMANHO_EXCEDIDO': 'Mensagem longa demais',
    'CACHE_HIT': 'Resposta do cache',
    'COTA_SESSAO_ESGOTADA': 'Cota da sessão esgotada',
    'ORCAMENTO_TURNO_ESGOTADO': 'Limite do turno atingido',
}
"""Mesmos títulos dos destaques da interface (`sabor.ui.apresentacao.ROTULOS_RETORNO`)."""


@dataclass(frozen=True)
class AuditoriaTurno:
    """Linha de `auditoria_mensagens` ligada à mensagem do assistente (#35)."""

    message_id: int
    modelo: str | None = None
    provedor: str | None = None
    ferramentas: list[str] = field(default_factory=list)
    regras_disparadas: list[str] = field(default_factory=list)
    retorno_antecipado: str | None = None
    cache_hit: bool = False
    tokens_entrada: int | None = None
    tokens_saida: int | None = None
    latencia_ms: int | None = None
    trace_id: str | None = None

    @classmethod
    def do_turno(cls, message_id: int, turno: RespostaTurno) -> 'AuditoriaTurno':
        """Fallback para mensagens sem auditoria (ex.: gravadas antes da #35)."""
        retorno = turno.retorno_antecipado
        return cls(
            message_id=message_id,
            modelo=turno.modelo_efetivo,
            provedor=turno.provedor_efetivo,
            ferramentas=list(turno.ferramentas),
            regras_disparadas=list(turno.regras_disparadas),
            retorno_antecipado=retorno.motivo if retorno else None,
            cache_hit=turno.cache_hit,
            tokens_entrada=turno.tokens_entrada,
            tokens_saida=turno.tokens_saida,
            latencia_ms=turno.latencia_ms or None,
            trace_id=turno.trace_id,
        )


@dataclass(frozen=True)
class Turno:
    pergunta: Mensagem | None
    resposta: Mensagem | None
    auditoria: AuditoriaTurno | None

    @property
    def momento(self) -> datetime | None:
        origem = self.pergunta or self.resposta
        return origem.criada_em if origem else None


def listar_auditoria(conn: psycopg.Connection, sessao_id: str) -> dict[int, AuditoriaTurno]:
    """Auditoria da sessão indexada pelo `message_id` da resposta do assistente."""
    rows = conn.execute(
        'SELECT message_id, modelo, provedor, ferramentas, regras_disparadas, '
        'retorno_antecipado, cache_hit, tokens_entrada, tokens_saida, latencia_ms, trace_id '
        'FROM auditoria_mensagens WHERE sessao_id = %s AND message_id IS NOT NULL ORDER BY id',
        (sessao_id,),
    ).fetchall()
    return {
        int(r[0]): AuditoriaTurno(
            message_id=int(r[0]),
            modelo=r[1],
            provedor=r[2],
            ferramentas=[str(f) for f in r[3] or []],
            regras_disparadas=[str(g) for g in r[4] or []],
            retorno_antecipado=r[5],
            cache_hit=bool(r[6]),
            tokens_entrada=r[7],
            tokens_saida=r[8],
            latencia_ms=r[9],
            trace_id=r[10],
        )
        for r in rows
    }


def agrupar_turnos(
    mensagens: Sequence[Mensagem], auditoria: Mapping[int, AuditoriaTurno] | None = None
) -> list[Turno]:
    """Pareia pergunta e resposta; perguntas sem resposta viram turnos sem resposta."""
    auditoria = auditoria or {}
    turnos: list[Turno] = []
    pergunta: Mensagem | None = None
    for msg in mensagens:
        if msg.papel == 'usuario':
            if pergunta is not None:
                turnos.append(Turno(pergunta, None, None))
            pergunta = msg
            continue
        audit = auditoria.get(msg.id)
        if audit is None and msg.turno is not None:
            audit = AuditoriaTurno.do_turno(msg.id, msg.turno)
        turnos.append(Turno(pergunta, msg, audit))
        pergunta = None
    if pergunta is not None:
        turnos.append(Turno(pergunta, None, None))
    return turnos


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


def _citacao(texto: str) -> list[str]:
    return [f'> {ln}'.rstrip() for ln in (texto.strip() or '(vazia)').splitlines()]


def titulo_do_turno(turno: RespostaTurno) -> str | None:
    """`None` para respostas normais do modelo."""
    if turno.falha_segura:
        return 'Falha segura'
    if turno.retorno_antecipado is not None:
        return TITULOS_RETORNO.get(turno.retorno_antecipado.motivo, 'Retorno antecipado')
    return None


def _rastreabilidade(audit: AuditoriaTurno) -> list[str]:
    itens = []
    if audit.ferramentas:
        itens.append('ferramentas: ' + ', '.join(f'`{f}`' for f in audit.ferramentas))
    if audit.regras_disparadas:
        itens.append('regras: ' + ', '.join(f'`{r}`' for r in audit.regras_disparadas))
    if audit.cache_hit:
        itens.append('resposta do cache')
    if audit.retorno_antecipado:
        itens.append(f'retorno antecipado `{audit.retorno_antecipado}`')
    if audit.modelo:
        itens.append(
            f'modelo {audit.modelo}' + (f' ({audit.provedor})' if audit.provedor else '')
        )
    if audit.tokens_entrada is not None or audit.tokens_saida is not None:
        itens.append(f'tokens {audit.tokens_entrada or 0} in / {audit.tokens_saida or 0} out')
    if audit.latencia_ms:
        itens.append(f'{audit.latencia_ms} ms')
    if audit.trace_id:
        itens.append(f'trace `{audit.trace_id}`')
    return [f'<sub>{" · ".join(itens)}</sub>', ''] if itens else []


def _secao_turno(numero: int, turno: Turno, ajustar: Callable[[str], str]) -> list[str]:
    linhas = [f'## Turno {numero} · {_hora(turno.momento, "%H:%M")}', '']
    if turno.pergunta is not None:
        linhas += ['**Pergunta**', '', *_citacao(ajustar(turno.pergunta.texto)), '']
    resposta = turno.resposta.turno if turno.resposta else None
    if resposta is None:
        linhas += ['_Sem resposta registrada._', '']
        return linhas

    linhas += ['**Resposta**', '']
    if titulo := titulo_do_turno(resposta):
        linhas += [f'_{titulo}._', '']
    linhas += [ajustar(resposta.texto).strip(), '']

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
    if turno.auditoria is not None:
        linhas += _rastreabilidade(turno.auditoria)
    return linhas


def _resumo(turnos: Sequence[Turno]) -> list[str]:
    respostas = [t.resposta.turno for t in turnos if t.resposta and t.resposta.turno]
    auditorias = [t.auditoria for t in turnos if t.auditoria is not None]
    ferramentas = Counter(f for a in auditorias for f in a.ferramentas)
    fontes = {f.formatar() for r in respostas for f in r.fontes}
    tokens_in = sum(a.tokens_entrada or 0 for a in auditorias)
    tokens_out = sum(a.tokens_saida or 0 for a in auditorias)

    linhas = [
        '## Resumo',
        '',
        f'- **Perguntas:** {sum(t.pergunta is not None for t in turnos)}',
        f'- **Fontes citadas (distintas):** {len(fontes)}',
        f'- **Sinalizações:** {sum(len(r.flags) for r in respostas)}',
        f'- **Rascunhos gerados:** {sum(len(r.rascunhos) for r in respostas)}',
        f'- **Relatórios gerados:** {sum(len(r.relatorios) for r in respostas)}',
        f'- **Respostas do cache:** {sum(a.cache_hit for a in auditorias)}',
        f'- **Retornos antecipados / falhas seguras:** '
        f'{sum(r.retorno_antecipado is not None for r in respostas)} / '
        f'{sum(r.falha_segura for r in respostas)}',
        f'- **Tokens:** {tokens_in} in / {tokens_out} out',
    ]
    if ferramentas:
        uso = ', '.join(f'`{nome}` ({n}x)' for nome, n in ferramentas.most_common())
        linhas.append(f'- **Ferramentas usadas:** {uso}')
    return [*linhas, '']


def mascara_pii_no_relatorio(perfil: str) -> bool:
    """Só a equipe (operador) recebe perguntas e respostas com dados pessoais em claro (#33)."""
    return perfil != 'funcionario'


def gerar_relatorio(
    sessao: Sessao,
    mensagens: Sequence[Mensagem],
    auditoria: Mapping[int, AuditoriaTurno] | None = None,
    *,
    data_referencia: date | None = None,
) -> str:
    """Monta o Markdown do relatório; não chama LLM nem grava nada."""
    data_ref = data_referencia or obter_data_referencia()
    perfil = _ROTULO_PERFIL.get(sessao.perfil, sessao.perfil)
    turnos = agrupar_turnos(mensagens, auditoria)

    mascarar = mascara_pii_no_relatorio(sessao.perfil)
    ajustar = Mascarador().mascarar if mascarar else (lambda texto: texto)
    aviso_pii = (
        'Dados pessoais (CPF, RG, telefone, e-mail, dados bancários e PIX) aparecem mascarados '
        'em todo o relatório.'
        if mascarar
        else 'Perguntas e respostas aparecem como foram exibidas à equipe; rascunhos e '
        'relatórios gerados mantêm dados pessoais mascarados e devem ser revisados antes do envio.'
    )

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
        '> Relatório somente leitura, montado sem IA a partir das mensagens e da auditoria '
        'salvas da sessão. Esta conversa não volta para o contexto do assistente. '
        f'{aviso_pii}',
        '',
    ]
    if not turnos:
        return '\n'.join([*linhas, '_Nenhuma mensagem nesta sessão._']) + '\n'

    linhas += _resumo(turnos)
    for numero, turno in enumerate(turnos, start=1):
        linhas += _secao_turno(numero, turno, ajustar)
    return '\n'.join(linhas).rstrip() + '\n'


def nome_arquivo_relatorio(sessao: Sessao) -> str:
    ascii_ = unicodedata.normalize('NFKD', sessao.usuario_id).encode('ascii', 'ignore').decode()
    usuario = re.sub(r'[^A-Za-z0-9]+', '-', ascii_).strip('-').lower() or 'usuario'
    return f'relatorio_{usuario}_{_hora(sessao.criada_em, "%Y%m%d-%H%M")}.md'


def relatorio_da_sessao(conn: psycopg.Connection, sessao_id: str) -> tuple[str, str]:
    """Retorna `(nome_do_arquivo, markdown)` da sessão. Só lê o banco."""
    sessao = obter_sessao(conn, sessao_id)
    if sessao is None:
        raise LookupError(f'Sessão {sessao_id} não encontrada.')
    markdown = gerar_relatorio(
        sessao, listar_mensagens(conn, sessao_id), listar_auditoria(conn, sessao_id)
    )
    return nome_arquivo_relatorio(sessao), markdown
