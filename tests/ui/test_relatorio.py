"""Relatório somente leitura da conversa (#38), montado sem LLM."""

from datetime import date, datetime, timezone

from sabor.agent.schemas import Flag, Fonte, Rascunho, RespostaTurno, RetornoAntecipado
from sabor.agent.memoria import Mensagem, Sessao, criar_sessao, registrar_turno
from sabor.ui.relatorio import gerar_relatorio, nome_arquivo_relatorio, relatorio_da_sessao

INICIO = datetime(2026, 11, 18, 13, 5, tzinfo=timezone.utc)  # 10:05 em Recife


def _sessao(**kwargs) -> Sessao:
    return Sessao(
        id='0000', usuario_id='Dona Lúcia', perfil='funcionario', criada_em=INICIO, **kwargs
    )


def _turno(texto: str, **kwargs) -> RespostaTurno:
    return RespostaTurno(sessao_id='0000', usuario='Dona Lúcia', texto=texto, **kwargs)


def test_relatorio_com_perguntas_respostas_fontes_flags_e_rascunhos():
    turno = _turno(
        'Não. P017 está suspenso.',
        fontes=[Fonte(arquivo='inspecao_adagro_2026.txt', referencia='NC-001')],
        flags=[Flag(tipo='conformidade', descricao='Sem SIE')],
        rascunhos=[Rascunho(destinatario='Hotel Fazenda', canal='email', conteudo='Prezados,')],
        modelo_efetivo='gemini-3.5-flash-lite',
        latencia_ms=2100,
    )
    mensagens = [
        Mensagem(1, 'usuario', 'Posso mandar frango para o Hotel Fazenda?', INICIO),
        Mensagem(2, 'assistente', turno.texto, INICIO, turno),
    ]
    md = gerar_relatorio(_sessao(), mensagens, data_referencia=date(2026, 10, 30))

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
    assert 'gemini-3.5-flash-lite · 2100 ms' in md


def test_relatorio_marca_retornos_antecipados():
    turno = _turno(
        'Só ajudo com a cooperativa.',
        bloqueado_guardrail=True,
        retorno_antecipado=RetornoAntecipado(motivo='FORA_DE_ESCOPO', mensagem='x'),
    )
    mensagens = [
        Mensagem(1, 'usuario', 'conta uma piada', INICIO),
        Mensagem(2, 'assistente', turno.texto, INICIO, turno),
    ]
    assert '_Fora de escopo._' in gerar_relatorio(_sessao(), mensagens)


def test_relatorio_vazio_e_nome_do_arquivo():
    md = gerar_relatorio(_sessao(encerrada_em=INICIO), [])
    assert 'Nenhuma mensagem nesta sessão' in md
    assert '**Encerramento:** 18/11/2026 10:05' in md
    assert nome_arquivo_relatorio(_sessao()) == 'relatorio_dona-lucia_20261118-1005.md'


def test_rascunho_com_crases_nao_quebra_o_markdown():
    turno = _turno('ok', rascunhos=[Rascunho(destinatario='X', canal='email', conteudo='a ``` b')])
    mensagens = [
        Mensagem(1, 'usuario', 'p', INICIO),
        Mensagem(2, 'assistente', 'ok', INICIO, turno),
    ]
    assert '````text\na ``` b\n````' in gerar_relatorio(_sessao(), mensagens)


def test_relatorio_da_sessao_no_banco(conn_temporaria):
    s = criar_sessao(conn_temporaria, 'P002')
    registrar_turno(conn_temporaria, s.id, 'quanto vendi?', _turno('R$ 0,00'))
    nome, md = relatorio_da_sessao(conn_temporaria, s.id)
    assert nome.startswith('relatorio_p002_') and nome.endswith('.md')
    assert '> quanto vendi?' in md and 'R$ 0,00' in md
    assert '(produtor(a))' in md
