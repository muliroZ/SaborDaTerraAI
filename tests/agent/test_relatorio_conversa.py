"""Relatório somente leitura da conversa (#38), montado no backend sem LLM."""

from datetime import date, datetime, timezone

from sabor.agent import memoria as sess
from sabor.agent.auditoria import registrar_auditoria
from sabor.agent.relatorio_conversa import (
    AuditoriaTurno,
    _secao_turno,
    agrupar_turnos,
    gerar_relatorio,
    listar_auditoria,
    mascara_pii_no_relatorio,
    nome_arquivo_relatorio,
    relatorio_da_sessao,
)
from sabor.agent.schemas import (
    Flag,
    Fonte,
    Rascunho,
    Relatorio,
    RespostaTurno,
    RetornoAntecipado,
)

INICIO = datetime(2026, 11, 18, 13, 5, tzinfo=timezone.utc)  # 10:05 em Recife


def _sessao(usuario_id: str = 'Dona Lúcia', **kwargs) -> sess.Sessao:
    return sess.Sessao(
        id='0000',
        usuario_id=usuario_id,
        perfil=sess.perfil_do_usuario(usuario_id),
        criada_em=INICIO,
        **kwargs,
    )


def _turno(texto: str, **kwargs) -> RespostaTurno:
    kwargs.setdefault('usuario', 'Dona Lúcia')
    return RespostaTurno(sessao_id='0000', texto=texto, **kwargs)


def _conversa(pergunta: str, turno: RespostaTurno) -> list[sess.Mensagem]:
    return [
        sess.Mensagem(1, 'usuario', pergunta, INICIO),
        sess.Mensagem(2, 'assistente', turno.texto, INICIO, turno),
    ]


# ---------------------------------------------------------------------------
# Unidade: montagem determinística
# ---------------------------------------------------------------------------


def test_relatorio_com_perguntas_respostas_fontes_flags_e_rascunhos():
    turno = _turno(
        'Não. P017 está suspenso.',
        fontes=[Fonte(arquivo='inspecao_adagro_2026.txt', referencia='NC-001')],
        flags=[Flag(tipo='conformidade', descricao='Sem SIE')],
        rascunhos=[Rascunho(destinatario='Hotel Fazenda', canal='email', conteudo='Prezados,')],
        modelo_efetivo='gemini-3.5-flash-lite',
        latencia_ms=2100,
    )
    md = gerar_relatorio(
        _sessao(),
        _conversa('Posso mandar frango para o Hotel Fazenda?', turno),
        data_referencia=date(2026, 10, 30),
    )

    assert md.startswith('# Relatório da conversa')
    assert '**Usuário:** Dona Lúcia (funcionário(a))' in md
    assert '**Início:** 18/11/2026 10:05' in md
    assert '**Encerramento:** em andamento' in md
    assert '30/10/2026' in md
    assert '> Posso mandar frango para o Hotel Fazenda?' in md
    assert 'Não. P017 está suspenso.' in md
    assert '`[inspecao_adagro_2026.txt · NC-001]`' in md
    assert '**conformidade:** Sem SIE' in md
    assert 'Para **Hotel Fazenda** (email)' in md
    assert '```text\nPrezados,\n```' in md
    assert 'modelo gemini-3.5-flash-lite' in md and '2100 ms' in md
    assert '- **Rascunhos gerados:** 1' in md and '- **Sinalizações:** 1' in md


def test_relatorio_e_deterministico():
    mensagens = _conversa('p', _turno('r', trace_id='t-1'))
    data = date(2026, 10, 30)
    assert gerar_relatorio(_sessao(), mensagens, data_referencia=data) == gerar_relatorio(
        _sessao(), mensagens, data_referencia=data
    )


