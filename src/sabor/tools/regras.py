"""Motor de regras determinísticas de classificação de pedidos (Issue #10)
e verificações reutilizáveis pelo Guardrail de Saída 4a (Issue #34).
"""

from __future__ import annotations

from datetime import date
import re
from typing import TYPE_CHECKING, Any

from sabor.tools.catalogo import Catalogo
from sabor.tools.datas import normalizar as _norm

if TYPE_CHECKING:
    from sabor.tools.pedidos import Item, Pedido


def _adicionar_sinalizacao(pedido: Pedido, flag: str) -> None:
    if flag not in pedido.sinalizacoes:
        pedido.sinalizacoes.append(flag)


def _conn_eh_real(conn: Any) -> bool:
    """Retorna True apenas se `conn` for uma conexão real do psycopg (ignora MagicMock)."""
    return conn is not None and type(conn).__module__.startswith('psycopg')


def _produto_interditado_em_inspecao(conn: Any, nome_produto: str, produtor_id: str | None = None) -> bool:
    """Verifica nos documentos `tipo_doc = 'inspecao'` se há não conformidade bloqueando o produto/produtor."""
    if not _conn_eh_real(conn) or not nome_produto:
        return False

    termo = _norm(nome_produto).split()[0]
    rows = conn.execute(
        """
        SELECT c.texto
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE d.tipo_doc = 'inspecao'
          AND c.texto ILIKE %s
        """,
        (f'%{termo}%',),
    ).fetchall()

    for (texto_chunk,) in rows:
        t_norm = _norm(texto_chunk)
        if any(p in t_norm for p in ('proib', 'interdit', 'suspens', 'bloquead', 'sem registro sie', 'nc-')):
            if produtor_id is None or produtor_id.lower() in t_norm:
                return True
    return False


def _consultar_capacidade_semanal_chunks(conn: Any, nome_produto: str) -> float | None:
    """Lê os chunks de fichas técnicas, certificados e observações de sazonalidade no banco para extrair a capacidade semanal."""
    if not _conn_eh_real(conn) or not nome_produto:
        return None

    prod_norm = _norm(nome_produto)
    palavras = [
        p for p in prod_norm.split()
        if len(p) >= 4 and p not in {'galinha', 'caipira', 'paluma', 'prata', 'tommy'}
    ]
    termo_busca = palavras[-1] if len(palavras) > 1 and palavras[0] == 'mel' else (palavras[0] if palavras else prod_norm)

    rows = conn.execute(
        """
        SELECT d.tipo_doc, COALESCE(c.secao, ''), c.texto
        FROM chunks c
        JOIN documentos d ON d.arquivo = c.arquivo
        WHERE d.tipo_doc IN ('ficha', 'certificado')
          AND (c.texto ILIKE %s OR COALESCE(c.secao, '') ILIKE %s)
        """,
        (f'%{termo_busca}%', f'%{termo_busca}%'),
    ).fetchall()

    # Também consulta a coluna `observacao` da tabela `sazonalidade`
    rows_saz = conn.execute(
        "SELECT 'sazonalidade', produto, COALESCE(observacao, '') FROM sazonalidade WHERE produto ILIKE %s",
        (f'%{termo_busca}%',),
    ).fetchall()

    padrao_sem = re.compile(
        r'(?:~\s*|ate\s+|de\s+\d+\s*(?:a|-|–)\s*)?(\d+(?:[.,]\d+)?)\s*'
        r'(?:kg|duzias?|dz|un(?:idades?)?|macos?|litros?|l)\s*(?:/\s*sem(?:ana)?|por\s+semana|semanais)',
    )
    padrao_mes = re.compile(
        r'(?:~\s*|ate\s+|de\s+\d+\s*(?:a|-|–)\s*)?(\d+(?:[.,]\d+)?)\s*'
        r'(?:kg|duzias?|dz|litros?|l)\s*(?:/\s*mes|por\s+mes|mensais)',
    )
    padrao_ano = re.compile(
        r'(?:~\s*|ate\s+|de\s+\d+\s*(?:a|-|–)\s*)?(\d+(?:[.,]\d+)?)\s*'
        r'(?:kg|duzias?|dz|litros?|l)\s*(?:/\s*ano|por\s+ano|anuais|/\s*safra)',
    )

    valores_ficha: list[float] = []
    valores_outros: list[float] = []

    for tipo_doc, secao, texto in [*rows, *rows_saz]:
        bloco = _norm(f'{secao}\n{texto}')
        # Se o chunk tiver várias linhas/produtos, prioriza linhas ou janelas próximas ao termo do produto
        for m_pos in re.finditer(re.escape(termo_busca), bloco):
            ini = max(0, m_pos.start() - 40)
            # Vai até a próxima quebra de parágrafo ou 220 caracteres
            trecho = bloco[ini : m_pos.end() + 220].split('\n\n')[0]

            achou_no_trecho = False
            for m in padrao_sem.finditer(trecho):
                val = float(m.group(1).replace(',', '.'))
                (valores_ficha if tipo_doc == 'ficha' else valores_outros).append(val)
                achou_no_trecho = True

            if not achou_no_trecho:
                for m in padrao_mes.finditer(trecho):
                    val = round(float(m.group(1).replace(',', '.')) / 4.33, 2)
                    (valores_ficha if tipo_doc == 'ficha' else valores_outros).append(val)
                    achou_no_trecho = True

            if not achou_no_trecho:
                for m in padrao_ano.finditer(trecho):
                    val = round(float(m.group(1).replace(',', '.')) / 52.0, 2)
                    (valores_ficha if tipo_doc == 'ficha' else valores_outros).append(val)

    if valores_ficha:
        return min(valores_ficha) if len(set(valores_ficha)) > 1 else valores_ficha[0]
    if valores_outros:
        return min(valores_outros) if len(set(valores_outros)) > 1 else valores_outros[0]
    return None


