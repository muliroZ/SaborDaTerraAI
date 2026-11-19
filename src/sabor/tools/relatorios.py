"""Ferramenta gerar_relatorio (Issue #15): relatórios autônomos sob demanda, em Markdown.

Relatórios pedidos no chat (não confundir com o relatório da conversa, #38):

- `contas_a_receber`: pendências financeiras vencidas × a vencer, por comprador;
- `organicos_vigentes`: produtores com certificação orgânica vigente, por produto;
- `dossie_adagro`: dossiê para a notificação da ADAGRO (PED-2026-014), com o que foi
  pedido, o que existe nos dados e o que falta.

Os relatórios são montados sem LLM, só com as consultas de `consultar_dados` (#7) e
`buscar_documentos` (#8), então herdam o filtro por perfil e o mascaramento de PII. O
Markdown completo vai para `ctx.deps.relatorios` e o runner o anexa ao turno, passando pelas
mesmas etapas de qualquer resposta; o modelo recebe só um resumo com os destaques.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from sabor.agent.deps import AgentDeps
from sabor.agent.schemas import Relatorio
from sabor.tools.base import (
    Fonte,
    SemPermissao,
    _mascarar_opcional,
    _resolver_usuario,
)
from sabor.tools.consultar_dados import (
    ItemFinanceiro,
    ProdutorResultado,
    buscar_produtores,
    consultar_financeiro,
    listar_lotes,
)
from sabor.tools.documentos import (
    DocumentoInventario,
    DocumentoResultado,
    TipoDocumento,
    buscar_documentos,
    listar_documentos,
)

logger = logging.getLogger(__name__)

TipoRelatorio = Literal['contas_a_receber', 'organicos_vigentes', 'dossie_adagro']

PEDIDO_ADAGRO = 'PED-2026-014'
"""Notificação da ADAGRO que pede os documentos para a inspeção de 11 e 12/11/2026."""

DIAS_ALERTA_RENOVACAO = 90
"""Certificados que vencem dentro deste prazo aparecem como atenção à renovação."""

LOTES_POR_PRODUTO = 3
"""A ADAGRO pede a rastreabilidade dos últimos 3 lotes de cada produto."""

MAX_FONTES_RESUMO = 12
"""Fontes devolvidas ao modelo; o arquivo traz todas."""

_PALAVRAS_INSTITUCIONAL = (
    'pnae',
    'prefeitura',
    'municipio de',
    'secretaria municipal',
    'secretaria de estado',
    'governo',
    'fnde',
    'paa',
)

_RE_DATA = r'(\d{2}/\d{2}/\d{4})'
_RE_DATA_INSPECAO = re.compile(r'data da inspe[cç][aã]o:\s*' + _RE_DATA, re.IGNORECASE)
_RE_NC = re.compile(r'(NC-\d{3})\s*\(([^)]+)\)\s*:\s*(.+?)(?=\n\s*NC-\d{3}|\n\s*\n[A-Z]|\Z)', re.S)
_RE_PRODUTOR = re.compile(r'\bP\d{3}\b')
_RE_NEGACAO = re.compile(r'\b(nao|exceto|exclui\w*|excluid\w*|sem certificacao)\b')
"""Linhas da lista de produtos certificados que não afirmam a certificação (texto normalizado)."""
_RE_CITACAO = re.compile(r'\[[^\[\]]+ · [^\[\]]+\]')

MAX_CITACOES_DESTAQUE = 4
"""Fontes coladas em cada destaque, para o modelo citar a fonte certa de cada número."""


# ---------------------------------------------------------------------------
# Contratos
# ---------------------------------------------------------------------------


class RelatorioResultado(BaseModel):
    """O que a tool devolve ao modelo: só os destaques. O arquivo vai anexado ao turno."""

    gerado: bool
    tipo: TipoRelatorio
    titulo: str | None = None
    nome_arquivo: str | None = None
    destaques: list[str] = Field(default_factory=list)
    fontes: list[Fonte] = Field(default_factory=list)
    mensagem: str | None = None


Situacao = Literal['ok', 'parcial', 'falta', 'nao_verificado']

_ROTULO_SITUACAO: dict[Situacao, str] = {
    'ok': '✅ Disponível',
    'parcial': '⚠️ Parcial',
    'falta': '❌ Não encontrado',
    'nao_verificado': '⏳ Não verificado',
}


@dataclass
class _Secao:
    """Um item pedido no dossiê, com o que existe nos dados, pontos de atenção e lacunas."""

    titulo: str
    resumo: str = ''
    """Uma linha para a tabela de situação por item."""
    existe: list[str] = field(default_factory=list)
    atencao: list[str] = field(default_factory=list)
    falta: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    """Linhas de Markdown livres (ex.: uma tabela) exibidas depois das listas."""
    nao_verificado: bool = False

    @property
    def situacao(self) -> Situacao:
        if self.nao_verificado:
            return 'nao_verificado'
        if self.falta:
            return 'parcial' if self.existe else 'falta'
        return 'ok' if self.existe else 'falta'


class _Fontes:
    """Acumula fontes sem repetição, na ordem em que aparecem."""

    def __init__(self) -> None:
        self.itens: list[Fonte] = []
        self._vistas: set[tuple[str, str]] = set()

    def add(self, fonte: Fonte) -> str:
        chave = (fonte.arquivo, fonte.referencia)
        if chave not in self._vistas:
            self._vistas.add(chave)
            self.itens.append(fonte)
        return fonte.formatar()

    def todas(self, fontes: Iterable[Fonte]) -> str:
        return ' '.join(self.add(f) for f in fontes)


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------


def _brl(valor: float) -> str:
    return f'R$ {valor:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def _data(valor: date | None) -> str:
    return valor.strftime('%d/%m/%Y') if valor else '—'


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize('NFD', texto)
    return ''.join(c for c in sem_acento if unicodedata.category(c) != 'Mn').lower()


def _celula(texto: object) -> str:
    return str(texto).replace('|', '\\|').replace('\n', ' ')


def _tabela(cabecalho: list[str], linhas: list[list[object]]) -> list[str]:
    saida = [
        '| ' + ' | '.join(cabecalho) + ' |',
        '|' + '|'.join('---' for _ in cabecalho) + '|',
    ]
    saida += ['| ' + ' | '.join(_celula(c) for c in linha) + ' |' for linha in linhas]
    return saida


def _citar(fontes: Iterable[Fonte], limite: int = MAX_CITACOES_DESTAQUE) -> str:
    """Citações de um destaque, sem repetição; indica quantas ficaram só no arquivo."""
    unicas = list(dict.fromkeys(f.formatar() for f in fontes))
    texto = ' '.join(unicas[:limite])
    if len(unicas) > limite:
        texto += f' (+{len(unicas) - limite} no arquivo)'
    return texto


def _lista(itens: Iterable[str]) -> list[str]:
    return [f'- {item}' for item in itens]


def _cabecalho(titulo: str, ctx: RunContext[AgentDeps], escopo: str | None = None) -> list[str]:
    linhas = [
        f'# {titulo}',
        '',
        '- **Cooperativa:** Sabor da Terra · Garanhuns (PE)',
        f'- **Data de referência dos dados:** {_data(ctx.deps.data_referencia)}',
        f'- **Solicitado por:** {ctx.deps.usuario}',
    ]
    if escopo:
        linhas.append(f'- **Escopo:** {escopo}')
    linhas += [
        '',
        '> Relatório gerado sob demanda pelo assistente, sem IA na montagem: os números vêm '
        'das consultas aos dados e documentos da cooperativa, com a fonte de cada linha entre '
        'colchetes. Dados pessoais ficam mascarados. Revise antes de compartilhar.',
        '',
    ]
    return linhas


def _rodape(fontes: _Fontes) -> list[str]:
    if not fontes.itens:
        return []
    return ['## Fontes consultadas', '', *_lista(f.formatar() for f in fontes.itens), '']


def _nome_arquivo(tipo: str, ctx: RunContext[AgentDeps], sufixo: str | None = None) -> str:
    partes = [tipo, sufixo, ctx.deps.data_referencia.strftime('%Y%m%d')]
    return '_'.join(p for p in partes if p) + '.md'


def _montar(
    tipo: TipoRelatorio, titulo: str, nome_arquivo: str, linhas: list[str], fontes: _Fontes
) -> Relatorio:
    markdown = '\n'.join([*linhas, *_rodape(fontes)]).rstrip() + '\n'
    return Relatorio(
        tipo=tipo,
        titulo=titulo,
        nome_arquivo=nome_arquivo,
        markdown=markdown,
        fontes=list(fontes.itens),
    )


def _escopo_produtor(ctx: RunContext[AgentDeps]) -> str | None:
    usuario = _resolver_usuario(ctx.deps.usuario)
    if usuario.perfil == 'produtor':
        return f'somente dados do produtor {usuario.produtor_id} (perfil produtor)'
    return None


def _buscar_docs(
    ctx: RunContext[AgentDeps],
    pergunta: str,
    tipo_doc: TipoDocumento,
    *,
    produtor_id: str | None = None,
    apenas_vigentes: bool = False,
    k: int = 20,
) -> list[DocumentoResultado] | None:
    """`buscar_documentos` tolerante a falha: `None` quando o acervo não pôde ser consultado
    (ex.: provedor de embeddings fora do ar), para o relatório marcar o item como não verificado
    em vez de afirmar que o documento não existe."""
    try:
        resultado = buscar_documentos(
            ctx,
            pergunta,
            tipo_doc=tipo_doc,
            produtor_id=produtor_id,
            apenas_vigentes=apenas_vigentes,
            k=k,
        )
    except SemPermissao:
        raise
    except Exception:
        logger.warning('Acervo documental indisponível para o relatório', exc_info=True)
        return None
    return resultado.resultados


def _agrupar_por_arquivo(docs: Iterable[DocumentoResultado]) -> dict[str, list[DocumentoResultado]]:
    grupos: dict[str, list[DocumentoResultado]] = defaultdict(list)
    for doc in docs:
        grupos[doc.arquivo].append(doc)
    return dict(grupos)


# ---------------------------------------------------------------------------
# 1. Contas a receber
# ---------------------------------------------------------------------------


def _relatorio_contas_a_receber(ctx: RunContext[AgentDeps]) -> tuple[Relatorio, list[str]]:
    data_ref = ctx.deps.data_referencia
    pendentes = consultar_financeiro(ctx, status='pendente')
    fontes = _Fontes()
    titulo = 'Contas a receber · vencidos × a vencer'
    escopo = _escopo_produtor(ctx)
    usuario = _resolver_usuario(ctx.deps.usuario)
    sufixo = usuario.produtor_id if usuario.perfil == 'produtor' else None
    linhas = _cabecalho(titulo, ctx, escopo)

    if not pendentes.itens:
        linhas += ['_Nenhuma conta a receber em aberto na data de referência._', '']
        destaques = ['Nenhuma conta a receber em aberto.']
        return _montar(
            'contas_a_receber',
            titulo,
            _nome_arquivo('contas_a_receber', ctx, sufixo),
            linhas,
            fontes,
        ), destaques

    vencidos = [i for i in pendentes.itens if i.vencido]
    a_vencer = [i for i in pendentes.itens if not i.vencido]
    total_vencido = round(sum(i.valor_total for i in vencidos), 2)
    total_a_vencer = round(sum(i.valor_total for i in a_vencer), 2)

    por_comprador: dict[str, list[ItemFinanceiro]] = defaultdict(list)
    for item in pendentes.itens:
        por_comprador[item.comprador].append(item)

    def _resumo_comprador(itens: list[ItemFinanceiro]) -> tuple[float, float, int]:
        venc = round(sum(i.valor_total for i in itens if i.vencido), 2)
        aven = round(sum(i.valor_total for i in itens if not i.vencido), 2)
        atraso = max(
            ((data_ref - i.data_vencimento).days for i in itens if i.vencido and i.data_vencimento),
            default=0,
        )
        return venc, aven, atraso

    ordem = sorted(
        por_comprador.items(),
        key=lambda kv: (-_resumo_comprador(kv[1])[0], -sum(i.valor_total for i in kv[1]), kv[0]),
    )

    linhas += [
        '## Resumo',
        '',
        f'- **Vencido:** {_brl(total_vencido)} em {len(vencidos)} transação(ões)',
        f'- **A vencer:** {_brl(total_a_vencer)} em {len(a_vencer)} transação(ões)',
        f'- **Total em aberto:** {_brl(pendentes.total_pendente)}',
        '',
        '## Por comprador',
        '',
    ]
    tabela: list[list[object]] = []
    for comprador, itens in ordem:
        venc, aven, atraso = _resumo_comprador(itens)
        tabela.append(
            [
                comprador,
                _brl(venc) if venc else '—',
                _brl(aven) if aven else '—',
                _brl(venc + aven),
                f'{atraso} dias' if atraso else '—',
                fontes.todas(i.fonte for i in itens),
            ]
        )
    tabela.append(
        [
            '**Total**',
            f'**{_brl(total_vencido)}**',
            f'**{_brl(total_a_vencer)}**',
            f'**{_brl(pendentes.total_pendente)}**',
            '',
            '',
        ]
    )
    linhas += _tabela(
        ['Comprador', 'Vencido', 'A vencer', 'Total', 'Maior atraso', 'Fontes'], tabela
    )
    linhas.append('')

    def _linhas_itens(itens: list[ItemFinanceiro], vencido: bool) -> list[list[object]]:
        saida = []
        for i in sorted(itens, key=lambda x: (x.data_vencimento or date.max, x.id_transacao)):
            if i.data_vencimento is None:
                prazo = 'sem vencimento'
            elif vencido:
                prazo = f'{(data_ref - i.data_vencimento).days} dias de atraso'
            else:
                prazo = f'vence em {(i.data_vencimento - data_ref).days} dias'
            produto = f'{i.produto} ({i.quantidade:g} {i.unidade})'
            if i.alerta_divergencia:
                produto += ' ⚠️ preço divergente da tabela'
            saida.append(
                [
                    fontes.add(i.fonte),
                    i.comprador,
                    i.produtor_id,
                    produto,
                    _brl(i.valor_total),
                    _data(i.data_vencimento),
                    prazo,
                ]
            )
        return saida

    cabecalho_itens = ['Fonte', 'Comprador', 'Produtor', 'Produto', 'Valor', 'Vencimento', 'Prazo']
    linhas += ['## Vencidos', '']
    if vencidos:
        linhas += _tabela(cabecalho_itens, _linhas_itens(vencidos, True))
    else:
        linhas.append('_Nenhuma transação vencida._')
    linhas += ['', '## A vencer', '']
    if a_vencer:
        linhas += _tabela(cabecalho_itens, _linhas_itens(a_vencer, False))
    else:
        linhas.append('_Nenhuma transação a vencer._')
    linhas.append('')

    destaques = [
        f'Vencido: {_brl(total_vencido)} ({len(vencidos)} transações); '
        f'a vencer: {_brl(total_a_vencer)} ({len(a_vencer)} transações); '
        f'total em aberto: {_brl(pendentes.total_pendente)}.',
    ]
    for comprador, itens in ordem[:3]:
        venc, aven, atraso = _resumo_comprador(itens)
        if venc:
            texto = (
                f'{comprador}: {_brl(venc)} vencidos (maior atraso {atraso} dias) '
                f'{_citar(i.fonte for i in itens if i.vencido)}'
            )
            if aven:
                texto += (
                    f'; {_brl(aven)} a vencer {_citar(i.fonte for i in itens if not i.vencido)}'
                )
            destaques.append(texto + '.')
    return _montar(
        'contas_a_receber', titulo, _nome_arquivo('contas_a_receber', ctx, sufixo), linhas, fontes
    ), destaques


# ---------------------------------------------------------------------------
# 2. Produtores com orgânico vigente, por produto
# ---------------------------------------------------------------------------


@dataclass
class _CertificadoProdutor:
    produtor: ProdutorResultado
    documento: DocumentoInventario | None = None
    """Certificado do produtor no inventário do acervo (`None` = não consta)."""
    fonte_certificado: Fonte | None = None
    """Trecho `PRODUTOS CERTIFICADOS` lido para conferir os produtos."""
    confirmados: list[str] = field(default_factory=list)
    nao_confirmados: list[str] = field(default_factory=list)

    @property
    def situacao(self) -> Literal['conferido', 'lista_nao_lida', 'fora_do_inventario']:
        if self.documento is None:
            return 'fora_do_inventario'
        return 'conferido' if self.fonte_certificado is not None else 'lista_nao_lida'

    def motivo_nao_confirmado(self) -> str:
        if self.situacao == 'fora_do_inventario':
            return 'o certificado não consta no inventário do acervo'
        if self.situacao == 'lista_nao_lida':
            return (
                'o certificado consta no inventário, mas não foi possível ler a lista de '
                'produtos certificados'
            )
        return 'não aparece na lista de produtos certificados'

    def citacao_certificado(self, fontes: _Fontes) -> str:
        if self.fonte_certificado is not None:
            return fontes.add(self.fonte_certificado)
        if self.documento is not None:
            return f'{fontes.add(self.documento.fonte)} (produtos não confirmados)'
        return 'não consta no inventário'


@dataclass
class _Organicos:
    vigentes: list[_CertificadoProdutor]
    vencendo: list[ProdutorResultado]
    vencidos: list[ProdutorResultado]
    acervo_consultado: bool
    """A busca no acervo funcionou (o inventário é consultado sempre)."""


def _secoes_produtos_certificados(
    docs: Iterable[DocumentoResultado],
) -> dict[str, list[DocumentoResultado]]:
    """Só os trechos da seção `PRODUTOS CERTIFICADOS` de cada certificado, por produtor.

    Outras seções (observações, auditoria, "produtos que eram certificados") podem citar um
    produto justamente para dizer que ele não está certificado.
    """
    secoes: dict[str, list[DocumentoResultado]] = defaultdict(list)
    for doc in docs:
        if doc.produtor_id and 'produtos certificados' in _normalizar(doc.secao or ''):
            secoes[doc.produtor_id].append(doc)
    return dict(secoes)


def _produto_na_lista(produto: str, trechos: Iterable[DocumentoResultado]) -> bool:
    """O produto aparece, como palavra inteira, numa linha que não o exclui."""
    alvo = re.compile(rf'\b{re.escape(_normalizar(produto))}\b')
    for trecho in trechos:
        for linha in _normalizar(trecho.trecho).splitlines():
            if alvo.search(linha) and not _RE_NEGACAO.search(linha):
                return True
    return False


def _coletar_organicos(ctx: RunContext[AgentDeps]) -> _Organicos:
    """Cruza o cadastro (validade real), o inventário e a lista de produtos de cada certificado."""
    data_ref = ctx.deps.data_referencia
    vigentes = buscar_produtores(ctx, organico=True, ativo=True)
    declarados_vencidos = [
        p
        for p in buscar_produtores(ctx, organico=False)
        if p.certificacao_organica_declarada and not p.certificacao_organica_vigente
    ]
    inventario = {
        d.produtor_id: d for d in listar_documentos(ctx, tipo_doc='certificado') if d.produtor_id
    }
    docs = _buscar_docs(
        ctx,
        'produtos certificados orgânicos',
        'certificado',
        apenas_vigentes=True,
        k=40,
    )
    secoes = _secoes_produtos_certificados(docs or [])

    resultado: list[_CertificadoProdutor] = []
    for produtor in vigentes:
        cert = _CertificadoProdutor(produtor=produtor, documento=inventario.get(produtor.id))
        trechos = secoes.get(produtor.id, []) if cert.documento is not None else []
        if cert.documento is not None and not trechos and docs is not None:
            # A busca ampla nem sempre traz todos: tenta direto no certificado do produtor.
            direcionada = _buscar_docs(
                ctx, 'produtos certificados', 'certificado', produtor_id=produtor.id, k=10
            )
            trechos = _secoes_produtos_certificados(direcionada or []).get(produtor.id, [])
        if trechos:
            cert.fonte_certificado = trechos[0].fonte
        for produto in produtor.produtos:
            if trechos and _produto_na_lista(produto, trechos):
                cert.confirmados.append(produto)
            else:
                cert.nao_confirmados.append(produto)
        resultado.append(cert)

    vencendo = [
        p
        for p in vigentes
        if p.validade_certificacao is not None
        and (p.validade_certificacao - data_ref).days <= DIAS_ALERTA_RENOVACAO
    ]
    return _Organicos(
        vigentes=resultado,
        vencendo=sorted(vencendo, key=lambda p: p.validade_certificacao or date.max),
        vencidos=declarados_vencidos,
        acervo_consultado=docs is not None,
    )


def _rotulo_produtor(p: ProdutorResultado) -> str:
    return f'{p.id} · {p.nome_completo}'


def _linhas_atencao_organicos(org: _Organicos, data_ref: date, fontes: _Fontes) -> list[str]:
    itens = []
    for p in org.vencendo:
        dias = (p.validade_certificacao - data_ref).days  # type: ignore[operator]
        itens.append(
            f'{_rotulo_produtor(p)}: certificação vence em {_data(p.validade_certificacao)} '
            f'({dias} dias); confirmar a renovação. {fontes.add(p.fonte)}'
        )
    for p in org.vencidos:
        itens.append(
            f'{_rotulo_produtor(p)}: cadastro marca certificação orgânica, mas ela venceu em '
            f'{_data(p.validade_certificacao)}; os produtos não podem ser vendidos como '
            f'orgânicos. {fontes.add(p.fonte)}'
        )
    return itens


def _relatorio_organicos(ctx: RunContext[AgentDeps]) -> tuple[Relatorio, list[str]]:
    data_ref = ctx.deps.data_referencia
    org = _coletar_organicos(ctx)
    fontes = _Fontes()
    titulo = 'Produtores com certificação orgânica vigente, por produto'
    usuario = _resolver_usuario(ctx.deps.usuario)
    sufixo = usuario.produtor_id if usuario.perfil == 'produtor' else None
    linhas = _cabecalho(titulo, ctx, _escopo_produtor(ctx))

    if not org.acervo_consultado:
        linhas += [
            '> ⚠️ Não foi possível consultar o texto dos certificados agora. A lista abaixo vem '
            'do cadastro e do inventário do acervo, e nenhum produto foi confirmado no '
            'certificado.',
            '',
        ]

    por_produto: dict[str, list[tuple[_CertificadoProdutor, bool]]] = defaultdict(list)
    for cert in org.vigentes:
        for produto in cert.confirmados:
            por_produto[produto].append((cert, True))
        for produto in cert.nao_confirmados:
            por_produto[produto].append((cert, False))

    produtos_confirmados = sorted(
        {p for cert in org.vigentes for p in cert.confirmados}, key=_normalizar
    )
    linhas += [
        '## Resumo',
        '',
        f'- **Produtores com certificação vigente:** {len(org.vigentes)}',
        f'- **Produtos com certificação confirmada no certificado:** {len(produtos_confirmados)}',
        '',
        '## Por produto',
        '',
    ]
    tabela = []
    for produto in sorted(por_produto, key=_normalizar):
        confirmados = [c for c, ok in por_produto[produto] if ok]
        if not confirmados:
            continue
        produtores = '; '.join(
            f'{_rotulo_produtor(c.produtor)} (até {_data(c.produtor.validade_certificacao)})'
            for c in confirmados
        )
        citacoes = fontes.todas(c.fonte_certificado or c.produtor.fonte for c in confirmados)
        tabela.append([produto, produtores, citacoes])
    if tabela:
        linhas += _tabela(['Produto', 'Produtores (validade)', 'Fontes'], tabela)
    else:
        linhas.append('_Nenhum produto com certificação orgânica confirmada._')
    linhas.append('')

    nao_confirmados = [
        f'{produto}: {_rotulo_produtor(c.produtor)}; {c.motivo_nao_confirmado()}. '
        f'{fontes.add(c.documento.fonte if c.documento else c.produtor.fonte)}'
        for produto in sorted(por_produto, key=_normalizar)
        for c, ok in por_produto[produto]
        if not ok
    ]
    if nao_confirmados:
        linhas += [
            '### Produtos do cadastro sem confirmação no certificado',
            '',
            'Constam no cadastro de produtores com certificação vigente, mas a certificação '
            'deles não foi confirmada no documento. Não ofereça como orgânicos sem conferir.',
            '',
            *_lista(nao_confirmados),
            '',
        ]

    linhas += ['## Por produtor', '']
    tabela_prod = []
    for cert in sorted(org.vigentes, key=lambda c: c.produtor.id):
        p = cert.produtor
        dias = (p.validade_certificacao - data_ref).days if p.validade_certificacao else None
        tabela_prod.append(
            [
                _rotulo_produtor(p),
                p.municipio,
                _data(p.validade_certificacao),
                f'{dias} dias' if dias is not None else '—',
                ', '.join(cert.confirmados) or '—',
                cert.citacao_certificado(fontes),
            ]
        )
    if tabela_prod:
        linhas += _tabela(
            ['Produtor', 'Município', 'Validade', 'Restam', 'Produtos certificados', 'Certificado'],
            tabela_prod,
        )
    else:
        linhas.append('_Nenhum produtor com certificação orgânica vigente._')
    linhas.append('')

    atencao = _linhas_atencao_organicos(org, data_ref, fontes)
    if atencao:
        linhas += ['## Atenção', '', *_lista(atencao), '']

    destaques = [
        f'{len(org.vigentes)} produtor(es) com certificação vigente e '
        f'{len(produtos_confirmados)} produto(s) confirmados no certificado.',
        *[
            f'{_rotulo_produtor(p)} vence em {_data(p.validade_certificacao)} {p.fonte.formatar()}.'
            for p in org.vencendo
        ],
        *[
            f'{_rotulo_produtor(p)}: certificação vencida em {_data(p.validade_certificacao)} '
            f'apesar do cadastro {p.fonte.formatar()}.'
            for p in org.vencidos
        ],
    ]
    if not org.acervo_consultado:
        destaques.append('Acervo de certificados indisponível: lista baseada só no cadastro.')
    return _montar(
        'organicos_vigentes',
        titulo,
        _nome_arquivo('organicos_vigentes', ctx, sufixo),
        linhas,
        fontes,
    ), destaques


# ---------------------------------------------------------------------------
# 3. Dossiê para a ADAGRO (PED-2026-014)
# ---------------------------------------------------------------------------


def _carregar_pedido(ctx: RunContext[AgentDeps], pedido_id: str) -> tuple | None:
    return ctx.deps.conn.execute(
        """
        SELECT id, data_recebimento, canal, remetente, comprador, mensagem_original
        FROM pedidos
        WHERE id = %s
        """,
        (pedido_id,),
    ).fetchone()


def _itens_pedidos(mensagem: str) -> list[str]:
    return [m.strip() for m in re.findall(r'^\s*[-•]\s*(.+)$', mensagem, re.MULTILINE)]


def _secao_cadastro(
    ctx: RunContext[AgentDeps], titulo: str, produtores: list[ProdutorResultado], fontes: _Fontes
) -> _Secao:
    secao = _Secao(titulo)
    ativos = sum(1 for p in produtores if p.status == 'ativo')
    if produtores:
        secao.existe.append(
            f'Cadastro com {len(produtores)} produtores ({ativos} ativos, '
            f'{len(produtores) - ativos} inativo(s)), com nome, CPF, município e produtos. '
            f'{fontes.add(Fonte(arquivo="produtores.csv", referencia="cadastro"))}'
        )
    for p in produtores:
        if not p.cpf:
            secao.falta.append(f'{_rotulo_produtor(p)} sem CPF no cadastro. {fontes.add(p.fonte)}')
        if not p.produtos:
            secao.falta.append(
                f'{_rotulo_produtor(p)} sem produtos cadastrados. {fontes.add(p.fonte)}'
            )
        if p.status != 'ativo':
            secao.atencao.append(
                f'{_rotulo_produtor(p)} está inativo; confirmar se entra no cadastro apresentado. '
                f'{fontes.add(p.fonte)}'
            )
        if p.certificacao_organica_declarada and not p.certificacao_organica_vigente:
            secao.atencao.append(
                f'{_rotulo_produtor(p)}: cadastro marca certificação orgânica vencida em '
                f'{_data(p.validade_certificacao)}; atualizar antes da inspeção. '
                f'{fontes.add(p.fonte)}'
            )
    secao.resumo = f'{len(produtores)} produtores ({ativos} ativos); endereço não verificado'
    secao.atencao.append(
        'O endereço completo não é exposto pelas consultas do assistente: conferir direto no '
        'cadastro antes de apresentar.'
    )
    return secao


def _secao_certificacoes(
    ctx: RunContext[AgentDeps], titulo: str, org: _Organicos, fontes: _Fontes
) -> _Secao:
    secao = _Secao(titulo)
    for cert in sorted(org.vigentes, key=lambda c: c.produtor.id):
        p = cert.produtor
        if cert.situacao == 'fora_do_inventario':
            # Ausência afirmada pelo inventário, não por uma busca sem resultado.
            secao.falta.append(
                f'{_rotulo_produtor(p)}: cadastro indica certificação vigente até '
                f'{_data(p.validade_certificacao)}, mas o certificado não consta no inventário '
                f'do acervo. {fontes.add(p.fonte)}'
            )
            continue
        secao.existe.append(
            f'{_rotulo_produtor(p)}: vigente até {_data(p.validade_certificacao)}. '
            f'{cert.citacao_certificado(fontes)}'
        )
        if cert.situacao == 'lista_nao_lida':
            secao.atencao.append(
                f'{_rotulo_produtor(p)}: não foi possível ler a lista de produtos do certificado; '
                'conferir o documento.'
            )
    secao.atencao += _linhas_atencao_organicos(org, ctx.deps.data_referencia, fontes)
    secao.resumo = (
        f'{len(secao.existe)} vigentes no acervo; {len(org.vencendo)} vencem em até '
        f'{DIAS_ALERTA_RENOVACAO} dias; {len(org.vencidos)} vencida(s) ainda marcada(s) no cadastro'
    )
    if not org.acervo_consultado:
        secao.atencao.append(
            'Busca no acervo indisponível agora: certificados conferidos só pelo inventário.'
        )
    return secao


def _data_inspecao(textos: Iterable[str]) -> date | None:
    for texto in textos:
        if achado := _RE_DATA_INSPECAO.search(texto):
            return datetime.strptime(achado.group(1), '%d/%m/%Y').date()
    return None


def _secao_laudos(
    ctx: RunContext[AgentDeps], titulo: str, produtores: list[ProdutorResultado], fontes: _Fontes
) -> _Secao:
    """Laudos dos últimos 12 meses: existência pelo inventário, data pelo texto do laudo."""
    data_ref = ctx.deps.data_referencia
    nomes = {p.id: p.nome_completo for p in produtores}
    inventario = listar_documentos(ctx, tipo_doc='laudo')
    docs = _buscar_docs(
        ctx, 'laudo de inspeção sanitária data da inspeção resultado geral', 'laudo', k=20
    )
    trechos_por_arquivo = _agrupar_por_arquivo(docs or [])

    secao = _Secao(titulo)
    com_laudo: set[str] = set()
    for doc in inventario:
        pid = doc.produtor_id
        rotulo = f'{pid} · {nomes[pid]}' if pid in nomes else (pid or doc.arquivo)
        trechos = trechos_por_arquivo.get(doc.arquivo, [])
        emitido = _data_inspecao(t.trecho for t in trechos)
        if emitido is None and docs is not None and pid:
            direcionada = _buscar_docs(
                ctx, 'laudo nº data da inspeção', 'laudo', produtor_id=pid, k=10
            )
            trechos = [t for t in direcionada or [] if t.arquivo == doc.arquivo] or trechos
            emitido = _data_inspecao(t.trecho for t in trechos)
        com_data = next((t for t in trechos if _RE_DATA_INSPECAO.search(t.trecho)), None)
        citacao = fontes.add(com_data.fonte if com_data else doc.fonte)
        if emitido is None:
            secao.existe.append(f'{rotulo}: laudo {doc.arquivo} (data não confirmada). {citacao}')
            secao.atencao.append(
                f'{rotulo}: não foi possível confirmar a data do laudo; conferir se é dos '
                'últimos 12 meses.'
            )
        elif (data_ref - emitido).days <= 365:
            secao.existe.append(f'{rotulo}: laudo de {_data(emitido)}. {citacao}')
        else:
            secao.falta.append(
                f'{rotulo}: último laudo é de {_data(emitido)}, '
                f'fora dos últimos 12 meses. {citacao}'
            )
        if pid:
            com_laudo.add(pid)

    for p in produtores:
        if p.status != 'ativo' or p.id in com_laudo:
            continue
        if p.registro_sie or p.produtos_origem_animal:
            motivo = (
                f'tem SIE {p.registro_sie}'
                if p.registro_sie
                else f'produz {", ".join(p.produtos_origem_animal)} (origem animal)'
            )
            # Ausência afirmada pelo inventário do acervo, não por uma busca sem resultado.
            secao.falta.append(
                f'{_rotulo_produtor(p)}: nenhum laudo sanitário no inventário do acervo; '
                f'{motivo}. {fontes.add(p.fonte)}'
            )
    if docs is None and inventario:
        secao.atencao.append(
            'Busca no acervo indisponível agora: datas dos laudos não confirmadas.'
        )
    sem_laudo = sum(1 for linha in secao.falta if 'nenhum laudo' in linha)
    secao.resumo = (
        f'{len(com_laudo)} produtor(es) com laudo no acervo; {sem_laudo} sem laudo '
        '(SIE ou produto de origem animal)'
    )
    return secao


def _secao_rastreabilidade(ctx: RunContext[AgentDeps], titulo: str, fontes: _Fontes) -> _Secao:
    lotes = listar_lotes(ctx)
    vendas = consultar_financeiro(ctx)
    secao = _Secao(titulo)

    por_produto: dict[str, list] = defaultdict(list)
    for lote in lotes:  # já vem do mais recente para o mais antigo
        por_produto[lote.produto.strip().lower()].append(lote)
    vendidos = {i.produto.strip().lower(): i for i in vendas.itens}

    completos: list[str] = []
    incompletos: list[str] = []
    sem_lote: list[str] = []
    tabela = []
    for produto in sorted(set(por_produto) | set(vendidos), key=_normalizar):
        registrados = por_produto.get(produto, [])
        ultimos = registrados[:LOTES_POR_PRODUTO]
        if len(registrados) >= LOTES_POR_PRODUTO:
            situacao = '✅'
            completos.append(produto)
        elif registrados:
            situacao = f'⚠️ faltam {LOTES_POR_PRODUTO - len(registrados)}'
            incompletos.append(produto)
        else:
            situacao = '❌ sem lote'
            sem_lote.append(f'{produto} {fontes.add(vendidos[produto].fonte)}')
        tabela.append(
            [
                produto,
                len(registrados),
                ', '.join(
                    f'{fontes.add(lote.fonte)} ({_data(lote.data_colheita)})' for lote in ultimos
                )
                or '—',
                situacao,
            ]
        )

    total = len(tabela)
    if completos:
        secao.existe.append(
            f'{len(completos)} de {total} produtos com os últimos {LOTES_POR_PRODUTO} lotes '
            f'registrados: {", ".join(completos)}.'
        )
    if incompletos:
        secao.falta.append(
            f'{len(incompletos)} produto(s) com menos de {LOTES_POR_PRODUTO} lotes: '
            f'{", ".join(incompletos)}.'
        )
    if sem_lote:
        secao.falta.append(
            f'{len(sem_lote)} produto(s) vendido(s) sem nenhum lote registrado: '
            f'{"; ".join(sem_lote)}.'
        )
    secao.resumo = (
        f'{len(completos)} de {total} produtos com {LOTES_POR_PRODUTO} lotes; '
        f'{len(incompletos)} com menos; {len(sem_lote)} sem lote'
    )
    secao.extra = _tabela(['Produto', 'Lotes', 'Últimos lotes', 'Situação'], tabela)
    return secao


def _eh_institucional(texto: str) -> bool:
    # Separadores viram espaço: em `contrato_pnae_garanhuns.txt` o `_` não é limite de palavra.
    texto_norm = re.sub(r'[^a-z0-9]+', ' ', _normalizar(texto))
    return any(re.search(rf'\b{p}\b', texto_norm) for p in _PALAVRAS_INSTITUCIONAL)


def _secao_contratos(ctx: RunContext[AgentDeps], titulo: str, fontes: _Fontes) -> _Secao:
    """Contratos do inventário, classificados pelo contratante quando o texto permite."""
    inventario = listar_documentos(ctx, tipo_doc='contrato')
    docs = _buscar_docs(ctx, 'contrato de fornecimento contratante comprador', 'contrato', k=20)
    trechos_por_arquivo = _agrupar_por_arquivo(docs or [])

    secao = _Secao(titulo)
    privados = indefinidos = 0
    for doc in inventario:
        trechos = trechos_por_arquivo.get(doc.arquivo, [])
        institucional = next((t for t in trechos if _eh_institucional(t.trecho)), None)
        if institucional is not None or _eh_institucional(f'{doc.arquivo} {doc.titulo or ""}'):
            citacao = fontes.add(institucional.fonte if institucional else doc.fonte)
            secao.existe.append(f'Contrato institucional: {doc.arquivo}. {citacao}')
        elif trechos:
            privados += 1
            secao.atencao.append(
                f'{doc.arquivo} é um contrato com comprador privado; não entra neste item. '
                f'{fontes.add(trechos[0].fonte)}'
            )
        else:
            indefinidos += 1
            secao.atencao.append(
                f'{doc.arquivo} consta no inventário, mas não foi possível confirmar o '
                f'contratante; conferir se é institucional. {fontes.add(doc.fonte)}'
            )

    if not secao.existe:
        if indefinidos:
            secao.nao_verificado = True
        elif not inventario:
            secao.falta.append('Nenhum contrato no inventário do acervo.')
        else:
            secao.falta.append(
                f'Nenhum dos {len(inventario)} contratos do inventário é com comprador '
                'institucional.'
            )
    secao.resumo = f'{len(secao.existe)} institucional(is); {privados} privado(s) fora do pedido'
    if indefinidos:
        secao.resumo += f'; {indefinidos} sem contratante confirmado'
    return secao


def _secao_item_nao_mapeado(titulo: str) -> _Secao:
    return _Secao(
        titulo,
        nao_verificado=True,
        atencao=['Item sem verificação automática; levantar manualmente.'],
    )


def _secao_pendencias_inspecao(ctx: RunContext[AgentDeps], fontes: _Fontes) -> _Secao:
    """Não conformidades da inspeção anterior e indícios de descumprimento nos dados."""
    titulo = 'Pendências da inspeção anterior (ADAGRO)'
    docs = _buscar_docs(ctx, 'não conformidades NC prazo data da inspeção ADAGRO', 'inspecao', k=20)
    if docs is None:
        return _Secao(
            titulo,
            nao_verificado=True,
            atencao=['Relatório de inspeção indisponível agora; conferir o documento.'],
        )
    secao = _Secao(titulo)
    datas = [d for d in (_data_inspecao([t.trecho]) for t in docs) if d]
    fim_inspecao = max(datas) if datas else None
    if fim_inspecao:
        # "Data da Inspeção: 15/05/2026 e 16/05/2026": o segundo dia também conta.
        segundo = re.search(
            r'data da inspe[cç][aã]o:\s*\d{2}/\d{2}/\d{4}\s*e\s*' + _RE_DATA,
            ' '.join(t.trecho for t in docs),
            re.IGNORECASE,
        )
        if segundo:
            fim_inspecao = datetime.strptime(segundo.group(1), '%d/%m/%Y').date()

    for trecho in docs:
        for codigo, gravidade, descricao in _RE_NC.findall(trecho.trecho):
            resumo = ' '.join(descricao.split())
            citacao = fontes.add(trecho.fonte)
            secao.existe.append(f'{codigo} ({gravidade.strip()}): {resumo} {citacao}')
            indicios = _indicios_descumprimento(ctx, resumo, fim_inspecao, fontes)
            if indicios:
                secao.falta.append(
                    f'{codigo}: vendas do item suspenso depois da inspeção, o que indica '
                    f'descumprimento: {indicios}'
                )
            else:
                secao.atencao.append(
                    f'{codigo}: não há evidência de resolução nos dados; levar comprovante.'
                )
    if not secao.existe:
        secao.atencao.append(
            'Nenhuma não conformidade localizada nos trechos consultados do relatório de '
            'inspeção; conferir o documento.'
        )
    secao.resumo = (
        f'{len(secao.existe)} não conformidade(s); {len(secao.falta)} com indício de descumprimento'
    )
    return secao


def _indicios_descumprimento(
    ctx: RunContext[AgentDeps], descricao: str, depois_de: date | None, fontes: _Fontes
) -> str:
    """Vendas do produtor citado na NC, de um produto citado nela, após a inspeção."""
    if depois_de is None or 'suspend' not in _normalizar(descricao):
        return ''
    descricao_norm = _normalizar(descricao)
    achados = []
    for pid in sorted(set(_RE_PRODUTOR.findall(descricao))):
        vendas = consultar_financeiro(ctx, produtor_id=pid, periodo_inicio=depois_de)
        for item in vendas.itens:
            if item.data > depois_de and _normalizar(item.produto) in descricao_norm:
                achados.append(
                    f'{item.produto} para {item.comprador} em {_data(item.data)} '
                    f'{fontes.add(item.fonte)}'
                )
    return '; '.join(achados)


_VERIFICADORES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('cadastro',), 'cadastro'),
    (('certifica',), 'certificacoes'),
    (('laudo',), 'laudos'),
    (('rastreab', 'lote'), 'rastreabilidade'),
    (('contrato',), 'contratos'),
)


def _relatorio_dossie_adagro(ctx: RunContext[AgentDeps]) -> tuple[Relatorio, list[str]]:
    usuario = _resolver_usuario(ctx.deps.usuario)
    if usuario.perfil == 'produtor':
        raise SemPermissao(
            'O dossiê da ADAGRO reúne dados de todos os produtores e é restrito à equipe da '
            'cooperativa.'
        )

    titulo = f'Dossiê para a inspeção da ADAGRO ({PEDIDO_ADAGRO})'
    fontes = _Fontes()
    nome = _nome_arquivo('dossie_adagro', ctx, PEDIDO_ADAGRO)
    linhas = _cabecalho(titulo, ctx)

    pedido = _carregar_pedido(ctx, PEDIDO_ADAGRO)
    if pedido is None:
        linhas += [f'_Não encontrei a notificação {PEDIDO_ADAGRO} nos pedidos._', '']
        return _montar('dossie_adagro', titulo, nome, linhas, fontes), [
            f'Notificação {PEDIDO_ADAGRO} não encontrada.'
        ]

    pid, recebido, canal, remetente, comprador, mensagem = pedido
    mascarador = ctx.deps.mascarador
    citacao_pedido = fontes.add(Fonte(arquivo='pedidos.json', referencia=pid))
    itens = _itens_pedidos(mensagem)
    linhas += [
        '## O que foi pedido',
        '',
        f'- **Notificação:** {pid} · {canal} de {_mascarar_opcional(mascarador, remetente)} '
        f'({comprador}), recebida em {recebido:%d/%m/%Y %H:%M} {citacao_pedido}',
        '',
        *[
            f'> {ln}' if ln.strip() else '>'
            for ln in (_mascarar_opcional(mascarador, mensagem) or '').strip().splitlines()
        ],
        '',
    ]

    produtores = buscar_produtores(ctx)
    org: _Organicos | None = None
    secoes: list[_Secao] = []
    for item in itens or ['(nenhum item listado na notificação)']:
        item_norm = _normalizar(item)
        chave = next(
            (c for termos, c in _VERIFICADORES if any(t in item_norm for t in termos)), None
        )
        if chave == 'cadastro':
            secoes.append(_secao_cadastro(ctx, item, produtores, fontes))
        elif chave == 'certificacoes':
            org = org or _coletar_organicos(ctx)
            secoes.append(_secao_certificacoes(ctx, item, org, fontes))
        elif chave == 'laudos':
            secoes.append(_secao_laudos(ctx, item, produtores, fontes))
        elif chave == 'rastreabilidade':
            secoes.append(_secao_rastreabilidade(ctx, item, fontes))
        elif chave == 'contratos':
            secoes.append(_secao_contratos(ctx, item, fontes))
        else:
            secoes.append(_secao_item_nao_mapeado(item))
    pendencias = _secao_pendencias_inspecao(ctx, fontes)

    linhas += ['## Situação por item', '']
    linhas += _tabela(
        ['Documento pedido', 'Situação', 'Resumo'],
        [[s.titulo, _ROTULO_SITUACAO[s.situacao], s.resumo or '—'] for s in [*secoes, pendencias]],
    )
    linhas.append('')

    for numero, secao in enumerate([*secoes, pendencias], start=1):
        linhas += [
            f'## {numero}. {secao.titulo}',
            '',
            f'**Situação:** {_ROTULO_SITUACAO[secao.situacao]}',
            '',
        ]
        if secao.existe:
            linhas += ['**O que existe**', '', *_lista(secao.existe), '']
        if secao.falta:
            linhas += ['**O que falta**', '', *_lista(secao.falta), '']
        if secao.atencao:
            linhas += ['**Atenção**', '', *_lista(secao.atencao), '']
        if secao.extra:
            linhas += [*secao.extra, '']

    destaques = [_destaque_secao(s) for s in [*secoes, pendencias]]
    return _montar('dossie_adagro', titulo, nome, linhas, fontes), destaques


def _destaque_secao(secao: _Secao) -> str:
    """Situação do item com as fontes do que falta e, depois, do que existe."""
    texto = f'{secao.titulo}: {_ROTULO_SITUACAO[secao.situacao]}'
    if secao.resumo:
        texto += f' ({secao.resumo})'
    citacoes = list(
        dict.fromkeys(
            c for linha in [*secao.falta, *secao.existe] for c in _RE_CITACAO.findall(linha)
        )
    )
    if citacoes:
        texto += ' ' + ' '.join(citacoes[:MAX_CITACOES_DESTAQUE])
        if len(citacoes) > MAX_CITACOES_DESTAQUE:
            texto += f' (+{len(citacoes) - MAX_CITACOES_DESTAQUE} no arquivo)'
    return texto


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

_MONTADORES: dict[TipoRelatorio, Callable[[RunContext[AgentDeps]], tuple[Relatorio, list[str]]]] = {
    'contas_a_receber': _relatorio_contas_a_receber,
    'organicos_vigentes': _relatorio_organicos,
    'dossie_adagro': _relatorio_dossie_adagro,
}


def gerar_relatorio(
    ctx: RunContext[AgentDeps],
    tipo: Annotated[
        TipoRelatorio,
        Field(
            description=(
                "'contas_a_receber' (vencidos × a vencer por comprador), "
                "'organicos_vigentes' (produtores com orgânico vigente, por produto) ou "
                "'dossie_adagro' (o que a ADAGRO pediu no PED-2026-014, o que existe e "
                'o que falta).'
            )
        ),
    ],
) -> RelatorioResultado:
    """Gera um relatório em Markdown para download e devolve só os destaques.

    QUANDO USAR:
    - Quando o usuário pedir um relatório, levantamento, planilha ou dossiê para baixar:
      contas a receber / inadimplência por comprador; produtores ou produtos orgânicos
      certificados vigentes; dossiê, checklist ou documentação para a inspeção da ADAGRO.

    QUANDO NÃO USAR:
    - Perguntas pontuais (ex.: "quanto a SuperBom deve?"): use `consultar_financeiro`.
    - Resumo de um produtor específico: use `resumir_produtor`.

    O arquivo completo é anexado automaticamente à resposta para download. Não copie o
    relatório no texto: resuma os destaques e diga que o arquivo está disponível para baixar.
    Cada destaque já traz as fontes do próprio número entre colchetes: cite exatamente essas,
    junto do número a que se referem, sem trocar nem inventar referências.
    """
    try:
        relatorio, destaques = _MONTADORES[tipo](ctx)
    except SemPermissao as exc:
        return RelatorioResultado(gerado=False, tipo=tipo, mensagem=str(exc))

    relatorios = getattr(ctx.deps, 'relatorios', None)
    if isinstance(relatorios, list):
        relatorios.append(relatorio)
    return RelatorioResultado(
        gerado=True,
        tipo=tipo,
        titulo=relatorio.titulo,
        nome_arquivo=relatorio.nome_arquivo,
        destaques=destaques,
        fontes=_fontes_dos_destaques(destaques, relatorio.fontes),
        mensagem=(
            f'Arquivo {relatorio.nome_arquivo} anexado à resposta para download '
            f'({len(relatorio.fontes)} fontes citadas no arquivo). Cite cada fonte junto do '
            'número do destaque em que ela aparece.'
        ),
    )


def _fontes_dos_destaques(destaques: list[str], fontes: list[Fonte]) -> list[Fonte]:
    """Fontes devolvidas ao modelo: as citadas nos destaques (ou as primeiras do arquivo)."""
    por_citacao = {f.formatar(): f for f in fontes}
    citadas = dict.fromkeys(c for d in destaques for c in _RE_CITACAO.findall(d))
    escolhidas = [por_citacao[c] for c in citadas if c in por_citacao]
    return (escolhidas or fontes)[:MAX_FONTES_RESUMO]
