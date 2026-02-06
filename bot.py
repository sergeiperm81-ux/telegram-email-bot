import os
import re
import uuid
import shutil
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from telegram import Update, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import smtplib
from email.utils import formatdate
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email import encoders


# ----------------------------
# Конфигурация и логирование
# ----------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

EMAIL_SENDER = os.getenv("EMAIL_SENDER", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()
EMAIL_RECIPIENT = os.getenv("EMAIL_RECIPIENT", "").strip()

TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/telegram_bot/"))

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))  # Gmail SSL

# Таймаут для “добора” сообщений медиагруппы
MEDIA_GROUP_FLUSH_DELAY_SEC = float(os.getenv("MEDIA_GROUP_FLUSH_DELAY_SEC", "1.6"))

# Предупреждение по размеру файла
WARN_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("telegram_email_bot")


# ----------------------------
# Внутреннее хранилище медиагрупп
# ----------------------------
# Ключ: (chat_id, media_group_id)
# Значение: {
#   "messages": [Message, ...],
#   "flush_task": asyncio.Task | None
# }

MediaGroupKey = Tuple[int, str]
media_groups: Dict[MediaGroupKey, Dict[str, object]] = {}


# ----------------------------
# Утилиты: HTML нормализация Telegram -> нужные теги
# ----------------------------

def telegram_html_to_required(html: str) -> str:
    """
    В PTB message.text_html / caption_html возвращает HTML с тегами <b>, <i>, <a>, <code>, <pre> и т.д.
    Требование: <strong>, <em>, <a>, <code>.
    Делается простая замена <b>/<i> -> <strong>/<em>.
    """
    if not html:
        return ""

    # PTB обычно использует <b> и <i>
    html = html.replace("<b>", "<strong>").replace("</b>", "</strong>")
    html = html.replace("<i>", "<em>").replace("</i>", "</em>")

    # На всякий случай: если где-то встретятся <strong>/<em> уже — всё ок.
    return html


def extract_message_html_text(message: Message) -> str:
    """
    Берём HTML-текст сообщения с сохранением форматирования.
    Для текста — message.text_html, для подписей к медиа — message.caption_html.
    """
    html = ""
    if message.text:
        html = message.text_html or ""
    elif message.caption:
        html = message.caption_html or ""

    html = telegram_html_to_required(html)

    # Если текста нет — возвращаем пусто
    return html.strip()


# ----------------------------
# Скачивание медиа
# ----------------------------

async def download_message_media(message: Message, dst_dir: Path) -> List[Path]:
    """
    Скачивает все медиа из конкретного сообщения и возвращает список путей к файлам.
    Поддержка: фото, видео, документ, анимация, голосовые/аудио (на всякий случай).
    """
    files: List[Path] = []

    # Фото: берём самое большое (последний элемент списка photo)
    if message.photo:
        photo = message.photo[-1]
        tg_file = await photo.get_file()
        filename = f"photo_{photo.file_unique_id}.jpg"
        out_path = dst_dir / filename
        logger.info("Скачивание фото -> %s", out_path)
        await tg_file.download_to_drive(custom_path=str(out_path))
        files.append(out_path)

    # Видео
    if message.video:
        tg_file = await message.video.get_file()
        ext = Path(message.video.file_name or "").suffix or ".mp4"
        filename = f"video_{message.video.file_unique_id}{ext}"
        out_path = dst_dir / filename
        logger.info("Скачивание видео -> %s", out_path)
        await tg_file.download_to_drive(custom_path=str(out_path))
        files.append(out_path)

    # Документы (в т.ч. файлы, иногда Telegram присылает видео как document)
    if message.document:
        tg_file = await message.document.get_file()
        original_name = message.document.file_name or f"document_{message.document.file_unique_id}"
        # Чистим имя
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original_name)
        out_path = dst_dir / safe_name
        logger.info("Скачивание документа -> %s", out_path)
        await tg_file.download_to_drive(custom_path=str(out_path))
        files.append(out_path)

    # Анимация (gif/mp4)
    if message.animation:
        tg_file = await message.animation.get_file()
        ext = Path(message.animation.file_name or "").suffix or ".mp4"
        filename = f"animation_{message.animation.file_unique_id}{ext}"
        out_path = dst_dir / filename
        logger.info("Скачивание анимации -> %s", out_path)
        await tg_file.download_to_drive(custom_path=str(out_path))
        files.append(out_path)

    # Аудио
    if message.audio:
        tg_file = await message.audio.get_file()
        ext = Path(message.audio.file_name or "").suffix or ".mp3"
        filename = f"audio_{message.audio.file_unique_id}{ext}"
        out_path = dst_dir / filename
        logger.info("Скачивание аудио -> %s", out_path)
        await tg_file.download_to_drive(custom_path=str(out_path))
        files.append(out_path)

    # Голосовые
    if message.voice:
        tg_file = await message.voice.get_file()
        filename = f"voice_{message.voice.file_unique_id}.ogg"
        out_path = dst_dir / filename
        logger.info("Скачивание voice -> %s", out_path)
        await tg_file.download_to_drive(custom_path=str(out_path))
        files.append(out_path)

    return files


