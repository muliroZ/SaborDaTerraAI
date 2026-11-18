import pandas as pd
from .Etl import Etl

class EtlProdutoSinonimos(Etl):
    def __init__(self):
        super().__init__()
        self.df = None
        self.table = "produto_sinonimos"

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        # Lê os produtos originais que já estão no banco para pegar os IDs
        self.df_produtos = pd.read_sql("SELECT id, nome FROM produtos", self.driver.engine)

    def transform(self):
        self.info(f"-- Transform: {self.table}")
        
        # Mapeamento exigido pela tabela_precos.md
        sinonimos = [
            {'nome': 'goiaba', 'sinonimo': 'goiaba paluma'},
            {'nome': 'goiaba', 'sinonimo': 'goiaba paluma extra'},
            {'nome': 'goiaba', 'sinonimo': 'goiaba paluma especial'},
            {'nome': 'goiaba', 'sinonimo': 'guava'},
            {'nome': 'manga', 'sinonimo': 'manga tommy'},
            {'nome': 'manga', 'sinonimo': 'mango'},
            {'nome': 'banana', 'sinonimo': 'banana prata'},
            {'nome': 'maracujá', 'sinonimo': 'passion fruit'},
            {'nome': 'tomate', 'sinonimo': 'tomate santa cruz'},
            {'nome': 'mel silvestre', 'sinonimo': 'mel puro'},
            {'nome': 'mel silvestre', 'sinonimo': 'mel variado'},
            {'nome': 'mel de abelha jandaíra', 'sinonimo': 'mel de jandaíra'},
            {'nome': 'pimentão', 'sinonimo': 'pimentão verde'},
            {'nome': 'pimentão', 'sinonimo': 'pimentão colorido'},
            {'nome': 'tapioca', 'sinonimo': 'tapioca/goma'},
            {'nome': 'queijo coalho', 'sinonimo': 'queijo'},
            {'nome': 'queijo coalho', 'sinonimo': 'o mais artesanal'},
            {'nome': 'maracujá', 'sinonimo': 'maracuja'},
            {'nome': 'rúcula', 'sinonimo': 'rucula'},
            {'nome': 'mel silvestre', 'sinonimo': 'mel'},   
        ]
        df_map = pd.DataFrame(sinonimos)
        
        # Faz o join com a tabela do banco para capturar o "produto_id"
        self.df = df_map.merge(self.df_produtos, on='nome', how='inner')
        
        # Prepara as colunas exatas exigidas pelo schema.sql
        self.df = self.df.rename(columns={'id': 'produto_id'})
        self.df = self.df[['sinonimo', 'produto_id']]

    def load(self):
        self.info(f"-- Load: {self.table}")
        self.driver.insert(self.table, self.df)

    def exec(self):
        self.extract()
        self.transform()
        self.load()