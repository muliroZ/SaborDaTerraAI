import pandas as pd
from .Etl import Etl

class EtlFinanceiro(Etl):
    def __init__(self):
        super().__init__()
        self.df = None
        self.table = "financeiro"
        self.file_path = self.path / 'financeiro.csv'

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.read_csv(self.file_path, encoding="utf-8")

    def transform(self):
        self.info(f"-- Transform: {self.table}")
        
        # Normalização de compradores (Exigência da Issue)
        compradores_map = {
            'SESC Garanhuns': 'SESC - Unidade Garanhuns',
            'Mercado Bom Preço - Filial Centro': 'Mercado Bom Preço'
        }
        self.df['comprador_normalizado'] = self.df['comprador'].replace(compradores_map)
        
        # Resolver a mistura de Quantidade e Unidade
        # Mapeamos os produtos que NÃO são vendidos em kg
        unidades_especiais = {
            'alface': 'unidade', 'coco verde': 'unidade',
            'coentro': 'maço', 'cebolinha': 'maço', 'couve': 'maço', 
            'espinafre': 'maço', 'hortelã': 'maço', 'manjericão': 'maço', 'rúcula': 'maço',
            'ovos caipira': 'dúzia',
            'iogurte natural': 'litro', 'leite': 'litro', 'melado': 'litro'
        }
        # Se não estiver no mapeamento, o padrão é 'kg'
        self.df['unidade'] = self.df['produto'].map(unidades_especiais).fillna('kg')
        self.df = self.df.rename(columns={'quantidade_kg': 'quantidade'})
        
        # Remover colunas denormalizadas e adicionar a sua flag de divergência
        self.df = self.df.drop(columns=['produtor_nome', 'produtor_cpf'], errors='ignore')
        self.df['alerta_divergencia'] = False

        colunas_data = ['data', 'data_vencimento', 'data_pagamento']
        for col in colunas_data:
            if col in self.df.columns:
                self.df[col] = pd.to_datetime(self.df[col], format='%Y-%m-%d', errors='coerce')

        self.df['produto_id'] = self.mapear_produto_id(self.df['produto'])

        # TESTE DE VALIDAÇÃO (Exigência da Issue)
        total_vendas = self.df['valor_total'].sum()
        self.info(f"TESTE: Total de Vendas = R$ {total_vendas:,.2f}")
        if total_vendas != 24634.00:
            self.info("O total não bate com os R$ 24.634,00 documentados")

    def load(self):
        self.info(f"-- Load: {self.table}")
        self.driver.insert(self.table, self.df)

    def exec(self):
        self.extract()
        self.transform()
        self.load()