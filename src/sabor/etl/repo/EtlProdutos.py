from .Etl import Etl

import pandas as pd

class EtlProdutos(Etl):

    def __init__(self):
        super().__init__()

        self.df    = None
        self.table = "produtos"
        self.dtype = {
            'nome'         : 'string',
            'unidade'      : 'string',
            'categoria'    : 'string',
            'origem_animal': 'bool'  ,
        }
        self.data  = [
            { 'nome': 'acerola',                'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'açúcar mascavo',         'unidade': 'kg',      'categoria': 'Doces e conservas',   'origem_animal': False },
            { 'nome': 'amora',                  'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'banana',                 'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'batata doce',            'unidade': 'kg',      'categoria': 'Raízes e derivados',  'origem_animal': False },
            { 'nome': 'berinjela',              'unidade': 'kg',      'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'cajá',                   'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'cera de abelha',         'unidade': 'kg',      'categoria': 'Apícolas',            'origem_animal': True  },
            { 'nome': 'doce de goiaba',         'unidade': 'kg',      'categoria': 'Doces e conservas',   'origem_animal': False },
            { 'nome': 'farinha de mandioca',    'unidade': 'kg',      'categoria': 'Raízes e derivados',  'origem_animal': False },
            { 'nome': 'framboesa',              'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'frango caipira',         'unidade': 'kg',      'categoria': 'Aves e ovos',         'origem_animal': True  },
            { 'nome': 'geleia de frutas',       'unidade': 'kg',      'categoria': 'Doces e conservas',   'origem_animal': False },
            { 'nome': 'goma',                   'unidade': 'kg',      'categoria': 'Raízes e derivados',  'origem_animal': False },
            { 'nome': 'goiaba',                 'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'graviola',               'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'inhame',                 'unidade': 'kg',      'categoria': 'Raízes e derivados',  'origem_animal': False },
            { 'nome': 'macaxeira',              'unidade': 'kg',      'categoria': 'Raízes e derivados',  'origem_animal': False },
            { 'nome': 'manga',                  'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'manteiga de garrafa',    'unidade': 'kg',      'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'maracujá',               'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'mel de abelha jandaíra', 'unidade': 'kg',      'categoria': 'Apícolas',            'origem_animal': True  },
            { 'nome': 'mel silvestre',          'unidade': 'kg',      'categoria': 'Apícolas',            'origem_animal': True  },
            { 'nome': 'mirtilo',                'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'morango',                'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'nata',                   'unidade': 'kg',      'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'pepino',                 'unidade': 'kg',      'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'pimentão',               'unidade': 'kg',      'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'pólen',                  'unidade': 'kg',      'categoria': 'Apícolas',            'origem_animal': True  },
            { 'nome': 'própolis',               'unidade': 'kg',      'categoria': 'Apícolas',            'origem_animal': True  },
            { 'nome': 'queijo coalho',          'unidade': 'kg',      'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'queijo manteiga',        'unidade': 'kg',      'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'quiabo',                 'unidade': 'kg',      'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'rapadura',               'unidade': 'kg',      'categoria': 'Doces e conservas',   'origem_animal': False },
            { 'nome': 'ricota',                 'unidade': 'kg',      'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'tapioca',                'unidade': 'kg',      'categoria': 'Raízes e derivados',  'origem_animal': False },
            { 'nome': 'tomate',                 'unidade': 'kg',      'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'tomate cereja',          'unidade': 'kg',      'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'umbu',                   'unidade': 'kg',      'categoria': 'Frutas',              'origem_animal': False },
            { 'nome': 'cebolinha',              'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'coentro',                'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'couve',                  'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'espinafre',              'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'hortelã',                'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'manjericão',             'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'rúcula',                 'unidade': 'maço',    'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'iogurte natural',        'unidade': 'litro',   'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'leite',                  'unidade': 'litro',   'categoria': 'Laticínios',          'origem_animal': True  },
            { 'nome': 'melado',                 'unidade': 'litro',   'categoria': 'Doces e conservas',   'origem_animal': False },
            { 'nome': 'ovos caipira',           'unidade': 'dúzia',   'categoria': 'Aves e ovos',         'origem_animal': True  },
            { 'nome': 'alface',                 'unidade': 'unidade', 'categoria': 'Hortaliças',          'origem_animal': False },
            { 'nome': 'coco verde',             'unidade': 'unidade', 'categoria': 'Frutas',              'origem_animal': False },
        ]

    def extract(self):
        self.info(f"-- Extract: {self.table}")
        self.df = pd.DataFrame(self.data).astype(self.dtype)

    def transform(self):
        self.info(f"-- Transform: {self.table}")

    def load(self):
        self.info(f"-- Load: {self.table}")
        self.driver.insert(self.table, self.df)
        self.driver.close()

    def exec(self):
        self.divider('-')
        self.extract()
        self.transform()
        self.load()
        self.divider('-')