def _calcular_demanda_pendente_caixa_entrada(conn: Any, nome_produto: str) -> float:
    """Soma a demanda desse produto nas mensagens pendentes da caixa de entrada (`pedidos`)."""
    if not _conn_eh_real(conn) or not nome_produto:
        return 0.0

    prod_norm = _norm(nome_produto)
    tokens = [t for t in prod_norm.split() if len(t) >= 4 and t not in {'galinha', 'caipira', 'paluma', 'prata'}]
    if not tokens:
        tokens = [prod_norm.split()[0]]

    rows_msg = conn.execute(
        "SELECT mensagem_original FROM pedidos WHERE status = 'pendente'"
    ).fetchall()

    total = 0.0
    padrao_token = '|'.join(re.escape(t) for t in tokens)
    for (msg,) in rows_msg:
        msg_norm = _norm(msg)
        for m in re.finditer(
            rf'(?:({padrao_token})[^.\n,;0-9]{{0,25}}(\d+(?:[.,]\d+)?)\s*(?:kg|dz|duzias?|un|macos?))'
            rf'|(?:(\d+(?:[.,]\d+)?)\s*(?:kg|dz|duzias?|un|macos?)\s+(?:de\s+)?({padrao_token}))',
            msg_norm,
        ):
            qtd_str = m.group(2) or m.group(3)
            if qtd_str:
                total += float(qtd_str.replace(',', '.'))
    return total


