"""Construção do agente único PydanticAI e orquestração de `processar_turno` (Etapas 1 a 5)."""

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
import contextvars
import inspect
import json
import logging
import re
import threading
import time
from types import ModuleType
from typing import Any

import httpx
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    UnexpectedModelBehavior,
    UsageLimitExceeded
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from sabor import tools as modulo_tools
from sabor.agent.cache import (
    PERFIS_CACHEAVEIS,
    gerar_chave_cache,
    gravar_no_cache,
    pergunta_autocontida,
    resposta_cacheavel,
    versao_permite_cache,
)
from sabor.agent.deps import AgentDeps, obter_data_referencia, obter_versao_dataset
from sabor.agent.fallback import (
    _PADRAO_CITACAO_FONTE_PONTO,
    construir_capability_fallback,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
    ler_segundos_env,
    limpar_citacoes_inline_texto,
)
from sabor.agent.guardrail_entry import (
    MENSAGEM_ACESSO_NAO_AUTORIZADO,
    criar_retorno_orcamento_turno,
    executar_guardrail_entrada,
    obter_usage_limits_turno,
)
from sabor.agent.guardrail_exit import processar_guardrail_saida
from sabor.agent.mascarador import Mascarador
from sabor.agent.prompts import SYSTEM_PROMPT_BASE, instrucoes_contexto_turno
from sabor.agent.schemas import (
    Flag,
    Fonte,
    Rascunho,
    Relatorio,
    RespostaAgente,
    RespostaTurno,
    RetornoAntecipado,
    TentativaFallback,
)
from sabor.db import conectar_readonly
from sabor.observabilidade import instrumentar_turno
from sabor.tools.base import SemPermissao, _resolver_usuario

logger = logging.getLogger(__name__)

TIMEOUT_TURNO_S = 20.0
"""Limite global de tempo do turno completo (em segundos) antes de acionar falha segura (#37).

Configurável em `TURNO_TIMEOUT_S`.
"""

FOLGA_ENCERRAMENTO_S = 1.0
"""Quanto o turno espera, além do prazo, a execução cancelada terminar (ex.: uma tool síncrona
em andamento) antes de devolver a falha segura sem ela (#49)."""

MENSAGEM_FALHA_SEGURA = (
    'Não consegui responder agora devido a uma instabilidade temporária nos provedores de IA. '
    'Por favor, tente novamente em instantes.'
)
"""Mensagem padrão devolvida quando todos os modelos falham ou o turno excede 20s."""

MENSAGEM_ENTRADA_VAZIA = 'Por favor, envie uma mensagem ou dúvida para que eu possa ajudar.'
"""Mensagem devolvida quando a entrada está vazia antes de acionar o LLM (Etapa 2)."""

NOMES_FERRAMENTAS_ESPERADAS = (
    'extrair_e_classificar_pedido',
    'consultar_dados',  # compatibilidade com testes e integrações legadas
    'buscar_produtores',
    'listar_produtos_catalogo',
    'consultar_financeiro',
    'rastrear_lote',
    'consultar_preco',
    'consultar_safra',
    'buscar_documentos',
    'resumir_produtor',
    'rascunhar_notificacao',
    'gerar_relatorio',
)
"""Nomes de referência previstos na arquitetura (#7, #8, #9, #11, #12, #15)."""

_PADRAO_SNAKE_CASE = re.compile(r'^[a-z][a-z0-9_]*$')


def carregar_ferramentas_registradas(
    modulo: ModuleType = modulo_tools,
) -> list[Callable[..., Any]]:
    """Carrega as ferramentas públicas em `snake_case` definidas em `sabor.tools`.

    Prioriza a ordem de `NOMES_FERRAMENTAS_ESPERADAS` e inclui também qualquer outra função
    pública em `snake_case` declarada no módulo pelo responsável pelas tools.
    """
    ferramentas: list[Callable[..., Any]] = []
    vistas: set[str] = set()

    for nome in NOMES_FERRAMENTAS_ESPERADAS:
        fn = getattr(modulo, nome, None)
        if callable(fn) and not inspect.isclass(fn):
            ferramentas.append(fn)
            vistas.add(nome)

    nome_modulo = getattr(modulo, '__name__', None)
    for nome, fn in vars(modulo).items():
        if nome in vistas or not _PADRAO_SNAKE_CASE.match(nome):
            continue
        if callable(fn) and not inspect.isclass(fn):
            mod_origem = getattr(fn, '__module__', None)
            pertence_ao_modulo = (
                nome_modulo is None
                or mod_origem in (None, nome_modulo)
                or (isinstance(mod_origem, str) and mod_origem.startswith(f'{nome_modulo}.'))
            )
            if pertence_ao_modulo:
                ferramentas.append(fn)
                vistas.add(nome)

    return ferramentas


