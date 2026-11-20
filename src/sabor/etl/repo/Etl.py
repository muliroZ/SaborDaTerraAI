from pathlib import Path
from dotenv import load_dotenv
from sabor.db import database_url
from sabor.etl.psql import PostgresDriver

load_dotenv()

class Etl:
    def __init__(self):
        self.driver = PostgresDriver.from_url(database_url())
        self.path = (Path(__file__).resolve().parent.parent / "data" / "bronze")

    def divider(self, char='-', length=80):
        print(char * length)

    def info(self, msg):
        print(msg)

    def exec(self):
        self.divider('-')
        self.extract()
        self.transform()
        self.load()
        self.divider('-')

    def mapear_produto_id(self, serie_nomes):
        import pandas as pd

        df_prod = pd.read_sql("SELECT id, nome FROM produtos", self.driver.engine)
        df_sin = pd.read_sql("SELECT sinonimo, produto_id FROM produto_sinonimos", self.driver.engine)

        mapa = dict(zip(df_prod["nome"].str.strip().str.lower(), df_prod["id"]))
        mapa.update(dict(zip(df_sin["sinonimo"].str.strip().str.lower(), df_sin["produto_id"])))

        chaves = serie_nomes.astype("string").str.strip().str.lower()
        resultado = chaves.map(mapa)
        return resultado.astype(object).where(resultado.notna(), None)