def listar_produtores_elegiveis_item(
    conn: Any,
    item: Item,
    *,
    produto_id: int | None = None,
    exige_organico: bool = False,
    exige_sie_estrito: bool = True,
    exige_dap_caf: bool = False,
    eh_institucional_publico: bool = False,
    data_validade_ref: date = date(2026, 10, 30),
) -> tuple[list[str], list[str]]:
    """Consulta o banco e retorna `(produtores_elegiveis, motivos_bloqueio_conformidade)` para o item."""
    if not _conn_eh_real(conn):
        return [], []

    nome_prod = (item.produto or item.produto_texto or '').strip()
    if not nome_prod or item.ambiguo:
        return [], []

    if produto_id is not None:
        linhas = conn.execute(
            """
            SELECT p.id, p.status, p.certificacao_organica, p.validade_certificacao,
                   p.registro_sie, p.dap_caf, COALESCE(pr.origem_animal, false), pr.nome
            FROM produtor_produtos pp
            JOIN produtores p ON p.id = pp.produtor_id
            LEFT JOIN produtos pr ON pr.id = pp.produto_id
            WHERE pp.produto_id = %s
            ORDER BY p.id
            """,
            (produto_id,),
        ).fetchall()
    else:
        linhas = conn.execute(
            """
            SELECT p.id, p.status, p.certificacao_organica, p.validade_certificacao,
                   p.registro_sie, p.dap_caf, COALESCE(pr.origem_animal, false), pr.nome
            FROM produtor_produtos pp
            JOIN produtores p ON p.id = pp.produtor_id
            LEFT JOIN produtos pr ON pr.id = pp.produto_id
            WHERE pr.nome ILIKE %s OR pp.nome_original ILIKE %s
            ORDER BY p.id
            """,
            (f'%{nome_prod}%', f'%{nome_prod}%'),
        ).fetchall()

    elegiveis: list[str] = []
    motivos_conformidade: list[str] = []

    for pid, status, cert_org, val_cert, sie, dap_caf, orig_animal, pr_nome in linhas:
        if status != 'ativo':
            continue

        nome_canon = pr_nome or nome_prod
        if _produto_interditado_em_inspecao(conn, nome_canon, pid):
            motivos_conformidade.append(f'{pid} interditado em inspeção sanitária ({nome_canon})')
            continue

        if orig_animal and exige_sie_estrito:
            if not sie:
                motivos_conformidade.append(f'{pid} sem registro SIE ({nome_canon})')
                continue
            if eh_institucional_publico and 'pendente' in _norm(sie):
                motivos_conformidade.append(f'{pid} com SIE pendente para contrato público ({nome_canon})')
                continue

        if exige_dap_caf and not dap_caf:
            motivos_conformidade.append(f'{pid} sem DAP/CAF ({nome_canon})')

        precisa_org = bool(item.organico) or exige_organico
        if precisa_org:
            if not cert_org or val_cert is None or val_cert < data_validade_ref:
                motivos_conformidade.append(f'{pid} sem certificação orgânica vigente em {data_validade_ref}')
                continue

        if pid not in elegiveis:
            elegiveis.append(pid)

    return elegiveis, motivos_conformidade


def verificar_inadimplencia_comprador(
    conn: Any,
    comprador_nome: str | None,
    texto_original: str,
    data_ref: date,
) -> float:
    """Soma títulos pendentes e vencidos (`data_vencimento < data_ref`) do comprador na tabela `financeiro`."""
    if not _conn_eh_real(conn):
        return 0.0

    rows = conn.execute(
        """
        SELECT id_transacao, comprador, COALESCE(comprador_normalizado, comprador), valor_total
        FROM financeiro
        WHERE status_pagamento = 'pendente'
          AND data_vencimento < %s
        """,
        (data_ref,),
    ).fetchall()

    texto_norm = _norm(f'{comprador_nome or ""} {texto_original}')
    stopwords = {
        'rede', 'atacado', 'prefeitura', 'municipal', 'mercado', 'restaurante',
        'lanchonete', 'hotel', 'garanhuns', 'francisco', 'serra', 'negra',
    }

    total_vencido = 0.0
    for _id_tx, comp_raw, comp_norm, valor in rows:
        c_norm = _norm(f'{comp_raw} {comp_norm}')
        tokens_comp = [t for t in re.findall(r'[a-z0-9]{4,}', c_norm) if t not in stopwords]
        if any(t in texto_norm for t in tokens_comp):
            total_vencido += float(valor)

    return round(total_vencido, 2)


