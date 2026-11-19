"""Testes do cache de respostas estáticas (etapas 2b e 5b, #36)."""

from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock

import psycopg
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
import pytest

from sabor.agent import cache as modulo_cache
from sabor.agent.cache import (
    CacheRespostas,
    RespostaCacheada,
    gerar_chave_cache,
    normalizar_pergunta,
    obter_ttl_cache_s,
    pergunta_autocontida,
    resposta_cacheavel,
)
from sabor.agent.deps import AgentDeps
from sabor.agent.fallback import MODELO_PRIMARIO
from sabor.agent.runner import criar_agente, processar_turno
from sabor.agent.schemas import Fonte, Rascunho, RespostaTurno, RetornoAntecipado

PERGUNTA = 'Como armazenar queijo coalho?'
VERSAO = 'sha256:dataset-teste'
FONTE = {'arquivo': 'fichas_tecnicas/ficha_queijo_coalho.txt', 'referencia': 'ARMAZENAMENTO'}
TEXTO = 'Mantenha o queijo coalho refrigerado entre 0 °C e 10 °C, embalado a vácuo.'


# ---------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------


class CacheEmMemoria:
    """Mesmo contrato de `CacheRespostas`, sem banco."""

    def __init__(self) -> None:
        self.itens: dict[str, RespostaCacheada] = {}
        self.consultas: list[str] = []

    def buscar_por_hash(self, chave: str) -> RespostaCacheada | None:
        self.consultas.append(chave)
        return self.itens.get(chave)

    def gravar(self, chave: str, resposta: RespostaCacheada) -> None:
        self.itens[chave] = resposta


class Contador:
    def __init__(self) -> None:
        self.chamadas = 0


def _agente_ficha(contador: Contador, *, texto: str = TEXTO, rascunhos: list | None = None):
    """Modelo falso: consulta a ficha técnica e responde citando a fonte devolvida pela tool."""

    def buscar_ficha(ctx: RunContext[AgentDeps]) -> dict:
        return {'trecho': 'Armazenar refrigerado.', 'fontes': [FONTE]}

    def fn_modelo(messages, info: AgentInfo) -> ModelResponse:
        contador.chamadas += 1
        ja_consultou = any(
            isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, 'parts', [])
        )
        if not ja_consultou:
            return ModelResponse(
                parts=[ToolCallPart(tool_name='buscar_ficha', args={}, tool_call_id='c1')]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name='final_result',
                    args={'texto': texto, 'fontes': [FONTE], 'rascunhos': rascunhos or []},
                )
            ]
        )

    modelo = FunctionModel(fn_modelo, model_name=MODELO_PRIMARIO)
    return criar_agente([modelo], tools=[buscar_ficha])


def _deps(usuario: str = 'Dona Lúcia', versao: str = VERSAO) -> AgentDeps:
    return AgentDeps(conn=MagicMock(), dataset_version=versao, usuario=usuario)


def _turno(sessao_id: str, cache_repo, contador: Contador, **kwargs) -> RespostaTurno:
    usuario = kwargs.pop('usuario', 'Dona Lúcia')
    mensagem = kwargs.pop('mensagem', PERGUNTA)
    agente = kwargs.pop('agente', None) or _agente_ficha(contador)
    return processar_turno(
        sessao_id,
        mensagem,
        usuario,
        agente=agente,
        deps=kwargs.pop('deps', None) or _deps(usuario),
        cache_repo=cache_repo,
        **kwargs,
    )


def _historico_anterior() -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content='Quem produz queijo coalho?')]),
        ModelResponse(parts=[TextPart(content='O P004.')]),
    ]


# ---------------------------------------------------------------------------
# Chave, normalização e regras de elegibilidade
# ---------------------------------------------------------------------------


def test_chave_ignora_caixa_acento_e_pontuacao():
    base = gerar_chave_cache(PERGUNTA, 'funcionario', VERSAO, date(2026, 10, 30))
    for variacao in ('como armazenar queijo coalho', '  COMO ARMAZENAR   QUEIJO COALHO?!  '):
        assert gerar_chave_cache(variacao, 'funcionario', VERSAO, date(2026, 10, 30)) == base
    assert normalizar_pergunta('Qual é a validade do pão?') == 'qual e a validade do pao'
    assert len(base) == 64


def test_chave_muda_com_perfil_versao_do_dataset_e_data_de_referencia():
    base = gerar_chave_cache(PERGUNTA, 'funcionario', VERSAO, date(2026, 10, 30))
    assert gerar_chave_cache(PERGUNTA, 'produtor', VERSAO, date(2026, 10, 30)) != base
    assert gerar_chave_cache(PERGUNTA, 'funcionario', 'sha256:nova', date(2026, 10, 30)) != base
    assert gerar_chave_cache(PERGUNTA, 'funcionario', VERSAO, date(2026, 11, 30)) != base


@pytest.mark.parametrize(
    ('pergunta', 'esperado'),
    [
        (PERGUNTA, True),
        ('Qual o preço da banana prata em outubro?', True),
        ('e o mel?', False),
        ('E quanto custa o quilo?', False),
        ('Qual a validade dele?', False),
        ('Repita a resposta anterior', False),
        ('Como armazenar isso?', False),
    ],
)
def test_pergunta_autocontida(pergunta, esperado):
    assert pergunta_autocontida(pergunta) is esperado