def criar_agente(
    modelos: Sequence[Model | str] | None = None,
    *,
    tools: Sequence[Callable[..., Any]] | None = None,
    gemini_api_key: str | None = None,
    openrouter_api_key: str | None = None,
    timeout_s: float | None = None,
    cooldown_s: float | None = None,
) -> Agent[AgentDeps, RespostaAgente]:
    """Cria e configura o agente único PydanticAI com prompt, fallback e tools disponíveis.

    `timeout_s` é o limite de cada chamada de modelo da cadeia de fallback; `None` usa
    `MODELO_TIMEOUT_S` do `.env` ou o padrão de `fallback.TIMEOUT_POR_MODELO_S`.
    `cooldown_s` controla a quarentena do circuit breaker por modelo com falha transitória
    (`None` usa `FALLBACK_COOLDOWN_S` ou `fallback.COOLDOWN_CIRCUIT_BREAKER_S`; `0` desliga).
    """
    capability_fallback = construir_capability_fallback(
        modelos,
        gemini_api_key=gemini_api_key,
        openrouter_api_key=openrouter_api_key,
        timeout_s=timeout_s,
        cooldown_s=cooldown_s,
    )
    ferramentas_ativas = list(tools) if tools is not None else carregar_ferramentas_registradas()
    # `instructions` (e não `system_prompt`): o PydanticAI só envia o system prompt quando o
    # `message_history` está vazio; as instructions vão em toda requisição, inclusive nos
    # turnos seguintes que carregam a memória da sessão (#31).
    agente: Agent[AgentDeps, RespostaAgente] = Agent(
        deps_type=AgentDeps,
        output_type=RespostaAgente,
        instructions=SYSTEM_PROMPT_BASE,
        tools=ferramentas_ativas,
        retries=1,
        capabilities=[capability_fallback],
    )
    agente.instructions(instrucoes_contexto_turno)
    return agente


def _anexar_relatorios(
    resposta: RespostaAgente,
    deps: AgentDeps,
    *,
    gerar_relatorio_chamado: bool = True,
) -> RespostaAgente:
    """Etapa 4: anexa à resposta só os relatórios que as tools geraram no turno (#15).

    `relatorios` fica fora do schema do LLM, mas o Pydantic ainda validaria um valor que o
    modelo mandasse no `final_result`. Por isso o campo é sempre sobrescrito com o que está em
    `deps.relatorios`: nenhum anexo vem do modelo.

    Quem decide se o relatório pode ser gerado é `FallbackComCircuitBreaker.prepare_tools`,
    que só oferece `gerar_relatorio` quando a pergunta pede um. Aqui só se descartam
    relatórios que apareçam sem `gerar_relatorio` ter rodado no turno; se a tool rodou, o
    anexo é mantido, para a resposta não dizer que o arquivo está pronto sem entregá-lo.

    O Markdown passa pelo mascarador como rede de segurança (o arquivo pode sair da
    cooperativa, como o dossiê da ADAGRO) e, como os rascunhos, não tem a PII restaurada.
    Se a mesma tool rodou mais de uma vez, fica a última versão de cada arquivo.

    O guardrail de saída avalia o texto do modelo e não remove os relatórios: o conteúdo deles
    é montado sem LLM, a partir das tools. A regra 4A, por exemplo, barra qualquer texto que
    cite o P008 junto de "vigente", inclusive o resumo correto que o aponta como vencido.
    """
    lista = getattr(deps, 'relatorios', None)
    gerados = [r for r in lista if isinstance(r, Relatorio)] if isinstance(lista, list) else []
    if isinstance(lista, list):
        lista.clear()

    if not gerar_relatorio_chamado:
        return resposta.model_copy(update={'relatorios': []})

    mascarar_fn = getattr(deps.mascarador, 'mascarar', None)
    por_arquivo: dict[str, Relatorio] = {}
    for relatorio in gerados:
        if callable(mascarar_fn):
            relatorio = relatorio.model_copy(update={'markdown': mascarar_fn(relatorio.markdown)})
        por_arquivo[relatorio.nome_arquivo] = relatorio
    return resposta.model_copy(update={'relatorios': list(por_arquivo.values())})


TOOLS_DE_RASCUNHO = frozenset({'resumir_produtor', 'rascunhar_notificacao'})
"""Tools que montam rascunhos sem LLM e os registram em `deps.rascunhos`."""


def _corrigir_quebras_escapadas(rascunho: Rascunho) -> Rascunho:
    """Troca `\\n` literal por quebra de linha quando o modelo escapou o texto duas vezes."""
    conteudo = rascunho.conteudo
    if '\\n' in conteudo and '\n' not in conteudo:
        return rascunho.model_copy(update={'conteudo': conteudo.replace('\\n', '\n')})
    return rascunho


def _anexar_rascunhos(
    resposta: RespostaAgente,
    deps: AgentDeps,
    *,
    tool_de_rascunho_chamada: bool,
) -> RespostaAgente:
    """Etapa 4: usa os rascunhos que as tools montaram no lugar dos que o modelo devolveu.

    Quando `resumir_produtor` ou `rascunhar_notificacao` rodam no turno, os rascunhos da
    resposta são exatamente os que elas registraram em `deps.rascunhos`. O modelo costumava
    redigitá-los: perdia quantidades, inventava itens e às vezes escapava as quebras de linha
    (F3 do Bloco 4). Os rascunhos do modelo são descartados mesmo quando a tool não montou
    nenhum, para ele não preencher a lacuna por conta própria.

    Sem essas tools, valem os rascunhos do modelo (ex.: resposta a um comprador), só com as
    quebras de linha corrigidas. Como os relatórios, os rascunhos passam pelo mascarador e não
    têm a PII restaurada; o guardrail de saída (4a) os avalia depois.
    """
    lista = getattr(deps, 'rascunhos', None)
    gerados = [r for r in lista if isinstance(r, Rascunho)] if isinstance(lista, list) else []
    if isinstance(lista, list):
        lista.clear()

    if not tool_de_rascunho_chamada:
        return resposta.model_copy(
            update={'rascunhos': [_corrigir_quebras_escapadas(r) for r in resposta.rascunhos]}
        )

    mascarar_fn = getattr(deps.mascarador, 'mascarar', None)
    unicos: dict[tuple[str, str], Rascunho] = {}
    for rascunho in gerados:
        if callable(mascarar_fn):
            rascunho = rascunho.model_copy(
                update={'conteudo': mascarar_fn(rascunho.conteudo)}
            )
        unicos.setdefault((rascunho.destinatario, rascunho.conteudo), rascunho)
    return resposta.model_copy(update={'rascunhos': list(unicos.values())})


