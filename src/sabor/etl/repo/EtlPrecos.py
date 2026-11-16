from .Etl import Etl

import pandas as pd
import numpy as np

class EtlPrecos(Etl):

    def __init__(self):
        super().__init__()

        self.df        = None
        self.table     = "precos"
        self.file_path = self.path / 'tabela_precos_outubro_2026.csv'

        self.dtype = {

        }

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.read_csv(self.file_path, encoding="utf-8", dtype=self.dtype)

    def transform(self):
        self.info(f"-- Transform: {self.table}")

        self.df['preco_convencional'] = pd.to_numeric(self.df['preco_convencional'].replace('n/d', np.nan))
        self.df['preco_organico'] = pd.to_numeric(self.df['preco_organico'].replace('n/d', np.nan))
        self.df = self.df.rename(columns={'volume_minimo_atacado_kg': 'volume_minimo_atacado'})
        self.df['volume_minimo_atacado'] = pd.to_numeric(self.df['volume_minimo_atacado'], errors='coerce').fillna(0)
        self.df['referencia'] = pd.to_datetime('2026-10-01')
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