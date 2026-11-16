import pandas as pd

from sqlalchemy import create_engine, text, URL
from sqlalchemy.exc import SQLAlchemyError

class PostgresDriver:

    def __init__(self, host, port, database, user, password):

        self.engine = create_engine(
            URL.create(
                drivername="postgresql+psycopg",
                username=user,
                password=password,
                host=host,
                port=port,
                database=database
            )
        )

    def insert(self, table: str, df: pd.DataFrame):

        return df.to_sql(
            name=table,
            con=self.engine,
            if_exists="append",
            index=False
        )

    def test_connection(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))

            return True

        except SQLAlchemyError as e:
            print(f"Erro de conexão: {e}")
            return False

    def close(self):
        self.engine.dispose()