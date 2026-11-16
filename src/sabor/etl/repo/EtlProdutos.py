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
            { 'nome': 'acerola',                'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'açúcar mascavo',         'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'amora',                  'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'banana',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'batata doce',            'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'berinjela',              'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'cajá',                   'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'cera de abelha',         'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'doce de goiaba',         'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'farinha de mandioca',    'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'fava',                   'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'feijão',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'framboesa',              'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'frango caipira',         'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'geleia de frutas',       'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'goma',                   'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'goiaba',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'graviola',               'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'inhame',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'macaxeira',              'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'manga',                  'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'manteiga de garrafa',    'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'maracujá',               'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'mel de abelha jandaíra', 'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'mel silvestre',          'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'milho',                  'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'mirtilo',                'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'morango',                'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'nata',                   'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'pepino',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'pimentão',               'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'pólen',                  'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'própolis',               'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'queijo coalho',          'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'queijo manteiga',        'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'quiabo',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'rapadura',               'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'ricota',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'tapioca',                'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'tomate',                 'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'tomate cereja',          'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'umbu',                   'unidade': 'kg',      'categoria': 'default', 'origem_animal': False },
            { 'nome': 'cebolinha',              'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'coentro',                'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'couve',                  'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'espinafre',              'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'hortelã',                'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'manjericão',             'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'rúcula',                 'unidade': 'maço',    'categoria': 'default', 'origem_animal': False },
            { 'nome': 'iogurte natural',        'unidade': 'litro',   'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'leite',                  'unidade': 'litro',   'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'melado',                 'unidade': 'litro',   'categoria': 'default', 'origem_animal': False },
            { 'nome': 'ovos caipira',           'unidade': 'dúzia',   'categoria': 'default', 'origem_animal': True  },
            { 'nome': 'alface',                 'unidade': 'unidade', 'categoria': 'default', 'origem_animal': False },
            { 'nome': 'coco verde',             'unidade': 'unidade', 'categoria': 'default', 'origem_animal': False },
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