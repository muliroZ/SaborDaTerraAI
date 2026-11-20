"""Mascaramento em memória de dados pessoais (PII) por turno e restauração para o operador (#33)."""

from collections import defaultdict
import re

_BANCOS_REGEX = re.compile(
    r'\b(?:Banco do Brasil|Banco do Nordeste|Caixa Econ[ôo]mica(?:\s+Federal)?|'
    r'Bradesco|Ita[úu]|Nubank|Sicoob)\b',
    re.IGNORECASE,
)

# Chave PIX explícita em texto livre (ex.: "Chave PIX: 890.123.456-78" ou "PIX: email@dominio.com")
_PIX_ROTULADO_REGEX = re.compile(
    r'(?i)(\b(?:chave\s+)?pix\s*[:\-]\s*)'
    r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
    r'|\d{3}\.\d{3}\.\d{3}-\d{2}'
    r'|\(\d{2}\)\s*9?\d{4}[-\s]?\d{4}'
    r'|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    r'|\b\d{11}\b)'
)

# RG rotulado ("RG: 5.432.109 SDS/PE", "RG 5432109") ou seguido de órgão/UF ("5.432.109 SDS/PE")
_RG_ROTULADO_REGEX = re.compile(
    r'(?i)(\brg\s*[:\-]?\s*)'
    r'(\d{1,2}\.?\d{3}\.?\d{3}(?:-[0-9Xx])?(?:\s+[A-Z]{2,6}/[A-Z]{2})?|\d{6,9}(?:\s+[A-Z]{2,6}/[A-Z]{2})?)'
)
_RG_COM_ORGAO_REGEX = re.compile(
    r'\b(\d{1,2}\.\d{3}\.\d{3}(?:-[0-9Xx])?\s+[A-Z]{2,6}/[A-Z]{2})\b'
)

# CPF pontuado (000.000.000-00), evitando casar parte de CNPJ (00.000.000/0001-00)
_CPF_PONTUADO_REGEX = re.compile(r'(?<![\d./-])\b\d{3}\.\d{3}\.\d{3}-\d{2}\b(?![/\d])')
# CPF sem pontuação com rótulo ("CPF 83145267490" ou "CPF: 83145267490")
_CPF_ROTULADO_SEM_PONTOS_REGEX = re.compile(r'(?i)(\bcpf\s*[:\-]?\s*)(\d{11})\b')

# E-mail
_EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')

# Telefone com ou sem pontuação:
# Ex.: "(87) 99812-3456", "(87) 3761-5000", "+55 87 99812-3456", "87 998123456"
_TEL_COM_DDD_REGEX = re.compile(
    r'(?:(?:\+55\s*)?\(\d{2}\)\s*9?\d{4}[-\s]?\d{4}\b'
    r'|(?:\+55\s+)?\b(?:1[1-9]|[2-9]\d)\s+9?\d{4}[-\s]?\d{4}\b)'
)
# Telefone rotulado sem pontuação ("Tel: 87998123456", "WhatsApp 998123456")
_TEL_ROTULADO_REGEX = re.compile(
    r'(?i)(\b(?:tel(?:efone)?|cel(?:ular)?|whatsapp|fone)\s*[:\-]?\s*)'
    r'((?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?9?\d{4}[-\s]?\d{4})\b'
)
# Telefone avulso pontuado sem DDD ("99812-3456") que não seja sufixo de código (LOT-2026-0089)
_TEL_SEM_DDD_PONTUADO_REGEX = re.compile(r'(?<![A-Za-z0-9_.-])\b9\d{4}-\d{4}\b(?![-\d])')

# Sequência pura de 11 dígitos avulsa (sem pontuação): TEL se iniciar com DDD+9, ou CPF.
_ONZE_DIGITOS_REGEX = re.compile(r'(?<![A-Za-z0-9_./-])\b\d{11}\b(?![A-Za-z0-9_/-]|\.\d)')

# Agência rotulada em texto livre ("Agência 0437", "Ag. 0023", "ag: 1582")
_AGENCIA_ROTULADA_REGEX = re.compile(r'(?i)(\bag(?:[êe]ncia|\.)?\s*[:\-]?\s*)(\d{4}(?:-\d)?)\b')
# Agência quando passada como valor de coluna isolado ("0437")
_AGENCIA_ISOLADA_REGEX = re.compile(r'^\d{4}$')

# Conta corrente rotulada ("C/C 55.678-9", "Conta: 28901-3", "conta corrente 00134578-2")
_CONTA_ROTULADA_REGEX = re.compile(
    r'(?i)(\b(?:c/c|conta(?:\s+corrente)?)\s*[:\-]?\s*)'
    r'(\d{1,3}(?:\.\d{3})+-[\dXx]|\d{4,10}-[\dXx]|\d{5,10})\b'
)
# Conta corrente pelo formato com dígito ("28901-3", "0073921-7", "00134578-2", "55.678-9")
# Não casa códigos como LOT-2026-0089 ou PE-SIE-0756 pois exige ausência de hífen/letra antes.
_CONTA_FORMATO_REGEX = re.compile(
    r'(?<![A-Za-z0-9_./-])\b(?:\d{1,3}\.\d{3}-[\dXx]|\d{5,9}-[\dXx])\b(?![A-Za-z0-9_/-]|\.\d)'
)

# Token já mascarado (ex.: [CPF_1], [PIX_2])
_TOKEN_MASCARADO_REGEX = re.compile(
    r'\[(?:CPF|RG|TEL|EMAIL|PIX|BANCO|AGENCIA|CONTA)_\d+\]'
)