TIPOS_FLAG_DAS_TOOLS = {'resumir_produtor': frozenset({'conformidade', 'inadimplencia'})}
"""Tipos de flag que cada tool monta sem LLM; quando ela roda, esses tipos vêm só dela."""


def _anexar_flags(
    resposta: RespostaAgente,
    deps: AgentDeps,
    *,
    tools_chamadas: set[str],
) -> RespostaAgente:
    """Etapa 4: troca as flags dos tipos cobertos pelas tools pelas que elas montaram.

    Com dois resumos no turno, o modelo deixava de copiar as flags do `resumir_produtor`, e
    às vezes marcava como inadimplente o produtor, que é o credor (F5 e F6 do Bloco 4). Flags
    de outros tipos que o modelo registrar (ex.: `ambiguidade`) continuam.
    """
    lista = getattr(deps, 'flags', None)
    geradas = [f for f in lista if isinstance(f, Flag)] if isinstance(lista, list) else []
    if isinstance(lista, list):
        lista.clear()

    tipos_cobertos: set[str] = set()
    for tool in tools_chamadas:
        tipos_cobertos |= TIPOS_FLAG_DAS_TOOLS.get(tool, frozenset())
    if not tipos_cobertos:
        return resposta

    unicas: dict[tuple[str, str], Flag] = {}
    for flag in geradas:
        unicas.setdefault((flag.tipo, flag.descricao), flag)
    do_modelo = [f for f in resposta.flags if f.tipo not in tipos_cobertos]
    return resposta.model_copy(update={'flags': [*unicas.values(), *do_modelo]})


def _normalizar_fontes_e_texto_resposta(resposta: RespostaAgente) -> RespostaAgente:
    """Move citações `[arquivo · ref]` do texto para `fontes` e limpa blocos `Fontes:`."""
    if not resposta.texto:
        return resposta

    fontes_atuais = list(resposta.fontes)
    vistas = {(f.arquivo.strip(), f.referencia.strip()) for f in fontes_atuais}
    for arq, ref in _PADRAO_CITACAO_FONTE_PONTO.findall(resposta.texto):
        chave = (arq.strip(), ref.strip())
        if chave[0] and chave[1] and chave not in vistas:
            vistas.add(chave)
            fontes_atuais.append(Fonte(arquivo=chave[0], referencia=chave[1]))

    texto_limpo = limpar_citacoes_inline_texto(resposta.texto)
    return resposta.model_copy(update={'texto': texto_limpo, 'fontes': fontes_atuais})


def _restaurar_pii_resposta(resposta: RespostaAgente, mascarador: Any) -> RespostaAgente:
    """Etapa 4c: Restaura tokens só no texto do operador; nunca em rascunhos (#33)."""
    restaurar_fn = getattr(mascarador, 'restaurar', None)
    if not callable(restaurar_fn):
        return resposta

    texto_restaurado = restaurar_fn(resposta.texto)
    return resposta.model_copy(update={'texto': texto_restaurado})


def obter_timeout_turno() -> float:
    """Teto do turno completo: `TURNO_TIMEOUT_S` ou `TIMEOUT_TURNO_S` (20 s, #37)."""
    return ler_segundos_env('TURNO_TIMEOUT_S', TIMEOUT_TURNO_S)


def _cancelar_consulta_em_andamento(conn: Any) -> None:
    """Cancela no servidor a consulta que uma execução abandonada ainda está rodando.

    `psycopg` segura `conn.lock` durante cada `execute`; lock livre = nada a cancelar.
    """
    lock = getattr(conn, 'lock', None)
    if lock is not None and not lock.locked():
        return
    try:
        conn.cancel_safe(timeout=2.0)
    except Exception:
        logger.warning('Não consegui cancelar a consulta do turno abandonado', exc_info=True)


def _fechar_quando_livre(conn: Any) -> None:
    """Fecha a conexão quando nenhuma consulta a estiver usando.

    `Connection.close()` do psycopg não pega o lock e finaliza o `PGconn` mesmo com outra
    thread no meio de uma consulta, o que não é seguro na libpq. Sob o lock, o fechamento
    espera a consulta em curso; uma tool órfã que tente outra consulta depois recebe
    "connection is closed", sem tocar no `PGconn` já finalizado.
    """
    lock = getattr(conn, 'lock', None)
    try:
        if lock is None:
            conn.close()
        else:
            with lock:
                conn.close()
    except Exception:
        logger.warning('Falha ao fechar a conexão do turno', exc_info=True)


