from .Etl import Etl


class EtlPedidoItens(Etl):
    """`pedido_itens` não tem fonte bruta: `pedidos.json` só traz a mensagem em texto livre.

    Quem preenche a tabela é a triagem (`sabor.triagem`), com os itens que o LLM extrai e as
    regras classificam. O TRUNCATE de `pedidos` (CASCADE) esvazia a tabela junto com
    `triagem_pedidos`, então, depois de uma carga, rode `scripts/triagem_lote.py` para os
    itens voltarem (o resumo por produtor depende deles).
    """

    def __init__(self):
        super().__init__()
        self.table = "pedido_itens"

    def exec(self):
        self.divider('-')
        self.info(
            f"-- {self.table}: sem fonte bruta; preenchida pela triagem "
            "(uv run python scripts/triagem_lote.py)"
        )
        self.divider('-')
