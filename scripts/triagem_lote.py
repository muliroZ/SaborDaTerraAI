"""Triagem em lote da caixa de entrada (#14): extrai e classifica todos os pedidos fora do chat.

Uso:
    uv run python scripts/triagem_lote.py                      # tria o que falta e mostra a fila
    uv run python scripts/triagem_lote.py --csv fila.csv --json fila.json
    uv run python scripts/triagem_lote.py --limite 10          # no máximo 10 chamadas ao modelo
    uv run python scripts/triagem_lote.py --tokens 50000       # teto de tokens deste lote
    uv run python scripts/triagem_lote.py --reprocessar        # ignora os resultados gravados
    uv run python scripts/triagem_lote.py --somente-exportar --csv fila.csv   # sem LLM

Pedidos já extraídos (mesma mensagem, recebimento, modelo e versão) não voltam ao LLM: só as
regras rodam de novo sobre o banco atual, sem tokens.
O resultado fica em `triagem_pedidos`; rode `scripts/init_db.py` antes para criar a tabela.
"""

import argparse
import asyncio
from dataclasses import replace
import logging
import os
from pathlib import Path
import sys

import psycopg

from sabor.agent.deps import AgentDeps, obter_data_referencia
from sabor.agent.mascarador import Mascarador
from sabor.db import conectar, database_url
from sabor.triagem import Orcamento, listar_fila, para_csv, para_json, triar_lote

ROTULOS = {'critica': 'CRÍTICA', 'alta': 'alta', 'normal': 'normal', 'baixa': 'baixa'}


def imprimir_fila(fila: list[dict]) -> None:
    print(f'\n{"#":>3}  {"pedido":<14} {"urgência":<9} {"tipo":<20} {"entrega":<10}  sinalizações')
    for linha in fila:
        urgencia = ROTULOS.get(linha['urgencia'], linha['situacao'])
        entrega = linha['data_entrega'].strftime('%d/%m/%Y') if linha['data_entrega'] else '-'
        print(
            f'{linha["posicao"]:>3}  {linha["pedido_id"]:<14} {urgencia:<9} '
            f'{linha["tipo"] or "-":<20} {entrega:<10}  {", ".join(linha["sinalizacoes"]) or "-"}'
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--reprocessar', action='store_true', help='ignora resultados gravados')
    parser.add_argument('--limite', type=int, help='máximo de pedidos enviados ao modelo')
    parser.add_argument('--tokens', type=int, help='teto de tokens do lote (TRIAGEM_TOKENS_LIMIT)')
    parser.add_argument('--csv', type=Path, help='exporta a fila ordenada em CSV')
    parser.add_argument('--json', type=Path, help='exporta a fila ordenada em JSON')
    parser.add_argument(
        '--somente-exportar',
        action='store_true',
        help='não chama o modelo; só lê o que foi gravado',
    )
    args = parser.parse_args()
    os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')

    try:
        conn = conectar(tentativas=1, autocommit=True)
    except psycopg.OperationalError as e:
        host = database_url().rsplit('@', 1)[-1]
        print(f'Não foi possível conectar em {host}: {e}', file=sys.stderr)
        print('O banco está no ar? Rode `docker compose up -d`.', file=sys.stderr)
        return 1

    with conn:
        if not args.somente_exportar:
            orcamento = Orcamento.configurado()
            if args.tokens is not None:
                orcamento = replace(orcamento, tokens_lote=args.tokens)
            deps = AgentDeps(
                conn=conn,
                data_referencia=obter_data_referencia(),
                usuario='triagem-lote',
                mascarador=Mascarador(),
            )
            resultado = asyncio.run(
                triar_lote(
                    deps,
                    orcamento=orcamento,
                    reprocessar=args.reprocessar,
                    limite=args.limite,
                    ao_processar=lambda pid, situacao: print(f'  {pid}: {situacao}', flush=True),
                )
            )
            uso = resultado.uso
            print(
                f'\n{len(resultado.processados)} triado(s), {len(resultado.reaproveitados)} '
                f'reclassificado(s) sem LLM, {len(resultado.erros)} com erro, '
                f'{len(resultado.nao_processados)} pendente(s). '
                f'Consumo: {uso.requests} chamada(s), {uso.input_tokens} tokens de entrada, '
                f'{uso.output_tokens} de saída (teto do lote: {orcamento.tokens_lote}).'
            )
            if resultado.orcamento_esgotado:
                print(
                    'Orçamento de tokens do lote esgotado: rode de novo para continuar '
                    'de onde parou.',
                    file=sys.stderr,
                )
            for pid, erro in resultado.erros.items():
                print(f'  erro em {pid}: {erro}', file=sys.stderr)

        fila = listar_fila(conn)

    imprimir_fila(fila)
    if args.csv:
        args.csv.write_text(para_csv(fila), encoding='utf-8-sig')
        print(f'\nCSV: {args.csv}')
    if args.json:
        args.json.write_text(para_json(fila), encoding='utf-8')
        print(f'JSON: {args.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
