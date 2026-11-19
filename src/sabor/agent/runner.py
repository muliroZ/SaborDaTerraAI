"""Construção do agente único PydanticAI e orquestração de `processar_turno` (Etapas 1 a 5)."""

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
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
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from sabor import tools as modulo_tools
from sabor.agent.deps import AgentDeps, obter_data_referencia, obter_versao_dataset
from sabor.agent.fallback import (
    construir_capability_fallback,
    extrair_metadados_resposta,
    extrair_tentativas_de_excecao,
    ler_segundos_env,
)
from sabor.agent.guardrail_entry import (
    criar_retorno_orcamento_turno,
    executar_guardrail_entrada,
    obter_usage_limits_turno,
)
from sabor.agent.guardrail_exit import processar_guardrail_saida
from sabor.agent.mascarador import Mascarador
from sabor.agent.prompts import SYSTEM_PROMPT_BASE, instrucoes_contexto_turno
from sabor.agent.schemas import (
    Relatorio,
    RespostaAgente,
    RespostaTurno,
    RetornoAntecipado,
    TentativaFallback,
)
from sabor.db import conectar_readonly

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
) -> Agent[AgentDeps, RespostaAgente]:
    """Cria e configura o agente único PydanticAI com prompt, fallback e tools disponíveis.

    `timeout_s` é o limite de cada chamada de modelo da cadeia de fallback; `None` usa
    `MODELO_TIMEOUT_S` do `.env` ou o padrão de `fallback.TIMEOUT_POR_MODELO_S`.
    """
    capability_fallback = construir_capability_fallback(
        modelos,
        gemini_api_key=gemini_api_key,
        openrouter_api_key=openrouter_api_key,
        timeout_s=timeout_s,
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
        retries=0,
        capabilities=[capability_fallback],
    )
    agente.instructions(instrucoes_contexto_turno)
    return agente


