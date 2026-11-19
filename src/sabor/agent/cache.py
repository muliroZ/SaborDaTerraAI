"""Cache de respostas estáticas em tabela `UNLOGGED` do PostgreSQL (etapas 2b e 5b, #36).

Redis foi descartado na reunião de 16/11: para a prova de conceito basta uma tabela
`UNLOGGED` (sem WAL, mais rápida; perder o conteúdo num crash é aceitável para cache).

- **Chave:** SHA-256 da pergunta normalizada (minúsculas, sem acento e sem pontuação) +
  perfil + versão do dataset + data de referência. Uma nova carga de dados publica outra
  versão do dataset (`dataset_metadata`), então as chaves antigas deixam de ser encontradas
  e expiram pelo TTL.
- **2b (leitura):** hit devolve a resposta sem chamar o LLM (zero tokens) e o turno sai com
  `cache_hit = True` para a auditoria (#35).
- **5b (gravação):** só respostas estáticas e seguras (`resposta_cacheavel`): sem dados
  pessoais, sem retorno antecipado, sem bloqueio do guardrail, sem rascunhos/relatórios,
  fora de contexto multi-turno e só do perfil `funcionario`.
- **TTL:** `CACHE_TTL_S` (padrão 24 h; `0` desliga o cache). Limpeza manual com
  `uv run python scripts/limpar_cache.py`.

O repositório usa conexões de escrita curtas (`sabor.db.conectar`), nunca a `sabor_ro` do
agente, e nenhuma conexão fica aberta durante a chamada ao modelo.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
import hashlib
import logging
import os
import re
import unicodedata
from typing import Any

from dotenv import load_dotenv
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from sabor.agent.mascarador import Mascarador
from sabor.agent.schemas import Flag, Fonte, RespostaTurno

logger = logging.getLogger(__name__)

TTL_PADRAO_S = 24 * 60 * 60
"""Validade padrão de uma resposta em cache (24 h). Configurável em `CACHE_TTL_S`."""

VERSOES_SEM_INVALIDACAO = frozenset({'sha256-unavailable'})
"""Versões que não identificam a carga de dados: sem elas o cache não invalidaria sozinho."""

PERFIS_CACHEAVEIS = frozenset({'funcionario'})
"""Só a equipe usa o cache; respostas do perfil produtor dependem do produtor logado."""

_NAO_ALFANUMERICO = re.compile(r'[^0-9a-z]+')

# Referências ao que veio antes na conversa: a pergunta só faz sentido com o histórico.
# Comparadas com o texto já normalizado (sem acento e sem pontuação).
_REFERENCIAS_CONTEXTO = re.compile(
    r'\b(?:'
    r'ele|ela|eles|elas|dele|dela|deles|delas|nele|nela|neles|nelas|'
    r'esse|essa|esses|essas|isso|desse|dessa|desses|dessas|disso|nesse|nessa|nesses|'
    r'nessas|nisso|este|esta|estes|estas|isto|deste|desta|destes|destas|disto|'
    r'aquele|aquela|aqueles|aquelas|aquilo|daquele|daquela|daquilo|naquele|naquela|'
    r'mesmo|mesma|mesmos|mesmas|tambem|anterior|anteriores|acima|ultima|ultimo|'
    r'outro|outra|outros|outras|resposta|disse|falou|mencionou|citou'
    r')\b'
)
_INICIO_CONTINUACAO = re.compile(r'^(?:e|mas|entao|ai|agora|so|tipo)\b')
MIN_PALAVRAS_AUTOCONTIDA = 3


def obter_ttl_cache_s() -> int:
    """Lê `CACHE_TTL_S`; vazio ou inválido usa 24 h. `0` (ou negativo) desliga o cache."""
    load_dotenv()
    valor = (os.getenv('CACHE_TTL_S') or '').strip()
    if not valor:
        return TTL_PADRAO_S
    try:
        return max(0, int(valor))
    except ValueError:
        return TTL_PADRAO_S


def normalizar_pergunta(texto: str) -> str:
    """Minúsculas, sem acento e sem pontuação, com espaços simples.

    "Como armazenar Queijo Coalho?" e "como  armazenar queijo coalho" geram a mesma chave.
    """
    sem_acento = ''.join(
        c for c in unicodedata.normalize('NFKD', texto.lower()) if not unicodedata.combining(c)
    )
    return ' '.join(_NAO_ALFANUMERICO.sub(' ', sem_acento).split())


def gerar_chave_cache(
    pergunta: str,
    perfil: str,
    dataset_version: str,
    data_referencia: date | str | None,
) -> str:
    """SHA-256 (64 hex) de pergunta normalizada + perfil + versão do dataset + data de ref."""
    data_ref = data_referencia.isoformat() if isinstance(data_referencia, date) else (
        str(data_referencia or '')
    )
    partes = (normalizar_pergunta(pergunta), perfil, dataset_version, data_ref)
    return hashlib.sha256('\x1f'.join(partes).encode('utf-8')).hexdigest()


def pergunta_autocontida(texto: str) -> bool:
    """Heurística conservadora: a pergunta se entende sem o histórico da sessão?

    Recusa perguntas curtas demais ("e o mel?"), que começam continuando a conversa
    ("e quanto custa...", "mas e o...") ou que apontam para algo dito antes ("dele", "isso",
    "o mesmo", "a resposta anterior").
    """
    normalizado = normalizar_pergunta(texto)
    if len(normalizado.split()) < MIN_PALAVRAS_AUTOCONTIDA:
        return False
    if _INICIO_CONTINUACAO.match(normalizado):
        return False
    return _REFERENCIAS_CONTEXTO.search(normalizado) is None


def versao_permite_cache(dataset_version: str | None) -> bool:
    """Sem versão do dataset a chave não muda com a carga de dados: não usa o cache."""
    return bool(dataset_version) and dataset_version not in VERSOES_SEM_INVALIDACAO


def _contem_dados_pessoais(texto: str) -> bool:
    return bool(texto) and Mascarador().mascarar(texto) != texto


class RespostaCacheada(BaseModel):
    """Conteúdo guardado em `cache_respostas.resposta` (jsonb)."""

    resposta: str
    fontes: list[Fonte] = Field(default_factory=list)
    flags: list[Flag] = Field(default_factory=list)

    @classmethod
    def do_turno(cls, turno: RespostaTurno) -> 'RespostaCacheada':
        return cls(resposta=turno.texto, fontes=list(turno.fontes), flags=list(turno.flags))


def resposta_cacheavel(turno: RespostaTurno, pergunta: str) -> bool:
    """5b: só grava respostas estáticas e seguras.

    Contexto (perfil, multi-turno e versão do dataset) é conferido pelo runner antes da
    leitura; aqui ficam as condições da própria resposta.
    """
    if turno.falha_segura or turno.bloqueado_guardrail or turno.regras_disparadas:
        return False
    if turno.retorno_antecipado is not None or turno.cache_hit:
        return False
    if turno.rascunhos or turno.relatorios:
        return False  # rascunhos e relatórios são montados para um caso específico
    if not turno.texto.strip():
        return False
    textos = [pergunta, turno.texto]
    textos += [f.descricao for f in turno.flags]
    textos += [f'{f.arquivo} {f.referencia}' for f in turno.fontes]
    return not any(_contem_dados_pessoais(t) for t in textos)


ConectarFn = Callable[[], AbstractContextManager[Any]]


def _conectar_padrao() -> AbstractContextManager[Any]:
    from sabor.db import conectar

    return conectar(tentativas=1, autocommit=True)


class CacheRespostas:
    """Repositório do cache na tabela `cache_respostas` (UNLOGGED).

    `conectar_fn` devolve uma conexão de escrita usada como context manager (padrão:
    `sabor.db.conectar(tentativas=1, autocommit=True)`); cada operação abre e fecha a sua.
    """

    def __init__(self, conectar_fn: ConectarFn | None = None, ttl_s: int | None = None) -> None:
        self._conectar = conectar_fn or _conectar_padrao
        self.ttl_s = obter_ttl_cache_s() if ttl_s is None else max(0, ttl_s)

    @property
    def ativo(self) -> bool:
        return self.ttl_s > 0

    def buscar_por_hash(self, chave: str) -> RespostaCacheada | None:
        """2b: resposta ainda válida para a chave (conta o hit) ou `None`."""
        if not self.ativo:
            return None
        with self._conectar() as conn:
            row = conn.execute(
                'UPDATE cache_respostas SET hits = hits + 1 '
                'WHERE chave = %s AND expira_em > now() RETURNING resposta',
                (chave,),
            ).fetchone()
        return RespostaCacheada.model_validate(row[0]) if row else None

    def gravar(self, chave: str, resposta: RespostaCacheada) -> None:
        """5b: grava (ou renova) a resposta e aproveita para descartar as expiradas."""
        if not self.ativo:
            return
        with self._conectar() as conn:
            conn.execute('DELETE FROM cache_respostas WHERE expira_em <= now()')
            conn.execute(
                'INSERT INTO cache_respostas (chave, resposta, expira_em) '
                "VALUES (%s, %s, now() + make_interval(secs => %s)) "
                'ON CONFLICT (chave) DO UPDATE SET resposta = EXCLUDED.resposta, '
                'criada_em = now(), expira_em = EXCLUDED.expira_em, hits = 0',
                (chave, Jsonb(resposta.model_dump(mode='json')), self.ttl_s),
            )

    def limpar(self, *, apenas_expirados: bool = False) -> int:
        """Remove as respostas (todas ou só as expiradas) e devolve quantas saíram."""
        filtro = ' WHERE expira_em <= now()' if apenas_expirados else ''
        with self._conectar() as conn:
            return conn.execute(f'DELETE FROM cache_respostas{filtro}').rowcount


def gravar_no_cache(cache_repo: Any, chave: str, turno: RespostaTurno) -> bool:
    """Grava sem derrubar o turno: falha no cache só vira log."""
    gravar = getattr(cache_repo, 'gravar', None)
    if not callable(gravar):
        return False
    try:
        gravar(chave, RespostaCacheada.do_turno(turno))
    except Exception:
        logger.warning('Falha ao gravar a resposta no cache', exc_info=True)
        return False
    return True