def classificar_urgencia(
    pedido: Pedido,
    texto_original: str,
    data_recebimento: date,
    data_estimada: date | None,
) -> str:
    """Calcula a urgência ('critica', 'alta', 'normal', 'baixa') puramente por prazo relativo e tipo."""
    texto_norm = _norm(texto_original)

    if pedido.tipo == 'notificacao_oficial':
        return 'critica'

    if data_estimada is not None:
        dias = (data_estimada - data_recebimento).days
        if dias <= 1:
            return 'critica'
        if pedido.tipo == 'consulta' and dias <= 3 and any(p in texto_norm for p in ('pronto', 'feira', 'esperando')):
            return 'critica'
        if dias <= 11:
            return 'alta'
        if dias > 30:
            return 'baixa'

    if 'data_inconsistente' in pedido.sinalizacoes:
        return 'alta'

    if pedido.tipo == 'cotacao' and any(t in texto_norm for t in ('month', 'export', 'mensal', 'internacional', 'international')):
        return 'baixa'

    return 'normal'


def classificar_pedido(
    pedido: Pedido,
    *,
    conn: Any,
    mensagem_original: str,
    data_referencia: date,
    data_global: date | None = None,
) -> Pedido:
    """Aplica as regras determinísticas da Issue #10 consultando dinamicamente o PostgreSQL.

    Espera o pedido já normalizado por `sabor.tools.pedidos.normalizar_pedido` (data de entrega
    resolvida e itens mapeados ao catálogo). Itens sem `produto_id` e não ambíguos, como os de
    triagens gravadas antes disso, ainda são buscados no catálogo.
    """
    data_corte = data_global or date(2026, 10, 30)
    texto_norm = _norm(mensagem_original)
    comprador_norm = _norm(pedido.comprador.nome)

    # Datas resolvidas antes, por `normalizar_pedido` (#9); `data` fica vazia quando conflita
    # com o dia da semana, mas a estimativa ainda vale para prazo e safra.
    data_estimada = pedido.entrega.data or pedido.entrega.data_estimada
    mes_entrega = data_estimada.month if data_estimada else data_referencia.month
    data_validade_ref = data_estimada or data_corte
    dias_ate_entrega = (data_estimada - data_referencia).days if data_estimada else None

    eh_institucional_publico = 'pnae' in texto_norm or 'prefeitura' in comprador_norm or 'empenho' in texto_norm
    exige_dap_caf = (
        eh_institucional_publico
        or pedido.tipo == 'compra_recorrente'
        or 'dap' in texto_norm
        or 'caf' in texto_norm
        or 'nota fiscal' in texto_norm
        or bool(pedido.comprador.contrato)
    )

    exige_org_geral = any(
        w in texto_norm for w in ('organico', 'organicos', 'organic')
    ) and any(
        w in texto_norm for w in ('rotulo', 'certificad', 'export', 'sempre que possivel', 'feira', 'certified')
    )

    # =========================================================================
    # 1. AMBIGUIDADE (itens já mapeados ao catálogo por `normalizar_pedido`, #9)
    # =========================================================================
    if 'data_inconsistente' in pedido.sinalizacoes or pedido.entrega.alerta:
        _adicionar_sinalizacao(pedido, 'ambiguidade')
    if any(item.ambiguo for item in pedido.itens):
        _adicionar_sinalizacao(pedido, 'ambiguidade')

    # Compatibilidade: itens sem `produto_id` (triagens gravadas antes da normalização ou
    # pedidos montados à mão) são buscados no catálogo, sem marcar ambiguidade aqui.
    sem_id = [i for i in pedido.itens if i.produto_id is None and not i.ambiguo]
    if sem_id and _conn_eh_real(conn):
        catalogo = Catalogo.carregar(conn)
        for item in sem_id:
            if (produto := catalogo.resolver(item.produto, item.produto_texto)) is not None:
                item.produto_id = produto.id

    # 1b. Referência temporal relativa aberta ("esse mês", "este mês") em consulta ou pedido
    if (
        (pedido.tipo == 'consulta_financeira' and pedido.entrega.data_texto is None)
        or any(expr in texto_norm for expr in ('esse mes', 'este mes'))
    ):
        _adicionar_sinalizacao(pedido, 'ambiguidade')

    # 1c. Divergência de cargo/qualificação do signatário em relação ao contrato cadastrado no banco
    if _conn_eh_real(conn):
        cpfs_mensagem = re.findall(r'\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b', mensagem_original)
        for cpf_msg in cpfs_mensagem:
            chunks_cpf = conn.execute(
                """
                SELECT STRING_AGG(c.texto, ' ')
                FROM chunks c
                JOIN documentos d ON d.arquivo = c.arquivo
                WHERE d.tipo_doc = 'contrato'
                  AND d.arquivo IN (
                      SELECT arquivo FROM chunks WHERE texto LIKE %s
                  )
                GROUP BY d.arquivo
                """,
                (f'%{cpf_msg}%',),
            ).fetchall()
            for (texto_contrato_completo,) in chunks_cpf:
                tc_norm = _norm(texto_contrato_completo)
                cargos_conhecidos = ('secretario', 'coordenador', 'diretor', 'presidente', 'gerente', 'prefeito')
                cargos_msg = {c for c in cargos_conhecidos if c in texto_norm}
                cargos_contrato = {c for c in cargos_conhecidos if c in tc_norm}
                if cargos_msg and cargos_contrato and cargos_msg != cargos_contrato:
                    _adicionar_sinalizacao(pedido, 'ambiguidade')
                    break

    # =========================================================================
    # 2. ITENS: ELEGIBILIDADE, SAZONALIDADE, CAPACIDADE E CONFORMIDADE
    # =========================================================================
    carga_por_produtor_elegivel: dict[str, float] = {}

    for item in pedido.itens:
        prod_id = item.produto_id
        nome_busca = (item.produto or item.produto_texto or '').strip()

        elegiveis, bloqueios_item = listar_produtores_elegiveis_item(
            conn,
            item,
            produto_id=prod_id,
            exige_organico=bool(item.organico) or exige_org_geral,
            exige_sie_estrito=True,
            exige_dap_caf=exige_dap_caf,
            eh_institucional_publico=eh_institucional_publico,
            data_validade_ref=data_validade_ref,
        )
        item.produtores_elegiveis = elegiveis
        if bloqueios_item:
            _adicionar_sinalizacao(pedido, 'conformidade')

        if _conn_eh_real(conn) and nome_busca and not item.ambiguo:
            if bool(item.organico) or exige_org_geral:
                row_preco = conn.execute(
                    'SELECT preco_organico FROM precos WHERE produto_id = %s OR produto ILIKE %s ORDER BY referencia DESC LIMIT 1',
                    (prod_id, f'%{nome_busca}%'),
                ).fetchone()
                if row_preco is not None and row_preco[0] is None:
                    _adicionar_sinalizacao(pedido, 'conformidade')

            row_safra = conn.execute(
                """
                SELECT sm.nivel
                FROM sazonalidade s
                JOIN sazonalidade_mensal sm ON sm.produto = s.produto
                WHERE (s.produto_id = %s OR s.produto ILIKE %s) AND sm.mes = %s
                LIMIT 1
                """,
                (prod_id, f'%{nome_busca}%', mes_entrega),
            ).fetchone()
            if row_safra and row_safra[0] in ('nenhuma', 'baixa'):
                _adicionar_sinalizacao(pedido, 'fora_de_safra')

            qtd = item.quantidade or 0.0
            cap_semanal = _consultar_capacidade_semanal_chunks(conn, nome_busca)
            if cap_semanal is not None and qtd > 0:
                demanda_caixa = _calcular_demanda_pendente_caixa_entrada(conn, nome_busca)
                if (
                    qtd > cap_semanal
                    or (dias_ate_entrega is not None and dias_ate_entrega <= 1 and qtd >= 0.8 * cap_semanal)
                    or (demanda_caixa > cap_semanal and qtd >= 0.25 * cap_semanal)
                ):
                    _adicionar_sinalizacao(pedido, 'acima_capacidade')

            for pid_eleg in elegiveis:
                carga_por_produtor_elegivel[pid_eleg] = carga_por_produtor_elegivel.get(pid_eleg, 0.0) + qtd

    if dias_ate_entrega is not None and dias_ate_entrega <= 1:
        if any(carga >= 200.0 for carga in carga_por_produtor_elegivel.values()):
            _adicionar_sinalizacao(pedido, 'acima_capacidade')

    if _conn_eh_real(conn) and dias_ate_entrega is not None and dias_ate_entrega > 30:
        if any(i.ambiguo for i in pedido.itens):
            baixas_mes = conn.execute(
                "SELECT COUNT(*) FROM sazonalidade_mensal WHERE mes = %s AND nivel IN ('nenhuma', 'baixa')",
                (mes_entrega,),
            ).fetchone()
            if baixas_mes and baixas_mes[0] > 0:
                _adicionar_sinalizacao(pedido, 'fora_de_safra')

        vencidos_futuro = conn.execute(
            """
            SELECT COUNT(*) FROM produtores
            WHERE status = 'ativo' AND certificacao_organica = true AND validade_certificacao < %s
            """,
            (data_validade_ref,),
        ).fetchone()
        if vencidos_futuro and vencidos_futuro[0] > 0:
            _adicionar_sinalizacao(pedido, 'conformidade')

    # =========================================================================
    # 3. CONFORMIDADE DE HISTÓRICO DO COMPRADOR, CATÁLOGO ORGÂNICO E AUDITORIA
    # =========================================================================
    if _conn_eh_real(conn):
        if pedido.comprador.nome:
            termo_comp = pedido.comprador.nome.split('-')[0].split('(')[0].strip()
            if len(termo_comp) >= 4:
                hist_sem_sie = conn.execute(
                    """
                    SELECT 1
                    FROM financeiro f
                    JOIN produtores p ON p.id = f.produtor_id
                    JOIN produtos pr ON pr.id = f.produto_id
                    WHERE (f.comprador ILIKE %s OR f.comprador_normalizado ILIKE %s)
                      AND pr.origem_animal = true
                      AND p.registro_sie IS NULL
                    LIMIT 1
                    """,
                    (f'%{termo_comp}%', f'%{termo_comp}%'),
                ).fetchone()
                if hist_sem_sie is not None:
                    _adicionar_sinalizacao(pedido, 'conformidade')

        if pedido.tipo in ('parceria', 'notificacao_oficial') or exige_org_geral or any(
            t in texto_norm for t in ('laudo', 'fitossanitario', 'phytosanitary', 'inspecao')
        ):
            _adicionar_sinalizacao(pedido, 'conformidade')

    # =========================================================================
    # 4. CRÉDITO / INADIMPLÊNCIA
    # =========================================================================
    if verificar_inadimplencia_comprador(conn, pedido.comprador.nome, mensagem_original, data_corte) > 0:
        _adicionar_sinalizacao(pedido, 'inadimplencia')
        _adicionar_sinalizacao(pedido, 'credito')

    # =========================================================================
    # 5. PRIVACIDADE E LGPD
    # =========================================================================
    tem_doc_pessoal = bool(re.search(r'\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b', mensagem_original))
    if (
        tem_doc_pessoal
        or pedido.tipo == 'consulta_financeira'
        or any(
            termo in texto_norm
            for termo in ('cpf', 'dados bancarios', 'bank', 'iban', 'swift', 'manda o pix', 'chave pix')
        )
    ):
        _adicionar_sinalizacao(pedido, 'privacidade')

    # =========================================================================
    # 6. URGÊNCIA DETERMINÍSTICA ('critica', 'alta', 'normal', 'baixa')
    # =========================================================================
    pedido.urgencia = classificar_urgencia(
        pedido, mensagem_original, data_referencia, data_estimada
    )  # type: ignore[assignment]

    return pedido