def _anexar_relatorios(resposta: RespostaAgente, deps: AgentDeps) -> RespostaAgente:
    """Etapa 4: anexa à resposta só os relatórios que as tools geraram no turno (#15).

    `relatorios` fica fora do schema do LLM, mas o Pydantic ainda validaria um valor que o
    modelo mandasse no `final_result`. Por isso o campo é sempre sobrescrito com o que está em
    `deps.relatorios`: nenhum anexo vem do modelo.

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

    mascarar_fn = getattr(deps.mascarador, 'mascarar', None)
    por_arquivo: dict[str, Relatorio] = {}
    for relatorio in gerados:
        if callable(mascarar_fn):
            relatorio = relatorio.model_copy(update={'markdown': mascarar_fn(relatorio.markdown)})
        por_arquivo[relatorio.nome_arquivo] = relatorio
    return resposta.model_copy(update={'relatorios': list(por_arquivo.values())})


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
    futuro: Future[Any] = executor.submit(lambda: asyncio.run(_rodar()))
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
    registrar_auditoria: Callable[[RespostaTurno], None] | None = None,  # Etapa 5a (#35)
    salvar_memoria: Callable[..., None] | None = None,  # Etapa 5b (#31)
) -> RespostaTurno:
    """Executa o turno completo do diagrama Agent System (etapas 1 a 5) com retornos antecipados."""
    inicio = time.perf_counter()

    # -----------------------------------------------------------------------
    # Etapa 1: Carregar memória da sessão (Issue #31)
    # -----------------------------------------------------------------------
    historico: list[ModelMessage] = (
        list(carregar_memoria(sessao_id)) if carregar_memoria is not None else []
    )
    conn_propria = None
    dataset_ver = deps.dataset_version if deps is not None else None
    # A Etapa 2 usa a versão persistida pelo ETL/ingestão, que também será injetada no prompt.
    # A leitura consulta uma linha de metadados, sem recalcular o hash das tabelas por turno.
    if not dataset_ver:
        conn_fingerprint = deps.conn if deps is not None else conectar_readonly()
        if deps is None:
            conn_propria = conn_fingerprint
        dataset_ver = obter_versao_dataset(conn_fingerprint)

    # -----------------------------------------------------------------------
    # Etapa 2: Guardrail de entrada (retorno antecipado sem chamada LLM)
    # -----------------------------------------------------------------------
    mensagem_limpa = mensagem.strip()
    retorno_antecipado: RetornoAntecipado | None = None

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
        dataset_ver = dataset_ver or 'sha256-unavailable'
        contagem_sessao = (
            requisicoes_na_sessao
            if requisicoes_na_sessao is not None
            else _contar_turnos_historico(historico)
        )
        retorno_antecipado = executar_guardrail_entrada(
            mensagem=mensagem_limpa,
            dataset_version=dataset_ver,
            requisicoes_na_sessao=contagem_sessao,
            cache_repo=cache_repo,
        )

    if retorno_antecipado is not None:
        latencia_ms = int((time.perf_counter() - inicio) * 1000)
        eh_cache_hit = retorno_antecipado.motivo == 'CACHE_HIT'
        turno_antecipado = RespostaTurno(
            sessao_id=sessao_id,
            usuario=usuario,
            texto=retorno_antecipado.mensagem,
            fontes=list(retorno_antecipado.fontes),
            bloqueado_guardrail=not eh_cache_hit,
            retorno_antecipado=retorno_antecipado,
            latencia_ms=latencia_ms,
        )
        if registrar_auditoria is not None:
            registrar_auditoria(turno_antecipado)
        if salvar_memoria is not None:
            salvar_memoria(sessao_id, mensagem_limpa, turno_antecipado)
        if conn_propria is not None:
            _fechar_quando_livre(conn_propria)
        return turno_antecipado

    # -----------------------------------------------------------------------
    # Etapa 3: Execução do Agente PydanticAI + UsageLimits (2c) + Fallback (#37)
    # -----------------------------------------------------------------------
    agente_exec = agente if agente is not None else criar_agente()
    prazo_turno_s = timeout_turno_s if timeout_turno_s is not None else obter_timeout_turno()
    limites_exec = usage_limits if usage_limits is not None else obter_usage_limits_turno()
    # Conexão aberta pelo turno: em erro/timeout, `_executar_com_prazo` agenda o fechamento;
    # no caminho normal, fica aberta até o fim da Etapa 4 (guardrail de saída) e fecha no finally.
    # Com `deps` do chamador, a conexão é dele.
    conn_propria = conn_propria or (conectar_readonly() if deps is None else None)

    try:
        deps_exec = (
            deps
            if deps is not None
            else AgentDeps(
                conn=conn_propria,
                data_referencia=obter_data_referencia(),
                usuario=usuario,
                dataset_version=obter_versao_dataset(conn_propria),
                mascarador=Mascarador(),
            )
        )
        if deps is not None and not deps_exec.dataset_version:
            deps_exec.dataset_version = obter_versao_dataset(deps_exec.conn)
        if isinstance(getattr(deps_exec, 'relatorios', None), list):
            deps_exec.relatorios.clear()  # só anexa o que for gerado neste turno (#15)
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
        if registrar_auditoria is not None:
            registrar_auditoria(turno_orcamento)
        if salvar_memoria is not None:
            salvar_memoria(sessao_id, mensagem_limpa, turno_orcamento)
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
        if registrar_auditoria is not None:
            registrar_auditoria(turno_falha)
        # Grava também a falha segura para o histórico exibido ficar coerente; a memória
        # (#31) não a repassa ao modelo nos turnos seguintes.
        if salvar_memoria is not None:
            salvar_memoria(sessao_id, mensagem_limpa, turno_falha)
        return turno_falha

    # -----------------------------------------------------------------------
    # Etapa 4: Pós-processamento e restauração de PII para o operador (#33)
    # -----------------------------------------------------------------------
    try:
        resposta_final = _anexar_relatorios(resultado.output, deps_exec)
        resposta_final = _restaurar_pii_resposta(resposta_final, deps_exec.mascarador)

        # Limpa eventual transação abortada antes das consultas read-only do guardrail de saída.
        if conn_propria is not None:
            info_conn = getattr(conn_propria, 'info', None)
            if getattr(info_conn, 'transaction_status', None) == 3:  # PQTRANS_INERROR
                conn_propria.rollback()

        # Guardrail de saída aqui!
        fontes_turno = [f'{f.arquivo} · {f.referencia}' for f in resposta_final.fontes]
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
    bloqueado_guardrail_saida = bool(regras_saida)
    if bloqueado_guardrail_saida:
        turno = turno.model_copy(update={'bloqueado_guardrail': True})

    if registrar_auditoria is not None:
        registrar_auditoria(turno)
    if salvar_memoria is not None:
        historico_json = None
        if not bloqueado_guardrail_saida:
            historico_json = resultado.new_messages_json()
            if isinstance(historico_json, bytes):
                historico_json = historico_json.decode('utf-8')
            mascarar_fn = getattr(deps_exec.mascarador, 'mascarar', None)
            if callable(mascarar_fn):
                historico_json = _mascarar_historico_json(historico_json, mascarar_fn)
        try:
            suporta_historico = (
                'historico_modelo_json' in inspect.signature(salvar_memoria).parameters
            )
        except (TypeError, ValueError):
            suporta_historico = False
        if suporta_historico:
            salvar_memoria(
                sessao_id, mensagem_limpa, turno,
                historico_modelo_json=historico_json,
            )
        else:
            salvar_memoria(sessao_id, mensagem_limpa, turno)

    return turno
