import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

def request_vsegpt_json(system_prompt: str, user_prompt_json: str) -> dict:
    """
    Универсальный ИИ-Транспорт UPort v1.2 (Выделенный шлюз VseGPT).
    Авторизует сессию через официальный SDK OpenAI и возвращает JSON-объект.
    """
    load_dotenv(dotenv_path=Path('/root/UPort/.env'))
    api_key = os.getenv("UPORT_TOKEN_AI")
    
    if not api_key:
        logging.error("❌ [VSEGPT CLIENT ERROR]: В .env отсутствует токен UPORT_TOKEN_AI!")
        return {}

    try:
        # Инициализируем клиент строго по инструкции VseGPT
        client = OpenAI(
            api_key=api_key,
            base_url="https://api.vsegpt.ru/v1"
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt_json}
        ]
        
        # Выстреливаем запрос к gpt-4o через прокси-шлюз
        res_obj = client.chat.completions.create(
            model='openai/gpt-4o',
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
            extra_headers={ "X-Title": "UPort VseGPT Transport Core" }
        )
        
        # 🔥 БРОНЕБОЙНЫЙ ПАРСИНГ: Извлекаем текст из структуры ответа SDK
        if isinstance(res_obj, str):
            raw_text = res_obj
        elif hasattr(res_obj, 'choices') and res_obj.choices:
            raw_text = res_obj.choices[0].message.content # 💡 Снайперский фикс: в SDK openai нужен индекс!
        else:
            raw_text = getattr(res_obj, 'content', str(res_obj))

        # 🔥 ВЫВОДИМ СЫРОЙ ОТВЕТ ДЛЯ ГЛАВНОГО АРХИТЕКТОРА В VS CODE
        print("\n" + "="*60)
        print("📡 [ДЕМПИНГ СЫРОГО ОТВЕТА ШЛЮЗА VSEGPT]:")
        print(raw_text)
        print("="*60 + "\n")

        # Автоматическая зачистка markdown
        raw_text = raw_text.strip()
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
        raw_text = raw_text.strip()

        return json.loads(raw_text)


    except Exception as network_err:
        logging.error(f"❌ [VSEGPT CLIENT NETWORK ERROR]: Сбой шлюза VseGPT: {network_err}")
        return {}
