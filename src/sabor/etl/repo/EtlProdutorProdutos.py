from .Etl import Etl

import pandas as pd

class EtlProdutorProdutos(Etl):

    def __init__(self):
        super().__init__()

        self.df        = None
        self.table     = "produtor_produtos"
        # self.file_path = self.path / '.csv'
        # self.dtype = {

        # }

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.file_path = self.path / 'produtores.csv'
        self.df = pd.read_csv(self.file_path, encoding="utf-8")

    def transform(self):
        self.info(f"-- Transform: {self.table}")
        # Manter apenas o ID do produtor e a lista de produtos
        df_produtos = self.df[['id', 'produtos']].copy()
        
        # Remover quem não tem produtos
        df_produtos = df_produtos.dropna(subset=['produtos'])
        
        # Renomear 'id' para 'produtor_id' para bater com o schema
        df_produtos = df_produtos.rename(columns={'id': 'produtor_id'})
        
        # Transformar a string "goiaba,acerola" numa lista real do Python
        df_produtos['nome_original'] = df_produtos['produtos'].str.split(',')
        
        # "Explodir" a lista (1 linha com 3 produtos vira 3 linhas separadas)
        df_produtos = df_produtos.explode('nome_original')
        
        # Limpar espaços em branco (strip) e remover a coluna antiga
        df_produtos['nome_original'] = df_produtos['nome_original'].str.strip()
        self.df = df_produtos[['produtor_id', 'nome_original']].drop_duplicates()
        self.df['produto_id'] = self.mapear_produto_id(self.df['nome_original'])
        

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