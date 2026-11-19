"""Catálogo canônico de produtos (`produtos` + `produto_sinonimos`) para a extração (#9).

Um nome livre ("mel (aquele silvestre mesmo)", "Ovos de galinha caipira", "maracuja") vira um
produto do catálogo só quando a correspondência é única; caso contrário o item fica ambíguo e
a equipe confirma. Nada aqui chama o LLM.

Ordem de busca, sem acentos e sem caixa:

1. nome ou sinônimo exato ("maracuja", "manga tommy");
2. o texto começa pela mesma palavra de um nome do catálogo e contém todas as palavras dele
   ("mel silvestre orgânico" → mel silvestre; "doce de leite" não vira leite); se mais de um
   nome serve, vence o mais específico ("tomate cereja");
3. as palavras do texto estão em um único nome ("frango" → frango caipira). "manteiga" não
   resolve: serve para manteiga de garrafa e para queijo manteiga.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re
from typing import Any

from sabor.tools.datas import normalizar

_PALAVRAS_IGNORADAS = frozenset(
    'de da do das dos e com organico organica organicos organicas organic '
    'fresco fresca frescos frescas'.split()
)
"""Qualificadores que não mudam o produto: "ricota fresca" é ricota."""


@dataclass(frozen=True)
class ProdutoCatalogo:
    id: int
    nome: str


def _palavras(texto: str | None) -> tuple[str, ...]:
    return tuple(
        p for p in re.findall(r'[a-z0-9]+', normalizar(texto)) if p not in _PALAVRAS_IGNORADAS
    )


class Catalogo:
    """Índice em memória do catálogo; carregado uma vez por pedido (são ~50 linhas)."""

    def __init__(
        self,
        produtos: Iterable[tuple[int, str]],
        sinonimos: Iterable[tuple[str, int]] = (),
    ) -> None:
        self.produtos = [ProdutoCatalogo(id_, nome) for id_, nome in produtos]
        por_id = {p.id: p for p in self.produtos}
        self._por_chave: dict[tuple[str, ...], ProdutoCatalogo] = {}
        for produto in self.produtos:
            self._por_chave[_palavras(produto.nome)] = produto
        for sinonimo, produto_id in sinonimos:
            if produto_id in por_id:
                self._por_chave.setdefault(_palavras(sinonimo), por_id[produto_id])
        self._palavras_nome = [(_palavras(p.nome), p) for p in self.produtos if _palavras(p.nome)]

    @classmethod
    def carregar(cls, conn: Any) -> Catalogo:
        produtos = conn.execute('SELECT id, nome FROM produtos').fetchall()
        sinonimos = conn.execute('SELECT sinonimo, produto_id FROM produto_sinonimos').fetchall()
        return cls(produtos, sinonimos)

    def nomes(self) -> list[str]:
        return sorted(p.nome for p in self.produtos)

    def resolver(self, *textos: str | None) -> ProdutoCatalogo | None:
        """Primeiro texto que corresponde a um único produto (ver a ordem no módulo)."""
        chaves = [chave for chave in map(_palavras, textos) if chave]
        for chave in chaves:
            if chave in self._por_chave:
                return self._por_chave[chave]
        for chave in chaves:
            if (produto := self._por_palavras(chave)) is not None:
                return produto
        return None

    def _por_palavras(self, chave: tuple[str, ...]) -> ProdutoCatalogo | None:
        presentes = set(chave)
        contidos = [
            (len(nome), p)
            for nome, p in self._palavras_nome
            if nome[0] == chave[0] and presentes >= set(nome)
        ]
        if contidos:
            maior = max(tamanho for tamanho, _ in contidos)
            mais_especificos = [p for tamanho, p in contidos if tamanho == maior]
            return mais_especificos[0] if len(mais_especificos) == 1 else None

        contem = [p for nome, p in self._palavras_nome if presentes <= set(nome)]
        return contem[0] if len(contem) == 1 else None
