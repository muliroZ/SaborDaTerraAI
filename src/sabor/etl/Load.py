class Load:

    def __init__(self):
        self.sv_files = {
            'Produtores'  : 'sabor/etl/data/silver/produtores.csv'                ,
            'Produtos'    : 'sabor/etl/data/silver/produtos.csv'                  ,
            'Financeiro'  : 'sabor/etl/data/silver/financeiro.csv'                ,
            'Lotes'       : 'sabor/etl/data/silver/lotes.csv'                     ,
            'Precos'      : 'sabor/etl/data/silver/tabela_precos_outubro_2026.csv',
            'Sazonalidade': 'sabor/etl/data/silver/sazonalidade.csv'              ,
            'Pedidos'     : 'sabor/etl/data/silver/pedidos.csv'                   ,
        }

    def info(self, message: str) -> None:
        print(message)

    def run(self):
        pass