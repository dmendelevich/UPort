import re

class ReportDispatcher:
    def __init__(self, db):
        self.db = db

    def dispatch(self, file_name, file_path):
        print(f"DEBUG: Обработка файла {file_name}")
        
        # 1. Проверка на английский язык (Портфель сына)
        # Если в названии только латиница, цифры и спецсимволы
        if not re.search(r'[а-яА-Я]', file_name):
            return {
                "status": "success",
                "parser": "Trading212 (Заглушка)",
                "portfolio": "Сын (по языку файла)",
                "action": "Будет реализовано позже"
            }

        # 2. Логика Freedom (Предмет + Email)
        # Регулярка ищет: (Слово) (пробелы) (email)
        match = re.search(r'(Позиции|Сделки|Приказы)\s+([\w\.-]+@[\w\.-]+)', file_name)
        
        if match:
            report_type = match.group(1)
            email = match.group(2)

            ####            
            # Пробуем найти портфель в БД через наш новый database.py
            portfolio_data = self.db.get_portfolio_data_by_email(email)
        
            if not portfolio_data:
                return {"status": "error", "message": f"Email {email} не найден."}

            portfolio_id = portfolio_data['id']
            portfolio_info = f"{portfolio_data['portfolio_name']} ({portfolio_data['owner_name']})"
            ####
            if report_type == "Позиции":
                from parsers.excel.freedom_positions import FreedomPositionsParser
                parser = FreedomPositionsParser(file_path, portfolio_id)
                parse_result = parser.parse() # Вызов реального парсинга Excel
                
                if parse_result["status"] == "error":
                    return parse_result
                
                # Запускаем сверку (метод reconcile мы добавим ниже)
                return self.reconcile(parse_result["data"], portfolio_id, portfolio_info)
            
            # Если это Сделки или Приказы (пока просто отчет)
            return {
                "status": "success",
                "parser": f"Freedom {report_type}",
                "portfolio_id": portfolio_id,
                "action": f"Парсер для {report_type} будет подключен позже"
            }

        
        return {
            "status": "error",
            "message": "Не удалось определить тип отчета по названию файла."
        }

    def reconcile(self, report_data, portfolio_id, portfolio_info):
        # 1. Получаем текущие активы из БД
        db_assets = self.db.get_assets_for_reconciliation(portfolio_id)
        # Превращаем в словарь {full_ticker: quantity} для быстрого поиска
        db_dict = {asset['full_ticker']: asset['quantity'] for asset in db_assets}
        
        summary = []
        for item in report_data:
            ticker = item['full_ticker']
            report_qty = item['quantity']
            db_qty = db_dict.get(ticker, 0)
            
            if report_qty == db_qty:
                summary.append(f"✅ {ticker}: {report_qty} шт. (ОК)")
            else:
                diff = report_qty - db_qty
                summary.append(f"❌ {ticker}: В файле {report_qty} | В базе {db_qty} (Разница: {diff:+})")
        
        return {
            "status": "success",
            "parser": "Freedom Позиции",
            "action": "Сверка завершена",
            "portfolio_info": portfolio_info,
            "details": "\n".join(summary)
        }
