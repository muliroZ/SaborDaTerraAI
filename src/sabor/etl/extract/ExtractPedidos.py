import pandas as pd

class ExtractPedidos:

    def __init__(self, file_path: str):
        self.file_path = file_path

    
    def exec(self):
        df = pd.read_json(self.file_path)
        return df