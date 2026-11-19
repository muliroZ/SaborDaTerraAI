"""Regras de apresentação da interface Streamlit (#13), sem dependência do Streamlit.

Fica separado de `app.py` para ser testável: usuários simulados, sugestões da demo,
destaque dos retornos antecipados, badges das flags e o trecho exibido para cada fonte.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal
import unicodedata

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from sabor.agent.mascarador import Mascarador
from sabor.agent.schemas import Flag, Fonte, RespostaTurno
from sabor.tools.base import _resolver_usuario

# ---------------------------------------------------------------------------
# Usuários simulados (decisão de 16/11: substitui a autenticação no protótipo)
# ---------------------------------------------------------------------------

FUNCIONARIOS = ('Dona Lúcia', 'Seu Antônio')
"""Equipe da cooperativa citada no enunciado (perfil `funcionario`)."""

PRODUTORES_IDS = tuple(f'P{n:03d}' for n in range(1, 19))
"""IDs dos 18 produtores de `produtores.csv` (perfil `produtor`)."""

USUARIOS_SIMULADOS = FUNCIONARIOS + PRODUTORES_IDS


ROTULOS_URGENCIA = {'critica': 'Crítica', 'alta': 'Alta', 'normal': 'Normal', 'baixa': 'Baixa'}
"""Rótulos da urgência da triagem (#10, #14)."""


def eh_produtor(usuario_id: str) -> bool:
    return _resolver_usuario(usuario_id).perfil == 'produtor'


def rotulo_usuario(usuario_id: str, nomes_produtores: Mapping[str, str] | None = None) -> str:
    """Texto do seletor: `Dona Lúcia · funcionária` ou `P001 · Maria das Graças · produtor`."""
    if not eh_produtor(usuario_id):
        return f'{usuario_id} · equipe da cooperativa'
    nome = (nomes_produtores or {}).get(usuario_id)
    return f'{usuario_id} · {nome} · produtor' if nome else f'{usuario_id} · produtor'


SUGESTOES_FUNCIONARIO = (
    'Quais produtores têm certificação orgânica vigente para mel?',
    'Qual a origem e a rastreabilidade do queijo do lote 2024-089?',
    'Quem a SuperBom está devendo e há quanto tempo?',
    'Posso mandar frango caipira para o Hotel Fazenda?',
    'Resuma a situação da Maria das Graças e rascunhe uma mensagem para ela.',
    'Tem dados sobre o PAA?',
)
"""Perguntas de referência de `docs/projeto.md` usadas na demonstração."""

SUGESTOES_PRODUTOR = (
    'Quanto eu vendi em setembro e o que tenho a receber?',
    'Tenho pedidos em aberto que eu possa atender?',
    'Minha certificação e meus documentos estão em dia?',
    'Quanto a Rede SuperBom está devendo para o P003?',
)
"""A última pergunta demonstra o bloqueio de dados de terceiros para o perfil produtor."""


def sugestoes_para(usuario_id: str) -> tuple[str, ...]:
    return SUGESTOES_PRODUTOR if eh_produtor(usuario_id) else SUGESTOES_FUNCIONARIO


# ---------------------------------------------------------------------------
# Retornos antecipados e falha segura: exibidos de forma distinta da resposta normal
# ---------------------------------------------------------------------------

Nivel = Literal['info', 'success', 'warning', 'error']


@dataclass(frozen=True)
class Destaque:
    nivel: Nivel
    titulo: str
    icone: str
    substitui_resposta: bool = True
    """Se `True`, a mensagem do turno vai dentro da caixa; se `False` (cache hit), a resposta é
    exibida normalmente e o destaque vira só um selo."""


ROTULOS_RETORNO: dict[str, Destaque] = {
    'FORA_DE_ESCOPO': Destaque('info', 'Fora de escopo', ':material/explore_off:'),
    'PROMPT_INJECTION': Destaque('warning', 'Mensagem bloqueada', ':material/gpp_bad:'),
    'ENTRADA_VAZIA': Destaque('info', 'Mensagem vazia', ':material/edit_off:'),
    'TAMANHO_EXCEDIDO': Destaque('warning', 'Mensagem longa demais', ':material/short_text:'),
    'CACHE_HIT': Destaque('success', 'Resposta do cache', ':material/bolt:', False),
    'COTA_SESSAO_ESGOTADA': Destaque('warning', 'Cota da sessão esgotada', ':material/block:'),
    'ORCAMENTO_TURNO_ESGOTADO': Destaque(
        'warning', 'Limite do turno atingido', ':material/hourglass_disabled:'
    ),
}

DESTAQUE_FALHA_SEGURA = Destaque('error', 'Falha segura', ':material/cloud_off:')


def destaque_do_turno(turno: RespostaTurno) -> Destaque | None:
    """`None` para respostas normais do modelo."""
    if turno.falha_segura:
        return DESTAQUE_FALHA_SEGURA
    if turno.retorno_antecipado is not None:
        return ROTULOS_RETORNO.get(
            turno.retorno_antecipado.motivo,
            Destaque('warning', 'Retorno antecipado', ':material/info:'),
        )
    return None


# ---------------------------------------------------------------------------
# Flags: badges de bloqueio, urgência, ambiguidade e privacidade
# ---------------------------------------------------------------------------

CorBadge = Literal['red', 'orange', 'yellow', 'blue', 'green', 'violet', 'gray']


@dataclass(frozen=True)
class EstiloFlag:
    cor: CorBadge
    icone: str
    rotulo: str


_ESTILOS_FLAG: tuple[tuple[tuple[str, ...], CorBadge, str], ...] = (
    # Bloqueios: não enviar / não vender sem resolver.
    (
        ('bloque', 'conformidade', 'sanitar', 'suspens', 'sie', 'vencid', 'inadimpl', 'credito'),
        'red',
        ':material/block:',
    ),
    # Urgência e riscos de atendimento.
    (
        ('urgen', 'prazo', 'safra', 'capacidade', 'estoque', 'atraso'),
        'orange',
        ':material/schedule:',
    ),
    (('ambigu', 'confirm'), 'blue', ':material/help:'),
    (('privacidade', 'lgpd', 'pessoal', 'pii'), 'violet', ':material/lock:'),
)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()
    return sem_acento.lower()


def estilo_flag(flag: Flag) -> EstiloFlag:
    tipo = _normalizar(flag.tipo)
    rotulo = flag.tipo.replace('_', ' ').strip().capitalize() or 'Alerta'
    for chaves, cor, icone in _ESTILOS_FLAG:
        if any(chave in tipo for chave in chaves):
            return EstiloFlag(cor, icone, rotulo)
    return EstiloFlag('gray', ':material/flag:', rotulo)


def badge_markdown(flag: Flag) -> str:
    """Badge na sintaxe de Markdown do Streamlit (`:red-badge[:material/block: Conformidade]`)."""
    estilo = estilo_flag(flag)
    rotulo = estilo.rotulo.replace('[', '(').replace(']', ')')
    return f':{estilo.cor}-badge[{estilo.icone} {rotulo}]'


# ---------------------------------------------------------------------------
# Trecho de cada fonte citada (expander "Fontes")
# ---------------------------------------------------------------------------

MAX_CHARS_TRECHO = 1200

_TABELAS_FONTE: dict[str, tuple[str, str]] = {
    'produtores': ('produtores', 'id'),
    'financeiro': ('financeiro', 'id_transacao'),
    'lotes': ('lotes', 'lote_id'),
    'pedidos': ('pedidos', 'id'),
}
"""Arquivo tabular citado → (tabela, chave) para recuperar o registro."""

_COLUNAS_INTERNAS = frozenset({'extracao', 'produto_id'})
"""Colunas técnicas que não ajudam a ler a fonte."""

_COLUNAS_PESSOAIS = frozenset(
    {
        'cpf',
        'data_nascimento',
        'telefone',
        'email',
        'endereco',
        'chave_pix',
        'chave_pix_deposito',
        'banco',
        'agencia',
        'conta_corrente',
    }
)
"""Dados pessoais e bancários. A equipe interna pode vê-los (docs/README.md, LGPD), como já
vê no texto da resposta restaurado na etapa 4c; para o perfil produtor ficam ocultos."""


def _truncar(texto: str, limite: int = MAX_CHARS_TRECHO) -> str:
    texto = texto.strip()
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + '…'


def _pode_ver(usuario_id: str, produtor_do_registro: str | None) -> bool:
    """Perfil produtor só vê trechos sem dono ou do próprio produtor (RBAC das ferramentas)."""
    usuario = _resolver_usuario(usuario_id)
    if usuario.perfil == 'funcionario' or produtor_do_registro is None:
        return True
    return produtor_do_registro == usuario.produtor_id


def _formatar_registro(registro: Mapping[str, Any], *, ocultar_pessoais: bool) -> str:
    ocultas = _COLUNAS_INTERNAS | (_COLUNAS_PESSOAIS if ocultar_pessoais else frozenset())
    linhas = []
    for coluna, valor in registro.items():
        if coluna in ocultas or valor is None or valor == [] or valor == '':
            continue
        if isinstance(valor, bool):
            valor = 'sim' if valor else 'não'
        linhas.append(f'{coluna}: {valor}')
    return '\n'.join(linhas)


def _trecho_documento(conn: psycopg.Connection, fonte: Fonte, usuario_id: str) -> str | None:
    arquivo = fonte.arquivo.strip()
    referencia = fonte.referencia.strip()
    rows = conn.execute(
        'SELECT arquivo, secao, texto, produtor_id FROM chunks_busca '
        'WHERE arquivo = %s OR arquivo LIKE %s ORDER BY (arquivo = %s) DESC, arquivo, ordem',
        (arquivo, f'%/{arquivo}', arquivo),
    ).fetchall()
    if not rows:
        return None
    alvo = _normalizar(referencia)
    escolhidos = [r for r in rows if alvo and r[1] and alvo in _normalizar(r[1])]
    if not escolhidos:
        escolhidos = [r for r in rows if r[0] == rows[0][0]][:1]
    if not _pode_ver(usuario_id, escolhidos[0][3]):
        return None
    return '\n\n'.join(r[2] for r in escolhidos if r[0] == escolhidos[0][0])


def _trecho_tabular(conn: psycopg.Connection, fonte: Fonte, usuario_id: str) -> str | None:
    nome = PurePosixPath(fonte.arquivo.strip()).stem.lower()
    if nome not in _TABELAS_FONTE:
        return None
    tabela, chave = _TABELAS_FONTE[nome]
    with conn.cursor(row_factory=dict_row) as cur:
        registro = cur.execute(
            sql.SQL('SELECT * FROM {} WHERE upper({}) = upper(%s)').format(
                sql.Identifier(tabela), sql.Identifier(chave)
            ),
            (fonte.referencia.strip(),),
        ).fetchone()
    if registro is None:
        return None
    dono = registro.get('produtor_id') or (registro.get('id') if tabela == 'produtores' else None)
    if not _pode_ver(usuario_id, dono):
        return None
    return _formatar_registro(registro, ocultar_pessoais=eh_produtor(usuario_id))


def buscar_trecho(conn: psycopg.Connection, fonte: Fonte, usuario_id: str) -> str | None:
    """Trecho do documento (chunk da seção) ou o registro tabular citado.

    Segue a mesma regra do texto da resposta: a equipe interna vê os dados completos; para o
    perfil produtor, dados pessoais são ocultados/mascarados (documentos e pedidos podem citar
    terceiros). `None` quando não há trecho localizável ou o perfil não pode vê-lo.
    """
    try:
        trecho = _trecho_tabular(conn, fonte, usuario_id)
        if trecho is None:
            trecho = _trecho_documento(conn, fonte, usuario_id)
    except psycopg.Error:
        if not conn.autocommit:
            conn.rollback()
        return None
    if not trecho:
        return None
    if eh_produtor(usuario_id):
        trecho = Mascarador().mascarar(trecho)
    return _truncar(trecho)
