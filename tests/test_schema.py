"""Testes do schema.

Os testes de integração criam um banco temporário no Postgres do docker-compose,
então não tocam no banco de desenvolvimento. São pulados se o banco não estiver no ar.
"""

import re
import uuid
from decimal import Decimal

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from sabor.db import (
    EMBEDDING_DIM,
    READONLY_ROLE,
    TABELAS,
    conectar,
    conectar_readonly,
    criar_schema,
    database_url,
    resetar_schema,
    schema_sql,
    tabelas_existentes,
)
from sabor.agent.auditoria import registrar_auditoria
from sabor.agent.schemas import Fonte, RespostaTurno


def test_role_readonly_definida_no_schema_sem_senha_exposta():
    sql = schema_sql()
    assert f'CREATE ROLE {READONLY_ROLE} NOLOGIN' in sql
    assert f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READONLY_ROLE}' in sql
    assert 'PASSWORD' not in sql.upper()


def test_dimensao_do_embedding_igual_ao_schema():
    dims = re.findall(r'vector\((\d+)\)', schema_sql())
    assert dims == [str(EMBEDDING_DIM)]


def test_todas_as_tabelas_do_schema_estao_listadas():
    criadas = re.findall(r'CREATE (?:UNLOGGED )?TABLE IF NOT EXISTS (\w+)', schema_sql())
    assert sorted(criadas) == sorted(TABELAS)


@pytest.fixture(scope='module')
def conn():
    base = database_url()
    try:
        admin = conectar(base, tentativas=1, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip('Postgres indisponível (rode `docker compose up -d`)')

    nome = f'sabor_test_{uuid.uuid4().hex[:8]}'
    admin.execute(f'CREATE DATABASE {nome}')
    params = conninfo_to_dict(base) | {'dbname': nome}
    c = psycopg.connect(make_conninfo(**params))
    try:
        criar_schema(c)
        yield c
    finally:
        c.close()
        admin.execute(f'DROP DATABASE {nome}')
        admin.close()


def test_cria_todas_as_tabelas_vazias(conn):
    assert sorted(tabelas_existentes(conn)) == sorted(TABELAS)
    for tabela in TABELAS:
        assert conn.execute(f'SELECT count(*) FROM {tabela}').fetchone()[0] == 0


def test_criar_schema_e_idempotente(conn):
    criar_schema(conn)
    criar_schema(conn)
    assert sorted(tabelas_existentes(conn)) == sorted(TABELAS)


def test_auditoria_liga_resposta_a_mensagem_e_sessao(conn):
    with conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO sessoes (id, usuario_id, perfil) "
            "VALUES ('sess-audit', 'Dona Lúcia', 'funcionario')"
        )
        message_id = conn.execute(
            "INSERT INTO mensagens (sessao_id, papel, conteudo) "
            "VALUES ('sess-audit', 'assistente', '{}'::jsonb) RETURNING id"
        ).fetchone()[0]
        turno = RespostaTurno(
            sessao_id='sess-audit',
            usuario='Dona Lúcia',
            texto='Resposta com CPF 123.456.789-00, que não deve ir para a auditoria',
            fontes=[Fonte(arquivo='produtores.csv', referencia='CPF 123.456.789-00')],
            regras_disparadas=['4B_SEM_FONTE_FACTUAL'],
            modelo_efetivo='gemini-3.5-flash-lite',
            provedor_efetivo='google',
            ferramentas=['buscar_produtores'],
            tokens_entrada=123,
            tokens_saida=45,
            trace_id='trace-test-35',
        )

        registrar_auditoria(
            conn,
            message_id=message_id,
            usuario_id='Dona Lúcia',
            perfil='funcionario',
            turno=turno,
        )

        row = conn.execute(
            'SELECT message_id, sessao_id, usuario_id, perfil, modelo, provedor, '
            'ferramentas, fontes, regras_disparadas, retorno_antecipado, cache_hit, '
            'tokens_entrada, tokens_saida, trace_id, mensagem_usuario, resposta_assistente '
            'FROM auditoria_mensagens WHERE message_id = %s',
            (message_id,),
        ).fetchone()
        assert row[:6] == (
            message_id, 'sess-audit', 'Dona Lúcia', 'funcionario',
            'gemini-3.5-flash-lite', 'google',
        )
        assert row[6] == ['buscar_produtores']
        assert row[7] == [{'arquivo': 'produtores.csv', 'referencia': 'CPF [CPF_1]'}]
        assert row[8] == ['4B_SEM_FONTE_FACTUAL']
        assert row[9:14] == (None, False, 123, 45, 'trace-test-35')
        # A auditoria não duplica em claro o texto do usuário nem a resposta.
        assert row[14:] == (None, None)
        assert '123.456.789-00' not in str(row)


