import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
from sabor.agent.mascarador import Mascarador
from sabor.tools.pedidos import Pedido, Item, Entrega, extrair_e_classificar_pedido

class Fake:
    def __init__(self, out): self.output, self.prompt = out, None
    async def run(self, prompt): self.prompt = prompt; return SimpleNamespace(output=self.output)

def ctx(conn=None):
    return SimpleNamespace(deps=SimpleNamespace(conn=conn or MagicMock(), data_referencia=date(2026,10,30), mascarador=Mascarador()))

@pytest.mark.parametrize('pid,kind,item,ambiguous', [
 ('001','compra','queijo coalho',False), ('003','compra','queijo da última vez',True),
 ('005','compra','manteiga',True), ('011','cotacao','mango',False)])
def test_pedidos_referencia(monkeypatch,pid,kind,item,ambiguous):
    text={'001':'sexta 01/nov','003':'queijo da última vez CPF 123.456.789-00','005':'manteiga o de sempre','011':'We need organic mango'}[pid]
    out=Pedido(tipo=kind,itens=[Item(produto_texto=item,ambiguo=ambiguous)], entrega=Entrega(data_texto='01/11/2026',alerta='inconsistente') if pid=='001' else Entrega())
    fake=Fake(out); monkeypatch.setattr('sabor.tools.pedidos._agente_extracao',lambda:fake)
    got=asyncio.run(extrair_e_classificar_pedido(ctx(),texto=text))
    assert got.tipo==kind and got.itens[0].ambiguo==ambiguous
    if pid=='001': assert got.entrega.data is None
    if pid=='003': assert '[CPF_1]' in fake.prompt and '123.456.789-00' not in fake.prompt
    if pid=='011': assert 'outro idioma' in fake.prompt

def test_pedido_salvo_ancora_data_em_data_recebimento(monkeypatch):
    fake=Fake(Pedido(tipo='compra',entrega=Entrega(data_texto='amanhã')))
    monkeypatch.setattr('sabor.tools.pedidos._agente_extracao',lambda:fake)
    conn=MagicMock(); conn.execute.return_value.fetchone.return_value=('PED-2026-003',datetime(2026,10,28,11,5),'Entrega amanhã')
    got=asyncio.run(extrair_e_classificar_pedido(ctx(conn),pedido_id='PED-2026-003'))
    assert got.pedido_id=='PED-2026-003' and got.entrega.data==date(2026,10,29)

def test_exige_uma_fonte():
    with pytest.raises(ValueError): asyncio.run(extrair_e_classificar_pedido(ctx(),texto='x',pedido_id='PED-1'))