class Mascarador:
    """Mascarador por turno: substitui PII por tokens (`[CPF_1]`, `[PIX_1]`, ...) só em memória."""

    def __init__(self) -> None:
        self._contadores: dict[str, int] = defaultdict(int)
        self._valor_para_token: dict[tuple[str, str], str] = {}
        self._token_para_valor: dict[str, str] = {}

    @property
    def mapa_tokens(self) -> dict[str, str]:
        """Retorna cópia somente leitura do mapa `token -> valor_original` em memória."""
        return dict(self._token_para_valor)

    def _registrar_token(self, categoria: str, valor_original: str) -> str:
        valor_limpo = valor_original.strip()
        if not valor_limpo:
            return valor_original
        if _TOKEN_MASCARADO_REGEX.fullmatch(valor_limpo):
            return valor_limpo

        # Reutiliza token se o mesmo valor já tiver sido mascarado em qualquer categoria compatível
        chave_exata = (categoria, valor_limpo)
        if chave_exata in self._valor_para_token:
            return self._valor_para_token[chave_exata]

        self._contadores[categoria] += 1
        token = f'[{categoria}_{self._contadores[categoria]}]'
        self._valor_para_token[chave_exata] = token
        self._token_para_valor[token] = valor_limpo
        return token

    def mascarar_campo(self, valor: str | None, categoria: str) -> str | None:
        """Mascara explicitamente um campo tipado de tabela (ex.: `PIX`, `CPF`, `AGENCIA`)."""
        if valor is None:
            return None
        texto = valor.strip()
        if not texto:
            return valor
        return self._registrar_token(categoria.upper(), texto)

    def mascarar(self, texto: str) -> str:
        """Substitui CPF, RG, telefone, e-mail, banco, agência, conta e PIX por tokens."""
        if not texto or not texto.strip():
            return texto

        texto_strip = texto.strip()
        # Caso especial: valor isolado de coluna de agência (ex.: "0437" em buscar_produtores)
        if _AGENCIA_ISOLADA_REGEX.fullmatch(texto_strip):
            return self._registrar_token('AGENCIA', texto_strip)

        resultado = texto

        # 1. PIX rotulado explicitamente ("Chave PIX: ...")
        resultado = _PIX_ROTULADO_REGEX.sub(
            lambda m: f"{m.group(1)}{self._registrar_token('PIX', m.group(2))}",
            resultado,
        )

        # 2. RG rotulado ("RG: 5.432.109 SDS/PE") ou com órgão expedidor
        resultado = _RG_ROTULADO_REGEX.sub(
            lambda m: f"{m.group(1)}{self._registrar_token('RG', m.group(2))}",
            resultado,
        )
        resultado = _RG_COM_ORGAO_REGEX.sub(
            lambda m: self._registrar_token('RG', m.group(1)),
            resultado,
        )

        # 3. CPF rotulado sem pontos ("CPF 83145267490") e CPF pontuado ("831.452.674-90")
        resultado = _CPF_ROTULADO_SEM_PONTOS_REGEX.sub(
            lambda m: f"{m.group(1)}{self._registrar_token('CPF', m.group(2))}",
            resultado,
        )
        resultado = _CPF_PONTUADO_REGEX.sub(
            lambda m: self._registrar_token('CPF', m.group(0)),
            resultado,
        )

        # 4. E-mail
        resultado = _EMAIL_REGEX.sub(
            lambda m: self._registrar_token('EMAIL', m.group(0)),
            resultado,
        )

        # 5. Telefone (rotulado, com DDD ou celular pontuado sem DDD)
        resultado = _TEL_ROTULADO_REGEX.sub(
            lambda m: f"{m.group(1)}{self._registrar_token('TEL', m.group(2))}",
            resultado,
        )
        resultado = _TEL_COM_DDD_REGEX.sub(
            lambda m: self._registrar_token('TEL', m.group(0)),
            resultado,
        )
        resultado = _TEL_SEM_DDD_PONTUADO_REGEX.sub(
            lambda m: self._registrar_token('TEL', m.group(0)),
            resultado,
        )

        # 6. Sequências de 11 dígitos sem pontuação remanescentes (celular DDD+9 vs CPF sem pontos)
        def _sub_onze_digitos(match: re.Match[str]) -> str:
            digitos = match.group(0)
            if digitos.startswith('879'):
                return self._registrar_token('TEL', digitos)
            return self._registrar_token('CPF', digitos)

        resultado = _ONZE_DIGITOS_REGEX.sub(_sub_onze_digitos, resultado)

        # 7. Agência e Conta Corrente (rotuladas e por formato) e Nome do Banco
        resultado = _AGENCIA_ROTULADA_REGEX.sub(
            lambda m: f"{m.group(1)}{self._registrar_token('AGENCIA', m.group(2))}",
            resultado,
        )
        resultado = _CONTA_ROTULADA_REGEX.sub(
            lambda m: f"{m.group(1)}{self._registrar_token('CONTA', m.group(2))}",
            resultado,
        )
        resultado = _CONTA_FORMATO_REGEX.sub(
            lambda m: self._registrar_token('CONTA', m.group(0)),
            resultado,
        )
        resultado = _BANCOS_REGEX.sub(
            lambda m: self._registrar_token('BANCO', m.group(0)),
            resultado,
        )

        return resultado

    def restaurar(self, texto: str) -> str:
        """Restaura os tokens presentes em `texto` para seus valores originais (Etapa 4c)."""
        if not texto or not self._token_para_valor:
            return texto

        def _substituir(match: re.Match[str]) -> str:
            token = match.group(0)
            return self._token_para_valor.get(token, token)

        return _TOKEN_MASCARADO_REGEX.sub(_substituir, texto)
