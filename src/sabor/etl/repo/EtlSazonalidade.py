from .Etl import Etl

import pandas as pd

class EtlSazonalidade(Etl):

    def __init__(self):
        super().__init__()

        self.df        = None
        self.table     = "sazonalidade"
        self.file_path = self.path / 'sazonalidade.csv'

    def extract(self):
        self.info(f"-- Extract: {self.table}")

        self.df = pd.read_csv(self.file_path, encoding="utf-8")

    def transform(self):
        # Filtrar apenas as colunas que pertencem à tabela sazonalidade
        self.df = self.df[['produto', 'observacao']].copy()
        self.df['produto_id'] = self.mapear_produto_id(self.df['produto'])

    def load(self):
        self.info(f"-- Load: {self.table}")
        self.driver.insert(self.table, self.df)
        self.driver.close()

    def exec(self):
        self.divider('-')
        self.extract()
        self.transform()
        self.load()
        self.divider('-')