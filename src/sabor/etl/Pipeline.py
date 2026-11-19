from sabor.etl.repo import EtlLotes
from sabor.etl.repo import EtlPrecos
from sabor.etl.repo import EtlChunks
from sabor.etl.repo import EtlPedidos
from sabor.etl.repo import EtlProdutos
from sabor.etl.repo import EtlDocumentos
from sabor.etl.repo import EtlFinanceiro
from sabor.etl.repo import EtlProdutores
from sabor.etl.repo import EtlPedidoItens
from sabor.etl.repo import EtlSazonalidade
from sabor.etl.repo import EtlProdutorProdutos
from sabor.etl.repo import EtlProdutoSinonimos
from sabor.etl.repo import EtlSazonalidadeMensal

from sabor.etl.repo import TruncateTables
from sabor.agent.deps import atualizar_versao_dataset
from sabor.db import conectar
class Pipeline:

    def __init__(self, label: str):
        self.label = label

        self.truncate = TruncateTables()

        self.etl_lotes = EtlLotes()
        self.etl_precos = EtlPrecos()
        self.etl_chuncks = EtlChunks()
        self.etl_pedidos = EtlPedidos()
        self.etl_produtos = EtlProdutos()
        self.etl_documentos = EtlDocumentos()
        self.etl_financeiro = EtlFinanceiro()
        self.etl_produtores = EtlProdutores()
        self.etl_pedido_itens = EtlPedidoItens()
        self.etl_sazonalidade = EtlSazonalidade()
        self.etl_produto_sinonimos = EtlProdutoSinonimos()
        self.etl_produtor_produtos = EtlProdutorProdutos()
        self.etl_sazonalidade_mensal = EtlSazonalidadeMensal()

    def run(self) -> None:
        self.divider('=')
        self.info(f'{self.label}.run: Executando ETL...')
        self.divider('-')

        self.truncate.exec()
        self.etl_pedidos.exec()

        # tratar etl
        self.etl_produtos.exec()
        self.etl_produto_sinonimos.exec()  
        self.etl_produtores.exec()
        self.etl_precos.exec()
        self.etl_sazonalidade.exec()
        self.etl_sazonalidade_mensal.exec()
        self.etl_documentos.exec()
        self.etl_chuncks.exec()
        self.etl_pedido_itens.exec()
        self.etl_produtor_produtos.exec()
        self.etl_financeiro.exec()
        self.etl_lotes.exec()

        # Publica fingerprint somente depois que todas as etapas da carga terminam.
        with conectar() as conn:
            versao = atualizar_versao_dataset(conn)
        self.info(f'{self.label}.run: versão do dataset atualizada ({versao}).')

        self.divider('-')
        self.info(f'{self.label}.run: ETL executada com sucesso!')
        self.divider('=')

    def divider(self, char: str) -> None:
        print(char * 80)
    
    def info(self, message: str) -> None:
        print(message)
