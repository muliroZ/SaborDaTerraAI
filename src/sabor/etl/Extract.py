from sabor.etl.extract import ExtractProdutores
from sabor.etl.extract import ExtractProdutos
from sabor.etl.extract import ExtractFinanceiro
from sabor.etl.extract import ExtractLotes
from sabor.etl.extract import ExtractPrecos
from sabor.etl.extract import ExtractSazonalidade
from sabor.etl.extract import ExtractPedidos

from pathlib import Path

class Extract:
    def __init__(self):
        self._data_dir = Path(__file__).resolve().parent / "data"
        self.bz_files  = {
            'Produtores'  : self._data_dir / 'bronze/produtores.csv'                ,
            'Produtos'    : self._data_dir / 'bronze/produtores.csv'                ,
            'Financeiro'  : self._data_dir / 'bronze/financeiro.csv'                ,
            'Lotes'       : self._data_dir / 'bronze/lotes.csv'                     ,
            'Precos'      : self._data_dir / 'bronze/tabela_precos_outubro_2026.csv',
            'Sazonalidade': self._data_dir / 'bronze/sazonalidade.csv'              ,
            'Pedidos'     : self._data_dir / 'bronze/pedidos.json'                  ,
        }
        self.df_dict   = {}
        

    def info(self, message: str) -> None:
        print(message)

    def run(self):
        self.info('\tExtract: Produtores')
        self.info('\tExtract: Produtos')
        self.info('\tExtract: Financeiro')
        self.info('\tExtract: Lotes')
        self.info('\tExtract: Preços')
        self.info('\tExtract: Sazonalidade')
        self.info('\tExtract: Pedidos')

        self.df_dict['Produtores']   = ExtractProdutores(file_path=self.bz_files['Produtores']).exec()
        self.df_dict['Produtos']     = ExtractProdutos(file_path=self.bz_files['Produtos']).exec()
        self.df_dict['Financeiro']   = ExtractFinanceiro(file_path=self.bz_files['Financeiro']).exec()
        self.df_dict['Lotes']        = ExtractLotes(file_path=self.bz_files['Lotes']).exec()
        self.df_dict['Precos']       = ExtractPrecos(file_path=self.bz_files['Precos']).exec()
        self.df_dict['Sazonalidade'] = ExtractSazonalidade(file_path=self.bz_files['Sazonalidade']).exec()
        self.df_dict['Pedidos']      = ExtractPedidos(file_path=self.bz_files['Pedidos']).exec()

