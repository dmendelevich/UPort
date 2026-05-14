from database import Database
from parsers.email.fb_email_parcer import FreedomEmailParser

db = Database()
parser = FreedomEmailParser()

def run_test():
    # Берем последние 10 писем из базы
    emails = db.execute_query("SELECT subject, body_content FROM incoming_messages ORDER BY id DESC LIMIT 10")
    
    print(f"🔍 Начинаю тест парсинга {len(emails)} писем...\n")
    for mail in emails:
        parser.parse_test(mail['subject'], mail['body_content'])
        print("-" * 30)

if __name__ == "__main__":
    run_test()
