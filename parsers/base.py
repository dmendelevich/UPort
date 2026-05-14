import pandas as pd
import re

class BaseParser:
    def __init__(self, file_path, portfolio_id):
        self.file_path = file_path
        self.portfolio_id = portfolio_id

    def clean_number(self, value):
        """Убирает пробелы, знаки валют и меняет запятые на точки"""
        if pd.isna(value) or value == '': return 0.0
        if isinstance(value, (int, float)): return float(value)
        # Очистка строки
        s = str(value).replace('\xa0', '').replace(' ', '').replace(',', '.')
        s = re.sub(r'[^\d.-]', '', s) # Оставляем только цифры, точки и минусы
        try:
            return float(s)
        except ValueError:
            return 0.0

    def parse(self):
        """Этот метод будет переопределен в дочерних классах"""
        raise NotImplementedError("Метод parse() должен быть реализован в подклассе")
