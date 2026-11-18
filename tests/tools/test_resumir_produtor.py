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