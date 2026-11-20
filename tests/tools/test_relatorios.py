"""Testes da tool gerar_relatorio (Issue #15): relatórios autônomos sob demanda em Markdown.

Os relatórios usam o banco de desenvolvimento já populado (fixture `conn`). A busca no acervo
documental é trocada por uma versão determinística que lê os chunks do banco sem gerar
embedding, para os testes não dependerem do provedor; o teste com a busca real só roda com
`GEMINI_API_KEY`.
"""

from __future__ import annotations

from datetime import date
import os

from dotenv import load_dotenv
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
import pytest

from sabor.agent.deps import AgentDeps
from sabor.agent.mascarador import Mascarador
from sabor.tools import gerar_relatorio
from sabor.tools import relatorios as mod
from sabor.tools.base import Fonte
from sabor.tools.documentos import BuscaDocumentosResultado, DocumentoResultado

load_dotenv()

DATA_REF = date(2026, 10, 30)


def _ctx(conn, usuario: str = 'Dona Lúcia') -> RunContext[AgentDeps]:
    deps = AgentDeps(conn=conn, data_referencia=DATA_REF, usuario=usuario, mascarador=Mascarador())
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def _acervo_ingerido(conn) -> bool:
    return conn.execute('SELECT count(*) FROM chunks').fetchone()[0] > 0


@pytest.fixture
def busca_local(monkeypatch, conn):
    """Troca `buscar_documentos` por uma leitura direta dos chunks (sem embedding)."""
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido (rode scripts/ingest_docs.py)')

    def _buscar(ctx, pergunta, *, tipo_doc=None, produtor_id=None, apenas_vigentes=False, k=5):
        linhas = ctx.deps.conn.execute(
            """
            SELECT cb.arquivo, cb.secao, cb.tipo_doc, cb.produtor_id, cb.texto,
                   COALESCE(cb.validade, p.validade_certificacao), p.nome_completo
            FROM chunks_busca cb
            LEFT JOIN produtores p ON p.id = cb.produtor_id
            WHERE cb.tipo_doc = %s AND (%s::text IS NULL OR cb.produtor_id = %s)
            ORDER BY cb.arquivo, cb.ordem
            """,
            (tipo_doc, produtor_id, produtor_id),
        ).fetchall()
        resultados = []
        for arquivo, secao, t_doc, pid, texto, validade, nome in linhas:
            vigente = validade >= DATA_REF if validade else None
            if apenas_vigentes and not vigente:
                continue
            resultados.append(
                DocumentoResultado(
                    arquivo=arquivo,
                    secao=secao,
                    citacao=f'[{arquivo} · {secao}]',
                    tipo_doc=t_doc,
                    produtor_id=pid,
                    produtor_nome=nome,
                    produtor_status=None,
                    registro_sie=None,
                    dap_caf=None,
                    validade=validade,
                    vigente=vigente,
                    trecho=ctx.deps.mascarador.mascarar(texto),
                    score_rrf=0.02,
                    similaridade_vetorial=None,
                    score_lexical=None,
                    fonte=Fonte(arquivo=arquivo, referencia=secao or 'documento'),
                )
            )
        return BuscaDocumentosResultado(encontrado=bool(resultados), resultados=resultados)

    monkeypatch.setattr(mod, 'buscar_documentos', _buscar)


@pytest.fixture
def busca_fora_do_ar(monkeypatch):
    def _falha(*args, **kwargs):
        raise ConnectionError('provedor de embeddings indisponível')

    monkeypatch.setattr(mod, 'buscar_documentos', _falha)


# =============================================================================
# Contas a receber
# =============================================================================


def test_contas_a_receber_vencidos_x_a_vencer_por_comprador(conn):
    ctx = _ctx(conn)
    resultado = gerar_relatorio(ctx, 'contas_a_receber')

    assert resultado.gerado is True
    assert resultado.nome_arquivo == 'contas_a_receber_20261030.md'
    # O modelo recebe só os destaques; o Markdown fica nas deps para o runner anexar.
    assert 'markdown' not in resultado.model_dump()
    assert 'R$ 8.690,00' in resultado.destaques[0]  # vencido: PNAE 2.670 + SuperBom 6.020
    assert 'R$ 4.690,00' in resultado.destaques[0]  # a vencer
    assert 'R$ 13.380,00' in resultado.destaques[0]

    [relatorio] = ctx.deps.relatorios
    md = relatorio.markdown
    assert md.startswith('# Contas a receber')
    assert '| Rede SuperBom Atacado | R$ 6.020,00 | — | R$ 6.020,00 | 12 dias |' in md
    assert '| Prefeitura de Garanhuns - PNAE | R$ 2.670,00 | R$ 2.480,00 | R$ 5.150,00 |' in md
    assert '[financeiro.csv · FIN-2026-013]' in md
    vencidos = md.split('## Vencidos')[1].split('## A vencer')[0]
    a_vencer = md.split('## A vencer')[1].split('## Fontes')[0]
    assert '[financeiro.csv · FIN-2026-013]' in vencidos and '12 dias de atraso' in vencidos
    assert '[financeiro.csv · FIN-2026-027]' in a_vencer  # SESC vence em 30/11
    assert 'FIN-2026-013' not in a_vencer
    assert {f.referencia for f in relatorio.fontes} >= {'FIN-2026-013', 'FIN-2026-014'}