def test_ttl_configuravel_por_env(monkeypatch):
    monkeypatch.setattr(modulo_cache, 'load_dotenv', lambda: None)
    monkeypatch.delenv('CACHE_TTL_S', raising=False)
    assert obter_ttl_cache_s() == 24 * 60 * 60
    monkeypatch.setenv('CACHE_TTL_S', '600')
    assert obter_ttl_cache_s() == 600
    monkeypatch.setenv('CACHE_TTL_S', '0')
    assert obter_ttl_cache_s() == 0
    assert CacheRespostas(ttl_s=None).ativo is False
    monkeypatch.setenv('CACHE_TTL_S', 'abc')
    assert obter_ttl_cache_s() == 24 * 60 * 60


def _turno_ok(**kwargs) -> RespostaTurno:
    dados = {'sessao_id': 's', 'usuario': 'Dona Lúcia', 'texto': TEXTO, 'fontes': [Fonte(**FONTE)]}
    return RespostaTurno(**(dados | kwargs))


def test_resposta_cacheavel_so_aceita_respostas_estaticas_e_seguras():
    assert resposta_cacheavel(_turno_ok(), PERGUNTA) is True

    recusadas = [
        _turno_ok(texto='O CPF do produtor é 831.452.674-90.'),
        _turno_ok(texto='Ligue para (87) 99812-3456.'),
        _turno_ok(bloqueado_guardrail=True),
        _turno_ok(regras_disparadas=['4A']),
        _turno_ok(falha_segura=True),
        _turno_ok(retorno_antecipado=RetornoAntecipado(motivo='CACHE_HIT', mensagem=TEXTO)),
        _turno_ok(rascunhos=[Rascunho(destinatario='P004', canal='whatsapp', conteudo='Oi')]),
        _turno_ok(texto='   '),
    ]
    for turno in recusadas:
        assert resposta_cacheavel(turno, PERGUNTA) is False
    assert resposta_cacheavel(_turno_ok(), 'Meu e-mail é ana@exemplo.com, como armazenar?') is False


# ---------------------------------------------------------------------------
# Critério de aceite e integração com o turno
# ---------------------------------------------------------------------------


def test_criterio_de_aceite_repetir_em_outra_sessao_responde_do_cache_sem_llm():
    """Repetir "Como armazenar queijo coalho?" em outra sessão responde do cache sem chamar o
    LLM, citando as mesmas fontes, e a auditoria recebe `cache_hit = True` (#35)."""
    cache = CacheEmMemoria()
    auditoria: list[RespostaTurno] = []

    contador_a = Contador()
    primeiro = _turno('sessao-A', cache, contador_a, registrar_auditoria=auditoria.append)
    assert contador_a.chamadas == 2  # tool + resposta final
    assert primeiro.cache_hit is False
    assert len(cache.itens) == 1

    contador_b = Contador()
    segundo = _turno(
        'sessao-B',
        cache,
        contador_b,
        mensagem='como armazenar QUEIJO coalho',
        registrar_auditoria=auditoria.append,
    )

    assert contador_b.chamadas == 0  # zero tokens: o LLM não foi chamado
    assert segundo.cache_hit is True
    assert segundo.retorno_antecipado is not None
    assert segundo.retorno_antecipado.motivo == 'CACHE_HIT'
    assert segundo.bloqueado_guardrail is False
    assert segundo.modelo_efetivo is None
    assert segundo.texto == primeiro.texto
    assert segundo.fontes == primeiro.fontes == [Fonte(**FONTE)]
    assert [t.cache_hit for t in auditoria] == [False, True]


def test_perfil_produtor_nao_usa_o_cache():
    cache = CacheEmMemoria()
    contador = Contador()
    _turno('sessao-P', cache, contador, usuario='P004')
    _turno('sessao-P2', cache, contador, usuario='P004')

    assert cache.itens == {}
    assert cache.consultas == []
    assert contador.chamadas == 4


def test_resposta_de_funcionario_nao_e_servida_ao_produtor():
    cache = CacheEmMemoria()
    _turno('sessao-F', cache, Contador())
    contador = Contador()
    turno = _turno('sessao-P', cache, contador, usuario='P004')

    assert turno.cache_hit is False
    assert contador.chamadas == 2


def test_pergunta_que_depende_do_historico_nao_le_nem_grava_no_cache():
    cache = CacheEmMemoria()
    contador = Contador()
    turno = _turno(
        'sessao-M',
        cache,
        contador,
        mensagem='E como armazenar ele?',
        carregar_memoria=lambda _sid: _historico_anterior(),
    )

    assert turno.cache_hit is False
    assert cache.consultas == []
    assert cache.itens == {}


def test_pergunta_autocontida_no_meio_da_sessao_usa_o_cache():
    cache = CacheEmMemoria()
    _turno('sessao-A', cache, Contador())
    contador = Contador()
    turno = _turno(
        'sessao-M', cache, contador, carregar_memoria=lambda _sid: _historico_anterior()
    )

    assert turno.cache_hit is True
    assert contador.chamadas == 0


