"""Testes unitários do Mascarador de PII por turno e integração 3.3 -> 4c (Issue #33)."""

from unittest.mock import MagicMock

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sabor.agent.deps import AgentDeps
from sabor.agent.mascarador import Mascarador
from sabor.agent.runner import criar_agente, processar_turno


def test_mascarar_e_restaurar_cpf_com_e_sem_pontuacao():
    m = Mascarador()
    texto = 'Produtora Maria CPF 831.452.674-90 e segundo CPF: 29418756341 ou avulso 72301649855.'
    mascarado = m.mascarar(texto)

    assert '831.452.674-90' not in mascarado
    assert '29418756341' not in mascarado
    assert '72301649855' not in mascarado
    assert '[CPF_1]' in mascarado
    assert '[CPF_2]' in mascarado
    assert '[CPF_3]' in mascarado
    assert m.restaurar(mascarado) == texto


def test_mascarar_e_restaurar_rg_com_e_sem_pontuacao():
    m = Mascarador()
    texto = 'Cláudia Mendonça - RG: 5.432.109 SDS/PE e outro RG 1234567.'
    mascarado = m.mascarar(texto)

    assert '5.432.109' not in mascarado
    assert '1234567' not in mascarado
    assert '[RG_1]' in mascarado
    assert '[RG_2]' in mascarado
    assert m.restaurar(mascarado) == texto


def test_mascarar_e_restaurar_telefone_com_e_sem_pontuacao():
    m = Mascarador()
    texto = 'Contatos: (87) 99812-3456, (87) 3761-5000, Tel: 87996347812 e avulso 87997451098.'
    mascarado = m.mascarar(texto)

    assert '99812-3456' not in mascarado
    assert '3761-5000' not in mascarado
    assert '87996347812' not in mascarado
    assert '87997451098' not in mascarado
    assert '[TEL_1]' in mascarado
    assert '[TEL_2]' in mascarado
    assert '[TEL_3]' in mascarado
    assert '[TEL_4]' in mascarado
    assert m.restaurar(mascarado) == texto


def test_mascarar_email_banco_agencia_conta_e_pix():
    m = Mascarador()
    texto = (
        'E-mail: jose.santos.mel@email.com | '
        'Banco do Nordeste - Ag. 0023 - C/C 63478-2 | '
        'Conta 55.678-9 | '
        'Chave PIX: 890.123.456-78'
    )
    mascarado = m.mascarar(texto)

    assert 'jose.santos.mel@email.com' not in mascarado
    assert 'Banco do Nordeste' not in mascarado
    assert '0023' not in mascarado
    assert '63478-2' not in mascarado
    assert '55.678-9' not in mascarado
    assert '890.123.456-78' not in mascarado

    assert '[EMAIL_1]' in mascarado
    assert '[BANCO_1]' in mascarado
    assert '[AGENCIA_1]' in mascarado
    assert '[CONTA_1]' in mascarado
    assert '[CONTA_2]' in mascarado
    assert '[PIX_1]' in mascarado
    assert m.restaurar(mascarado) == texto


def test_mascarar_valores_isolados_vindos_das_tools():
    m = Mascarador()
    assert m.mascarar('0437') == '[AGENCIA_1]'
    assert m.mascarar('00134578-2') == '[CONTA_1]'
    assert m.mascarar('Banco do Brasil') == '[BANCO_1]'
    assert m.mascarar_campo('831.452.674-90', 'PIX') == '[PIX_1]'

    # Idempotência no mesmo turno: repetir o mesmo valor reutiliza o mesmo token
    assert m.mascarar('0437') == '[AGENCIA_1]'
    assert m.restaurar('[AGENCIA_1] / [CONTA_1] / [PIX_1]') == '0437 / 00134578-2 / 831.452.674-90'


def test_nao_mascara_codigos_de_negocio_datas_ou_cnpj():
    m = Mascarador()
    texto = (
        'Lote LOT-2026-0089, transação FIN-2026-013, pedido PED-2026-001, '
        'produtor P008, SIE PE-SIE-0756, DAP SDR/PE-2019-0047821, '
        'data 30/10/2026, CNPJ 10.578.224/0034-56 e 500kg.'
    )
    assert m.mascarar(texto) == texto