def test_auditoria_rejeita_sessao_ou_mensagem_sem_relacao(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute("INSERT INTO auditoria_mensagens (sessao_id) VALUES ('sessao-inexistente')")

    with conn.transaction(force_rollback=True):
        conn.execute(
            "INSERT INTO sessoes (id, usuario_id, perfil) "
            "VALUES ('sess-audit-fk', 'Dona Lúcia', 'funcionario')"
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute(
                "INSERT INTO auditoria_mensagens (message_id, sessao_id) "
                "VALUES (999999, 'sess-audit-fk')"
            )


def test_embedding_e_indice_hnsw(conn):
    tipo = conn.execute(
        'SELECT format_type(atttypid, atttypmod) FROM pg_attribute '
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    ).fetchone()[0]
    assert tipo == f'vector({EMBEDDING_DIM})'

    indexdef = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_hnsw_idx'"
    ).fetchone()[0]
    assert 'USING hnsw' in indexdef
    assert 'vector_cosine_ops' in indexdef


def _produtor(conn, pid='P001'):
    conn.execute(
        'INSERT INTO produtores (id, nome_completo, municipio, status) '
        "VALUES (%s, 'Teste', 'Garanhuns', 'ativo')",
        (pid,),
    )


def test_quantidade_com_unidade_separada(conn):
    with conn.transaction(force_rollback=True):
        _produtor(conn)
        conn.execute(
            'INSERT INTO financeiro (id_transacao, data, produtor_id, comprador, produto, '
            'quantidade, unidade, valor_unitario, valor_total, status_pagamento) '
            "VALUES ('FIN-T', '2026-10-01', 'P001', 'X', 'ovos caipira', 40, 'dúzia', 12, 480, "
            "'pendente')"
        )
        qtd, unidade = conn.execute(
            "SELECT quantidade, unidade FROM financeiro WHERE id_transacao = 'FIN-T'"
        ).fetchone()
        assert (qtd, unidade) == (Decimal('40.000'), 'dúzia')

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _produtor(conn)
        conn.execute(
            'INSERT INTO lotes (lote_id, data_colheita, data_entrega, produtor_id, produto, '
            'quantidade, unidade, refrigerado) '
            "VALUES ('LOT-T', '2026-10-01', '2026-10-01', 'P001', 'alface', 300, 'pé', false)"
        )


def test_busca_vetorial_e_lexical(conn):
    with conn.transaction(force_rollback=True):
        _produtor(conn, 'P008')
        conn.execute(
            'INSERT INTO documentos (arquivo, tipo_doc, produtor_id, sha256) '
            "VALUES ('certificacoes/cert_P008.txt', 'certificado', 'P008', %s)",
            ('0' * 64,),
        )
        vetor = '[' + ','.join(['0.1'] * EMBEDDING_DIM) + ']'
        conn.execute(
            'INSERT INTO chunks (arquivo, ordem, secao, texto, embedding) '
            "VALUES ('certificacoes/cert_P008.txt', 0, 'VALIDADE', "
            "'Certificado orgânico de mel válido até 30/09/2026', %s)",
            (vetor,),
        )
        row = conn.execute(
            'SELECT arquivo, secao, tipo_doc, produtor_id, embedding <=> %s::vector AS dist '
            'FROM chunks_busca '
            "WHERE texto_busca @@ plainto_tsquery('portuguese', 'certificados orgânicos') "
            'ORDER BY dist LIMIT 1',
            (vetor,),
        ).fetchone()
        assert row[:4] == ('certificacoes/cert_P008.txt', 'VALIDADE', 'certificado', 'P008')
        assert row[4] == pytest.approx(0)


def test_reset_apaga_dados(conn):
    _produtor(conn, 'P099')
    conn.commit()
    resetar_schema(conn)
    assert conn.execute('SELECT count(*) FROM produtores').fetchone()[0] == 0
    assert sorted(tabelas_existentes(conn)) == sorted(TABELAS)


def test_conexao_readonly_permite_select_e_bloqueia_escrita(conn):
    # `conn.info.dsn` omite a senha: reaproveita a da própria conexão do fixture, que já
    # resolveu `DATABASE_PASSWORD` vazio para o padrão do docker-compose.
    with conectar_readonly(conn.info.dsn, password=conn.info.password, tentativas=1) as ro_conn:
        assert ro_conn.read_only is True
        usuario = ro_conn.execute('SELECT current_user').fetchone()[0]
        assert usuario == READONLY_ROLE
        assert ro_conn.execute('SELECT count(*) FROM produtores').fetchone()[0] == 0
        assert ro_conn.execute('SELECT count(*) FROM chunks_busca').fetchone()[0] == 0

        erros_bloqueio = (
            psycopg.errors.ReadOnlySqlTransaction,
            psycopg.errors.InsufficientPrivilege,
        )
        comandos_escrita = [
            (
                'INSERT INTO produtores (id, nome_completo, municipio, status) '
                "VALUES ('P001', 'Teste', 'Garanhuns', 'ativo')"
            ),
            "UPDATE produtores SET status = 'inativo' WHERE id = 'P001'",
            "DELETE FROM produtores WHERE id = 'P001'",
            'DROP TABLE produtores',
        ]
        for sql_cmd in comandos_escrita:
            with pytest.raises(erros_bloqueio), ro_conn.transaction():
                ro_conn.execute(sql_cmd)
