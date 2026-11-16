import os
from dotenv import load_dotenv
from sabor.etl.psql import PostgresDriver

from pathlib import Path
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

load_dotenv()

class TruncateTables:

    def __init__(self):
        self.driver = PostgresDriver(
            user    =os.getenv("DATABASE_USER", "sabor"),
            password=os.getenv("DATABASE_PASSWORD"),
            database=os.getenv("DATABASE_NAME", "sabor"),
            host    =os.getenv("DATABASE_HOST", "localhost"),
            port    =int(os.getenv("DATABASE_PORT", 5432))
        )

    def info(self, message: str) -> None:
        print(message)

    def divider(self, char: str) -> None:
        print(char * 80)

    def truncate(self) -> bool:
        try:
            with self.driver.engine.begin() as conn:
                conn.execute(text(
                    """
                    TRUNCATE TABLE 
                        pedidos,
                        produtos,
                        produtores
                    RESTART IDENTITY CASCADE;
                    """
                ))
            return True

        except SQLAlchemyError as e:
            print(f"Erro de conexão: {e}")
            return False

    def exec(self):
        self.divider('-')
        self.info("-- Truncate Tables --")
        self.info(f"-- Status: {'[OK]' if self.truncate() == True else '[FAIL]'}")
        self.driver.close()
        self.divider('-')