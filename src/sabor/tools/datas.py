"""Resolução determinística das datas de entrega citadas nos pedidos (#9).

O LLM só copia a expressão da mensagem (`Entrega.data_texto`, ex.: "essa sexta (01/nov)");
a data em si é calculada aqui, sem modelo, a partir da data de recebimento do pedido. Assim
"amanhã", "sábado" ou "04 a 08 de novembro" saem sempre iguais e testáveis, e o conflito entre
a data e o dia da semana (PED-001: "sexta 01/nov" cai num domingo) é detectado em vez de
corrigido em silêncio.

Ordem de leitura de uma expressão:

1. data explícita, com ou sem intervalo ("29/10", "01/nov", "04 a 08 de novembro de 2026");
   se a expressão também cita um dia da semana diferente, a data fica para confirmação;
2. mês sem dia ("jan/2027", "janeiro de 2027"): data aproximada, no primeiro dia do mês;
3. referência relativa ("hoje", "amanhã", "depois de amanhã");
4. só o dia da semana ("pra sábado"): a próxima ocorrência depois do recebimento.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
import unicodedata

NOMES_DIAS_SEMANA = (
    'segunda-feira',
    'terça-feira',
    'quarta-feira',
    'quinta-feira',
    'sexta-feira',
    'sábado',
    'domingo',
)

_DIAS_SEMANA = {
    nome: numero
    for nomes in (
        'segunda terca quarta quinta sexta sabado domingo',
        'monday tuesday wednesday thursday friday saturday sunday',
    )
    for numero, nome in enumerate(nomes.split())
}

_MESES = {
    nome: numero
    for numero, nome in enumerate('jan fev mar abr mai jun jul ago set out nov dez'.split(), 1)
}

_DESLOCAMENTOS_RELATIVOS = (
    # A mais longa primeiro: "depois de amanhã" contém "amanhã".
    (re.compile(r'\bdepois de amanha\b'), 2),
    (re.compile(r'\b(?:amanha|tomorrow)\b'), 1),
    (re.compile(r'\b(?:hoje|today)\b'), 0),
)

_MES = (
    r'(?P<{nome}>jan(?:eiro)?|fev(?:ereiro)?|mar(?:co)?|abr(?:il)?|mai(?:o)?|jun(?:ho)?'
    r'|jul(?:ho)?|ago(?:sto)?|set(?:embro)?|out(?:ubro)?|nov(?:embro)?|dez(?:embro)?)\b'
)
_SEPARADOR = r'(?:\s*[/.-]\s*|\s+de\s+|\s+)'
_DIA_MES = (
    r'(?P<dia>\d{1,2})'
    rf'(?:[/.-](?P<mes_numero>\d{{1,2}})|{_SEPARADOR}{_MES.format(nome="mes_nome")})'
    # Ano curto só depois de barra ou hífen: em "04/11 10h", o "10" é a hora.
    rf'(?:[/.-](?P<ano>\d{{4}}|\d{{2}})(?!\d)|{_SEPARADOR}(?P<ano_longo>\d{{4}})(?!\d))?'
)
_DATA = re.compile(rf'\b{_DIA_MES}')
_INTERVALO = re.compile(rf'\b(?P<dia_inicio>\d{{1,2}})\s*(?:a|ate|e|-)\s*{_DIA_MES}')
_MES_ANO = re.compile(rf'\b{_MES.format(nome="mes_nome")}(?:{_SEPARADOR}(?P<ano>\d{{4}}))?')
_DIA_SEMANA = re.compile(r'\b(' + '|'.join(_DIAS_SEMANA) + r')(?:[- ]feira)?\b')

_TOLERANCIA_PASSADO = timedelta(days=60)
"""Uma data sem ano mais de 60 dias antes do recebimento é lida como do ano seguinte
("05/01" recebido em outubro é janeiro do ano que vem)."""


@dataclass(frozen=True)
class DataResolvida:
    """Resultado da leitura de uma expressão de data.

    `data` é a data firme de entrega (início, num intervalo) e fica vazia quando precisa de
    confirmação. `estimada` é a melhor estimativa mesmo nesse caso (a data escrita, quando
    conflita com o dia da semana; o dia 1º, quando só o mês foi dito) e é a que as regras de
    urgência e safra (#10) usam.
    """

    data: date | None = None
    data_fim: date | None = None
    estimada: date | None = None
    alerta: str | None = None

    @property
    def inconsistente(self) -> bool:
        return self.alerta is not None


def normalizar(texto: str | None) -> str:
    """Minúsculas e sem acentos: "Sábado" e "sabado" viram a mesma chave."""
    if not texto:
        return ''
    decomposto = unicodedata.normalize('NFKD', texto.lower())
    return ''.join(c for c in decomposto if not unicodedata.combining(c))


def resolver_data(texto: str | None, recebido_em: date) -> DataResolvida:
    """Converte a expressão de data de um pedido numa data, relativa a `recebido_em`."""
    norm = normalizar(texto)
    if not norm.strip():
        return DataResolvida()

    dia_semana = _dia_semana(norm)

    if intervalo := _data_explicita(norm, recebido_em):
        inicio, fim = intervalo
        if dia_semana is not None and inicio.weekday() != dia_semana:
            return DataResolvida(estimada=inicio, alerta=_alerta_dia_semana(inicio, dia_semana))
        return DataResolvida(data=inicio, data_fim=fim, estimada=inicio)

    if (mes := _mes_sem_dia(norm, recebido_em)) is not None:
        return DataResolvida(estimada=mes)

    for padrao, dias in _DESLOCAMENTOS_RELATIVOS:
        if padrao.search(norm):
            data = recebido_em + timedelta(days=dias)
            return DataResolvida(data=data, estimada=data)

    if dia_semana is not None:
        data = recebido_em + timedelta(days=(dia_semana - recebido_em.weekday() - 1) % 7 + 1)
        return DataResolvida(data=data, estimada=data)

    return DataResolvida()


def _dia_semana(norm: str) -> int | None:
    """Primeiro dia da semana citado na expressão (segunda=0 … domingo=6)."""
    encontrado = _DIA_SEMANA.search(norm)
    return _DIAS_SEMANA[encontrado.group(1)] if encontrado else None


def _data_explicita(norm: str, recebido_em: date) -> tuple[date, date | None] | None:
    """Primeira data com dia e mês da expressão; num intervalo, devolve início e fim."""
    if encontrado := _INTERVALO.search(norm):
        fim = _montar_data(encontrado, recebido_em)
        if fim is not None and 1 <= int(encontrado['dia_inicio']) < fim.day:
            return fim.replace(day=int(encontrado['dia_inicio'])), fim

    for encontrado in _DATA.finditer(norm):
        if (data := _montar_data(encontrado, recebido_em)) is not None:
            return data, None
    return None


def _montar_data(encontrado: re.Match[str], recebido_em: date) -> date | None:
    mes = (
        int(encontrado['mes_numero'])
        if encontrado['mes_numero']
        else _MESES[encontrado['mes_nome'][:3]]
    )
    ano = encontrado['ano'] or encontrado['ano_longo']
    try:
        if ano:
            return date(int(ano) + (2000 if len(ano) == 2 else 0), mes, int(encontrado['dia']))
        return _com_ano_inferido(int(encontrado['dia']), mes, recebido_em)
    except ValueError:  # 31/11, 30/02, mês 13…
        return None


def _com_ano_inferido(dia: int, mes: int, recebido_em: date) -> date:
    data = date(recebido_em.year, mes, dia)
    if data < recebido_em - _TOLERANCIA_PASSADO:
        return data.replace(year=recebido_em.year + 1)
    return data


def _mes_sem_dia(norm: str, recebido_em: date) -> date | None:
    """Primeiro dia do mês citado sem dia ("jan/2027", "janeiro de 2027").

    Sem ano, só vale o nome completo do mês: "mar", "set" e "out" soltos são palavras comuns.
    """
    for encontrado in _MES_ANO.finditer(norm):
        nome, ano = encontrado['mes_nome'], encontrado['ano']
        mes = _MESES[nome[:3]]
        if ano:
            return date(int(ano), mes, 1)
        if len(nome) > 3:
            return _com_ano_inferido(1, mes, recebido_em)
    return None


def _alerta_dia_semana(data: date, dia_informado: int) -> str:
    """Texto do conflito, com a data mais próxima que cai no dia da semana citado."""
    atraso = (data.weekday() - dia_informado) % 7
    sugestao = data - timedelta(days=atraso) if atraso <= 3 else data + timedelta(days=7 - atraso)
    return (
        f'A mensagem cita {NOMES_DIAS_SEMANA[dia_informado]}, mas {data:%d/%m/%Y} cai em '
        f'{NOMES_DIAS_SEMANA[data.weekday()]} ({NOMES_DIAS_SEMANA[dia_informado]} mais '
        f'próxima: {sugestao:%d/%m/%Y}). Confirme a data com o comprador.'
    )