def test_contas_a_receber_perfil_produtor_ve_so_o_proprio(conn):
    ctx = _ctx(conn, 'P003')
    resultado = gerar_relatorio(ctx, 'contas_a_receber')

    assert resultado.gerado is True
    assert resultado.nome_arquivo == 'contas_a_receber_P003_20261030.md'
    md = ctx.deps.relatorios[0].markdown
    assert 'somente dados do produtor P003' in md
    assert 'Rede SuperBom Atacado' in md
    assert 'PNAE' not in md
    assert 'Mercado Bom Preço' not in md


# =============================================================================
# Orgânicos vigentes por produto
# =============================================================================


def test_organicos_vigentes_por_produto(conn, busca_local):
    ctx = _ctx(conn)
    resultado = gerar_relatorio(ctx, 'organicos_vigentes')

    assert resultado.gerado is True
    assert resultado.destaques[0].startswith('8 produtor(es) com certificação vigente')
    md = ctx.deps.relatorios[0].markdown
    por_produto = md.split('## Por produto')[1].split('## Por produtor')[0]

    linha_mel = next(ln for ln in por_produto.splitlines() if ln.startswith('| mel silvestre |'))
    assert 'P002' in linha_mel and 'P018' in linha_mel
    assert 'P008' not in linha_mel  # certificação vencida em 30/09/2026
    assert 'cert_organica_jose_santos_P002.txt · PRODUTOS CERTIFICADOS' in linha_mel

    atencao = md.split('## Atenção')[1]
    assert 'P004' in atencao and '01/12/2026' in atencao
    assert 'P008' in atencao and 'venceu em 30/09/2026' in atencao


def test_organicos_perfil_produtor_sem_certificacao(conn, busca_local):
    ctx = _ctx(conn, 'P003')
    resultado = gerar_relatorio(ctx, 'organicos_vigentes')

    assert resultado.gerado is True
    md = ctx.deps.relatorios[0].markdown
    assert 'Nenhum produtor com certificação orgânica vigente' in md
    assert 'P002' not in md


def test_organicos_sem_acervo_avisa_e_nao_confirma_produtos(conn, busca_fora_do_ar):
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido')
    ctx = _ctx(conn)
    resultado = gerar_relatorio(ctx, 'organicos_vigentes')

    assert resultado.gerado is True
    md = ctx.deps.relatorios[0].markdown
    assert 'Não foi possível consultar o texto dos certificados' in md
    sem_confirmacao = md.split('### Produtos do cadastro sem confirmação no certificado')[1]
    # O certificado existe (inventário): não afirma ausência, só que a lista não foi lida.
    assert 'consta no inventário, mas não foi possível ler a lista' in sem_confirmacao
    assert 'não consta no inventário' not in sem_confirmacao
    assert any('indisponível' in d for d in resultado.destaques)


# =============================================================================
# Dossiê ADAGRO (PED-2026-014)
# =============================================================================


def _secao(md: str, numero: int) -> str:
    return md.split(f'\n## {numero}. ')[1].split('\n## ')[0]