def test_fluxo_completo_mascara_prompt_ped_013_e_protege_rascunhos():
    prompts_usuario_enviados: list[str] = []

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        for msg in messages:
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, UserPromptPart):
                        prompts_usuario_enviados.append(str(part.content))
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={
                        'texto': 'Pedido da coordenadora (CPF [CPF_1], RG [RG_1]) registrado.',
                        'fontes': [{'arquivo': 'pedidos.json', 'referencia': 'PED-2026-013'}],
                        'flags': [],
                        'rascunhos': [
                            {
                                'destinatario': 'SESC Garanhuns',
                                'canal': 'email',
                                'conteudo': 'Confirmamos recebimento para [CPF_1] / [RG_1].',
                            }
                        ],
                    },
                )
            ]
        )

    agente = criar_agente([FunctionModel(fn_modelo, model_name='gemini-3.5-flash-lite')])
    deps = AgentDeps(conn=MagicMock(), usuario='Dona Lúcia')

    mensagem_ped_013 = (
        'Cláudia Mendonça - Coordenadora\n'
        'RG: 5.432.109 SDS/PE\n'
        'CPF: 876.543.210-98'
    )

    turno = processar_turno(
        sessao_id='sess-ped-013',
        mensagem=mensagem_ped_013,
        usuario='Dona Lúcia',
        agente=agente,
        deps=deps,
    )

    # 1. O prompt que chegou ao LLM estava mascarado (sem RG nem CPF reais)
    assert len(prompts_usuario_enviados) == 1
    assert '5.432.109' not in prompts_usuario_enviados[0]
    assert '876.543.210-98' not in prompts_usuario_enviados[0]
    assert '[RG_1]' in prompts_usuario_enviados[0]
    assert '[CPF_1]' in prompts_usuario_enviados[0]

    # 2. Etapa 4c: O texto para o operador interno tem os valores restaurados
    assert '876.543.210-98' in turno.texto
    assert '5.432.109 SDS/PE' in turno.texto

    # 3. Etapa 4c: O rascunho para terceiros NUNCA tem os tokens restaurados
    assert '876.543.210-98' not in turno.rascunhos[0].conteudo
    assert '5.432.109' not in turno.rascunhos[0].conteudo


def test_ferramentas_devolvem_resultados_ja_mascarados(monkeypatch):
    from datetime import date
    from sabor.tools import buscar_documentos, buscar_produtores, consultar_financeiro

    conn = MagicMock()
    deps = AgentDeps(conn=conn, usuario='Dona Lúcia')
    ctx = MagicMock()
    ctx.deps = deps

    # 1. buscar_produtores (#7)
    conn.execute.return_value.fetchall.return_value = [
        (
            'P001',
            'Maria das Graças',
            '831.452.674-90',
            '(87) 99812-3456',
            'maria.graca@email.com',
            'Garanhuns',
            True,
            date(2027, 6, 15),
            'ativo',
            '831.452.674-90',
            'Banco do Brasil',
            '0437',
            '28901-3',
            'SDR/PE-2019-0047821',
            None,
            ['goiaba'],
        )
    ]
    produtores = buscar_produtores(ctx)
    assert len(produtores) == 1
    p = produtores[0]
    assert p.cpf == '[CPF_1]'
    assert p.telefone == '[TEL_1]'
    assert p.email == '[EMAIL_1]'
    assert p.chave_pix == '[PIX_1]'
    assert p.banco == '[BANCO_1]'
    assert p.agencia == '[AGENCIA_1]'
    assert p.conta_corrente == '[CONTA_1]'

    # 2. consultar_financeiro (#7)
    conn.execute.return_value.fetchall.return_value = [
        (
            'FIN-2026-001',
            date(2026, 9, 10),
            'P001',
            'Mercado Bom Preço',
            'goiaba',
            100.0,
            'kg',
            5.40,
            540.0,
            'pago',
            date(2026, 10, 1),
            date(2026, 10, 1),
            False,
            '831.452.674-90',
        )
    ]
    res_fin = consultar_financeiro(ctx, produtor_id='P001')
    assert res_fin.itens[0].chave_pix_deposito == '[PIX_1]'

    # 3. buscar_documentos (#8)
    monkeypatch.setattr(
        'sabor.tools.documentos._gerar_embedding_consulta',
        lambda _: [0.1] * 768,
    )
    conn.execute.return_value.fetchall.return_value = [
        (
            'certificacoes/cert_organica_maria_silva_P001.txt',
            'DADOS DO PRODUTOR',
            'certificado',
            'P001',
            date(2027, 6, 15),
            'Titular Maria das Graças, CPF: 831.452.674-90, Banco do Brasil - Ag. 0437.',
            'Maria das Graças',
            'ativo',
            date(2027, 6, 15),
            None,
            'SDR/PE-2019-0047821',
            0.032,
            0.85,
            0.50,
        )
    ]
    res_doc = buscar_documentos(ctx, 'certificado P001')
    trecho = res_doc.resultados[0].trecho
    assert '831.452.674-90' not in trecho
    assert 'Banco do Brasil' not in trecho
    assert '0437' not in trecho
    assert '[CPF_1]' in trecho
    assert '[BANCO_1]' in trecho
    assert '[AGENCIA_1]' in trecho