def test_nova_versao_do_dataset_invalida_o_cache():
    cache = CacheEmMemoria()
    _turno('sessao-A', cache, Contador())
    contador = Contador()
    turno = _turno('sessao-B', cache, contador, deps=_deps(versao='sha256:nova-carga'))

    assert turno.cache_hit is False
    assert contador.chamadas == 2
    assert len(cache.itens) == 2


def test_sem_versao_do_dataset_nao_usa_o_cache():
    cache = CacheEmMemoria()
    contador = Contador()
    _turno('sessao-A', cache, contador, deps=_deps(versao='sha256-unavailable'))

    assert cache.consultas == []
    assert cache.itens == {}


def test_resposta_com_rascunho_ou_dado_pessoal_nao_vai_para_o_cache():
    cache = CacheEmMemoria()
    rascunho = [{'destinatario': 'P004', 'canal': 'whatsapp', 'conteudo': 'Oi'}]
    _turno('sessao-R', cache, Contador(), agente=_agente_ficha(Contador(), rascunhos=rascunho))
    _turno(
        'sessao-D',
        cache,
        Contador(),
        agente=_agente_ficha(Contador(), texto='Fale com Ana pelo (87) 99812-3456.'),
    )
    assert cache.itens == {}


def test_falha_no_cache_nao_derruba_o_turno():
    class CacheQuebrado:
        def buscar_por_hash(self, chave):
            raise psycopg.OperationalError('banco fora')

        def gravar(self, chave, resposta):
            raise psycopg.OperationalError('banco fora')

    contador = Contador()
    turno = _turno('sessao-X', CacheQuebrado(), contador)

    assert turno.falha_segura is False
    assert turno.texto == TEXTO
    assert contador.chamadas == 2


def test_hit_preserva_flags_da_resposta_original():
    cache = CacheEmMemoria()
    chave = gerar_chave_cache(PERGUNTA, 'funcionario', VERSAO, _deps().data_referencia)
    cache.itens[chave] = RespostaCacheada.model_validate(
        {
            'resposta': TEXTO,
            'fontes': [FONTE],
            'flags': [{'tipo': 'conformidade', 'descricao': 'Venda exige SIE.'}],
        }
    )

    turno = _turno('sessao-F', cache, Contador())

    assert turno.cache_hit is True
    assert [f.tipo for f in turno.flags] == ['conformidade']


# ---------------------------------------------------------------------------
# Repositório no PostgreSQL (banco temporário)
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_pg(url_banco_temporario):
    @contextmanager
    def conectar():
        with psycopg.connect(url_banco_temporario, autocommit=True) as conn:
            yield conn

    repo = CacheRespostas(conectar, ttl_s=3600)
    repo.limpar()
    return repo


def test_tabela_de_cache_e_unlogged(conn_temporaria):
    persistencia, colunas = conn_temporaria.execute(
        "SELECT c.relpersistence, array_agg(a.attname::text ORDER BY a.attnum) "
        'FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid '
        "WHERE c.relname = 'cache_respostas' AND a.attnum > 0 GROUP BY c.relpersistence"
    ).fetchone()
    assert persistencia == 'u'
    assert colunas == ['chave', 'resposta', 'criada_em', 'expira_em', 'hits']


def test_repositorio_grava_busca_conta_hits_e_limpa(repo_pg, conn_temporaria):
    chave = gerar_chave_cache(PERGUNTA, 'funcionario', VERSAO, date(2026, 10, 30))
    assert repo_pg.buscar_por_hash(chave) is None

    repo_pg.gravar(chave, RespostaCacheada(resposta=TEXTO, fontes=[Fonte(**FONTE)]))
    hit = repo_pg.buscar_por_hash(chave)
    repo_pg.buscar_por_hash(chave)

    assert hit is not None and hit.resposta == TEXTO and hit.fontes == [Fonte(**FONTE)]
    hits = conn_temporaria.execute(
        'SELECT hits FROM cache_respostas WHERE chave = %s', (chave,)
    ).fetchone()[0]
    assert hits == 2

    # Expirada: não é servida e sai na limpeza dos expirados.
    conn_temporaria.execute(
        "UPDATE cache_respostas SET expira_em = now() - interval '1 second' WHERE chave = %s",
        (chave,),
    )
    assert repo_pg.buscar_por_hash(chave) is None
    assert repo_pg.limpar(apenas_expirados=True) == 1

    repo_pg.gravar(chave, RespostaCacheada(resposta=TEXTO))
    assert repo_pg.limpar() == 1
    assert repo_pg.buscar_por_hash(chave) is None


def test_criterio_de_aceite_com_postgres(repo_pg):
    contador_a, contador_b = Contador(), Contador()
    primeiro = _turno('sessao-A', repo_pg, contador_a)
    segundo = _turno('sessao-B', repo_pg, contador_b)

    assert contador_a.chamadas == 2
    assert contador_b.chamadas == 0
    assert segundo.cache_hit is True
    assert segundo.fontes == primeiro.fontes
