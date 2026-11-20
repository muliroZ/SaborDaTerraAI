from datetime import date
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from sabor.agent.deps import AgentDeps
from sabor.agent.mascarador import Mascarador
from sabor.db import conectar_readonly
from sabor.tools import resumir_produtor
from sabor.tools.base import SemPermissao
from sabor.tools.resumir_produtor import _chunk_tem_nc_para_produtor


def _ctx(conn, usuario: str = 'Dona Lúcia') -> RunContext[AgentDeps]:
    deps = AgentDeps(
        conn=conn,
        data_referencia=date(2026, 10, 30),
        usuario=usuario,
        mascarador=Mascarador(),
    )
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def test_resumir_p001_por_id_e_por_nome():
    """Teste da issue: P001 = R$ 1.090,00 em setembro, nada em outubro, R$ 0,00 pendente."""
    with conectar_readonly() as conn:
        res_id = resumir_produtor(_ctx(conn, 'Dona Lúcia'), 'P001')
        res_nome = resumir_produtor(_ctx(conn, 'Dona Lúcia'), 'Maria das Graças')

    assert res_id.encontrado is True
    assert res_id.produtor_id == 'P001'
    assert res_id.vendas_setembro == 1090.00
    assert res_id.vendas_outubro == 0.00
    assert res_id.total_pendente == 0.00
    assert res_id.rascunho is not None
    assert res_id.rascunho.canal == 'padrao'
    assert 'R$ 1.090,00' in res_id.rascunho.conteudo

    assert res_nome.produtor_id == 'P001'
    assert res_nome.vendas_setembro == 1090.00


def test_perfil_produtor_so_pode_ver_proprio_resumo():
    """Produtor só pode pedir o próprio resumo; pedir o de outro é recusado na ferramenta."""
    with conectar_readonly() as conn:
        proprio = resumir_produtor(_ctx(conn, 'P001'), 'P001')
        assert proprio.encontrado is True
        assert proprio.produtor_id == 'P001'

        with pytest.raises(SemPermissao):
            resumir_produtor(_ctx(conn, 'P001'), 'P002')

        with pytest.raises(SemPermissao):
            resumir_produtor(_ctx(conn, 'P002'), 'Maria das Graças')


def test_alertas_certificacao_e_adagro():
    """Verifica alertas de certificação vencendo (P004), vencida (P008) e NC ADAGRO (P017)."""
    with conectar_readonly() as conn:
        res_p004 = resumir_produtor(_ctx(conn), 'P004')
        res_p008 = resumir_produtor(_ctx(conn), 'P008')
        res_p017 = resumir_produtor(_ctx(conn), 'P017')

    assert any('vencendo' in a.lower() for a in res_p004.alertas)
    assert any('vencida' in a.lower() for a in res_p008.alertas)
    assert any('adagro' in a.lower() or 'sie' in a.lower() for a in res_p017.alertas)


def test_chunk_nc_nao_vaza_irregularidade_de_outro_produtor():
    """Documento coletivo com P001 REGULAR e P017 IRREGULAR não deve alertar P001."""
    chunk_coletivo = (
        '1. P001 - Maria das Graças S. Ferreira (Garanhuns)\n'
        '   Certificação orgânica: VIGENTE\n'
        '   Situação: REGULAR\n\n'
        '5. P017 - Wellington da Silva Monteiro (Garanhuns)\n'
        '   Registro SIE: NÃO POSSUI\n'
        '   Situação: IRREGULAR - Produtor sem SIE.\n'
    )
    assert _chunk_tem_nc_para_produtor(chunk_coletivo, 'P001', None) is False
    assert _chunk_tem_nc_para_produtor(chunk_coletivo, 'P017', None) is True

    chunk_compacto = 'P001: Situação REGULAR\nP017: Situação IRREGULAR'
    assert _chunk_tem_nc_para_produtor(chunk_compacto, 'P001', None) is False
    assert _chunk_tem_nc_para_produtor(chunk_compacto, 'P017', None) is True

    # Documento individual do próprio produtor avalia o texto todo
    assert _chunk_tem_nc_para_produtor('NC-002 pendente de adequação.', 'P003', 'P003') is True


def test_produtores_regulares_na_adagro_nao_recebem_falso_positivo():
    """P001, P002 e P005 são regulares na inspeção ADAGRO; P003 só cita seu próprio laudo."""
    with conectar_readonly() as conn:
        res_p001 = resumir_produtor(_ctx(conn), 'P001')
        res_p002 = resumir_produtor(_ctx(conn), 'P002')
        res_p003 = resumir_produtor(_ctx(conn), 'P003')
        res_p005 = resumir_produtor(_ctx(conn), 'P005')
        res_p017 = resumir_produtor(_ctx(conn), 'P017')

    for res in (res_p001, res_p002, res_p005):
        assert not any('inspecao_adagro' in a.lower() for a in res.alertas)
        assert not any('não conformidade' in a.lower() for a in res.alertas)

    assert any('laudo_sanitario_queijaria_cicero_p003' in a.lower() for a in res_p003.alertas)
    assert not any('inspecao_adagro' in a.lower() for a in res_p003.alertas)
    assert any('inspecao_adagro' in a.lower() for a in res_p017.alertas)


def test_resumo_traz_situacao_da_certificacao_e_sie():
    """F4 do Bloco 4: sem isso, o modelo complementava o resumo com `buscar_documentos`."""
    with conectar_readonly() as conn:
        maria = resumir_produtor(_ctx(conn), 'P001')
        josefa = resumir_produtor(_ctx(conn), 'P008')
        cicero = resumir_produtor(_ctx(conn), 'P003')
        inexistente = resumir_produtor(_ctx(conn), 'P999')

    assert (maria.certificacao_organica, maria.validade_certificacao) == (
        'vigente', date(2027, 6, 15)
    )
    assert maria.registro_sie is None
    assert (josefa.certificacao_organica, josefa.validade_certificacao) == (
        'vencida', date(2026, 9, 30)
    )
    assert (cicero.certificacao_organica, cicero.registro_sie) == ('nao_possui', 'PE-SIE-0756')
    assert inexistente.encontrado is False and inexistente.certificacao_organica is None
