-- Schema do SaborDaTerraAI (PostgreSQL 17 + pgvector).
--
-- Idempotente: pode ser reaplicado sem erro. Para recriar do zero use
-- `uv run python scripts/init_db.py --reset`.
--
-- Convenções:
--   * Colunas de texto vindas das fontes guardam o valor ORIGINAL; colunas
--     `*_normalizado` / `produto_id` são preenchidas pela ingestão (#4).
--   * `quantidade_kg` das fontes foi separada em `quantidade` + `unidade`,
--     porque a coluna mistura kg, dúzia, unidade e maço (docs/README.md, achado 9).
--   * A dimensão do embedding (768) segue o ADR: gemini-embedding-001 com MRL.
--     Se mudar aqui, mude também `EMBEDDING_DIM` em src/sabor/db.py.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Produtores
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS produtores (
    id                     text PRIMARY KEY CHECK (id ~ '^P\d{3}$'),
    nome_completo          text NOT NULL,
    cpf                    text,
    data_nascimento        date,
    telefone               text,
    email                  text,
    endereco               text,
    municipio              text NOT NULL,
    uf                     char(2) NOT NULL DEFAULT 'PE',
    certificacao_organica  boolean NOT NULL DEFAULT false,
    validade_certificacao  date,
    chave_pix              text,
    banco                  text,
    agencia                text,
    conta_corrente         text,
    status                 text NOT NULL CHECK (status IN ('ativo', 'inativo')),
    -- Atributos que não estão no CSV e só existem nos TXT (enriquecimento).
    dap_caf                text,
    registro_sie           text
);

COMMENT ON COLUMN produtores.certificacao_organica IS
    'Valor declarado no cadastro. NÃO considera vencimento (P008): use validade_certificacao.';
COMMENT ON COLUMN produtores.agencia IS 'Texto para preservar zeros à esquerda.';

-- ---------------------------------------------------------------------------
-- Catálogo canônico de produtos (nome da tabela de preços) e sinônimos
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS produtos (
    id         serial PRIMARY KEY,
    nome       text NOT NULL UNIQUE,
    unidade    text NOT NULL CHECK (unidade IN ('kg', 'unidade', 'dúzia', 'maço', 'litro')),
    categoria  text,
    origem_animal boolean NOT NULL DEFAULT false
);

COMMENT ON COLUMN produtos.origem_animal IS
    'Produtos de origem animal exigem SIE/SIM/SISBI para venda a terceiros.';

CREATE TABLE IF NOT EXISTS produto_sinonimos (
    sinonimo    text PRIMARY KEY,
    produto_id  integer NOT NULL REFERENCES produtos (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS produtor_produtos (
    produtor_id    text NOT NULL REFERENCES produtores (id) ON DELETE CASCADE,
    nome_original  text NOT NULL,
    produto_id     integer REFERENCES produtos (id) ON DELETE SET NULL,
    PRIMARY KEY (produtor_id, nome_original)
);

COMMENT ON COLUMN produtor_produtos.produto_id IS
    'NULL quando o produto está fora do catálogo (ex.: feijão, milho e fava de P015).';

CREATE INDEX IF NOT EXISTS produtor_produtos_produto_idx ON produtor_produtos (produto_id);

-- ---------------------------------------------------------------------------
-- Preços e sazonalidade
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS precos (
    id                    serial PRIMARY KEY,
    referencia            date NOT NULL,
    produto               text NOT NULL,
    produto_id            integer REFERENCES produtos (id) ON DELETE SET NULL,
    unidade               text NOT NULL CHECK (unidade IN ('kg', 'unidade', 'dúzia', 'maço', 'litro')),
    preco_convencional    numeric(10, 2) CHECK (preco_convencional >= 0),
    preco_organico        numeric(10, 2) CHECK (preco_organico >= 0),
    desconto_atacado_pct  smallint NOT NULL DEFAULT 0
                          CHECK (desconto_atacado_pct BETWEEN 0 AND 100),
    volume_minimo_atacado numeric(10, 2) NOT NULL DEFAULT 0 CHECK (volume_minimo_atacado >= 0),
    observacao            text,
    UNIQUE (referencia, produto)
);

COMMENT ON COLUMN precos.referencia IS 'Mês de vigência da tabela (ex.: 2026-10-01).';
COMMENT ON COLUMN precos.preco_convencional IS 'NULL = "n/d" na fonte (versão inexistente).';
COMMENT ON COLUMN precos.volume_minimo_atacado IS
    'volume_minimo_atacado_kg da fonte, mas expresso na UNIDADE do produto. 0 = sem atacado.';

CREATE TABLE IF NOT EXISTS sazonalidade (
    produto     text PRIMARY KEY,
    produto_id  integer REFERENCES produtos (id) ON DELETE SET NULL,
    observacao  text
);

CREATE TABLE IF NOT EXISTS sazonalidade_mensal (
    produto    text NOT NULL REFERENCES sazonalidade (produto) ON DELETE CASCADE,
    mes        smallint NOT NULL CHECK (mes BETWEEN 1 AND 12),
    nivel      text NOT NULL CHECK (nivel IN ('nenhuma', 'baixa', 'media', 'alta')),
    nivel_num  smallint GENERATED ALWAYS AS (
                   CASE nivel WHEN 'nenhuma' THEN 0 WHEN 'baixa' THEN 1
                              WHEN 'media' THEN 2 WHEN 'alta' THEN 3 END
               ) STORED,
    PRIMARY KEY (produto, mes)
);

-- ---------------------------------------------------------------------------
-- Pedidos (caixa de entrada): mensagem bruta + campos extraídos (#9, #10)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pedidos (
    id                     text PRIMARY KEY,
    data_recebimento       timestamp NOT NULL,
    canal                  text NOT NULL CHECK (canal IN ('email', 'whatsapp', 'formulario_web')),
    remetente              text,
    comprador              text,
    mensagem_original      text NOT NULL,
    status                 text NOT NULL DEFAULT 'pendente',
    -- Campos extraídos pelo agente (NULL até a extração rodar).
    tipo                   text CHECK (tipo IN (
                               'compra', 'compra_recorrente', 'cotacao', 'consulta',
                               'parceria', 'consulta_financeira', 'notificacao_oficial')),
    urgencia               text CHECK (urgencia IN ('baixa', 'media', 'alta')),
    comprador_normalizado  text,
    data_entrega           date,
    data_entrega_texto     text,
    janela_entrega         text,
    forma_pagamento        text,
    sinalizacoes           text[] NOT NULL DEFAULT '{}',
    extracao               jsonb,
    extraido_em            timestamptz
);

COMMENT ON COLUMN pedidos.data_recebimento IS 'Hora local (America/Recife), sem fuso na fonte.';
COMMENT ON COLUMN pedidos.extracao IS 'Saída completa e validada do modelo Pydantic Pedido.';

CREATE TABLE IF NOT EXISTS pedido_itens (
    id             bigserial PRIMARY KEY,
    pedido_id      text NOT NULL REFERENCES pedidos (id) ON DELETE CASCADE,
    ordem          smallint NOT NULL,
    produto_texto  text NOT NULL,
    produto_id     integer REFERENCES produtos (id) ON DELETE SET NULL,
    quantidade     numeric(12, 3) CHECK (quantidade > 0),
    unidade        text CHECK (unidade IN ('kg', 'unidade', 'dúzia', 'maço', 'litro')),
    organico       boolean,
    opcional       boolean NOT NULL DEFAULT false,
    ambiguo        boolean NOT NULL DEFAULT false,
    observacao     text,
    UNIQUE (pedido_id, ordem),
    CHECK (quantidade IS NULL OR unidade IS NOT NULL)
);

COMMENT ON COLUMN pedido_itens.organico IS 'Preferência do comprador; NULL = não informada.';
COMMENT ON COLUMN pedido_itens.ambiguo IS
    'Item que exige confirmação ("o queijo da última vez") em vez de suposição.';

-- ---------------------------------------------------------------------------
-- Financeiro e lotes
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS financeiro (
    id_transacao           text PRIMARY KEY,
    data                   date NOT NULL,
    produtor_id            text NOT NULL REFERENCES produtores (id),
    comprador              text NOT NULL,
    comprador_normalizado  text,
    tipo                   text NOT NULL DEFAULT 'venda',
    produto                text NOT NULL,
    produto_id             integer REFERENCES produtos (id) ON DELETE SET NULL,
    quantidade             numeric(12, 3) NOT NULL CHECK (quantidade > 0),
    unidade                text NOT NULL CHECK (unidade IN ('kg', 'unidade', 'dúzia', 'maço', 'litro')),
    valor_unitario         numeric(12, 2) NOT NULL CHECK (valor_unitario >= 0),
    valor_total            numeric(12, 2) NOT NULL CHECK (valor_total >= 0),
    forma_pagamento        text,
    status_pagamento       text NOT NULL CHECK (status_pagamento IN ('pago', 'pendente')),
    data_vencimento        date,
    data_pagamento         date,
    chave_pix_deposito     text,
    observacao             text,
    alerta_divergencia     boolean NOT NULL DEFAULT false,
    CHECK (status_pagamento = 'pago' OR data_pagamento IS NULL)
);

COMMENT ON TABLE financeiro IS
    'produtor_nome/produtor_cpf da fonte não são copiados: vêm do join com produtores.';
COMMENT ON COLUMN financeiro.quantidade IS
    'quantidade_kg da fonte; a unidade real está em `unidade` (alface em unidades, ovos em dúzias).';
COMMENT ON COLUMN financeiro.status_pagamento IS
    'Atraso não é um status: compare data_vencimento com DATA_REF_DATE.';
COMMENT ON COLUMN financeiro.alerta_divergencia IS
    'Flag gerada pelo ETL para indicar divergência entre o valor_unitario cobrado e a tabela de preços oficial.';

CREATE INDEX IF NOT EXISTS financeiro_produtor_idx ON financeiro (produtor_id);
CREATE INDEX IF NOT EXISTS financeiro_pendentes_idx ON financeiro (data_vencimento)
    WHERE status_pagamento = 'pendente';

CREATE TABLE IF NOT EXISTS lotes (
    lote_id                   text PRIMARY KEY,
    data_colheita             date NOT NULL,
    data_entrega              date NOT NULL,
    produtor_id               text NOT NULL REFERENCES produtores (id),
    produto                   text NOT NULL,
    produto_id                integer REFERENCES produtos (id) ON DELETE SET NULL,
    quantidade                numeric(12, 3) NOT NULL CHECK (quantidade > 0),
    unidade                   text NOT NULL CHECK (unidade IN ('kg', 'unidade', 'dúzia', 'maço', 'litro')),
    comprador                 text,
    comprador_normalizado     text,
    pedido_ref                text,
    transacao_id              text REFERENCES financeiro (id_transacao) ON DELETE SET NULL,
    refrigerado               boolean NOT NULL,
    temperatura_transporte_c  numeric(4, 1),
    validade                  date,
    inspecao_visual           text,
    observacao                text,
    CHECK (refrigerado OR temperatura_transporte_c IS NULL)
);

COMMENT ON COLUMN lotes.data_colheita IS 'Para processados (queijo, mel, farinha), data de fabricação/envase.';
COMMENT ON COLUMN lotes.pedido_ref IS
    'pedido_id da fonte (PED-2026-SEP-003). Formato diferente de pedidos.id: sem FK.';
COMMENT ON COLUMN lotes.transacao_id IS
    'Vínculo inferido com financeiro por (data, produtor, produto, quantidade, comprador).';
COMMENT ON COLUMN lotes.refrigerado IS
    'false quando temperatura_transporte_c = "ambiente" na fonte.';

CREATE INDEX IF NOT EXISTS lotes_produtor_idx ON lotes (produtor_id);
CREATE INDEX IF NOT EXISTS lotes_produto_idx ON lotes (produto_id, data_colheita DESC);

-- ---------------------------------------------------------------------------
-- Documentos TXT e chunks vetorizados (#5)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS documentos (
    arquivo      text PRIMARY KEY,
    tipo_doc     text NOT NULL CHECK (tipo_doc IN (
                     'certificado', 'laudo', 'inspecao', 'contrato', 'ficha')),
    produtor_id  text REFERENCES produtores (id) ON DELETE SET NULL,
    titulo       text,
    validade     date,
    sha256       char(64) NOT NULL,
    metadados    jsonb NOT NULL DEFAULT '{}',
    ingerido_em  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN documentos.arquivo IS
    'Caminho relativo a dados/Facepe, ex.: certificacoes/cert_organica_jose_santos_P002.txt.';
COMMENT ON COLUMN documentos.sha256 IS
    'Hash do conteúdo: a reingestão só troca os chunks quando o arquivo mudou.';

CREATE TABLE IF NOT EXISTS chunks (
    id           bigserial PRIMARY KEY,
    arquivo      text NOT NULL REFERENCES documentos (arquivo)
                 ON DELETE CASCADE ON UPDATE CASCADE,
    ordem        integer NOT NULL,
    secao        text,
    texto        text NOT NULL,
    embedding    vector(768) NOT NULL,
    metadados    jsonb NOT NULL DEFAULT '{}',
    texto_busca  tsvector GENERATED ALWAYS AS (
                     to_tsvector('portuguese'::regconfig,
                                 coalesce(secao, '') || ' ' || texto)
                 ) STORED,
    UNIQUE (arquivo, ordem)
);

COMMENT ON COLUMN chunks.texto_busca IS
    'Busca lexical (códigos como P008 ou 2024-089) para combinar com a vetorial (busca híbrida).';

-- Similaridade de cosseno: use o operador <=> nas consultas.
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_texto_busca_idx ON chunks USING gin (texto_busca);
CREATE INDEX IF NOT EXISTS chunks_metadados_idx ON chunks USING gin (metadados);

-- Visão usada pela busca: cada chunk com os metadados do documento para citar a fonte
-- no formato `[arquivo · secao]`.
CREATE OR REPLACE VIEW chunks_busca AS
SELECT c.id,
       c.arquivo,
       c.ordem,
       c.secao,
       d.tipo_doc,
       d.produtor_id,
       d.validade,
       c.texto,
       c.embedding,
       c.texto_busca,
       d.metadados || c.metadados AS metadados
FROM chunks c
JOIN documentos d USING (arquivo);
