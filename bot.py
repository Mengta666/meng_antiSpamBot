import os
import logging
import sys
import time
import re
import json
import bleach
import html
import io
import traceback
import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaDocument, InputMediaVideo
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, ConversationHandler, CommandHandler, filters
from telegram.request import HTTPXRequest
from collections import defaultdict
from PIL import Image
from io import BytesIO
import mysql.connector
from mysql.connector import Error, pooling

# -------------------------------
# 全局常量定义
# -------------------------------
MESSAGE_INTERVAL = 10  # 每个用户间隔 10 秒
MAX_REPLY_LENGTH = 500
MAX_MESSAGE_LENGTH = 500
WAITING_REPLY = 1

# -------------------------------
# 日志配置
# -------------------------------
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(
    filename="/var/log/telegram_bots/meng_antiSpamBot.log",
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)

# -------------------------------
# 全局异常处理器
# -------------------------------
def exception_handler(type, value, tb):
    logger.error(f"Unhandled exception: {type} {value}")
    logger.error("".join(traceback.format_tb(tb)))
sys.excepthook = exception_handler

# -------------------------------
# 加载环境变量 (.env)
# -------------------------------
load_dotenv("/opt/telegram_bots/meng_antiSpamBot/config.env")
TOKEN = os.getenv("TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_API_BASE_URL = os.getenv("LLM_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_API_URL = os.getenv("LLM_API_URL") or f"{LLM_API_BASE_URL}/chat/completions"
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "15"))
SPAM_REJECT_REPLY = "检测到您的消息中可能包含垃圾信息，请修改后再发送。"
SPAM_CHECK_ERROR_REPLY = "系统暂时无法完成垃圾校验，请稍后再试。"
TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "60"))
TELEGRAM_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "30"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))

# -------------------------------
# MySQL 配置和连接池
# -------------------------------
MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")

# 设置连接池
dbconfig = {
    "host": MYSQL_HOST,
    "user": MYSQL_USER,
    "password": MYSQL_PASSWORD,
    "database": MYSQL_DATABASE,
    "pool_size": 16  # 可根据实际需求调整连接池大小
}

# 初始化连接池
connection_pool = pooling.MySQLConnectionPool(**dbconfig)

def get_db_connection():
    try:
        return connection_pool.get_connection()
    except Error as e:
        logger.error(f"数据库连接错误：{e}")
    return None

# -------------------------------
# MySQL 表和用户处理
# -------------------------------
def create_allowed_users_table():
    connection = get_db_connection()
    if connection is None:
        return
    create_table_query = """
        CREATE TABLE IF NOT EXISTS allowed_users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE,
        username VARCHAR(100) NOT NULL,
        first_name VARCHAR(100) DEFAULT NULL,
        last_name VARCHAR(100) DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB ROW_FORMAT=COMPRESSED;
    """
    try:
        cursor = connection.cursor()
        cursor.execute(create_table_query)
        connection.commit()
        cursor.close()
    except Error as e:
        logger.error(f"创建表错误：{e}")
    finally:
        connection.close()

def is_user_allowed(user_id):
    connection = get_db_connection()
    if connection is None:
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT user_id FROM allowed_users WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        cursor.close()
        return result is not None
    except Error as e:
        logger.error(f"查询 allowed_users 错误：{e}")
        return False
    finally:
        connection.close()

def add_allowed_user(user_id, username, first_name, last_name):
    connection = get_db_connection()
    if connection is None:
        return
    try:
        cursor = connection.cursor()
        query = "INSERT INTO allowed_users (user_id, username, first_name, last_name) VALUES (%s, %s, %s, %s)"
        cursor.execute(query, (user_id, username or "", first_name, last_name))
        connection.commit()
        cursor.close()
    except Error as e:
        logger.error(f"添加 allowed_user 错误：{e}")
    finally:
        connection.close()

