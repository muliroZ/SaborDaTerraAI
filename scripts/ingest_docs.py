"""Ingestão, enriquecimento cadastral e vetorização de documentos TXT (Issue #5)."""

import re
import hashlib
import json
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from sabor.db import conectar, EMBEDDING_DIM
import time
from google.genai import errors

load_dotenv()
client = genai.Client()


def extrair_metadados(caminho_arquivo: Path, texto: str):
    """Extrai produtor_id, tipo_doc, titulo e validade do documento."""
    match_id = re.search(r'_(P\d{3})\.txt$', caminho_arquivo.name)
    produtor_id = match_id.group(1) if match_id else None

    pasta = caminho_arquivo.parent.name.lower()
    nome = caminho_arquivo.name.lower()

    # Ordem importa: laudos e inspeção ficam dentro de certificacoes/
    if 'laudo' in nome:
        tipo_doc = 'laudo'
    elif 'inspecao' in nome:
        tipo_doc = 'inspecao'
    elif 'certificacoes' in pasta or 'cert' in nome:
        tipo_doc = 'certificado'
    elif 'contratos' in pasta or 'contrato' in nome:
        tipo_doc = 'contrato'
    else:
        tipo_doc = 'ficha'

    # Título: primeira linha útil do documento (fallback para nome do arquivo)
    linhas_nao_vazias = [l.strip() for l in texto.splitlines() if l.strip() and not set(l.strip()) <= {'-', '='}]
    titulo = linhas_nao_vazias[0][:120] if linhas_nao_vazias else caminho_arquivo.stem

    # Buscar data de validade (ex: Validade: 30/09/2026 ou Válido até: 15/06/2027)
    validade_db = None
    match_data = re.search(r'v[áa]lid(?:ade|[oa]\s+at[ée]).*?(\d{2}/\d{2}/\d{4})', texto, re.IGNORECASE)
    if match_data:
        dia, mes, ano = match_data.group(1).split('/')
        validade_db = f"{ano}-{mes}-{dia}"

    return produtor_id, tipo_doc, titulo, validade_db


def enriquecer_produtores(conn, produtor_id: str | None, texto: str):
    """Extrai dap_caf e registro_sie dos TXT e atualiza a tabela produtores."""
    # 1. Documentos individuais (_P00X.txt)
    if produtor_id:
        match_dap = re.search(r'(SDR/PE-\d{4}-\d{7})', texto)
        match_sie = re.search(r'(PE-SIE-\d{4})', texto)
        dap = match_dap.group(1) if match_dap else None
        sie = match_sie.group(1) if match_sie else None

        if not sie and re.search(r'SIE\s*[:\-]?\s*pendente', texto, re.IGNORECASE):
            sie = 'pendente'

        if dap or sie:
            conn.execute(
                """
                UPDATE produtores
                SET dap_caf = COALESCE(%s, dap_caf),
                    registro_sie = COALESCE(%s, registro_sie)
                WHERE id = %s
                """,
                (dap, sie, produtor_id),
            )
        return

    # 2. Documentos coletivos (inspecao_adagro_2026.txt e fichas_tecnicas/*.txt)
    for linha in texto.splitlines():
        pids = re.findall(r'\b(P\d{3})\b', linha)
        if not pids:
            continue

        match_dap = re.search(r'(SDR/PE-\d{4}-\d{7})', linha)
        match_sie = re.search(r'(PE-SIE-\d{4})', linha)
        dap = match_dap.group(1) if match_dap else None
        sie = match_sie.group(1) if match_sie else None

        if not dap and re.search(r'DAP(?:/CAF)?\s+vigente', linha, re.IGNORECASE):
            dap = 'vigente'

        if dap or sie:
            for pid in set(pids):
                conn.execute(
                    """
                    UPDATE produtores
                    SET dap_caf = COALESCE(%s, dap_caf),
                        registro_sie = COALESCE(%s, registro_sie)
                    WHERE id = %s
                    """,
                    (dap, sie, pid),
                )


def quebrar_em_secoes(texto: str) -> list[dict]:
    """Chunking por seção do documento (títulos em caixa alta, ignorando Rótulo: VALOR)."""
    linhas = texto.splitlines()
    chunks = []
    secao_atual = "CABEÇALHO"
    texto_acumulado = []

    for linha in linhas:
        linha_limpa = linha.strip()
        tem_letras = any(c.isalpha() for c in linha_limpa)
        eh_rotulo_valor = (':' in linha_limpa) and (not linha_limpa.endswith(':'))

        if (
            linha_limpa
            and tem_letras
            and linha_limpa.isupper()
            and not eh_rotulo_valor
            and len(linha_limpa) < 80
        ):
            if texto_acumulado:
                conteudo_secao = "\n".join(texto_acumulado).strip()
                if conteudo_secao:
                    chunks.append({"secao": secao_atual, "texto": conteudo_secao})
            secao_atual = linha_limpa.rstrip(':').strip()
            texto_acumulado = []
        else:
            texto_acumulado.append(linha)

    if texto_acumulado:
        conteudo_secao = "\n".join(texto_acumulado).strip()
        if conteudo_secao:
            chunks.append({"secao": secao_atual, "texto": conteudo_secao})

    return chunks