def _executar_com_prazo(
    agente: Agent[AgentDeps, RespostaAgente],
    mensagem: str,
    *,
    prazo_s: float,
    conn_propria: Any | None = None,
    **kwargs: Any,
) -> Any:
    """Roda o agente numa thread própria e devolve o resultado em até `prazo_s` (#49).

    - O prazo é aplicado dentro da thread com `asyncio.wait_for`, que cancela a requisição
      HTTP em andamento em vez de deixá-la rodar (e seguir para o próximo modelo) no fundo.
    - Se a execução não encerrar logo após o cancelamento (ex.: tool síncrona no meio de uma
      consulta), o turno não a espera: levanta `TimeoutError` em `prazo_s + FOLGA_ENCERRAMENTO_S`
      e encerra o executor sem bloquear (`shutdown(wait=False)`).
    - Tools síncronas rodam em threads que o cancelamento do asyncio não interrompe. Por isso
      a conexão aberta pelo turno é fechada só quando estiver livre (`_fechar_quando_livre`)
      e, num turno abandonado, a consulta em andamento é cancelada no servidor.
    """

    async def _rodar() -> Any:
        return await asyncio.wait_for(agente.run(mensagem, **kwargs), timeout=prazo_s)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='sabor-turno')
    # A thread não herda os contextvars: sem a cópia, os spans do PydanticAI ficariam fora do
    # trace do turno (#39).
    contexto = contextvars.copy_context()
    futuro: Future[Any] = executor.submit(contexto.run, lambda: asyncio.run(_rodar()))
    abandonado = False
    fechar_lock = threading.Lock()
    fechado = False

    def _fechar_uma_vez() -> None:
        nonlocal fechado
        with fechar_lock:
            if fechado:
                return
            fechado = True
        _fechar_quando_livre(conn_propria)

    if conn_propria is not None:

        def _ao_terminar(f: Future[Any]) -> None:
            # No caminho de sucesso o turno ainda usa `conn_propria` na Etapa 4 (guardrail de saída)
            # e a fecha lá. Em erro/cancelamento/timeout, fecha aqui em segundo plano quando livre.
            if abandonado or f.cancelled() or f.exception() is not None:
                _fechar_uma_vez()

        futuro.add_done_callback(_ao_terminar)

    try:
        return futuro.result(timeout=prazo_s + FOLGA_ENCERRAMENTO_S)
    except TimeoutError:
        abandonado = True
        if conn_propria is not None:
            _cancelar_consulta_em_andamento(conn_propria)
            if futuro.done():
                threading.Thread(
                    target=_fechar_uma_vez,
                    name='sabor-fechar-conn',
                    daemon=True,
                ).start()
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _contar_turnos_historico(historico: Sequence[ModelMessage]) -> int:
    """Conta perguntas de usuário na sessão atual (ignora retornos de tools)."""
    return sum(
        1
        for msg in historico
        if isinstance(msg, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in msg.parts)
    )


def _mascarar_historico_json(serializado: str, mascarar: Callable[[str], str]) -> str:
    """Mascara strings do histórico sem tocar em IDs que pareiam chamadas/retornos de tools."""
    dados = json.loads(serializado)
    chaves_identidade = {'id', 'tool_call_id', 'conversation_id', 'message_id', 'metadata'}

    def percorrer(valor: Any, chave_pai: str | None = None) -> Any:
        if chave_pai in chaves_identidade:
            return valor
        if isinstance(valor, dict):
            return {k: percorrer(v, k) for k, v in valor.items()}
        if isinstance(valor, list):
            return [percorrer(v, chave_pai) for v in valor]
        if isinstance(valor, str):
            return mascarar(valor)
        return valor

    return json.dumps(percorrer(dados), ensure_ascii=False, separators=(',', ':'))


def _coletar_fontes_valor(
    valor: Any, destino: list[str], *, aceitar_str: bool = False
) -> None:
    if valor is None:
        return
    if hasattr(valor, 'model_dump'):
        valor = valor.model_dump()
    if isinstance(valor, dict):
        arquivo = valor.get('arquivo')
        referencia = valor.get('referencia')
        if isinstance(arquivo, str) and arquivo.strip():
            ref = (
                f' {referencia.strip()}'
                if isinstance(referencia, str) and referencia.strip()
                else ''
            )
            destino.append(f'{arquivo.strip()}{ref}')
        for chave in ('fontes', 'fonte'):
            if chave in valor:
                _coletar_fontes_valor(valor[chave], destino, aceitar_str=True)
        for k, v in valor.items():
            if k not in ('arquivo', 'referencia', 'fontes', 'fonte') and isinstance(
                v, (dict, list, tuple)
            ):
                _coletar_fontes_valor(v, destino, aceitar_str=False)
    elif isinstance(valor, (list, tuple)):
        for item in valor:
            _coletar_fontes_valor(item, destino, aceitar_str=aceitar_str)
    elif aceitar_str and isinstance(valor, str) and valor.strip():
        destino.append(valor.strip())


def _sem_extensao(arquivo: str) -> str:
    """`laudos/laudo_x_P003.txt` → `laudos/laudo_x_p003` (tudo depois do 1º ponto do nome sai)."""
    pasta, _, nome = arquivo.strip().rpartition('/')
    base = nome.split('.', 1)[0]
    return f'{pasta}/{base}'.lower() if pasta else base.lower()


def _fontes_estruturadas_das_tools(novas_mensagens: Sequence[ModelMessage]) -> list[Fonte]:
    """Fontes (`arquivo`, `referencia`) que as tools devolveram no turno."""
    fontes: list[Fonte] = []

    def percorrer(valor: Any) -> None:
        if hasattr(valor, 'model_dump'):
            valor = valor.model_dump()
        if isinstance(valor, dict):
            arquivo, referencia = valor.get('arquivo'), valor.get('referencia')
            if isinstance(arquivo, str) and isinstance(referencia, str):
                fontes.append(Fonte(arquivo=arquivo, referencia=referencia))
            for v in valor.values():
                if isinstance(v, (dict, list, tuple)):
                    percorrer(v)
        elif isinstance(valor, (list, tuple)):
            for item in valor:
                percorrer(item)

    for msg in novas_mensagens:
        for part in getattr(msg, 'parts', None) or ():
            if isinstance(part, ToolReturnPart):
                percorrer(part.content)
    return fontes