def test_mascarar_todos_os_18_produtores_do_csv():
    import csv
    from pathlib import Path

    caminho = Path('src/sabor/etl/data/bronze/produtores.csv')
    with caminho.open(encoding='utf-8') as f:
        linhas = list(csv.DictReader(f))

    assert len(linhas) == 18
    for row in linhas:
        m = Mascarador()
        cpf_m = m.mascarar(row['cpf'])
        tel_m = m.mascarar(row['telefone'])
        email_m = m.mascarar(row['email'])
        pix_m = m.mascarar_campo(row['chave_pix'], 'PIX')
        banco_m = m.mascarar(row['banco'])
        ag_m = m.mascarar(row['agencia'])
        cc_m = m.mascarar(row['conta_corrente'])

        assert cpf_m == '[CPF_1]'
        assert tel_m == '[TEL_1]'
        assert email_m == '[EMAIL_1]'
        assert pix_m == '[PIX_1]'
        assert banco_m == '[BANCO_1]'
        assert ag_m == '[AGENCIA_1]'
        assert cc_m == '[CONTA_1]'

        assert m.restaurar(cpf_m) == row['cpf']
        assert m.restaurar(tel_m) == row['telefone']
        assert m.restaurar(email_m) == row['email']
        assert m.restaurar(pix_m or '') == row['chave_pix']
        assert m.restaurar(banco_m) == row['banco']
        assert m.restaurar(ag_m) == row['agencia']
        assert m.restaurar(cc_m) == row['conta_corrente']


def test_mascarar_pedidos_reais_ped_012_013_015():
    m = Mascarador()

    ped_012 = (
        'Dona Lúcia, tô ligando pra saber quanto eu vendi esse mês. '
        'Minha conta é no BB agência 0437. Me diz quanto tá o saldo.'
    )
    m_012 = m.mascarar(ped_012)
    assert '0437' not in m_012
    assert '[AGENCIA_1]' in m_012
    assert m.restaurar(m_012) == ped_012

    ped_015 = (
        'O CPF dele é 432.198.765-01, mora aqui no Sítio Vizinho, Caetés. '
        'Posso passar o contato dele?'
    )
    m_015 = m.mascarar(ped_015)
    assert '432.198.765-01' not in m_015
    assert '[CPF_1]' in m_015
    assert m.restaurar(m_015) == ped_015


def test_isolamento_entre_turnos_e_idempotencia_dupla_chamada():
    m_turno_1 = Mascarador()
    m_turno_2 = Mascarador()

    t1 = m_turno_1.mascarar('CPF 831.452.674-90 e Chave PIX: 123e4567-e89b-12d3-a456-426614174000')
    assert t1 == 'CPF [CPF_1] e Chave PIX: [PIX_1]'
    # Aplicar mascarar novamente sobre o texto já mascarado é idempotente
    assert m_turno_1.mascarar(t1) == t1

    # O turno 2 tem mapa limpo em memória e não conhece os tokens do turno 1
    assert m_turno_2.mapa_tokens == {}
    assert m_turno_2.restaurar('[CPF_1]') == '[CPF_1]'


