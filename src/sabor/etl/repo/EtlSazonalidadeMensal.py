from .Etl import Etl

import pandas as pd

class EtlSazonalidadeMensal(Etl):

    def __init__(self):
        super().__init__()
        self.df        = None
        self.table     = "sazonalidade_mensal"
        self.file_path = self.path / 'sazonalidade.csv' 

    def extract(self):
        self.info(f"-- Extract: {self.table}")
       
        self.df = pd.read_csv(self.file_path, encoding="utf-8")

    def transform(self):
        #Mapeamento de meses para números
        meses_map = {
            'jan': 1, 'fev': 2, 'mar': 3, 'abr': 4, 'mai': 5, 'jun': 6,
            'jul': 7, 'ago': 8, 'set': 9, 'out': 10, 'nov': 11, 'dez': 12
        }
        
        #Desdobrar as colunas de meses em linhas (Unpivot)
        df_melt = self.df.melt(
            id_vars=['produto'], 
            value_vars=list(meses_map.keys()), 
            var_name='mes_str', 
            value_name='nivel'
        )
        
        # Converter o nome do mês para número e preparar para o banco
        df_melt['mes'] = df_melt['mes_str'].map(meses_map)
        self.df = df_melt[['produto', 'mes', 'nivel']]

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