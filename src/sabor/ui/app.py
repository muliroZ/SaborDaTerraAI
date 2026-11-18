"""Interface Streamlit do SaborDaTerraAI (#13).

Rodar com `uv run sabor` (ou `uv run streamlit run src/sabor/ui/app.py`).

- Entrada só pelo chat; cada mensagem chama `processar_turno` (#6).
- O histórico exibido e a memória do agente vêm do banco (`sessoes`/`mensagens`, #31),
  então recarregar a página retoma a sessão ativa do usuário selecionado.
- Trava de turno: o input fica desabilitado enquanto o turno roda, e uma trava por sessão
  no processo impede dois turnos simultâneos na mesma sessão (ex.: duas abas abertas).
- Seletor de usuário simulado (funcionários e produtores P001…P018) no lugar da autenticação.
- "Nova sessão" oferece o relatório da conversa anterior (#38).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields
import logging
import threading

import psycopg
import streamlit as st

from sabor.agent import memoria as sess
from sabor.agent import AgentDeps, RespostaTurno, criar_agente, processar_turno
from sabor.agent.deps import obter_data_referencia
from sabor.agent.runner import MENSAGEM_FALHA_SEGURA
from sabor.agent.schemas import Fonte
from sabor.db import conectar, conectar_readonly
from sabor.ui import apresentacao as ap
from sabor.ui.relatorio import FUSO, relatorio_da_sessao

logger = logging.getLogger(__name__)

DATASET_VERSION = next(f.default for f in fields(AgentDeps) if f.name == 'dataset_version')


class TurnoEmAndamento(RuntimeError):
    """Já existe um turno rodando nesta sessão (outra aba ou clique repetido)."""


# ---------------------------------------------------------------------------
# Recursos compartilhados
# ---------------------------------------------------------------------------


@contextmanager
def conexao() -> Iterator[psycopg.Connection]:
    """Conexão de escrita curta (sessões e mensagens). Falha rápido se o banco estiver fora."""
    with conectar(tentativas=1, autocommit=True) as conn:
        yield conn


@st.cache_resource(show_spinner=False)
def agente():
    return criar_agente()


@st.cache_resource
def _travas() -> tuple[threading.Lock, dict[str, threading.Lock]]:
    return threading.Lock(), {}


def trava_da_sessao(sessao_id: str) -> threading.Lock:
    guarda, travas = _travas()
    with guarda:
        return travas.setdefault(sessao_id, threading.Lock())


@st.cache_data(ttl=30, show_spinner=False)
def status_banco() -> dict | None:
    try:
        with conectar(tentativas=1) as conn:
            produtores, documentos, chunks, assinatura = conn.execute(
                'SELECT (SELECT count(*) FROM produtores), (SELECT count(*) FROM documentos), '
                '(SELECT count(*) FROM chunks), '
                "(SELECT left(md5(coalesce(string_agg(sha256, '' ORDER BY arquivo), '')), 8) "
                'FROM documentos)'
            ).fetchone()
    except psycopg.Error:
        return None
    return {
        'produtores': produtores,
        'documentos': documentos,
        'chunks': chunks,
        'assinatura': assinatura if documentos else None,
    }


@st.cache_data(ttl=300, show_spinner=False)
def nomes_produtores() -> dict[str, str]:
    try:
        with conectar(tentativas=1) as conn:
            return dict(conn.execute('SELECT id, nome_completo FROM produtores').fetchall())
    except psycopg.Error:
        return {}


@st.cache_data(ttl=600, show_spinner=False)
def trechos(fontes_json: tuple[str, ...], usuario_id: str) -> list[str | None]:
    fontes = [Fonte.model_validate_json(f) for f in fontes_json]
    try:
        with conectar_readonly(tentativas=1) as conn:
            return [ap.buscar_trecho(conn, f, usuario_id) for f in fontes]
    except psycopg.Error:
        return [None] * len(fontes)


# ---------------------------------------------------------------------------
# Turno
# ---------------------------------------------------------------------------


def executar_turno(sessao_id: str, usuario_id: str, mensagem: str) -> RespostaTurno:
    """Roda `processar_turno` com a memória da sessão e grava pergunta e resposta no banco.

    Não faz chamadas ao Streamlit: se a página for recarregada no meio do turno, a resposta
    ainda é gravada e aparece no histórico.
    """
    trava = trava_da_sessao(sessao_id)
    if not trava.acquire(blocking=False):
        raise TurnoEmAndamento
    try:
        with conexao() as conn:
            requisicoes = sess.contar_turnos(conn, sessao_id)
            max_turnos = sess.max_turnos_memoria()
            try:
                turno = processar_turno(
                    sessao_id,
                    mensagem,
                    usuario_id,
                    agente=agente(),
                    requisicoes_na_sessao=requisicoes,
                    carregar_memoria=lambda sid: sess.carregar_historico(conn, sid, max_turnos),
                )
            except Exception:
                logger.exception('Falha inesperada no turno da sessão %s', sessao_id)
                turno = RespostaTurno(
                    sessao_id=sessao_id,
                    usuario=usuario_id,
                    texto=MENSAGEM_FALHA_SEGURA,
                    falha_segura=True,
                )
            sess.registrar_turno(conn, sessao_id, mensagem.strip(), turno)
        return turno
    finally:
        trava.release()


def enviar(mensagem: str | None = None) -> None:
    """Callback do chat e das sugestões: enfileira a mensagem e trava o input já neste rerun."""
    texto = (mensagem if mensagem is not None else st.session_state.get('entrada')) or ''
    if texto.strip() and not st.session_state.processando:
        st.session_state.pendente = texto.strip()
        st.session_state.processando = True


# ---------------------------------------------------------------------------
# Renderização
# ---------------------------------------------------------------------------


def render_fontes(turno: RespostaTurno, usuario_id: str) -> None:
    if not turno.fontes:
        return
    with st.expander(f'Fontes ({len(turno.fontes)})', icon=':material/description:'):
        achados = trechos(tuple(f.model_dump_json() for f in turno.fontes), usuario_id)
        for fonte, trecho in zip(turno.fontes, achados):
            st.markdown(f'**`{fonte.formatar()}`**')
            if trecho:
                st.text(trecho)
            else:
                st.caption('Trecho não disponível para exibição.')


def render_flags(turno: RespostaTurno) -> None:
    for flag in turno.flags:
        st.markdown(f'{ap.badge_markdown(flag)} {flag.descricao}')


def render_rascunhos(turno: RespostaTurno) -> None:
    for rascunho in turno.rascunhos:
        st.markdown(
            f':material/draft: **Rascunho para {rascunho.destinatario}** · {rascunho.canal}'
        )
        st.code(rascunho.conteudo, language=None, wrap_lines=True)
    if turno.rascunhos:
        st.caption('Revise antes de enviar. Use o ícone de cópia no canto da caixa.')


def render_resposta(turno: RespostaTurno, usuario_id: str) -> None:
    destaque = ap.destaque_do_turno(turno)
    if destaque is not None and destaque.substitui_resposta:
        caixa = getattr(st, destaque.nivel)
        caixa(f'**{destaque.titulo}**\n\n{turno.texto}', icon=destaque.icone)
    else:
        if destaque is not None:
            st.badge(destaque.titulo, icon=destaque.icone, color='green')
        st.markdown(turno.texto)

    render_flags(turno)
    render_rascunhos(turno)
    render_fontes(turno, usuario_id)

    meta = []
    if turno.modelo_efetivo:
        meta.append(turno.modelo_efetivo)
    if turno.tentativas_fallback:
        meta.append(f'{len(turno.tentativas_fallback)} tentativa(s) de fallback')
    if turno.latencia_ms:
        meta.append(f'{turno.latencia_ms / 1000:.1f} s')
    if meta:
        st.caption(' · '.join(meta))


@st.dialog('Nova sessão')
def dialogo_nova_sessao(sessao_id: str, usuario_id: str) -> None:
    st.write(
        'A conversa atual será encerrada e **não** volta para o contexto do assistente. '
        'Baixe o relatório se quiser guardar o que foi útil.'
    )
    with conexao() as conn:
        nome, markdown = relatorio_da_sessao(conn, sessao_id)
    st.download_button(
        'Baixar relatório da conversa',
        markdown,
        file_name=nome,
        mime='text/markdown',
        icon=':material/download:',
        on_click='ignore',
        width='stretch',
    )
    with st.expander('Pré-visualizar relatório'):
        st.markdown(markdown)
    if st.button('Encerrar e iniciar nova sessão', type='primary', width='stretch'):
        with conexao() as conn:
            sess.nova_sessao(conn, sessao_id, usuario_id)
        st.rerun()


@st.dialog('Sessão anterior', width='large')
def dialogo_relatorio(sessao_id: str) -> None:
    with conexao() as conn:
        nome, markdown = relatorio_da_sessao(conn, sessao_id)
    st.download_button(
        'Baixar relatório',
        markdown,
        file_name=nome,
        mime='text/markdown',
        icon=':material/download:',
        on_click='ignore',
    )
    st.markdown(markdown)


def render_sidebar(processando: bool) -> str:
    """Seletor de usuário, status e sugestões. Devolve o usuário selecionado."""
    nomes = nomes_produtores()
    opcoes = list(ap.USUARIOS_SIMULADOS)
    inicial = st.query_params.get('usuario', opcoes[0])
    with st.sidebar:
        st.subheader('Sabor da Terra')
        usuario_id = st.selectbox(
            'Usuário simulado',
            opcoes,
            index=opcoes.index(inicial) if inicial in opcoes else 0,
            format_func=lambda u: ap.rotulo_usuario(u, nomes),
            disabled=processando,
            key='usuario',
            help='Substitui a autenticação no protótipo. Produtores só veem os próprios dados.',
        )
        st.query_params['usuario'] = usuario_id
    return usuario_id


def render_sidebar_sessao(sessao: sess.Sessao, processando: bool) -> None:
    status = status_banco()
    with st.sidebar:
        if st.button(
            'Nova sessão',
            icon=':material/add_comment:',
            disabled=processando or sessao.total_mensagens == 0,
            width='stretch',
        ):
            dialogo_nova_sessao(sessao.id, sessao.usuario_id)

        st.divider()
        st.markdown(f'**Data de referência:** {obter_data_referencia():%d/%m/%Y}')
        versao = DATASET_VERSION
        if status and status['assinatura']:
            versao += f' · docs {status["assinatura"]}'
        st.markdown(f'**Dataset:** `{versao}`')
        if status is None:
            st.markdown(':red-badge[:material/database_off: Banco fora do ar]')
        else:
            st.markdown(':green-badge[:material/database: Banco conectado]')
            st.caption(
                f'{status["produtores"]} produtores · {status["documentos"]} documentos · '
                f'{status["chunks"]} trechos indexados'
            )
            if not status['chunks']:
                st.caption(':orange[Documentos não ingeridos: rode `scripts/ingest_docs.py`.]')

        st.divider()
        st.markdown('**Sugestões de perguntas**')
        for i, pergunta in enumerate(ap.sugestoes_para(sessao.usuario_id)):
            st.button(
                pergunta,
                key=f'sugestao_{i}',
                on_click=enviar,
                args=(pergunta,),
                disabled=processando,
                width='stretch',
                type='tertiary',
            )

        with conexao() as conn:
            anteriores = sess.listar_sessoes_encerradas(conn, sessao.usuario_id, limite=5)
        if anteriores:
            st.divider()
            st.markdown('**Sessões anteriores** (somente leitura)')
            for anterior in anteriores:
                rotulo = (
                    f'{anterior.criada_em.astimezone(FUSO):%d/%m %H:%M} · '
                    f'{anterior.total_mensagens // 2} turno(s)'
                )
                if st.button(
                    rotulo,
                    key=f'anterior_{anterior.id}',
                    icon=':material/history:',
                    disabled=processando,
                    type='tertiary',
                ):
                    dialogo_relatorio(anterior.id)


def main() -> None:
    st.set_page_config(page_title='Sabor da Terra', page_icon=':material/eco:')
    estado = st.session_state
    estado.setdefault('pendente', None)
    estado.setdefault('processando', False)
    if estado.pendente is None:
        # Um rerun que interrompeu o turno anterior não deixa o input travado para sempre.
        estado.processando = False

    usuario_id = render_sidebar(estado.processando)
    try:
        with conexao() as conn:
            sessao = sess.obter_ou_criar_sessao_ativa(conn, usuario_id)
            mensagens = sess.listar_mensagens(conn, sessao.id)
    except psycopg.Error:
        st.error(
            'Não consegui conectar ao banco. Suba o Postgres com `docker compose up -d` e '
            'rode `scripts/init_db.py`.',
            icon=':material/database_off:',
        )
        st.stop()

    render_sidebar_sessao(sessao, estado.processando)

    perfil = 'produtor' if ap.eh_produtor(usuario_id) else 'equipe da cooperativa'
    st.title('Assistente Sabor da Terra')
    inicio = sessao.criada_em.astimezone(FUSO)
    st.caption(f'{usuario_id} · {perfil} · sessão iniciada em {inicio:%d/%m %H:%M}')
    if aviso := estado.pop('aviso', None):
        st.warning(aviso, icon=':material/warning:')

    if not mensagens and not estado.pendente:
        with st.chat_message('assistant'):
            st.markdown(
                'Olá! Posso interpretar pedidos, consultar certificações, lotes, safra, preços e '
                'financeiro, e preparar resumos e rascunhos para produtores. '
                'Escreva sua pergunta abaixo ou use uma sugestão ao lado.'
            )

    for msg in mensagens:
        if msg.papel == 'usuario':
            with st.chat_message('user'):
                st.markdown(msg.texto)
        elif msg.turno is not None:
            with st.chat_message('assistant'):
                render_resposta(msg.turno, usuario_id)

    placeholder = 'Pergunte sobre pedidos, produtores, lotes, safra…'
    st.chat_input(
        'Aguarde a resposta…' if estado.processando else placeholder,
        key='entrada',
        on_submit=enviar,
        disabled=estado.processando,
        submit_mode='disable',
        max_chars=4000,
    )

    if estado.pendente:
        mensagem = estado.pendente
        with st.chat_message('user'):
            st.markdown(mensagem)
        with st.chat_message('assistant'):
            try:
                with st.spinner('Consultando dados e documentos…'):
                    executar_turno(sessao.id, usuario_id, mensagem)
            except TurnoEmAndamento:
                estado.aviso = (
                    'Já havia uma resposta em andamento nesta sessão; aguarde e envie de novo.'
                )
            except psycopg.Error:
                logger.exception('Falha de banco ao gravar o turno')
                estado.aviso = 'Não consegui gravar a conversa no banco. Tente novamente.'
        estado.pendente = None
        estado.processando = False
        st.rerun()


if __name__ == '__main__':
    main()
