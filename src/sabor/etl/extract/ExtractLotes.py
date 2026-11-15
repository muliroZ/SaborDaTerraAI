import pandas as pd

class ExtractLotes:

    def __init__(self, file_path: str):
        self.file_path = file_path

    
    def exec(self):
        df = pd.read_csv(self.file_path)
        return df