def gerar_embeddings_lote(textos: list[str]) -> list[str]:
    """Gera embeddings de 768 dimensões em lote (1 requisição por documento) com retry."""
    for tentativa in range(3):
        try:
            resposta = client.models.embed_content(
                model="gemini-embedding-001",
                contents=textos,
                config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIM),
            )
            return [str(emb.values) for emb in resposta.embeddings]
        except errors.ClientError as e:
            if e.code == 429 and tentativa < 2:
                print("Limite de 100 RPM atingido. Aguardando 45s para continuar...")
                time.sleep(45)
            else:
                raise


def testar_busca(conn, consulta: str = "certificação orgânica mel"):
    """Valida a busca semântica exigida na Issue #5."""
    emb_consulta = gerar_embeddings_lote([consulta])[0]
    resultados = conn.execute(
        """
        SELECT arquivo, secao, tipo_doc, produtor_id, validade,
               1 - (embedding <=> %s::vector) AS similaridade
        FROM chunks_busca
        ORDER BY embedding <=> %s::vector
        LIMIT 6
        """,
        (emb_consulta, emb_consulta),
    ).fetchall()

    print(f"\n--- Teste de busca: '{consulta}' ---")
    for arq, secao, tipo_doc, pid, val, sim in resultados:
        print(f"[{arq} · {secao}] | tipo={tipo_doc} | produtor={pid} | validade={val} | sim={sim:.4f}")


def main():
    conn = conectar()
    pasta_base = Path("data/facepe") if Path("data/facepe").exists() else Path("dados/Facepe")

    arquivos_txt = sorted(pasta_base.rglob("*.txt"))
    print(f"Encontrados {len(arquivos_txt)} arquivos TXT em {pasta_base}.")

    for arquivo in arquivos_txt:
        caminho_relativo = arquivo.relative_to(pasta_base).as_posix()
        conteudo = arquivo.read_text(encoding="utf-8")
        hash_atual = hashlib.sha256(conteudo.encode("utf-8")).hexdigest()

        with conn.transaction():
            prod_id, t_doc, titulo, validade = extrair_metadados(arquivo, conteudo)
            enriquecer_produtores(conn, prod_id, conteudo)

            doc_existente = conn.execute(
                "SELECT sha256 FROM documentos WHERE arquivo = %s",
                (caminho_relativo,),
            ).fetchone()

            if doc_existente and doc_existente[0] == hash_atual:
                print(f"[SKIP] {caminho_relativo} (sem alterações)")
                continue

            if doc_existente:
                conn.execute("DELETE FROM documentos WHERE arquivo = %s", (caminho_relativo,))

            conn.execute(
                """
                INSERT INTO documentos (arquivo, tipo_doc, produtor_id, titulo, validade, sha256)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (caminho_relativo, t_doc, prod_id, titulo, validade, hash_atual),
            )

            chunks = quebrar_em_secoes(conteudo)
            # Remove chunks que têm apenas separadores (==== ou ----)
            chunks = [
                c for c in chunks
                if any(ch.isalnum() for ch in c["texto"])
            ]
            if chunks:
                textos_para_vetor = [f"{titulo} | {c['secao']}\n{c['texto']}" for c in chunks]
                embeddings = gerar_embeddings_lote(textos_para_vetor)

                for ordem, (chunk, embedding_str) in enumerate(zip(chunks, embeddings)):
                    pids_secao = sorted(set(re.findall(r'\bP\d{3}\b', chunk["texto"])))
                    meta_chunk = json.dumps({"produtores_citados": pids_secao}) if pids_secao else "{}"

                    conn.execute(
                        """
                        INSERT INTO chunks (arquivo, ordem, secao, texto, embedding, metadados)
                        VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb)
                        """,
                        (caminho_relativo, ordem, chunk["secao"], chunk["texto"], embedding_str, meta_chunk),
                    )

            print(f"[OK] {caminho_relativo} ({t_doc}, {len(chunks)} chunks)")

    testar_busca(conn, "certificação orgânica mel")
    print("\nIngestão concluída com sucesso.")


if __name__ == "__main__":
    main()