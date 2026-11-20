import pandas as pd
import numpy as np
from .Etl import Etl


class EtlLotes(Etl):
    def __init__(self):
        super().__init__()
        self.df = None
        self.table = "lotes"
        self.file_path = self.path / 'lotes.csv'

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.read_csv(self.file_path, encoding="utf-8")

    def transform(self):
        self.info(f"-- Transform: {self.table}")

        # Normalização e Unidades
        compradores_map = {'SESC Garanhuns': 'SESC - Unidade Garanhuns'}
        self.df['comprador_normalizado'] = self.df['comprador'].replace(compradores_map)

        unidades_especiais = {
            'alface': 'unidade',
            'coentro': 'maço',
            'couve': 'maço',
            'rúcula': 'maço',
            'ovos caipira': 'dúzia',
        }
        self.df['unidade'] = self.df['produto'].map(unidades_especiais).fillna('kg')
        self.df = self.df.rename(columns={'quantidade_kg': 'quantidade', 'pedido_id': 'pedido_ref'})

        # Cálculo da Refrigeração (Baseado na temperatura)
        self.df['refrigerado'] = self.df['temperatura_transporte_c'].astype(str).str.lower() != 'ambiente'
        self.df['temperatura_transporte_c'] = pd.to_numeric(
            self.df['temperatura_transporte_c'].replace({'ambiente': np.nan, 'Ambiente': np.nan}),
            errors='coerce',
        )

        self.df = self.df.drop(columns=['produtor_nome', 'produtor_cpf'], errors='ignore')

        colunas_data = ['data_colheita', 'data_entrega', 'validade']
        for col in colunas_data:
            if col in self.df.columns:
                self.df[col] = pd.to_datetime(self.df[col], format='%Y-%m-%d', errors='coerce')

        self.df['produto_id'] = self.mapear_produto_id(self.df['produto'])

        # Inferir vínculo transacao_id com a tabela financeiro
        df_fin = pd.read_sql(
            """
            SELECT id_transacao AS transacao_id,
                   data,
                   produtor_id,
                   produto_id,
                   quantidade,
                   comprador_normalizado
            FROM financeiro
            WHERE tipo = 'venda'
            """,
            self.driver.engine,
        )

        # Padronizar chaves para garantir match exato no Pandas
        self.df['_data_key'] = self.df['data_entrega'].dt.strftime('%Y-%m-%d')
        self.df['produtor_id'] = self.df['produtor_id'].astype(str).str.strip()
        self.df['comprador_normalizado'] = self.df['comprador_normalizado'].astype(str).str.strip()
        self.df['produto_id'] = pd.Series(self.df['produto_id'], index=self.df.index).astype('Int64')
        self.df['quantidade'] = self.df['quantidade'].astype(float).round(3)

        df_fin['_data_key'] = pd.to_datetime(df_fin['data']).dt.strftime('%Y-%m-%d')
        df_fin['produtor_id'] = df_fin['produtor_id'].astype(str).str.strip()
        df_fin['comprador_normalizado'] = df_fin['comprador_normalizado'].astype(str).str.strip()
        df_fin['produto_id'] = df_fin['produto_id'].astype('Int64')
        df_fin['quantidade'] = df_fin['quantidade'].astype(float).round(3)

        # 1. Cruzamento exato (data, produtor, produto, quantidade, comprador)
        chaves_exatas = ['_data_key', 'produtor_id', 'produto_id', 'quantidade', 'comprador_normalizado']
        df_fin_exato = df_fin[chaves_exatas + ['transacao_id']].drop_duplicates(subset=chaves_exatas)

        self.df = self.df.drop(columns=['transacao_id'], errors='ignore')
        self.df = self.df.merge(df_fin_exato, on=chaves_exatas, how='left')

        # 2. Fallback sem a data (cobre LOT-2026-0089 e 0090: entrega 02/09, financeiro 05/09)
        chaves_sem_data = ['produtor_id', 'produto_id', 'quantidade', 'comprador_normalizado']
        df_fin_fb = (
            df_fin[chaves_sem_data + ['transacao_id']]
            .drop_duplicates(subset=chaves_sem_data)
            .rename(columns={'transacao_id': 'transacao_id_fb'})
        )
        self.df = self.df.merge(df_fin_fb, on=chaves_sem_data, how='left')
        self.df['transacao_id'] = self.df['transacao_id'].fillna(self.df['transacao_id_fb'])

        # Remover colunas auxiliares e garantir None puro para o psycopg
        self.df = self.df.drop(columns=['_data_key', 'transacao_id_fb'])
        self.df['produto_id'] = self.df['produto_id'].astype(object).where(self.df['produto_id'].notna(), None)
        self.df['transacao_id'] = self.df['transacao_id'].astype(object).where(self.df['transacao_id'].notna(), None)

    def load(self):
        self.info(f"-- Load: {self.table}")
        self.driver.insert(self.table, self.df)

    def exec(self):
        self.extract()
        self.transform()
        self.load()