def _dividir_citacoes(fonte: Fonte) -> list[Fonte]:
    """Quebra `P001, P004 e P005` ou `a.txt · X, b.txt · Y` em uma fonte por citação.

    Um pedaço sem `·` herda o arquivo do pedaço anterior.
    """
    arquivo_atual = fonte.arquivo.strip()
    citacoes: list[Fonte] = []
    for pedaco in re.split(r'\s*(?:,|;|\be\b)\s*', f'{arquivo_atual} · {fonte.referencia}'):
        if not pedaco.strip():
            continue
        if '·' in pedaco:
            partes = [p.strip() for p in pedaco.split('·') if p.strip()]
            arquivo_atual, referencia = partes[0], partes[-1]
        else:
            referencia = pedaco.strip()
        citacoes.append(Fonte(arquivo=arquivo_atual, referencia=referencia))
    return citacoes


def _reconciliar_fontes_com_tools(
    resposta: RespostaAgente, fontes_tools: Sequence[Fonte]
) -> RespostaAgente:
    """Desfaz dois erros do modelo ao redigitar fontes que as tools retornaram (F8 do Bloco 4).

    - Extensão trocada: `laudo…P003.txt` virou `.csv`, `.___` e `.quist.txt` no REL-04, e a
      regra 4B bloqueava a resposta inteira. A fonte é reparada só quando uma fonte do turno
      tem a mesma referência, a mesma pasta e o mesmo nome, mudando só a extensão.
    - Citações juntadas numa fonte só: `produtores.csv · P001, P004, P005` no REL-08, ou
      várias certificações de arquivos diferentes no REL-07. A fonte é dividida só quando
      cada pedaço é uma fonte retornada no turno.
    - Referência com o formato inteiro (`referencia="produtores.csv · P017"`, REL-07): o
      prefixo com o arquivo sai.
    - Referência igual ao arquivo (`produtores.csv · produtores.csv`, REL-07): não aponta
      registro nenhum e é descartada.

    Uma fonte inventada não tem correspondência, fica como veio e é barrada pela 4B.
    """
    exatas = {(f.arquivo.strip(), f.referencia.strip().lower()) for f in fontes_tools}
    por_nome = {
        (_sem_extensao(f.arquivo), f.referencia.strip().lower()): f.arquivo.strip()
        for f in fontes_tools
    }

    def reparar(fonte: Fonte) -> Fonte:
        ref = fonte.referencia.strip().lower()
        if (fonte.arquivo.strip(), ref) in exatas:
            return fonte
        original = por_nome.get((_sem_extensao(fonte.arquivo), ref))
        return fonte.model_copy(update={'arquivo': original}) if original else fonte

    reconciliadas: list[Fonte] = []
    vistas: set[tuple[str, str]] = set()
    for fonte in resposta.fontes:
        # Referência com o formato inteiro (`produtores.csv · P017` dentro de `referencia`).
        prefixo = re.compile(rf'^\s*{re.escape(fonte.arquivo.strip())}\s*·\s*')
        if prefixo.match(fonte.referencia):
            fonte = fonte.model_copy(update={'referencia': prefixo.sub('', fonte.referencia)})
        # `produtores.csv · produtores.csv` (REL-07) não aponta registro nenhum: descarta.
        if not fonte.referencia.strip() or fonte.referencia.strip() == fonte.arquivo.strip():
            continue
        fonte = reparar(fonte)
        candidatas = [fonte]
        if (fonte.arquivo.strip(), fonte.referencia.strip().lower()) not in exatas:
            divididas = [reparar(f) for f in _dividir_citacoes(fonte)]
            if len(divididas) > 1 and all(
                (f.arquivo.strip(), f.referencia.strip().lower()) in exatas for f in divididas
            ):
                candidatas = divididas
        for candidata in candidatas:
            chave = (candidata.arquivo.strip(), candidata.referencia.strip().lower())
            if chave not in vistas:
                vistas.add(chave)
                reconciliadas.append(candidata)
    return resposta.model_copy(update={'fontes': reconciliadas})


TOOLS_COM_FONTES_DOS_RASCUNHOS = frozenset({'rascunhar_notificacao'})
"""Tools cujas fontes (pedido e produtores de cada rascunho) sempre entram na resposta."""


def _completar_fontes_dos_rascunhos(
    resposta: RespostaAgente, novas_mensagens: Sequence[ModelMessage]
) -> RespostaAgente:
    """Acrescenta as fontes de `rascunhar_notificacao` que o modelo não citou.

    Os rascunhos são anexados sem passar pelo modelo (F3); as fontes deles também devem
    estar na resposta. No REL-08 do Bloco 4, o modelo citou só o pedido em 2 de 3 rodadas.
    """
    retornos = [
        part
        for msg in novas_mensagens
        for part in getattr(msg, 'parts', None) or ()
        if isinstance(part, ToolReturnPart) and part.tool_name in TOOLS_COM_FONTES_DOS_RASCUNHOS
    ]
    if not retornos:
        return resposta
    fontes = list(resposta.fontes)
    vistas = {(f.arquivo.strip(), f.referencia.strip().lower()) for f in fontes}
    for fonte in _fontes_estruturadas_das_tools([ModelRequest(parts=retornos)]):
        chave = (fonte.arquivo.strip(), fonte.referencia.strip().lower())
        if chave not in vistas:
            vistas.add(chave)
            fontes.append(fonte)
    return resposta.model_copy(update={'fontes': fontes})


