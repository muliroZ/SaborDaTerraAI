import os
from pathlib import Path
from dotenv import load_dotenv
from sabor.etl.psql import PostgresDriver

load_dotenv()

class Etl:
    def __init__(self):
        self.driver = PostgresDriver(
            user=os.getenv("DATABASE_USER", "sabor"),
            password=os.getenv("DATABASE_PASSWORD"),
            database=os.getenv("DATABASE_NAME", "sabor"),
            host=os.getenv("DATABASE_HOST", "localhost"),
            port=int(os.getenv("DATABASE_PORT", 5432))
        )
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
        import os
        import pandas as pd

        url = (
            f"postgresql+psycopg://{os.getenv('DATABASE_USER', 'sabor')}:"
            f"{os.getenv('DATABASE_PASSWORD')}@{os.getenv('DATABASE_HOST', 'localhost')}:"
            f"{os.getenv('DATABASE_PORT', 5432)}/{os.getenv('DATABASE_NAME', 'sabor')}"
        )

        df_prod = pd.read_sql("SELECT id, nome FROM produtos", url)
        df_sin = pd.read_sql("SELECT sinonimo, produto_id FROM produto_sinonimos", url)

        mapa = dict(zip(df_prod["nome"].str.strip().str.lower(), df_prod["id"]))
        mapa.update(dict(zip(df_sin["sinonimo"].str.strip().str.lower(), df_sin["produto_id"])))

        chaves = serie_nomes.astype("string").str.strip().str.lower()
        resultado = chaves.map(mapa)
        return resultado.astype(object).where(resultado.notna(), None)