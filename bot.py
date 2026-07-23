import html
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()
POLL_INTERVAL_SECONDS = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "120")))
DB_PATH = Path(os.getenv("DB_PATH", "data/streams.db"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
ADMIN_STATUSES = {"creator", "owner", "administrator"}


@dataclass
class LiveStream:
    stream_id: str
    title: str
    url: str
    game_name: str | None = None
    thumbnail_url: str | None = None


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                configured_by INTEGER NOT NULL,
                notification_template TEXT NOT NULL DEFAULT '🔴 Новые эфиры: {count}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                platform TEXT NOT NULL CHECK(platform IN ('twitch', 'youtube')),
                channel_key TEXT NOT NULL,
                channel_name TEXT NOT NULL,
                channel_url TEXT NOT NULL,
                initialized INTEGER NOT NULL DEFAULT 0,
                active_stream_id TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, platform, channel_key),
                FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_access (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY(chat_id, user_id),
                FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );
            """
        )
        chat_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(chats)").fetchall()
        }
        if "notification_template" not in chat_columns:
            self.connection.execute(
                """
                ALTER TABLE chats
                ADD COLUMN notification_template TEXT NOT NULL
                DEFAULT '🔴 Новые эфиры: {count}'
                """
            )
        self.connection.commit()

    def connect_chat(self, chat_id: int, title: str, user_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO chats(chat_id, title, configured_by)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                configured_by = excluded.configured_by
            """,
            (chat_id, title, user_id),
        )
        self.connection.execute(
            """
            INSERT OR IGNORE INTO chat_access(chat_id, user_id)
            VALUES (?, ?)
            """,
            (chat_id, user_id),
        )
        self.connection.commit()

    def is_configured(self, chat_id: int) -> bool:
        return bool(
            self.connection.execute(
                "SELECT 1 FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        )

    def add_subscription(
        self,
        chat_id: int,
        platform: str,
        channel_key: str,
        channel_name: str,
        channel_url: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO subscriptions(
                chat_id, platform, channel_key, channel_name, channel_url
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, platform, channel_key, channel_name, channel_url),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_subscriptions(self, chat_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT id, platform, channel_name, channel_url, initialized, active_stream_id
            FROM subscriptions
            WHERE chat_id = ?
            ORDER BY platform, channel_name COLLATE NOCASE
            """,
            (chat_id,),
        ).fetchall()

    def list_user_chats(self, user_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT chats.chat_id, chats.title
            FROM chats
            INNER JOIN chat_access ON chat_access.chat_id = chats.chat_id
            WHERE chat_access.user_id = ?
            ORDER BY chats.title COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()

    def user_can_access_chat(self, user_id: int, chat_id: int) -> bool:
        return bool(
            self.connection.execute(
                """
                SELECT 1 FROM chat_access
                WHERE user_id = ? AND chat_id = ?
                """,
                (user_id, chat_id),
            ).fetchone()
        )

    def get_notification_template(self, chat_id: int) -> str:
        row = self.connection.execute(
            "SELECT notification_template FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row["notification_template"] if row else "🔴 Новые эфиры: {count}"

    def set_notification_template(self, chat_id: int, template: str) -> None:
        self.connection.execute(
            "UPDATE chats SET notification_template = ? WHERE chat_id = ?",
            (template, chat_id),
        )
        self.connection.commit()

    def list_user_subscriptions(self, user_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT
                subscriptions.id,
                subscriptions.platform,
                subscriptions.channel_name,
                subscriptions.channel_url,
                subscriptions.active_stream_id,
                chats.title AS chat_title
            FROM subscriptions
            INNER JOIN chats ON chats.chat_id = subscriptions.chat_id
            INNER JOIN chat_access ON chat_access.chat_id = chats.chat_id
            WHERE chat_access.user_id = ?
            ORDER BY chats.title COLLATE NOCASE, subscriptions.platform,
                     subscriptions.channel_name COLLATE NOCASE
            """,
            (user_id,),
        ).fetchall()

    def get_all_subscriptions(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM subscriptions ORDER BY id"
        ).fetchall()

    def remove_subscription(self, chat_id: int, subscription_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM subscriptions WHERE id = ? AND chat_id = ?",
            (subscription_id, chat_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def remove_user_subscription(self, user_id: int, subscription_id: int) -> bool:
        cursor = self.connection.execute(
            """
            DELETE FROM subscriptions
            WHERE id = ?
              AND chat_id IN (
                  SELECT chat_id FROM chat_access WHERE user_id = ?
              )
            """,
            (subscription_id, user_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def set_state(
        self,
        subscription_id: int,
        *,
        initialized: bool = True,
        active_stream_id: str | None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE subscriptions
            SET initialized = ?, active_stream_id = ?
            WHERE id = ?
            """,
            (int(initialized), active_stream_id, subscription_id),
        )
        self.connection.commit()


class StreamProviders:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        self.twitch_access_token: str | None = None
        self.twitch_token_expires_at = 0.0

    async def close(self) -> None:
        await self.client.aclose()

    async def _twitch_token(self) -> str:
        if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
            raise RuntimeError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET не заданы")

        if (
            self.twitch_access_token
            and time.time() < self.twitch_token_expires_at - 60
        ):
            return self.twitch_access_token

        response = await self.client.post(
            TWITCH_TOKEN_URL,
            data={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()
        payload = response.json()
        self.twitch_access_token = payload["access_token"]
        self.twitch_token_expires_at = time.time() + int(payload["expires_in"])
        return self.twitch_access_token

    async def twitch_live(self, login: str) -> LiveStream | None:
        token = await self._twitch_token()
        response = await self.client.get(
            TWITCH_STREAMS_URL,
            params={"user_login": login},
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": TWITCH_CLIENT_ID,
            },
        )
        response.raise_for_status()
        streams = response.json().get("data", [])
        if not streams:
            return None

        stream = streams[0]
        return LiveStream(
            stream_id=stream["id"],
            title=stream.get("title") or "Без названия",
            url=f"https://www.twitch.tv/{stream['user_login']}",
            game_name=stream.get("game_name") or None,
            thumbnail_url=(stream.get("thumbnail_url") or "")
            .replace("{width}", "1280")
            .replace("{height}", "720")
            or None,
        )

    async def youtube_channel_id(self, url: str) -> tuple[str, str, str]:
        if not YOUTUBE_API_KEY:
            raise RuntimeError("YOUTUBE_API_KEY не задан")

        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        if host not in {"youtube.com", "m.youtube.com", "youtu.be"}:
            raise ValueError("Нужна ссылка на канал YouTube")

        path = parsed.path.strip("/")
        channel_id = None
        handle = None
        if path.startswith("channel/"):
            channel_id = path.split("/", 1)[1]
        elif path.startswith("@"):
            handle = path.split("/", 1)[0]
        else:
            raise ValueError(
                "Поддерживаются ссылки вида youtube.com/channel/UC... "
                "или youtube.com/@название"
            )

        params = {"part": "snippet", "key": YOUTUBE_API_KEY}
        if channel_id:
            params["id"] = channel_id
        else:
            params["forHandle"] = handle

        response = await self.client.get(YOUTUBE_CHANNELS_URL, params=params)
        response.raise_for_status()
        channels = response.json().get("items", [])
        if not channels:
            raise ValueError("Канал YouTube не найден")

        channel = channels[0]
        resolved_id = channel["id"]
        title = channel["snippet"]["title"]
        return resolved_id, title, f"https://www.youtube.com/channel/{resolved_id}"

    async def youtube_live(self, channel_id: str) -> LiveStream | None:
        if not YOUTUBE_API_KEY:
            raise RuntimeError("YOUTUBE_API_KEY не задан")

        response = await self.client.get(
            YOUTUBE_SEARCH_URL,
            params={
                "part": "snippet",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 1,
                "key": YOUTUBE_API_KEY,
            },
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        if not items:
            return None

        item = items[0]
        video_id = item["id"]["videoId"]
        snippet = item["snippet"]
        thumbnails = snippet.get("thumbnails", {})
        thumbnail = (
            thumbnails.get("high")
            or thumbnails.get("medium")
            or thumbnails.get("default")
            or {}
        ).get("url")
        return LiveStream(
            stream_id=video_id,
            title=snippet.get("title") or "Без названия",
            url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=thumbnail,
        )


def parse_twitch_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"twitch.tv", "m.twitch.tv"}:
        raise ValueError("Нужна ссылка вида https://twitch.tv/название")

    login = parsed.path.strip("/").split("/", 1)[0].lower()
    if not re.fullmatch(r"[a-z0-9_]{4,25}", login):
        raise ValueError("Не удалось определить канал Twitch из ссылки")
    return login, login, f"https://www.twitch.tv/{login}"


def help_text() -> str:
    return (
        "Я сообщаю о начале Twitch и YouTube-эфиров.\n\n"
        "1. Добавь меня администратором в нужный канал или группу. "
        "Ничего писать там не нужно.\n"
        "2. Открой личку со мной и добавь канал:\n"
        "/add twitch <ссылка>\n"
        "/add youtube <ссылка>\n"
        "3. Выбери канал или группу кнопкой — туда придёт уведомление.\n\n"
        "В личке доступны:\n"
        "/chats — подключённые каналы и группы\n"
        "/list — отслеживаемые каналы\n"
        "/remove <номер> — удалить канал\n"
        "/check — проверить свои каналы сейчас\n\n"
        "/template <текст> — изменить заголовок уведомления для канала или группы\n"
        "В тексте можно использовать {count} — число новых эфиров.\n\n"
        "Первый опрос только запоминает текущий статус: уже идущий эфир "
        "не вызовет уведомление. Следующий новый эфир — вызовет."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(help_text())


async def track_connected_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Запоминает чат, когда пользователя добавляет в него самого бота."""
    change = update.my_chat_member
    if not change or change.new_chat_member.status not in ADMIN_STATUSES:
        return

    chat = change.chat
    actor = change.from_user
    database: Database = context.application.bot_data["database"]
    database.connect_chat(chat.id, chat.title or str(chat.id), actor.id)
    logger.info(
        "Бот подключён к чату chat_id=%s пользователем user_id=%s",
        chat.id,
        actor.id,
    )


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if chat.type != "private":
        await message.reply_text(
            "Каналы добавляются в личке с ботом."
        )
        return

    database: Database = context.application.bot_data["database"]
    providers: StreamProviders = context.application.bot_data["providers"]

    if len(context.args) != 2:
        await message.reply_text("Формат: /add twitch <ссылка> или /add youtube <ссылка>")
        return

    platform, url = context.args[0].lower(), context.args[1]
    platform = {"twich": "twitch"}.get(platform, platform)
    try:
        if platform == "twitch":
            channel_key, channel_name, channel_url = parse_twitch_url(url)
        elif platform == "youtube":
            channel_key, channel_name, channel_url = await providers.youtube_channel_id(url)
        else:
            raise ValueError("Платформа должна быть twitch или youtube")
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        logger.warning("Не удалось добавить канал: %s", error)
        await message.reply_text(f"Не удалось добавить канал: {error}")
        return

    chats = database.list_user_chats(user.id)
    if not chats:
        await message.reply_text(
            "Добавь бота администратором в нужный канал или группу, "
            "затем подожди несколько секунд и повтори /add."
        )
        return

    context.user_data["pending_subscription"] = {
        "platform": platform,
        "channel_key": channel_key,
        "channel_name": channel_name,
        "channel_url": channel_url,
    }
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"target_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    await message.reply_text(
        f"Канал «{channel_name}» добавляется. Выбери чат для уведомлений:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Список доступен в личке с ботом.")
        return

    database: Database = context.application.bot_data["database"]
    subscriptions = database.list_user_subscriptions(update.effective_user.id)
    if not subscriptions:
        await update.effective_message.reply_text(
            "Подписок пока нет. Добавь канал: /add twitch <ссылка>"
        )
        return

    lines = ["Отслеживаемые каналы:"]
    for subscription in subscriptions:
        status = "в эфире" if subscription["active_stream_id"] else "офлайн"
        lines.append(
            f"#{subscription['id']} · {html.escape(subscription['chat_title'])} · "
            f"{subscription['platform']} · "
            f"<a href=\"{html.escape(subscription['channel_url'], quote=True)}\">"
            f"{html.escape(subscription['channel_name'])}</a> · {status}"
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Удаление подписок доступно в личке с ботом.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Формат: /remove <номер из /list>")
        return

    database: Database = context.application.bot_data["database"]
    removed = database.remove_user_subscription(
        update.effective_user.id, int(context.args[0])
    )
    await update.effective_message.reply_text(
        "Подписка удалена." if removed else "Подписка с таким номером не найдена."
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Проверка доступна в личке с ботом.")
        return
    database: Database = context.application.bot_data["database"]
    subscriptions = database.list_user_subscriptions(update.effective_user.id)
    if not subscriptions:
        await update.effective_message.reply_text("У тебя нет доступных подписок.")
        return

    await update.effective_message.reply_text("Проверяю каналы…")
    results = await check_streams(
        context.application,
        only_subscription_ids={subscription["id"] for subscription in subscriptions},
    )
    await update.effective_message.reply_text(
        "Результат проверки:\n" + "\n".join(results),
        disable_web_page_preview=True,
    )


async def chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Список чатов доступен в личке с ботом.")
        return

    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await update.effective_message.reply_text(
            "Нет подключённых чатов. Добавь бота администратором в канал "
            "или группу, затем подожди несколько секунд."
        )
        return
    await update.effective_message.reply_text(
        "Подключённые группы:\n" + "\n".join(f"• {chat['title']}" for chat in chats)
    )


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Настройка текста уведомления доступна в личке с ботом."
        )
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Формат: /template <текст>\n\n"
            "Это заголовок уведомления. Можно использовать {count} — "
            "число начавшихся эфиров.\n"
            "Пример: /template 🔴 В эфире прямо сейчас: {count}"
        )
        return

    template = " ".join(context.args).strip()
    if len(template) > 300:
        await update.effective_message.reply_text(
            "Текст слишком длинный: максимум 300 символов."
        )
        return

    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await update.effective_message.reply_text("Нет доступных каналов или групп.")
        return

    context.user_data["pending_template"] = template
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"template_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    await update.effective_message.reply_text(
        "Выбери канал или группу, для которых изменить заголовок уведомления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def select_target_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_subscription")
    if not pending or not query.data.startswith("target_chat:"):
        await query.edit_message_text("Эта кнопка уже неактуальна. Повтори /add.")
        return

    try:
        chat_id = int(query.data.removeprefix("target_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори /add.")
        return

    database: Database = context.application.bot_data["database"]
    user_id = update.effective_user.id
    if not database.user_can_access_chat(user_id, chat_id):
        await query.edit_message_text(
            "Нет доступа к этому чату. Добавь бота в него администратором заново."
        )
        return

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ADMIN_STATUSES:
            await query.edit_message_text(
                "Ты больше не администратор этого чата, поэтому выбрать его нельзя."
            )
            return
        subscription_id = database.add_subscription(chat_id=chat_id, **pending)
    except sqlite3.IntegrityError:
        await query.edit_message_text("Этот канал уже отслеживается в выбранном чате.")
        return
    except Exception as error:
        logger.warning("Не удалось создать подписку: %s", error)
        await query.edit_message_text(f"Не удалось добавить канал: {error}")
        return

    context.user_data.pop("pending_subscription", None)
    await query.edit_message_text(
        f"Готово: #{subscription_id} — {pending['channel_name']}.\n"
        "Первый опрос только запомнит текущий статус эфира."
    )


async def select_template_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    template = context.user_data.get("pending_template")
    if template is None or not query.data.startswith("template_chat:"):
        await query.edit_message_text("Эта кнопка уже неактуальна. Повтори /template.")
        return

    try:
        chat_id = int(query.data.removeprefix("template_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори /template.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.set_notification_template(chat_id, template)
    context.user_data.pop("pending_template", None)
    await query.edit_message_text(
        "Заголовок уведомления сохранён.\n"
        f"Предпросмотр: {template.replace('{count}', '2')}"
    )


async def fetch_live_stream(
    providers: StreamProviders, subscription: sqlite3.Row
) -> LiveStream | None:
    if subscription["platform"] == "twitch":
        return await providers.twitch_live(subscription["channel_key"])
    if subscription["platform"] == "youtube":
        return await providers.youtube_live(subscription["channel_key"])
    raise RuntimeError(f"Неизвестная платформа {subscription['platform']}")


async def send_live_notification(
    application: Application,
    chat_id: int,
    notifications: list[tuple[sqlite3.Row, LiveStream]],
    template: str,
) -> None:
    header = html.escape(template.replace("{count}", str(len(notifications))))
    lines = [f"<b>{header}</b>"]
    hidden_count = 0
    for subscription, stream in notifications:
        platform = "Twitch" if subscription["platform"] == "twitch" else "YouTube"
        title = html.escape(stream.title[:160] + ("…" if len(stream.title) > 160 else ""))
        details = (
            f"\nКатегория: {html.escape(stream.game_name[:80])}"
            if stream.game_name
            else ""
        )
        line = (
            f"• <b>{platform} — {html.escape(subscription['channel_name'])}</b>\n"
            f"<a href=\"{html.escape(stream.url, quote=True)}\">{title}</a>{details}"
        )
        if len("\n\n".join(lines + [line])) > 950:
            hidden_count += 1
            continue
        lines.append(line)

    if hidden_count:
        lines.append(f"…и ещё {hidden_count}.")
    text = "\n\n".join(lines)
    thumbnail_url = notifications[0][1].thumbnail_url

    if thumbnail_url:
        try:
            await application.bot.send_photo(
                chat_id=chat_id,
                photo=thumbnail_url,
                caption=text,
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as error:
            logger.warning(
                "Не удалось отправить превью в chat_id=%s, отправляю текст: %s",
                chat_id,
                error,
            )

    await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def check_streams(
    application: Application,
    *,
    only_subscription_ids: set[int] | None = None,
) -> list[str]:
    database: Database = application.bot_data["database"]
    providers: StreamProviders = application.bot_data["providers"]
    subscriptions = database.get_all_subscriptions()
    results = []
    pending_notifications: dict[int, list[tuple[sqlite3.Row, LiveStream]]] = {}

    for subscription in subscriptions:
        if (
            only_subscription_ids is not None
            and subscription["id"] not in only_subscription_ids
        ):
            continue

        try:
            stream = await fetch_live_stream(providers, subscription)
        except (RuntimeError, ValueError, httpx.HTTPError, KeyError) as error:
            logger.warning(
                "Проверка %s/%s не удалась: %s",
                subscription["platform"],
                subscription["channel_key"],
                error,
            )
            results.append(
                f"⚠ #{subscription['id']} {subscription['platform']} "
                f"{subscription['channel_name']}: ошибка API — {error}"
            )
            continue

        stream_id = stream.stream_id if stream else None
        if stream:
            results.append(
                f"🔴 #{subscription['id']} {subscription['platform']} "
                f"{subscription['channel_name']}: эфир найден — {stream.title}"
            )
        else:
            results.append(
                f"⚪ #{subscription['id']} {subscription['platform']} "
                f"{subscription['channel_name']}: API не нашёл активный эфир"
            )

        if not subscription["initialized"]:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=stream_id,
            )
            logger.info(
                "Первичный статус %s/%s: %s",
                subscription["platform"],
                subscription["channel_key"],
                "в эфире" if stream else "офлайн",
            )
            continue

        if stream and stream_id != subscription["active_stream_id"]:
            pending_notifications.setdefault(subscription["chat_id"], []).append(
                (subscription, stream)
            )
        elif not stream and subscription["active_stream_id"] is not None:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=None,
            )

    for chat_id, notifications in pending_notifications.items():
        try:
            await send_live_notification(
                application,
                chat_id,
                notifications,
                database.get_notification_template(chat_id),
            )
        except Exception as error:
            logger.warning(
                "Не удалось отправить уведомление в chat_id=%s: %s",
                chat_id,
                error,
            )
            continue

        for subscription, stream in notifications:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=stream.stream_id,
            )
            logger.info(
                "Отправлено уведомление: %s/%s",
                subscription["platform"],
                subscription["channel_key"],
            )

    return results


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_streams(context.application)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Необработанная ошибка Telegram: %s", context.error, exc_info=context.error)


async def post_init(application: Application) -> None:
    application.job_queue.run_repeating(
        scheduled_check,
        interval=POLL_INTERVAL_SECONDS,
        first=10,
        name="stream-status-check",
    )
    logger.info("Проверка стримов каждые %d секунд", POLL_INTERVAL_SECONDS)


async def post_shutdown(application: Application) -> None:
    providers: StreamProviders = application.bot_data["providers"]
    await providers.close()


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        logger.warning("Twitch не настроен: добавь TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET")
    if not YOUTUBE_API_KEY:
        logger.warning("YouTube не настроен: добавь YOUTUBE_API_KEY")

    database = Database(DB_PATH)
    providers = StreamProviders()
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["database"] = database
    application.bot_data["providers"] = providers

    application.add_error_handler(on_error)
    application.add_handler(
        ChatMemberHandler(
            track_connected_chat,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("chats", chats_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("template", template_command))
    application.add_handler(
        CallbackQueryHandler(select_target_chat, pattern=r"^target_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_template_chat, pattern=r"^template_chat:")
    )

    logger.info("Бот уведомлений запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