def _extrair_fontes_das_tools(novas_mensagens: Sequence[ModelMessage]) -> list[str]:
    fontes: list[str] = []
    for msg in novas_mensagens:
        for part in getattr(msg, "parts", None) or ():
            if isinstance(part, ToolReturnPart):
                _coletar_fontes_valor(part.content, fontes)
    return fontes


def _cache_aplicavel(
    cache_repo: Any | None,
    perfil: str,
    dataset_version: str | None,
    historico: Sequence[ModelMessage],
    mensagem: str,
) -> bool:
    """2b/5b (#36): o cache só vale para a equipe, com versão do dataset conhecida e fora de
    contexto multi-turno (primeira pergunta da sessão ou pergunta autocontida)."""
    if cache_repo is None or perfil not in PERFIS_CACHEAVEIS:
        return False
    if not versao_permite_cache(dataset_version):
        return False
    return not historico or pergunta_autocontida(mensagem)


@instrumentar_turno
def processar_turno(
    sessao_id: str,
    mensagem: str,
    usuario: str = 'Dona Lúcia',
    *,
    agente: Agent[AgentDeps, RespostaAgente] | None = None,
    deps: AgentDeps | None = None,
    timeout_turno_s: float | None = None,
    usage_limits: UsageLimits | None = None,
    requisicoes_na_sessao: int | None = None,
    cache_repo: Any | None = None,
    # Hooks de integração com as demais issues do diagrama Agent System:
    carregar_memoria: Callable[[str], Sequence[ModelMessage]] | None = None,  # Etapa 1 (#31)
    guardrail_entrada: Callable[[str], RetornoAntecipado | str | None] | None = None,  # Etapa 2
    pos_processar: Callable[[RespostaAgente], RespostaAgente] | None = None,  # Etapa 4
    registrar_auditoria: Callable[..., None] | None = None,  # Etapa 5a (#35)
    salvar_memoria: Callable[..., int | None] | None = None,  # Etapa 5b (#31)
) -> RespostaTurno:
    """Executa o turno completo do diagrama Agent System (etapas 1 a 5) com retornos antecipados."""
    inicio = time.perf_counter()

    def persistir_e_auditar(
        turno: RespostaTurno, historico_modelo_json: str | None = None
    ) -> None:
        """Salva a mensagem primeiro para obter seu id e então grava a auditoria vinculada."""
        message_id = None
        if salvar_memoria is not None:
            try:
                parametros = inspect.signature(salvar_memoria).parameters
            except (TypeError, ValueError):
                parametros = {}
            if 'historico_modelo_json' in parametros:
                message_id = salvar_memoria(
                    sessao_id,
                    mensagem.strip(),
                    turno,
                    historico_modelo_json=historico_modelo_json,
                )
            else:
                message_id = salvar_memoria(sessao_id, mensagem.strip(), turno)
        if registrar_auditoria is not None:
            try:
                parametros = inspect.signature(registrar_auditoria).parameters
            except (TypeError, ValueError):
                parametros = {}
            if 'message_id' in parametros:
                registrar_auditoria(turno, message_id=message_id)
            else:
                registrar_auditoria(turno)

    # -----------------------------------------------------------------------
    # Etapa 1: Carregar memória da sessão (Issue #31)
    # -----------------------------------------------------------------------
    historico: list[ModelMessage] = (
        list(carregar_memoria(sessao_id)) if carregar_memoria is not None else []
    )
    conn_propria = None
    dataset_ver = deps.dataset_version if deps is not None else None
    perfil_turno = _resolver_usuario(usuario).perfil
    data_ref_turno = deps.data_referencia if deps is not None else obter_data_referencia()
    usar_cache = False

    # -----------------------------------------------------------------------
    # Etapa 2: Guardrail de entrada (retorno antecipado sem chamada LLM)
    # -----------------------------------------------------------------------
    mensagem_limpa = mensagem.strip()
    retorno_antecipado: RetornoAntecipado | None = None

    try:
        if not mensagem_limpa:
            retorno_antecipado = RetornoAntecipado(
                motivo='ENTRADA_VAZIA',
                mensagem=MENSAGEM_ENTRADA_VAZIA,
            )
        elif guardrail_entrada is not None:
            resultado_hook = guardrail_entrada(mensagem_limpa)
            if isinstance(resultado_hook, RetornoAntecipado):
                retorno_antecipado = resultado_hook
            elif isinstance(resultado_hook, str):
                retorno_antecipado = RetornoAntecipado(
                    motivo='FORA_DE_ESCOPO',
                    mensagem=resultado_hook,
                )
        else:
            # A Etapa 2 usa a versão persistida pelo ETL/ingestão (1 linha de metadados),
            # reutilizada na Etapa 3 sem abrir conexão para entradas vazias/hooks customizados.
            if not dataset_ver:
                conn_fingerprint = deps.conn if deps is not None else conectar_readonly()
                if deps is None:
                    conn_propria = conn_fingerprint
                dataset_ver = obter_versao_dataset(conn_fingerprint)
            dataset_ver = dataset_ver or 'sha256-unavailable'
            contagem_sessao = (
                requisicoes_na_sessao
                if requisicoes_na_sessao is not None
                else _contar_turnos_historico(historico)
            )
            usar_cache = _cache_aplicavel(
                cache_repo, perfil_turno, dataset_ver, historico, mensagem_limpa
            )
            retorno_antecipado = executar_guardrail_entrada(
                mensagem=mensagem_limpa,
                dataset_version=dataset_ver,
                requisicoes_na_sessao=contagem_sessao,
                cache_repo=cache_repo if usar_cache else None,
                perfil=perfil_turno,
                produtor_id=_resolver_usuario(usuario).produtor_id,
                data_referencia=data_ref_turno,
            )

        if retorno_antecipado is not None:
            if conn_propria is not None:
                _fechar_quando_livre(conn_propria)
                conn_propria = None
            latencia_ms = int((time.perf_counter() - inicio) * 1000)
            eh_cache_hit = retorno_antecipado.motivo == 'CACHE_HIT'
            turno_antecipado = RespostaTurno(
                sessao_id=sessao_id,
                usuario=usuario,
                texto=retorno_antecipado.mensagem,
                fontes=list(retorno_antecipado.fontes),
                flags=list(retorno_antecipado.flags),
                bloqueado_guardrail=not eh_cache_hit,
                retorno_antecipado=retorno_antecipado,
                cache_hit=eh_cache_hit,
                latencia_ms=latencia_ms,
            )
            persistir_e_auditar(turno_antecipado)
            return turno_antecipado

        # -----------------------------------------------------------------------
        # Etapa 3: Execução do Agente PydanticAI + UsageLimits (2c) + Fallback (#37)
        # -----------------------------------------------------------------------
        agente_exec = agente if agente is not None else criar_agente()
        prazo_turno_s = timeout_turno_s if timeout_turno_s is not None else obter_timeout_turno()
        limites_exec = usage_limits if usage_limits is not None else obter_usage_limits_turno()
        # Conexão aberta pelo turno: em erro/timeout, `_executar_com_prazo` agenda o fechamento;
        # no caminho normal, segue aberta até a Etapa 4 (guardrail de saída) e fecha no finally.
        # Com `deps` do chamador, a conexão é dele.
        if conn_propria is None and deps is None:
            conn_propria = conectar_readonly()
        if not dataset_ver:
            dataset_ver = obter_versao_dataset(conn_propria if deps is None else deps.conn)

        deps_exec = (
            deps
            if deps is not None
            else AgentDeps(
                conn=conn_propria,
                data_referencia=data_ref_turno,
                usuario=usuario,
                dataset_version=dataset_ver,
                mascarador=Mascarador(),
            )
        )
        if deps is not None and not deps_exec.dataset_version:
            deps_exec.dataset_version = dataset_ver
        if isinstance(getattr(deps_exec, 'relatorios', None), list):
            deps_exec.relatorios.clear()  # só anexa o que for gerado neste turno (#15)
        if isinstance(getattr(deps_exec, 'rascunhos', None), list):
            deps_exec.rascunhos.clear()  # idem para os rascunhos das tools
        if isinstance(getattr(deps_exec, 'flags', None), list):
            deps_exec.flags.clear()  # e para as flags das tools
        mascarar_fn = getattr(deps_exec.mascarador, 'mascarar', None)
        mensagem_para_llm = (
            mascarar_fn(mensagem_limpa) if callable(mascarar_fn) else mensagem_limpa
        )
    except BaseException:
        if conn_propria is not None:
            _fechar_quando_livre(conn_propria)
        raise

    tentativas_erro: list[TentativaFallback] = []
    try:
        # O timeout de cada chamada vem dos modelos do agente (`criar_agente(timeout_s=...)`);
        # o turno só impõe o teto global.
        resultado = _executar_com_prazo(
            agente_exec,
            mensagem_para_llm,
            prazo_s=prazo_turno_s,
            conn_propria=conn_propria,
            deps=deps_exec,
            message_history=historico,
            usage_limits=limites_exec,
        )
    except SemPermissao:
        # Uma tool pode descobrir a violação apenas durante a consulta (por exemplo,
        # ao resolver o nome de outro produtor). Não exponha a mensagem técnica da
        # exceção, que pode conter IDs, nem classifique a recusa como falha do provedor.
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        retorno_acesso = RetornoAntecipado(
            motivo='ACESSO_NAO_AUTORIZADO',
            mensagem=MENSAGEM_ACESSO_NAO_AUTORIZADO,
        )
        turno_acesso = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=retorno_acesso.mensagem,
            bloqueado_guardrail=True,
            retorno_antecipado=retorno_acesso,
            latencia_ms=latencia_ms,
        )
        persistir_e_auditar(turno_acesso)
        return turno_acesso
    except UsageLimitExceeded as exc:
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        logger.warning(
            'Orçamento do turno esgotado desfecho=orcamento_esgotado '
            'sessao=%s latencia_ms=%s motivo=%s',
            sessao_id,
            latencia_ms,
            exc,
        )
        retorno_orcamento = criar_retorno_orcamento_turno()
        turno_orcamento = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=retorno_orcamento.mensagem,
            bloqueado_guardrail=True,
            retorno_antecipado=retorno_orcamento,
            latencia_ms=latencia_ms,
        )
        persistir_e_auditar(turno_orcamento)
        return turno_orcamento
    except (
        TimeoutError,
        httpx.TimeoutException,
        httpx.TransportError,
        FallbackExceptionGroup,
        ModelAPIError,
        UnexpectedModelBehavior,
    ) as exc:
        tentativas_erro = extrair_tentativas_de_excecao(exc)
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        turno_falha = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=MENSAGEM_FALHA_SEGURA,
            falha_segura=True,
            tentativas_fallback=tentativas_erro,
            latencia_ms=latencia_ms,
        )
        # Grava também a falha segura para o histórico exibido ficar coerente; a memória
        # (#31) não a repassa ao modelo nos turnos seguintes.
        persistir_e_auditar(turno_falha)
        return turno_falha

    # -----------------------------------------------------------------------
    # Etapa 4: Pós-processamento e restauração de PII para o operador (#33)
    # -----------------------------------------------------------------------
    try:
        resposta_final = _anexar_relatorios(
            resultado.output,
            deps_exec,
            gerar_relatorio_chamado=any(
                isinstance(part, ToolCallPart) and part.tool_name == 'gerar_relatorio'
                for msg in resultado.new_messages()
                for part in getattr(msg, 'parts', ())
            ),
        )
        tools_chamadas = {
            part.tool_name
            for msg in resultado.new_messages()
            for part in getattr(msg, 'parts', ())
            if isinstance(part, ToolCallPart)
        }
        resposta_final = _anexar_rascunhos(
            resposta_final,
            deps_exec,
            tool_de_rascunho_chamada=bool(tools_chamadas & TOOLS_DE_RASCUNHO),
        )
        resposta_final = _anexar_flags(resposta_final, deps_exec, tools_chamadas=tools_chamadas)
        resposta_final = _normalizar_fontes_e_texto_resposta(resposta_final)
        resposta_final = _restaurar_pii_resposta(resposta_final, deps_exec.mascarador)

        # Limpa eventual transação abortada antes das consultas read-only do guardrail de saída.
        if conn_propria is not None:
            info_conn = getattr(conn_propria, 'info', None)
            if getattr(info_conn, 'transaction_status', None) == 3:  # PQTRANS_INERROR
                conn_propria.rollback()

        # Guardrail de saída aqui!
        resposta_final = _reconciliar_fontes_com_tools(
            resposta_final, _fontes_estruturadas_das_tools(resultado.new_messages())
        )
        resposta_final = _completar_fontes_dos_rascunhos(resposta_final, resultado.new_messages())
        fontes_turno = _extrair_fontes_das_tools(resultado.new_messages())
        resposta_final, regras_saida = processar_guardrail_saida(
            resposta_final,
            conn=deps_exec.conn,
            data_referencia=deps_exec.data_referencia,
            fontes_retornadas_no_turno=fontes_turno,
        )

        if pos_processar is not None:
            resposta_final = pos_processar(resposta_final)
    finally:
        if conn_propria is not None:
            _fechar_quando_livre(conn_propria)

    # -----------------------------------------------------------------------
    # Etapa 5: Auditoria (#35) e persistência de memória da sessão (#31)
    # -----------------------------------------------------------------------
    modelo_efetivo, provedor_efetivo, tentativas = extrair_metadados_resposta(
        resultado.new_messages()
    )
    latencia_ms = int((time.perf_counter() - inicio) * 1000)
    uso = getattr(resultado, 'usage', None)
    if uso is not None:
        logger.info(
            'desfecho=sucesso sessao=%s modelo=%s chamadas=%s tokens_in=%s '
            'tokens_out=%s tokens_total=%s latencia_ms=%s',
            sessao_id,
            modelo_efetivo,
            getattr(uso, 'requests', None),
            getattr(uso, 'input_tokens', None),
            getattr(uso, 'output_tokens', None),
            getattr(uso, 'total_tokens', None),
            latencia_ms,
        )
    turno = RespostaTurno.de_resposta_agente(
        sessao_id=sessao_id,
        usuario=usuario,
        resposta=resposta_final,
        modelo_efetivo=modelo_efetivo,
        provedor_efetivo=provedor_efetivo,
        tentativas_fallback=tentativas,
        latencia_ms=latencia_ms,
    )
    ferramentas = list(dict.fromkeys(
        part.tool_name
        for msg in resultado.new_messages()
        for part in getattr(msg, 'parts', ())
        if isinstance(part, ToolCallPart) and part.tool_name != 'final_result'
    ))
    turno = turno.model_copy(
        update={
            'ferramentas': ferramentas,
            'tokens_entrada': getattr(uso, 'input_tokens', None),
            'tokens_saida': getattr(uso, 'output_tokens', None),
        }
    )
    bloqueado_guardrail_saida = bool(regras_saida)
    if regras_saida:
        turno = turno.model_copy(
            update={
                "bloqueado_guardrail": True,
                "regras_disparadas": list(regras_saida),
            }
        )

    historico_json = None
    if not bloqueado_guardrail_saida:
        historico_json = resultado.new_messages_json()
        if isinstance(historico_json, bytes):
            historico_json = historico_json.decode('utf-8')
        mascarar_fn = getattr(deps_exec.mascarador, 'mascarar', None)
        if callable(mascarar_fn):
            historico_json = _mascarar_historico_json(historico_json, mascarar_fn)
    persistir_e_auditar(turno, historico_json)

    # -----------------------------------------------------------------------
    # Etapa 5b: Atualizar o cache de respostas estáticas (#36)
    # -----------------------------------------------------------------------
    if usar_cache and resposta_cacheavel(turno, mensagem_limpa):
        chave = gerar_chave_cache(mensagem_limpa, perfil_turno, dataset_ver, data_ref_turno)
        gravar_no_cache(cache_repo, chave, turno)

    return turno
