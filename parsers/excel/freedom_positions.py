import pandas as pd
from ..base import BaseParser

class FreedomPositionsParser(BaseParser):
    def parse(self):
        try:
            # Читаем без заголовков, чтобы самим найти нужную строку
            df = pd.read_excel(self.file_path, header=None)
        except Exception as e:
            return {"status": "error", "message": f"Ошибка чтения Excel: {e}"}

        # 1. Ищем индекс строки, где есть слово "Тикер"
        header_idx = None
        for i, row in df.iterrows():
            # Превращаем всё в строку, убираем пробелы и приводим к нижнему регистру
            row_str = [str(val).strip().lower() for val in row.values if pd.notna(val)]
            if 'тикер' in row_str: # Ищем без учета регистра
                header_idx = i
                break
                        
        if header_idx is None:
            return {"status": "error", "message": "Не найдена колонка 'Тикер'. Проверьте формат файла."}

        # 2. Устанавливаем найденную строку как заголовок
        df.columns = df.iloc[header_idx]
        # Оставляем данные только НИЖЕ заголовка
        df = df.iloc[header_idx + 1:].reset_index(drop=True)
        
        # 3. Чистим строки
        results = []
        for _, row in df.iterrows():
            ticker_raw = str(row.get('Тикер', '')).strip()
            
            # Пропускаем: пустые, итоги (Инструменты в...), и всякий мусор
            if not ticker_raw or ticker_raw == 'nan' or 'Инструменты в' in ticker_raw:
                continue
                
            try:
                # Если в 'Кол-во' не число, это не строка актива
                qty = self.clean_number(row.get('Кол-во', 0))
                if qty == 0: continue 

                results.append({
                    'full_ticker': ticker_raw,
                    'quantity': int(qty),
                    'market_price': self.clean_number(row.get('Цена', 0)),
                    'total_value': self.clean_number(row.get('Стоимость', 0))
                })
            except:
                continue # Если строка не парсится — просто идем дальше
            
        return {"status": "success", "data": results}