def test_relatorio_marca_retornos_antecipados_e_falha_segura():
    bloqueado = _turno(
        'Só ajudo com a cooperativa.',
        bloqueado_guardrail=True,
        retorno_antecipado=RetornoAntecipado(motivo='FORA_DE_ESCOPO', mensagem='x'),
    )
    md = gerar_relatorio(_sessao(), _conversa('conta uma piada', bloqueado))
    assert '_Fora de escopo._' in md
    assert 'retorno antecipado `FORA_DE_ESCOPO`' in md

    falha = _turno('Tente de novo.', falha_segura=True)
    md = gerar_relatorio(_sessao(), _conversa('oi', falha))
    assert '_Falha segura._' in md
    assert '**Retornos antecipados / falhas seguras:** 0 / 1' in md


def test_relatorio_vazio_e_nome_do_arquivo():
    md = gerar_relatorio(_sessao(encerrada_em=INICIO), [])
    assert 'Nenhuma mensagem nesta sessão' in md
    assert '**Encerramento:** 18/11/2026 10:05' in md
    assert nome_arquivo_relatorio(_sessao()) == 'relatorio_dona-lucia_20261118-1005.md'


def test_rascunho_com_crases_nao_quebra_o_markdown():
    turno = _turno('ok', rascunhos=[Rascunho(destinatario='X', canal='email', conteudo='a ``` b')])
    assert '````text\na ``` b\n````' in gerar_relatorio(_sessao(), _conversa('p', turno))


def test_lista_relatorios_gerados():
    resposta = _turno(
        'Pronto.',
        relatorios=[
            Relatorio(
                tipo='contas_a_receber',
                titulo='Contas a receber',
                nome_arquivo='contas_a_receber_20261030.md',
                markdown='# x',
            )
        ],
    )
    turno = agrupar_turnos([sess.Mensagem(2, 'assistente', 'Pronto.', INICIO, resposta)])[0]
    linhas = '\n'.join(_secao_turno(1, turno, str))
    assert '**Relatórios gerados**' in linhas
    assert '- Contas a receber (`contas_a_receber_20261030.md`)' in linhas


def test_auditoria_tem_prioridade_sobre_os_campos_do_turno():
    turno = _turno('r', ferramentas=['x'], tokens_entrada=1, tokens_saida=1)
    auditoria = {
        2: AuditoriaTurno(
            message_id=2,
            modelo='qwen3-32b',
            provedor='groq',
            ferramentas=['consultar_financeiro', 'buscar_documentos'],
            regras_disparadas=['R-CREDITO'],
            cache_hit=True,
            tokens_entrada=120,
            tokens_saida=30,
            trace_id='abc',
        )
    }
    md = gerar_relatorio(_sessao(), _conversa('p', turno), auditoria)
    assert 'ferramentas: `consultar_financeiro`, `buscar_documentos`' in md
    assert 'regras: `R-CREDITO`' in md
    assert 'resposta do cache' in md
    assert 'modelo qwen3-32b (groq)' in md
    assert 'tokens 120 in / 30 out' in md
    assert 'trace `abc`' in md
    assert '- **Respostas do cache:** 1' in md
    assert '`consultar_financeiro` (1x)' in md


def test_pergunta_sem_resposta_vira_turno_sem_resposta():
    mensagens = [
        sess.Mensagem(1, 'usuario', 'primeira', INICIO),
        sess.Mensagem(2, 'usuario', 'segunda', INICIO),
    ]
    md = gerar_relatorio(_sessao(), mensagens)
    assert md.count('_Sem resposta registrada._') == 2
    assert '- **Turnos:** 2' in md


# ---------------------------------------------------------------------------
# Dados pessoais conforme o perfil (#33)
# ---------------------------------------------------------------------------

CPF = '831.452.674-90'


def test_politica_de_pii_por_perfil():
    assert not mascara_pii_no_relatorio('funcionario')
    assert mascara_pii_no_relatorio('produtor')


def test_funcionario_ve_texto_como_exibido_e_rascunho_segue_mascarado():
    turno = _turno(
        f'O CPF do produtor é {CPF}.',
        rascunhos=[Rascunho(destinatario='Hospital', canal='email', conteudo='CPF: [CPF_1]')],
    )
    md = gerar_relatorio(_sessao(), _conversa(f'confere o CPF {CPF}?', turno))
    assert f'O CPF do produtor é {CPF}.' in md
    assert f'> confere o CPF {CPF}?' in md
    assert 'CPF: [CPF_1]' in md


