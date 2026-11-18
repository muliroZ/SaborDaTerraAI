from .Etl import Etl

import pandas as pd

class EtlPedidos(Etl):

    def __init__(self):
        super().__init__()

        self.df        = None
        self.table     = "pedidos"
        self.file_path = self.path / 'pedidos.json'

        self.dtype = {
            'id'               : 'string',
            'canal'            : 'string',
            'remetente'        : 'string',
            'comprador'        : 'string',
            'mensagem_original': 'string',
            'status'           : 'string',
        }

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.read_json(self.file_path, encoding="utf-8", dtype=self.dtype)

    def transform(self):
        self.info(f"-- Transform: {self.table}")
        self.df['data_recebimento'] = pd.to_datetime(self.df['data_recebimento'], errors='coerce')

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