# ----------------------------
# Email отправка
# ----------------------------

def build_email(subject: str, html_body: str, attachments: List[Path]) -> MIMEMultipart:
    """
    Собирает письмо с HTML-телом и вложениями.
    """
    msg = MIMEMultipart("mixed")
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = subject

    # Тело письма (HTML)
    body_part = MIMEText(html_body or "<div>(без текста)</div>", "html", "utf-8")
    msg.attach(body_part)

    # Вложения
    for path in attachments:
        if not path.exists() or not path.is_file():
            continue

        part = MIMEBase("application", "octet-stream")
        with open(path, "rb") as f:
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{path.name}"'
        )
        msg.attach(part)

    return msg


def send_email_via_gmail(msg: MIMEMultipart) -> None:
    """
    Отправка через Gmail SMTP SSL.
    """
    logger.info("Подключение к SMTP %s:%s", SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, [EMAIL_RECIPIENT], msg.as_string())
    logger.info("Письмо отправлено успешно")


# ----------------------------
# Обработка одного “поста”: текст + файлы
# ----------------------------

def compose_html_document(html_text: str) -> str:
    """
    Упаковываем в простой HTML-документ.
    """
    # Небольшой базовый стиль, чтобы письмо читалось нормально
    return f"""\
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Telegram Post</title>
</head>
<body>
  <div style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.45;">
    {html_text if html_text else "<div>(без текста)</div>"}
  </div>
</body>
</html>
"""


async def process_messages_and_send_email(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    messages: List[Message],
) -> None:
    """
    Собирает текст (HTML) и медиа из списка сообщений (в т.ч. медиагруппа),
    скачивает вложения, отправляет email, удаляет временные файлы.
    """
    chat_id = update.effective_chat.id if update.effective_chat else 0

    # Папка под конкретную операцию
    op_id = uuid.uuid4().hex
    op_dir = TEMP_DIR / op_id
    op_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Начало обработки, op_dir=%s, сообщений=%d", op_dir, len(messages))

    try:
        # 1) Собираем HTML текст: обычно текст находится в одном из сообщений
        #    Если текстов несколько — склеим через <hr>
        html_parts: List[str] = []
        all_attachments: List[Path] = []

        for msg in messages:
            html_text = extract_message_html_text(msg)
            if html_text:
                html_parts.append(html_text)

        combined_html_text = ""
        if html_parts:
            # Если несколько частей — разделим
            combined_html_text = "<hr>".join(f"<div>{p}</div>" for p in html_parts)

        # 2) Скачиваем медиа из каждого сообщения
        for msg in messages:
            files = await download_message_media(msg, op_dir)
            all_attachments.extend(files)

        # 3) Предупреждение про большие файлы (>25MB)
        big_files = [p for p in all_attachments if p.exists() and p.stat().st_size > WARN_SIZE_BYTES]
        if big_files and update.effective_message:
            names = ", ".join(p.name for p in big_files[:5])
            more = "" if len(big_files) <= 5 else f" и ещё {len(big_files)-5}"
            await update.effective_message.reply_text(
                f"⚠️ Есть файлы больше 25MB: {names}{more}. Попробую отправить, но почтовый сервер может не принять."
            )

        # 4) Формируем и отправляем письмо
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = f"Пост из Telegram - {now_str}"

        html_doc = compose_html_document(combined_html_text)

        email_msg = build_email(subject=subject, html_body=html_doc, attachments=all_attachments)
        # Отправка — синхронная (smtplib), чтобы нормально ловить исключения
        await asyncio.to_thread(send_email_via_gmail, email_msg)

        # 5) Удаляем файлы
        shutil.rmtree(op_dir, ignore_errors=True)

        # 6) Сообщение пользователю
        if update.effective_message:
            await update.effective_message.reply_text("✅ Отправлено на email!")
        logger.info("Готово, op_id=%s", op_id)

    except Exception as e:
        logger.exception("Ошибка при обработке op_id=%s: %s", op_id, str(e))
        # Чистим за собой
        shutil.rmtree(op_dir, ignore_errors=True)

        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Ошибка отправки: {e}")


