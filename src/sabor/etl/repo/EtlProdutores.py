from .Etl import Etl

import pandas as pd

class EtlProdutores(Etl):

    def __init__(self):
        super().__init__()

        self.df        = None
        self.table     = "produtores"
        self.file_path = self.path / 'produtores.csv'

        self.dtype = {
            'agencia': 'string',
            'conta_corrente': 'string',
            'cpf': 'string',
            'telefone': 'string',
        }

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.read_csv(self.file_path, encoding="utf-8", dtype=self.dtype)

    def transform(self):
        self.info(f"-- Transform: {self.table}")
        
        # Converter "sim"/"não" para Boolean (True/False)
        self.df['certificacao_organica'] = self.df['certificacao_organica'].str.lower().map({'sim': True, 'nao': False, 'não': False})
        
        # Converter agência e conta corrente para texto
        self.df['agencia'] = self.df['agencia'].astype(str)
        self.df['conta_corrente'] = self.df['conta_corrente'].astype(str)
        
        # Converter datas de texto para o tipo datetime do Pandas (necessário para o PostgreSQL)
        if 'data_nascimento' in self.df.columns:
            self.df['data_nascimento'] = pd.to_datetime(self.df['data_nascimento'], format='%Y-%m-%d', errors='coerce')
            
        if 'validade_certificacao' in self.df.columns:
            self.df['validade_certificacao'] = pd.to_datetime(self.df['validade_certificacao'], format='%Y-%m-%d', errors='coerce')
        
        # Remover colunas do CSV que NÃO existem na tabela 'produtores' do banco
        self.df = self.df.drop(columns=['produtos'], errors='ignore')

    def load(self):
        self.driver.insert('produtores', self.df)

    def exec(self):
        self.divider('-')
        self.extract()
        self.transform()
        self.load()
        self.divider('-')