def test_dossie_adagro_pedido_existe_e_falta(conn, busca_local):
    ctx = _ctx(conn)
    resultado = gerar_relatorio(ctx, 'dossie_adagro')

    assert resultado.gerado is True
    assert resultado.nome_arquivo == 'dossie_adagro_PED-2026-014_20261030.md'
    md = ctx.deps.relatorios[0].markdown

    # O que foi pedido: os cinco itens da notificação, com a fonte do pedido.
    assert '[pedidos.json · PED-2026-014]' in md
    assert 'fiscalizacao@adagro' not in md  # e-mail mascarado
    assert len(resultado.destaques) == 6

    cadastro = _secao(md, 1)
    assert cadastro.startswith('Cadastro atualizado')
    assert 'P008' in cadastro
    assert '[produtores.csv · P001–P018]' in cadastro  # registros reais, não "cadastro" (F8)
    assert 'produtores.csv · cadastro' not in md

    certificacoes = _secao(md, 2)
    assert certificacoes.count('vigente até') == 8
    assert 'P004' in certificacoes.split('**Atenção**')[1]

    laudos = _secao(md, 3)
    existe, falta = laudos.split('**O que falta**')
    assert 'P002' in existe and '05/05/2026' in existe
    assert 'P003' in existe and '12/08/2026' in existe
    assert 'P007' in falta and 'PE-SIE-1203' in falta
    assert '⚠️ Parcial' in laudos

    rastreio = _secao(md, 4)
    assert '2 de 21 produtos' in rastreio
    assert 'mel silvestre, queijo coalho' in rastreio
    assert 'ovos caipira [financeiro.csv · FIN-2026-019]' in rastreio

    contratos = _secao(md, 5)
    assert 'Contrato institucional: contratos/contrato_pnae_garanhuns.txt' in contratos
    assert 'contrato_mercado_bom_preco.txt é um contrato com comprador privado' in contratos

    pendencias = _secao(md, 6)
    assert 'NC-001 (GRAVE)' in pendencias and 'NC-002 (MENOR)' in pendencias
    falta_nc = pendencias.split('**O que falta**')[1]
    assert 'frango caipira' in falta_nc and '[financeiro.csv · FIN-2026-018]' in falta_nc


def test_dossie_adagro_restrito_a_equipe(conn):
    ctx = _ctx(conn, 'P007')
    resultado = gerar_relatorio(ctx, 'dossie_adagro')

    assert resultado.gerado is False
    assert 'restrito à equipe' in resultado.mensagem
    assert ctx.deps.relatorios == []


def test_dossie_sem_busca_usa_o_inventario_e_nao_inventa_ausencias(conn, busca_fora_do_ar):
    """Busca fora do ar: o que existe vem do inventário; o que depende do texto fica a confirmar."""
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido')
    ctx = _ctx(conn)
    resultado = gerar_relatorio(ctx, 'dossie_adagro')

    assert resultado.gerado is True
    md = ctx.deps.relatorios[0].markdown

    certificados = _secao(md, 2)
    assert certificados.count('vigente até') == 8
    assert '**O que falta**' not in certificados
    assert 'inventário' in certificados

    laudos = _secao(md, 3)
    existe, falta = laudos.split('**O que falta**')
    assert 'P002' in existe and 'data não confirmada' in existe
    assert 'P007' in falta and 'nenhum laudo sanitário no inventário' in falta

    contratos = _secao(md, 5)
    assert 'Contrato institucional: contratos/contrato_pnae_garanhuns.txt' in contratos
    assert 'não foi possível confirmar o contratante' in contratos
    assert 'Nenhum' not in contratos

    assert '⏳ Não verificado' in _secao(md, 6)  # NCs dependem do texto da inspeção
    assert '2 de 21 produtos' in _secao(md, 4)  # não depende do acervo


def test_itens_pedidos_extrai_lista_da_notificacao():
    mensagem = 'Solicitamos:\n- Cadastro atualizado\n- Laudos\n\nAtt.'
    assert mod._itens_pedidos(mensagem) == ['Cadastro atualizado', 'Laudos']


@pytest.mark.skipif(not os.getenv('GEMINI_API_KEY'), reason='Sem GEMINI_API_KEY')
def test_dossie_com_busca_hibrida_real(conn):
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido')
    ctx = _ctx(conn)
    resultado = gerar_relatorio(ctx, 'dossie_adagro')

    assert resultado.gerado is True
    md = ctx.deps.relatorios[0].markdown
    assert 'P007' in _secao(md, 3).split('**O que falta**')[1]
    assert 'FIN-2026-018' in _secao(md, 6)


def test_destaques_trazem_a_fonte_de_cada_numero(conn, busca_local):
    """O modelo só vê os destaques: cada número precisa chegar com a própria fonte."""
    ctx = _ctx(conn)
    contas = gerar_relatorio(ctx, 'contas_a_receber')
    pnae = next(d for d in contas.destaques if d.startswith('Prefeitura de Garanhuns'))
    vencido, a_vencer = pnae.split('; ')
    assert 'FIN-2026-005' in vencido and 'FIN-2026-030' not in vencido
    assert '[financeiro.csv · FIN-2026-030]' in a_vencer
    citadas = {f.referencia for f in contas.fontes}
    assert {'FIN-2026-013', 'FIN-2026-014', 'FIN-2026-030'} <= citadas
    assert 'FIN-2026-027' not in citadas  # SESC não aparece em nenhum destaque

    dossie = gerar_relatorio(ctx, 'dossie_adagro')
    laudos = next(d for d in dossie.destaques if d.startswith('Laudos'))
    assert '[produtores.csv · P007]' in laudos
    pendencias = dossie.destaques[-1]
    assert '[financeiro.csv · FIN-2026-018]' in pendencias
    assert 'inspecao/inspecao_adagro_2026.txt' in pendencias


