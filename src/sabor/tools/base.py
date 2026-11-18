"""Componentes base compartilhados entre as ferramentas de `sabor.tools`."""

from __future__ import annotations

from datetime import date
import re
from typing import Literal, Protocol

import psycopg
from pydantic import BaseModel
from sabor.agent.schemas import Fonte

DATA_REFERENCIA_PADRAO = date(2026, 10, 30)
TABELA_PRECOS_REFERENCIA_PADRAO = date(2026, 10, 1)

_MESES = {
    'jan': 1,
    'fev': 2,
    'mar': 3,
    'abr': 4,
    'mai': 5,
    'jun': 6,
    'jul': 7,
    'ago': 8,
    'set': 9,
    'out': 10,
    'nov': 11,
    'dez': 12,
}


# ---------------------------------------------------------------------------
# Perfil do usuário e aplicação do filtro de linha
# ---------------------------------------------------------------------------


class Usuario(BaseModel):
    """Perfil simulado logado. Decide o filtro de linha aplicado nas tools."""

    perfil: Literal['funcionario', 'produtor']
    produtor_id: str | None = None  # obrigatório quando perfil == 'produtor'


class SemPermissao(PermissionError):
    """Perfil produtor tentando ver dado de outro produtor."""


def _resolver_usuario(usuario: Usuario | str) -> Usuario:
    """Converte `AgentDeps.usuario` (str ou Usuario) para o contrato `Usuario`."""
    if isinstance(usuario, Usuario):
        return usuario
    texto = usuario.strip()
    if re.fullmatch(r'P\d{3}', texto, re.IGNORECASE):
        return Usuario(perfil='produtor', produtor_id=texto.upper())
    return Usuario(perfil='funcionario')


def _aplicar_filtro_produtor(usuario: Usuario, produtor_id: str | None) -> str | None:
    """Resolve o produtor_id efetivo da consulta, aplicando o perfil.

    - Perfil funcionário: passa produtor_id como veio (None = sem filtro).
    - Perfil produtor: sempre trava no próprio produtor_id. Se pedir
      explicitamente o dado de outro produtor, nega com `SemPermissao`.
    """
    if usuario.perfil == 'funcionario':
        return produtor_id
    if usuario.produtor_id is None:
        raise ValueError("Usuario com perfil='produtor' precisa de produtor_id.")
    if produtor_id is not None and produtor_id != usuario.produtor_id:
        raise SemPermissao(
            f'Perfil produtor ({usuario.produtor_id}) não pode consultar dados de {produtor_id}.'
        )
    return usuario.produtor_id


# ---------------------------------------------------------------------------
# Mascarador (integração com Issue #33)
# ---------------------------------------------------------------------------


class Mascarador(Protocol):
    """Interface mínima do mascarador de PII (Issue #33)."""

    def mascarar(self, texto: str) -> str: ...

    def mascarar_campo(self, valor: str | None, categoria: str) -> str | None: ...


class _SemMascaramento:
    """No-op de fallback caso nenhum mascarador seja fornecido."""

    def mascarar(self, texto: str) -> str:
        return texto


SEM_MASCARAMENTO: Mascarador = _SemMascaramento()


def _mascarar_opcional(
    mascarador: Mascarador, valor: str | None, categoria: str | None = None
) -> str | None:
    if valor is None:
        return None
    mascarar_campo_fn = getattr(mascarador, 'mascarar_campo', None)
    if categoria is not None and callable(mascarar_campo_fn):
        return mascarar_campo_fn(valor, categoria)
    return mascarador.mascarar(valor)


def _mascarar_pix(mascarador: Mascarador, valor: str | None) -> str | None:
    if valor is None:
        return None
    return _mascarar_opcional(mascarador, valor, 'PIX')


# ---------------------------------------------------------------------------
# Fonte (convenção de citação) e resolução de produto_id
# ---------------------------------------------------------------------------


def _resolver_produto_id(conn: psycopg.Connection, nome_ou_sinonimo: str) -> int | None:
    chave = nome_ou_sinonimo.strip().lower()
    linha = conn.execute('SELECT id FROM produtos WHERE lower(nome) = %s', (chave,)).fetchone()
    if linha:
        return linha[0]
    linha = conn.execute(
        'SELECT produto_id FROM produto_sinonimos WHERE lower(sinonimo) = %s', (chave,)
    ).fetchone()
    return linha[0] if linha else None
