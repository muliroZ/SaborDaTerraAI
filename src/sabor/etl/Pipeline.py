from sabor.etl.Extract   import Extract
from sabor.etl.Transform import Transform
from sabor.etl.Load      import Load
from sabor.etl.DatabasePurger import DatabasePurger

class Pipeline:

    def __init__(self, label: str):
        self.label            = label
        self.extract_runner   = Extract()
        self.transform_runner = Transform()
        self.load_runner      = Load()
        self.purge_runner     = DatabasePurger()
        
    def purge(self):
        self.divider('-')
        self.info(f'{self.label}.purge: Executando remoção completa dos dados...')
        # self.purge_runner.run()
        self.info(f'{self.label}.purge: Remoção completa dos dados realizada com sucesso!')
        self.divider('-')

    def extract(self):
        self.divider('-')
        self.info(f'{self.label}.extract: Executando extração...')
        self.extract_runner.run()
        self.info(f'{self.label}.extract: Extração finalizada com sucesso!')
        self.divider('-')

    def transform(self):
        self.divider('-')
        self.info(f'{self.label}.transform: Executando transformação...')
        # self.transform_runner = self.extract_runner.df_dict
        # self.transform_runner.run()
        self.info(f'{self.label}.transform: Transformação finalizada com sucesso!')
        self.divider('-')

    def load(self):
        self.divider('-')
        self.info(f'{self.label}.load: Executando carregamento...')
        self.load_runner.run()
        self.info(f'{self.label}.load: Carregamento finalizado com sucesso!')
        self.divider('-')

    def run(self, methods: list) -> None:
        self.divider('=')
        self.info(f'{self.label}.run: Executando ETL...')
        self.divider('=')
        for method in methods:
            getattr(self, method)()
        self.divider('=')
        self.info(f'{self.label}.run: ETL executada com sucesso!')
        self.divider('=')

    def divider(self, char: str) -> None:
        print(char * 80)
    
    def info(self, message: str) -> None:
        print(message)