# ----------------------------
# Медиагруппы: сбор и отложенная отправка
# ----------------------------

async def flush_media_group(key: MediaGroupKey, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ждёт небольшую паузу и отправляет накопленные сообщения медиагруппы одним письмом.
    """
    await asyncio.sleep(MEDIA_GROUP_FLUSH_DELAY_SEC)

    group = media_groups.get(key)
    if not group:
        return

    messages: List[Message] = group.get("messages", [])  # type: ignore
    logger.info("Flush медиагруппы %s: сообщений=%d", key, len(messages))

    # Удаляем из хранилища до отправки (чтобы не было дублей)
    media_groups.pop(key, None)

    await process_messages_and_send_email(update, context, messages)


async def handle_incoming_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Основной обработчик входящих сообщений:
    - если media_group_id есть -> копим и отправляем после паузы
    - если нет -> обрабатываем сразу
    """
    msg = update.effective_message
    if not msg:
        return

    # Игнорируем сервисные сообщения
    if msg.new_chat_members or msg.left_chat_member:
        return

    chat_id = msg.chat_id
    media_group_id = msg.media_group_id

    if media_group_id:
        key: MediaGroupKey = (chat_id, str(media_group_id))
        group = media_groups.setdefault(key, {"messages": [], "flush_task": None})

        # Добавляем сообщение в список
        group["messages"].append(msg)  # type: ignore

        # Перезапускаем flush-задачу (чтобы дождаться последнего сообщения в группе)
        old_task: Optional[asyncio.Task] = group.get("flush_task")  # type: ignore
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(flush_media_group(key, update, context))
        group["flush_task"] = task

        logger.info("Сообщение добавлено в медиагруппу %s (всего %d)", key, len(group["messages"]))  # type: ignore
        return

    # Не медиагруппа — отправляем сразу
    await process_messages_and_send_email(update, context, [msg])


# ----------------------------
# Команды
# ----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Пересылай мне любые посты из Telegram, я отправлю их на твою почту."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1) Перешли мне сообщение/пост из Telegram (текст, фото, видео или комбинацию).\n"
        "2) Я скачиваю медиа, сохраняю форматирование текста и отправляю на email.\n"
        "3) После отправки я напишу: «✅ Отправлено на email!»\n\n"
        "Примечание: если вложения большие (25MB+), почта может отклонить письмо — я всё равно попробую отправить."
    )


# ----------------------------
# Глобальный обработчик ошибок
# ----------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Глобальная ошибка: %s", context.error)


# ----------------------------
# Проверка конфигурации
# ----------------------------

def validate_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not EMAIL_SENDER:
        missing.append("EMAIL_SENDER")
    if not EMAIL_PASSWORD:
        missing.append("EMAIL_PASSWORD")
    if not EMAIL_RECIPIENT:
        missing.append("EMAIL_RECIPIENT")

    if missing:
        raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------------
# main
# ----------------------------

def main() -> None:
    validate_config()

    logger.info("Запуск бота...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # Любой контент: текст и медиа
    app.add_handler(MessageHandler(filters.ALL, handle_incoming_post))

    # Ошибки
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