def get_known_username(user_id, username=None):
    if username:
        return username

    connection = get_db_connection()
    if connection is None:
        return None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT username FROM allowed_users WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        cursor.close()
        if result and result[0]:
            return result[0]
    except Error as e:
        logger.error(f"查询已知 username 错误：{e}")
    finally:
        connection.close()
    return None

def create_message_history_table():
    connection = get_db_connection()
    if connection is None:
        return
    create_table_query = """
        CREATE TABLE IF NOT EXISTS message_history (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id BIGINT NOT NULL,
        username VARCHAR(100) DEFAULT NULL,
        message_type VARCHAR(20) NOT NULL DEFAULT 'text',
        message_text TEXT NOT NULL,
        file_id TEXT NULL,
        file_unique_id VARCHAR(255) DEFAULT NULL,
        caption TEXT NULL,
        file_name VARCHAR(255) DEFAULT NULL,
        mime_type VARCHAR(100) DEFAULT NULL,
        file_size BIGINT DEFAULT NULL,
        width INT DEFAULT NULL,
        height INT DEFAULT NULL,
        duration INT DEFAULT NULL,
        emoji VARCHAR(64) DEFAULT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_message_history_username_created (username, created_at),
        INDEX idx_message_history_user_id_created (user_id, created_at)
        ) ENGINE=InnoDB ROW_FORMAT=COMPRESSED;
    """
    try:
        cursor = connection.cursor()
        cursor.execute(create_table_query)
        connection.commit()
        cursor.close()
    except Error as e:
        logger.error(f"创建 message_history 表错误：{e}")
    finally:
        connection.close()

def ensure_message_history_columns():
    columns = {
        "file_id": "TEXT NULL",
        "file_unique_id": "VARCHAR(255) DEFAULT NULL",
        "caption": "TEXT NULL",
        "file_name": "VARCHAR(255) DEFAULT NULL",
        "mime_type": "VARCHAR(100) DEFAULT NULL",
        "file_size": "BIGINT DEFAULT NULL",
        "width": "INT DEFAULT NULL",
        "height": "INT DEFAULT NULL",
        "duration": "INT DEFAULT NULL",
        "emoji": "VARCHAR(64) DEFAULT NULL",
    }
    connection = get_db_connection()
    if connection is None:
        return

    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
            AND TABLE_NAME = 'message_history'
            """
        )
        existing_columns = {row[0] for row in cursor.fetchall()}
        for column_name, column_type in columns.items():
            if column_name not in existing_columns:
                cursor.execute(f"ALTER TABLE message_history ADD COLUMN {column_name} {column_type}")
        connection.commit()
        cursor.close()
    except Error as e:
        logger.error(f"升级 message_history 表结构错误：{e}")
    finally:
        connection.close()

def build_history_text(message_type, text=None, caption=None, file_name=None, emoji=None):
    labels = {
        "text": "文本",
        "photo": "图片",
        "sticker": "贴图",
        "video": "视频",
        "document": "文件",
    }
    label = labels.get(message_type, message_type)
    if message_type == "text":
        return (text or "").strip()

    details = []
    if file_name:
        details.append(file_name)
    if emoji:
        details.append(emoji)
    if caption:
        details.append(f"caption: {caption}")
    return f"[{label}] " + " | ".join(details) if details else f"[{label}]"

def to_optional_int(value):
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds())
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def add_message_history(
    user_id,
    username,
    message_text,
    message_type="text",
    file_id=None,
    file_unique_id=None,
    caption=None,
    file_name=None,
    mime_type=None,
    file_size=None,
    width=None,
    height=None,
    duration=None,
    emoji=None,
):
    message_text = (message_text or "").strip()
    if not message_text:
        return

    lookup_username = get_known_username(user_id, username)
    connection = get_db_connection()
    if connection is None:
        return
    try:
        cursor = connection.cursor()
        query = """
            INSERT INTO message_history (
                user_id, username, message_type, message_text,
                file_id, file_unique_id, caption, file_name, mime_type,
                file_size, width, height, duration, emoji
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            query,
            (
                user_id,
                lookup_username,
                message_type,
                message_text,
                file_id,
                file_unique_id,
                caption or None,
                file_name or None,
                mime_type or None,
                to_optional_int(file_size),
                to_optional_int(width),
                to_optional_int(height),
                to_optional_int(duration),
                emoji or None,
            ),
        )
        connection.commit()
        cursor.close()
    except Error as e:
        logger.error(f"添加 message_history 错误：{e}")
    finally:
        connection.close()