def test_produtor_recebe_pergunta_e_resposta_mascaradas():
    turno = _turno(f'Seu CPF cadastrado é {CPF}.', usuario='P002')
    md = gerar_relatorio(_sessao('P002'), _conversa(f'meu CPF é {CPF}?', turno))
    assert CPF not in md
    assert '[CPF_1]' in md
    assert 'aparecem mascarados' in md


# ---------------------------------------------------------------------------
# Integração com o banco
# ---------------------------------------------------------------------------


def test_relatorio_da_sessao_le_mensagens_e_auditoria(conn_temporaria):
    s = sess.criar_sessao(conn_temporaria, 'P002')
    turno = RespostaTurno(
        sessao_id=s.id,
        usuario='P002',
        texto='R$ 0,00',
        ferramentas=['consultar_financeiro'],
        tokens_entrada=50,
        tokens_saida=10,
        trace_id='trace-p002',
    )
    message_id = sess.registrar_turno(conn_temporaria, s.id, 'quanto vendi?', turno)
    registrar_auditoria(
        conn_temporaria, message_id=message_id, usuario_id='P002', perfil='produtor', turno=turno
    )

    auditoria = listar_auditoria(conn_temporaria, s.id)
    assert auditoria[message_id].ferramentas == ['consultar_financeiro']

    nome, md = relatorio_da_sessao(conn_temporaria, s.id)
    assert nome.startswith('relatorio_p002_') and nome.endswith('.md')
    assert '> quanto vendi?' in md and 'R$ 0,00' in md
    assert '(produtor(a))' in md
    assert 'ferramentas: `consultar_financeiro`' in md
    assert 'trace `trace-p002`' in md


def test_criterio_de_aceite_nova_sessao_gera_relatorio_e_comeca_sem_contexto(conn_temporaria):
    """Conversa de demo → "Nova sessão" → relatório da anterior e nova conversa sem contexto."""
    demo = sess.criar_sessao(conn_temporaria, 'Seu Antônio')
    perguntas = (
        'Quais produtores têm certificação orgânica vigente?',
        'Posso mandar frango para o Hotel Fazenda?',
    )
    for pergunta in perguntas:
        turno = RespostaTurno(
            sessao_id=demo.id,
            usuario='Seu Antônio',
            texto=f'Resposta para: {pergunta}',
            fontes=[Fonte(arquivo='produtores.csv', referencia='P003')],
            flags=[Flag(tipo='conformidade', descricao='Checar SIE')],
        )
        message_id = sess.registrar_turno(conn_temporaria, demo.id, pergunta, turno)
        registrar_auditoria(
            conn_temporaria,
            message_id=message_id,
            usuario_id='Seu Antônio',
            perfil='funcionario',
            turno=turno,
        )
    assert len(sess.carregar_historico(conn_temporaria, demo.id)) == 4

    _, antes = relatorio_da_sessao(conn_temporaria, demo.id)
    nova = sess.nova_sessao(conn_temporaria, demo.id, 'Seu Antônio')

    # A nova conversa começa sem contexto.
    assert nova.id != demo.id and nova.ativa
    assert sess.carregar_historico(conn_temporaria, nova.id) == []
    assert sess.sessao_ativa(conn_temporaria, 'Seu Antônio').id == nova.id

    # A anterior fica só para leitura, com o relatório completo.
    assert [s.id for s in sess.listar_sessoes_encerradas(conn_temporaria, 'Seu Antônio')] == [
        demo.id
    ]
    _, depois = relatorio_da_sessao(conn_temporaria, demo.id)
    assert '**Encerramento:** em andamento' in antes
    assert '**Encerramento:** em andamento' not in depois
    for pergunta in perguntas:
        assert f'> {pergunta}' in depois
    assert '- **Turnos:** 2' in depois
    assert '`[produtores.csv · P003]`' in depois
