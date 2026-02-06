# Telegram -> Email Bot

Бот принимает пересланные сообщения (текст/фото/видео/файлы, включая медиагруппы),
скачивает медиа во временную папку и отправляет email с HTML-форматированием текста
и вложениями.

## Требования
- Python 3.9+
- Gmail App Password (обычный пароль Gmail не подойдёт)

## Установка
```bash
cd telegram_email_bot
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt

