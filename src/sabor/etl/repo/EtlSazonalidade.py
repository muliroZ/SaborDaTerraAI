from .Etl import Etl

import pandas as pd

class EtlSazonalidade(Etl):

    def __init__(self):
        super().__init__()

        self.df        = None
        self.table     = "sazonalidade"
        # self.file_path = self.path / '.csv'
        # self.dtype = {

        # }

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        # self.df = pd.read_csv(self.file_path, encoding="utf-8", dtype=self.dtype)

    def transform(self):
        self.info(f"-- Transform: {self.table}")

    def load(self):
        self.info(f"-- Load: {self.table}")

    def exec(self):
        self.divider('-')
        self.extract()
        self.transform()
        self.load()
        self.divider('-')