def get_message_history(user_id, username, limit=5):
    lookup_username = get_known_username(user_id, username)
    connection = get_db_connection()
    if connection is None:
        return []

    try:
        cursor = connection.cursor()
        rows = []
        if lookup_username:
            cursor.execute(
                """
                SELECT message_text FROM (
                    SELECT id, created_at, message_text
                    FROM message_history
                    WHERE username = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                ) recent
                ORDER BY created_at ASC, id ASC
                """,
                (lookup_username, limit),
            )
            rows = cursor.fetchall()

        if not rows:
            cursor.execute(
                """
                SELECT message_text FROM (
                    SELECT id, created_at, message_text
                    FROM message_history
                    WHERE user_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                ) recent
                ORDER BY created_at ASC, id ASC
                """,
                (user_id, limit),
            )
            rows = cursor.fetchall()

        cursor.close()
        return [row[0] for row in rows]
    except Error as e:
        logger.error(f"查询 message_history 错误：{e}")
        return []
    finally:
        connection.close()

def format_message_history(user_id, username, limit=5):
    messages = get_message_history(user_id, username, limit)
    return "\n".join([f"{i+1}. {msg}" for i, msg in enumerate(messages)])

# 在启动时创建表
create_allowed_users_table()
create_message_history_table()
ensure_message_history_columns()

# -------------------------------
# 其他安全性函数
# -------------------------------
def is_safe_input(text):
    allowed_tags = ['b', 'i', 'u', 'strong', 'em', 'a']
    allowed_attributes = {'a': ['href', 'title']}
    clean_text = bleach.clean(text, tags=allowed_tags, attributes=allowed_attributes)
    return clean_text

def escape_user_input(text):
    return html.escape(text)

def is_valid_command(command):
    if len(command) > 50:
        return False
    return re.match(r'^[a-zA-Z0-9_]+$', command) is not None

def is_safe_url(url):
    unsafe_protocols = ["javascript:", "data:", "file:"]
    return not any(url.startswith(protocol) for protocol in unsafe_protocols)

