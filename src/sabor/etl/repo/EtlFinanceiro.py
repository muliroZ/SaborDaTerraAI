import pandas as pd
from .Etl import Etl

class EtlFinanceiro(Etl):
    def __init__(self):
        super().__init__()
        self.df = None
        self.table = "financeiro"
        self.file_path = self.path / 'financeiro.csv'

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.read_csv(self.file_path, encoding="utf-8")

    def transform(self):
        self.info(f"-- Transform: {self.table}")

        # Normalização de compradores (Exigência da Issue)
        compradores_map = {
            'SESC Garanhuns': 'SESC - Unidade Garanhuns',
            'Mercado Bom Preço - Filial Centro': 'Mercado Bom Preço',
        }
        self.df['comprador_normalizado'] = self.df['comprador'].replace(compradores_map)

        # Resolver a mistura de Quantidade e Unidade
        unidades_especiais = {
            'alface': 'unidade', 'coco verde': 'unidade',
            'coentro': 'maço', 'cebolinha': 'maço', 'couve': 'maço',
            'espinafre': 'maço', 'hortelã': 'maço', 'manjericão': 'maço', 'rúcula': 'maço',
            'ovos caipira': 'dúzia',
            'iogurte natural': 'litro', 'leite': 'litro', 'melado': 'litro',
        }
        self.df['unidade'] = self.df['produto'].map(unidades_especiais).fillna('kg')
        self.df = self.df.rename(columns={'quantidade_kg': 'quantidade'})

        # Remover colunas denormalizadas
        self.df = self.df.drop(columns=['produtor_nome', 'produtor_cpf'], errors='ignore')

        # 1. Converter datas e mapear produto_id ANTES de calcular divergências
        colunas_data = ['data', 'data_vencimento', 'data_pagamento']
        for col in colunas_data:
            if col in self.df.columns:
                self.df[col] = pd.to_datetime(self.df[col], format='%Y-%m-%d', errors='coerce')

        self.df['produto_id'] = self.mapear_produto_id(self.df['produto'])

        # 2. Calcular alerta_divergencia (Contrato PNAE + Tabela de Preços + Vigência Orgânica)
        df_precos = pd.read_sql(
            """
            SELECT produto_id, preco_convencional, preco_organico,
                   desconto_atacado_pct, volume_minimo_atacado
            FROM precos
            WHERE produto_id IS NOT NULL
            """,
            self.driver.engine,
        )
        df_prod = pd.read_sql(
            "SELECT id AS produtor_id, certificacao_organica, validade_certificacao FROM produtores",
            self.driver.engine,
        )

        # Agrupar faixas por produto_id (evita duplicar linhas de goiaba e pimentão no merge)
        regras_preco: dict[int, list[dict]] = {}
        for row in df_precos.itertuples(index=False):
            regras_preco.setdefault(int(row.produto_id), []).append({
                'conv': float(row.preco_convencional) if pd.notna(row.preco_convencional) else None,
                'org': float(row.preco_organico) if pd.notna(row.preco_organico) else None,
                'desc': float(row.desconto_atacado_pct or 0),
                'vol_min': float(row.volume_minimo_atacado or 0),
            })

        precos_pnae = {
            'banana prata': 3.80, 'goiaba paluma': 5.20, 'manga tommy': 5.50,
            'maracujá': 6.00, 'macaxeira': 3.50, 'inhame': 5.00, 'batata doce': 4.00,
            'alface': 2.80, 'coentro': 2.00, 'tomate': 7.00, 'queijo coalho': 34.00,
            'mel silvestre': 58.00, 'ovos caipira': 18.00, 'frango caipira': 29.00,
        }

        df_check = self.df[[
            'data', 'produtor_id', 'comprador_normalizado',
            'produto', 'produto_id', 'tipo', 'quantidade', 'valor_unitario',
        ]].merge(df_prod, on='produtor_id', how='left')

        def verificar_divergencia(row) -> bool:
            if row['tipo'] != 'venda' or pd.isna(row['produto_id']):
                return False

            cobrado = float(row['valor_unitario'])
            prod_nome = str(row['produto']).strip().lower()
            pid = int(row['produto_id'])

            # Regra contratual PNAE (preço fixo)
            if 'PNAE' in str(row['comprador_normalizado']) and prod_nome in precos_pnae:
                return abs(cobrado - precos_pnae[prod_nome]) > 0.01

            # Verificar se o produtor tinha certificação orgânica vigente na data da venda
            org_vigente = (
                bool(row['certificacao_organica'])
                and pd.notna(row['validade_certificacao'])
                and pd.Timestamp(row['validade_certificacao']) >= pd.Timestamp(row['data'])
            )

            faixas = regras_preco.get(pid, [])
            if not faixas:
                return False

            # Venda a preço orgânico com certificado vencido (ex.: P008 em 01/10)
            if not org_vigente:
                for f in faixas:
                    if f['org'] is not None and f['conv'] is not None:
                        if abs(cobrado - f['org']) <= 0.01:
                            return True

            # Setembro é anterior à tabela de outubro
            if pd.Timestamp(row['data']) < pd.Timestamp('2026-10-01'):
                return False

            # Validar contra preços permitidos na tabela de outubro (varejo e atacado)
            tipos_permitidos = ['conv', 'org'] if org_vigente else ['conv']
            precos_validos = []
            for f in faixas:
                for tipo_p in tipos_permitidos:
                    base = f[tipo_p]
                    if base is not None:
                        precos_validos.append(base)
                        if f['vol_min'] > 0 and float(row['quantidade']) >= f['vol_min']:
                            precos_validos.append(round(base * (1.0 - f['desc'] / 100.0), 2))

            return not any(abs(cobrado - p) <= 0.01 for p in precos_validos)

        self.df['alerta_divergencia'] = df_check.apply(verificar_divergencia, axis=1)

        # TESTE DE VALIDAÇÃO (Exigência da Issue)
        total_vendas = self.df['valor_total'].sum()
        self.info(f"TESTE: Total de Vendas = R$ {total_vendas:,.2f}")
        if total_vendas != 24634.00:
            self.info("O total não bate com os R$ 24.634,00 documentados")

    def load(self):
        self.info(f"-- Load: {self.table}")
        self.driver.insert(self.table, self.df)

    def exec(self):
        self.extract()
        self.transform()
        self.load()