# =============================================================================
# Revisão do PR #48: conferência do certificado e ausência documental
# =============================================================================


def _trecho_certificado(arquivo: str, pid: str, secao: str, texto: str) -> DocumentoResultado:
    return DocumentoResultado(
        arquivo=arquivo,
        secao=secao,
        citacao=f'[{arquivo} · {secao}]',
        tipo_doc='certificado',
        produtor_id=pid,
        produtor_nome=None,
        produtor_status=None,
        registro_sie=None,
        dap_caf=None,
        validade=date(2027, 3, 20),
        vigente=True,
        trecho=texto,
        score_rrf=0.02,
        similaridade_vetorial=None,
        score_lexical=None,
        fonte=Fonte(arquivo=arquivo, referencia=secao),
    )


def test_certificado_so_confirma_produto_afirmado_na_lista(conn, monkeypatch):
    """Cenário da revisão: "mel não está contemplado pelo certificado" não pode confirmar o mel.

    Só a seção PRODUTOS CERTIFICADOS conta, e uma linha que exclui o produto não o confirma.
    """
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido')
    arquivo = 'certificacoes/cert_organica_jose_santos_P002.txt'
    trechos = [
        _trecho_certificado(
            arquivo,
            'P002',
            'OBSERVAÇÕES',
            'Mel silvestre e mel de abelha jandaíra não estão contemplados pelo certificado.',
        ),
        _trecho_certificado(
            arquivo,
            'P002',
            'PRODUTOS CERTIFICADOS',
            '1. Própolis verde\n2. Mel de abelha jandaíra\n'
            '3. Mel silvestre - não contemplado nesta certificação',
        ),
    ]

    def _buscar(ctx, pergunta, *, tipo_doc=None, produtor_id=None, apenas_vigentes=False, k=5):
        return BuscaDocumentosResultado(encontrado=True, resultados=trechos)

    monkeypatch.setattr(mod, 'buscar_documentos', _buscar)
    ctx = _ctx(conn, 'P002')
    gerar_relatorio(ctx, 'organicos_vigentes')
    md = ctx.deps.relatorios[0].markdown

    por_produto = md.split('## Por produto')[1].split('### Produtos do cadastro')[0]
    assert '| própolis |' in por_produto
    assert '| mel de abelha jandaíra |' in por_produto  # a linha da lista afirma o produto
    assert '| mel silvestre |' not in por_produto
    sem_confirmacao = md.split('### Produtos do cadastro sem confirmação no certificado')[1]
    assert 'mel silvestre: P002' in sem_confirmacao
    assert 'não aparece na lista de produtos certificados' in sem_confirmacao


def test_ausencia_de_certificado_so_e_afirmada_pelo_inventario(conn, monkeypatch):
    """Busca sem resultado não prova ausência: certificado no inventário fica "a confirmar";
    só o que não consta no inventário vira lacuna."""
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido')

    def _busca_vazia(ctx, pergunta, *, tipo_doc=None, produtor_id=None, **kwargs):
        return BuscaDocumentosResultado(encontrado=False, resultados=[])

    inventario_real = mod.listar_documentos

    def _inventario_sem_p014(ctx, *, tipo_doc=None):
        return [d for d in inventario_real(ctx, tipo_doc=tipo_doc) if d.produtor_id != 'P014']

    monkeypatch.setattr(mod, 'buscar_documentos', _busca_vazia)
    monkeypatch.setattr(mod, 'listar_documentos', _inventario_sem_p014)
    ctx = _ctx(conn)
    gerar_relatorio(ctx, 'dossie_adagro')
    certificados = _secao(ctx.deps.relatorios[0].markdown, 2)

    existe, resto = certificados.split('**O que falta**')
    falta, atencao = resto.split('**Atenção**')
    assert 'P001' in existe and '(produtos não confirmados)' in existe
    assert 'P001' not in falta
    assert 'P001' in atencao and 'não foi possível ler a lista de produtos' in atencao
    assert 'P014' in falta and 'não consta no inventário do acervo' in falta
    assert '⚠️ Parcial' in certificados


def test_inventario_respeita_o_perfil_produtor(conn):
    if not _acervo_ingerido(conn):
        pytest.skip('Acervo documental não ingerido')
    from sabor.tools.documentos import listar_documentos

    todos = listar_documentos(_ctx(conn), tipo_doc='certificado')
    proprios = listar_documentos(_ctx(conn, 'P002'), tipo_doc='certificado')
    assert len(todos) == 9
    assert [d.produtor_id for d in proprios] == ['P002']
