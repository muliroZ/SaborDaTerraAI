from sabor.etl.psql import PostgresDriver

from pathlib import Path

class Etl:

    def __init__(self):
        self.driver = PostgresDriver(
            user    ="sabor",
            password="sabor",
            database="sabor",
            host    ="localhost",
            port    =5432
        )
        self.path = (Path(__file__).resolve().parent.parent / "data" / "bronze")

    def info(self, message: str) -> None:
        print(message)

    def divider(self, char: str) -> None:
        print(char * 80)