def extract_json_object(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("LLM response does not contain a JSON object")
    return text[start:end + 1]

async def llm_spam_check(message_content, message_type, user_history):
    if not LLM_API_KEY:
        logger.error("LLM_API_KEY 或 OPENAI_API_KEY 未配置，无法进行垃圾校验")
        return None, "LLM API key is missing"

    message_content = (message_content or "").strip()
    user_history = (user_history or "").strip()
    prompt = (
        "请判断下面 Telegram 私聊消息是否属于垃圾信息、诈骗、钓鱼、广告引流、"
        "恶意链接、批量营销、色情招嫖、虚假福利、账号验证诈骗或明显骚扰。\n"
        "只返回 JSON，不要返回额外解释。格式："
        '{"is_spam": true 或 false, "reason": "简短原因"}\n\n'
        f"消息类型: {message_type}\n"
        f"消息内容或说明: {message_content[:2000] or '[无文本内容]'}\n"
        f"该用户最近历史消息: {user_history[:2000] or '[无历史消息]'}"
    )
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "你是垃圾信息审核器。宁可拦截明显可疑内容，但不要拦截正常咨询、正常联系方式或普通聊天。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT) as client:
            response = await client.post(LLM_API_URL, headers=headers, json=payload)
            response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        result = json.loads(extract_json_object(content))
        is_spam = result.get("is_spam")
        if not isinstance(is_spam, bool):
            raise ValueError("LLM response JSON missing boolean is_spam")
        return is_spam, result.get("reason", "")
    except Exception as e:
        logger.error(f"LLM 垃圾校验失败：{e}")
        return None, str(e)

async def verify_message_not_spam(update, message_content, message_type, user_id, username):
    user_history = format_message_history(user_id, username)
    is_spam, reason = await llm_spam_check(message_content, message_type, user_history)
    if is_spam is None:
        await update.message.reply_text(SPAM_CHECK_ERROR_REPLY)
        return False
    if is_spam:
        logger.warning(f"LLM 拦截垃圾消息: 用户 {user_id}, 类型 {message_type}, 原因: {reason}")
        await update.message.reply_text(SPAM_REJECT_REPLY)
        return False
    return True

user_attempts = defaultdict(int)
user_last_attempt = defaultdict(float)

def check_brute_force(user_id):
    current_time = time.time()
    if current_time - user_last_attempt[user_id] < 1:
        user_attempts[user_id] += 1
    else:
        user_attempts[user_id] = 1
    user_last_attempt[user_id] = current_time
    return user_attempts[user_id] <= 5

# -------------------------------
# 安全图片验证函数
# -------------------------------
def is_safe_image(file_data):
    try:
        image = Image.open(io.BytesIO(file_data))
        image.verify()
        
        if image.format not in ['JPEG', 'PNG']:
            return False, "图片格式不支持，仅允许 JPEG 和 PNG 图片。"
        
        if len(file_data) > 20 * 1024 * 1024:  # 20MB
            return False, "图片文件太大，最大允许大小为 20MB。"
        
        width, height = image.size
        if width < 100 or height < 100:
            return False, "图片分辨率太低，必须大于 100x100 像素。"

        exif_data = image._getexif()
        if exif_data:
            for tag, value in exif_data.items():
                if isinstance(value, str) and ("<script>" in value or "javascript:" in value):
                    return False, "图片包含恶意脚本代码，已被拒绝。"
        
        return True, "图片验证通过。"
    except (IOError, SyntaxError) as e:
        return False, "图片无效或损坏，无法识别。"
    except Exception as e:
        return False, f"图片检查时发生错误: {str(e)}"

# -------------------------------
# 内存数据：用户消息时间戳及历史记录
# -------------------------------
user_message_times = {}
user_message_times_photo = {}
user_message_times_sticker = {}

# -------------------------------
# 消息处理函数
# -------------------------------

# 记录并发送消息到管理员
async def log_and_send_to_admin(update, context, media, user_link, message_time, user_history, media_type, user_id):
    caption = f"\n用户：{user_link}\n时间: {message_time}\n\n用户的历史消息：\n{user_history}"
    keyboard = [[InlineKeyboardButton("------------回复------------", callback_data=f"reply_{user_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if media_type == 'photo':
        media = InputMediaPhoto(media=media, caption=caption, parse_mode="Markdown")
    elif media_type == 'sticker':
        await context.bot.send_sticker(chat_id=OWNER_ID, sticker=media),
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"\n用户：{user_link} \n时间: {message_time}\n\n用户的历史消息：\n{user_history}\n点击下方按钮进行回复",
            reply_markup=reply_markup
        )
    elif media_type == 'video':
        media = InputMediaVideo(media=media, caption=caption, parse_mode="Markdown")
    elif media_type == 'document':
        media = InputMediaDocument(media=media, caption=caption, parse_mode="Markdown")
    
    # 发送媒体到管理员
    if media_type != 'sticker':
        await context.bot.send_media_group(chat_id=OWNER_ID, media=[media])
        # 发送带回复按钮的消息
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text="点击下方按钮进行回复",
            reply_markup=reply_markup
        )

# 时间间隔检查
async def is_message_interval_ok(user_id, message_times):
    current_time = time.time()
    last_message_time = message_times.get(user_id, 0)
    if current_time - last_message_time < MESSAGE_INTERVAL:
        return False
    message_times[user_id] = current_time
    return True

# 处理消息
async def handle_message(update, context):
    user_id = update.message.chat_id
    username = update.message.from_user.username
    first_name = update.message.from_user.first_name or ""
    last_name = update.message.from_user.last_name or ""
    message_text = update.message.text
    message_caption = update.message.caption or ""
    message_time = update.message.date.strftime("%Y-%m-%d %H:%M:%S")

    # 用户链接
    if username:
        user_link = f"[{username}](https://t.me/{username})"
    else:
        user_link = f"[{first_name} {last_name}](tg://user?id={user_id})"

    logger.info(f"处理消息: 用户 {user_id}, 内容: {message_text}")

    # 处理图片消息
    if update.message.photo:
        if not is_user_allowed(user_id):
            await update.message.reply_text("第一次只能发文本消息！")
            return
        photo = update.message.photo[-1]
        file = await photo.get_file()
        file_data = await file.download_as_bytearray()
        is_safe, msg = is_safe_image(file_data)
        if not is_safe:
            await update.message.reply_text(msg)
            return

        if not await verify_message_not_spam(update, message_caption, "photo", user_id, username):
            return

        if not await is_message_interval_ok(user_id, user_message_times_photo):
            await update.message.reply_text("请稍等一会儿再发送图片（10秒）。🤖")
            return

        user_message_times_photo[user_id] = time.time()
        message_link = photo.file_id
        add_message_history(
            user_id,
            username,
            build_history_text("photo", caption=message_caption),
            message_type="photo",
            file_id=getattr(photo, "file_id", None),
            file_unique_id=getattr(photo, "file_unique_id", None),
            caption=message_caption,
            file_size=getattr(photo, "file_size", None),
            width=getattr(photo, "width", None),
            height=getattr(photo, "height", None),
        )
        user_history = format_message_history(user_id, username)
        
        # 调用通用函数发送消息
        await log_and_send_to_admin(update, context, message_link, user_link, message_time, user_history, 'photo', user_id)
        return

    # 处理贴图消息
    elif update.message.sticker:
        if not is_user_allowed(user_id):
            await update.message.reply_text("第一次只能发文本消息！")
            return
        sticker = update.message.sticker
        file_id = sticker.file_id
        sticker_description = getattr(sticker, "emoji", None) or "sticker"

        if not await verify_message_not_spam(update, sticker_description, "sticker", user_id, username):
            return

        if not await is_message_interval_ok(user_id, user_message_times_sticker):
            await update.message.reply_text("请稍等一会儿再发送贴图（10秒）。🤖")
            return

        user_message_times_sticker[user_id] = time.time()
        add_message_history(
            user_id,
            username,
            build_history_text("sticker", emoji=getattr(sticker, "emoji", None)),
            message_type="sticker",
            file_id=getattr(sticker, "file_id", None),
            file_unique_id=getattr(sticker, "file_unique_id", None),
            file_size=getattr(sticker, "file_size", None),
            width=getattr(sticker, "width", None),
            height=getattr(sticker, "height", None),
            emoji=getattr(sticker, "emoji", None),
        )
        user_history = format_message_history(user_id, username)
        
        # 调用通用函数发送消息
        await log_and_send_to_admin(update, context, file_id, user_link, message_time, user_history, 'sticker', user_id)
        return

    # 处理视频消息
    elif update.message.video:
        if not is_user_allowed(user_id):
            await update.message.reply_text("第一次只能发文本消息！")
            return
        video = update.message.video
        file_id = video.file_id

        if not await is_message_interval_ok(user_id, user_message_times):
            await update.message.reply_text("请稍等一会儿再发送视频（10秒）。🤖")
            return

        user_message_times[user_id] = time.time()
        add_message_history(
            user_id,
            username,
            build_history_text("video", caption=message_caption),
            message_type="video",
            file_id=getattr(video, "file_id", None),
            file_unique_id=getattr(video, "file_unique_id", None),
            caption=message_caption,
            file_name=getattr(video, "file_name", None),
            mime_type=getattr(video, "mime_type", None),
            file_size=getattr(video, "file_size", None),
            width=getattr(video, "width", None),
            height=getattr(video, "height", None),
            duration=getattr(video, "duration", None),
        )
        user_history = format_message_history(user_id, username)

        # 调用通用函数发送消息
        await log_and_send_to_admin(update, context, file_id, user_link, message_time, user_history, 'video', user_id)
        return

    # 处理文件消息
    elif update.message.document:
        if not is_user_allowed(user_id):
            await update.message.reply_text("第一次只能发文本消息！")
            return
        document = update.message.document
        file_id = document.file_id

        if not await is_message_interval_ok(user_id, user_message_times):
            await update.message.reply_text("请稍等一会儿再发送文件（10秒）。🤖")
            return

        user_message_times[user_id] = time.time()
        add_message_history(
            user_id,
            username,
            build_history_text("document", caption=message_caption, file_name=getattr(document, "file_name", None)),
            message_type="document",
            file_id=getattr(document, "file_id", None),
            file_unique_id=getattr(document, "file_unique_id", None),
            caption=message_caption,
            file_name=getattr(document, "file_name", None),
            mime_type=getattr(document, "mime_type", None),
            file_size=getattr(document, "file_size", None),
        )
        user_history = format_message_history(user_id, username)

        # 调用通用函数发送消息
        await log_and_send_to_admin(update, context, file_id, user_link, message_time, user_history, 'document', user_id)
        return

    # 处理文本消息
    elif message_text:
        clean_message = is_safe_input(message_text)
        if clean_message != message_text:
            await update.message.reply_text("您的消息包含不安全的内容，已被清除!")
            return
        
        scam_keywords = ["Spam Info Bot", "点击链接", "账号验证", "永久停用", "恢复功能", "免费领取"]
        if any(keyword in message_text for keyword in scam_keywords):
            await update.message.reply_text(SPAM_REJECT_REPLY)
            return

        if not await verify_message_not_spam(update, message_text, "text", user_id, username):
            return

        if not await is_message_interval_ok(user_id, user_message_times):
            await update.message.reply_text("请稍等一会儿再发送消息（10秒）。🤖")
            return

        user_message_times[user_id] = time.time()

        if not is_user_allowed(user_id):
            add_allowed_user(user_id, username or "", first_name, last_name)
            await update.message.reply_text("❤️❤️收到消息，我会尽快联系你！❤️❤️")

        add_message_history(user_id, username, build_history_text("text", text=message_text))
        user_history = format_message_history(user_id, username)

        keyboard = [[InlineKeyboardButton("------------回复------------", callback_data=f"reply_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"📩 收到消息：\n用户: {user_link}\n时间: {message_time}\n内容: {message_text}\n\n用户的历史消息：\n{user_history}",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    # 处理其他类型消息（例如语音、地理位置等）
    await update.message.reply_text("只能发送表情包、文件、照片、视频、文本信息！！！")

# -------------------------------
# 按钮点击和回复处理函数
# -------------------------------
async def button(update, context):
    query = update.callback_query
    data = query.data
    logger.info(f"按钮被点击，回调数据：{data}")
    try:
        if data.startswith("reply_"):
            _, user_id = data.split('_')
            user_id = int(user_id)
            if not check_brute_force(user_id):
                await query.answer("操作太频繁，请稍后再试！😃")
                return
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text="回复的消息内容："
            )
            context.user_data["user_to_reply"] = user_id
            logger.info(f"管理员 {query.from_user.id} 开始回复用户 {user_id}")
            return WAITING_REPLY
    except Exception as e:
        logger.error(f"处理按钮点击事件时发生错误: {e}")
        await query.answer("发生了错误，请稍后再试！")

async def reply_message(update, context):
    reply_text = update.message.text
    
    if reply_text is None:
        # 判断是否存在待回复的消息
        if "user_to_reply" in context.user_data:
            user_id = context.user_data.pop("user_to_reply")
            
            # 检查是什么类型文件
            if update.message.video:
                # 处理视频
                reply_video = update.message.video.file_id
                await context.bot.send_video(
                    chat_id=user_id,
                    video=reply_video,
                )
                await update.message.reply_text("回复的视频已成功发送！")
            elif update.message.photo:
                # 处理照片
                reply_photo = update.message.photo[-1].file_id
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=reply_photo,
                )
                await update.message.reply_text("回复的照片已成功发送！")
            elif update.message.sticker:
                # 处理贴图
                reply_sticker = update.message.sticker.file_id
                await context.bot.send_sticker(
                    chat_id=user_id,
                    sticker=reply_sticker,
                )
                await update.message.reply_text("回复的贴图已成功发送！")
            elif update.message.animation:
                # 处理动画
                reply_animation = update.message.animation.file_id
                await context.bot.send_animation(
                    chat_id=user_id,
                    animation=reply_animation,
                )
                await update.message.reply_text("回复的动画已成功发送！")
            elif update.message.video_note:
                # 处理视频通话截图
                reply_video_note = update.message.video_note.file_id
                await context.bot.send_document(
                    chat_id=user_id,
                    document=reply_video_note,  # 使用 document 类型发送视频通话截图
                )
                await update.message.reply_text("回复的视频通话截图已成功发送！")
            elif update.message.document:
                # 处理文件
                reply_document = update.message.document.file_id
                await context.bot.send_document(
                    chat_id=user_id,
                    document=reply_document,
                )
                await update.message.reply_text("回复的文件已成功发送！")
            else:
                # 如果既没有视频、文件、照片、贴图，也没有动画
                await update.message.reply_text("没有待回复的消息。")
            
            return ConversationHandler.END
        
    if len(reply_text) > MAX_REPLY_LENGTH:
        await update.message.reply_text(f"回复内容过长，请输入少于 {MAX_REPLY_LENGTH} 个字符的消息。")
        return
    if reply_text.strip().lower() == "/cancel":
        logger.info(f"管理员 {update.message.from_user.id} 取消了回复操作")
        del context.user_data["user_to_reply"]
        await update.message.reply_text("已取消回复。")
        return ConversationHandler.END
    if "user_to_reply" in context.user_data:
        user_id = context.user_data.pop("user_to_reply")
        await context.bot.send_message(
            chat_id=user_id,
            text=reply_text
        )
        await update.message.reply_text("回复已成功发送！")
        return ConversationHandler.END
    else:
        await update.message.reply_text("没有待回复的消息。")
        return ConversationHandler.END

async def cancel(update, context):
    """取消回复操作"""
    await update.message.reply_text("已取消回复。")
    return ConversationHandler.END

async def error_handler(update, context):
    error = context.error
    if isinstance(error, (NetworkError, TimedOut)):
        logger.warning(f"Telegram 网络异常，稍后会自动重试: {error.__class__.__name__}: {error}")
        return

    logger.error(f"处理更新时发生异常: {error}", exc_info=error)

# -------------------------------
# 初始化机器人及对话处理器
# -------------------------------

telegram_request_kwargs = {
    "connect_timeout": TELEGRAM_CONNECT_TIMEOUT,
    "read_timeout": TELEGRAM_READ_TIMEOUT,
    "write_timeout": TELEGRAM_WRITE_TIMEOUT,
    "pool_timeout": TELEGRAM_POOL_TIMEOUT,
}
telegram_request = HTTPXRequest(**telegram_request_kwargs)
telegram_updates_request = HTTPXRequest(**telegram_request_kwargs)
application = Application.builder().token(TOKEN).request(telegram_request).get_updates_request(telegram_updates_request).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(button)],
    states={ 
        WAITING_REPLY: [MessageHandler(filters.ALL & ~filters.COMMAND, reply_message)],
    },
    fallbacks=[CommandHandler('cancel', cancel)],
    conversation_timeout=300,  # 超过300秒未回复会自动退出
)

application.add_handler(conv_handler)
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
application.add_error_handler(error_handler)

logger.info("机器人已启动...")
application.run_polling(poll_interval=5)
