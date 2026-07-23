import html
from io import BytesIO
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
from PIL import Image, ImageDraw, ImageOps
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "").strip()
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "").strip()
FAST_POLL_INTERVAL_SECONDS = max(
    5, int(os.getenv("FAST_POLL_INTERVAL_SECONDS", "10"))
)
YOUTUBE_POLL_INTERVAL_SECONDS = max(
    30, int(os.getenv("YOUTUBE_POLL_INTERVAL_SECONDS", "30"))
)
COMBINE_DELAY_SECONDS = max(
    0, int(os.getenv("COMBINE_DELAY_SECONDS", "0"))
)
DB_PATH = Path(os.getenv("DB_PATH", "data/streams.db"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"
TWITCH_GAMES_URL = "https://api.twitch.tv/helix/games"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_URL = "https://api.kick.com/public/v1/channels"
ADMIN_STATUSES = {"creator", "owner", "administrator"}
MENU_ADD = "➕ Добавить канал"
MENU_SUBSCRIPTIONS = "📺 Мои подписки"
MENU_CHECK = "🔎 Проверить эфиры"
MENU_APPEARANCE = "🎨 Оформление"
MENU_HELP = "ℹ️ Помощь"
MENU_CANCEL = "↩️ Отмена"


@dataclass
class LiveStream:
    stream_id: str
    title: str
    url: str
    game_name: str | None = None
    thumbnail_url: str | None = None
    broadcaster_logo_url: str | None = None
    game_box_url: str | None = None


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
                notification_description TEXT NOT NULL DEFAULT '',
                preview_file_id TEXT,
                notification_thread_id INTEGER,
                notification_message_id INTEGER,
                notification_has_photo INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                platform TEXT NOT NULL CHECK(platform IN ('twitch', 'youtube', 'kick')),
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
        missing_columns = {
            "notification_template": (
                "TEXT NOT NULL DEFAULT '🔴 Новые эфиры: {count}'"
            ),
            "notification_description": "TEXT NOT NULL DEFAULT ''",
            "preview_file_id": "TEXT",
            "notification_thread_id": "INTEGER",
            "notification_message_id": "INTEGER",
            "notification_has_photo": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in missing_columns.items():
            if name not in chat_columns:
                self.connection.execute(
                    f"ALTER TABLE chats ADD COLUMN {name} {definition}"
                )
        subscriptions_sql = self.connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'subscriptions'
            """
        ).fetchone()["sql"]
        if "'kick'" not in subscriptions_sql:
            self.connection.execute("ALTER TABLE subscriptions RENAME TO subscriptions_old")
            self.connection.executescript(
                """
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    platform TEXT NOT NULL
                        CHECK(platform IN ('twitch', 'youtube', 'kick')),
                    channel_key TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    channel_url TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    active_stream_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, platform, channel_key),
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );
                INSERT INTO subscriptions (
                    id, chat_id, platform, channel_key, channel_name, channel_url,
                    initialized, active_stream_id, created_at
                )
                SELECT
                    id, chat_id, platform, channel_key, channel_name, channel_url,
                    initialized, active_stream_id, created_at
                FROM subscriptions_old;
                DROP TABLE subscriptions_old;
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

    def get_notification_settings(self, chat_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT notification_template, notification_description, preview_file_id,
                   notification_thread_id, notification_message_id,
                   notification_has_photo
            FROM chats WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()

    def set_notification_template(self, chat_id: int, template: str) -> None:
        self.connection.execute(
            "UPDATE chats SET notification_template = ? WHERE chat_id = ?",
            (template, chat_id),
        )
        self.connection.commit()

    def set_notification_description(self, chat_id: int, description: str) -> None:
        self.connection.execute(
            "UPDATE chats SET notification_description = ? WHERE chat_id = ?",
            (description, chat_id),
        )
        self.connection.commit()

    def set_preview_file_id(self, chat_id: int, file_id: str) -> None:
        self.connection.execute(
            "UPDATE chats SET preview_file_id = ? WHERE chat_id = ?",
            (file_id, chat_id),
        )
        self.connection.commit()

    def set_notification_thread(self, chat_id: int, thread_id: int) -> None:
        self.connection.execute(
            "UPDATE chats SET notification_thread_id = ? WHERE chat_id = ?",
            (thread_id, chat_id),
        )
        self.connection.commit()

    def set_notification_message(
        self, chat_id: int, message_id: int, *, has_photo: bool
    ) -> None:
        self.connection.execute(
            """
            UPDATE chats
            SET notification_message_id = ?, notification_has_photo = ?
            WHERE chat_id = ?
            """,
            (message_id, int(has_photo), chat_id),
        )
        self.connection.commit()

    def clear_notification_message(self, chat_id: int) -> None:
        self.connection.execute(
            """
            UPDATE chats
            SET notification_message_id = NULL, notification_has_photo = 0
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        self.connection.commit()

    def get_chat_subscriptions(self, chat_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()

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
        self.kick_access_token: str | None = None
        self.kick_token_expires_at = 0.0

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
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": TWITCH_CLIENT_ID,
        }
        response = await self.client.get(
            TWITCH_STREAMS_URL,
            params={"user_login": login},
            headers=headers,
        )
        response.raise_for_status()
        streams = response.json().get("data", [])
        if not streams:
            return None

        stream = streams[0]
        user_response = await self.client.get(
            TWITCH_USERS_URL,
            params={"id": stream["user_id"]},
            headers=headers,
        )
        user_response.raise_for_status()
        user = (user_response.json().get("data") or [{}])[0]

        game = {}
        if stream.get("game_id"):
            game_response = await self.client.get(
                TWITCH_GAMES_URL,
                params={"id": stream["game_id"]},
                headers=headers,
            )
            game_response.raise_for_status()
            game = (game_response.json().get("data") or [{}])[0]

        return LiveStream(
            stream_id=stream["id"],
            title=stream.get("title") or "Без названия",
            url=f"https://www.twitch.tv/{stream['user_login']}",
            game_name=stream.get("game_name") or None,
            thumbnail_url=(stream.get("thumbnail_url") or "")
            .replace("{width}", "1280")
            .replace("{height}", "720")
            or None,
            broadcaster_logo_url=user.get("profile_image_url") or None,
            game_box_url=(game.get("box_art_url") or "")
            .replace("{width}", "285")
            .replace("{height}", "380")
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

    async def _kick_token(self) -> str:
        if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
            raise RuntimeError("KICK_CLIENT_ID / KICK_CLIENT_SECRET не заданы")
        if self.kick_access_token and time.time() < self.kick_token_expires_at - 60:
            return self.kick_access_token

        response = await self.client.post(
            KICK_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": KICK_CLIENT_ID,
                "client_secret": KICK_CLIENT_SECRET,
            },
        )
        response.raise_for_status()
        payload = response.json()
        self.kick_access_token = payload["access_token"]
        self.kick_token_expires_at = time.time() + int(payload["expires_in"])
        return self.kick_access_token

    async def kick_channel(self, slug: str) -> tuple[str, str, str]:
        token = await self._kick_token()
        response = await self.client.get(
            KICK_CHANNELS_URL,
            params={"slug": slug},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        channels = response.json().get("data", [])
        if not channels:
            raise ValueError("Канал Kick не найден")
        channel = channels[0]
        resolved_slug = channel["slug"]
        return resolved_slug, resolved_slug, f"https://kick.com/{resolved_slug}"

    async def kick_live(self, slug: str) -> LiveStream | None:
        token = await self._kick_token()
        response = await self.client.get(
            KICK_CHANNELS_URL,
            params={"slug": slug},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        channels = response.json().get("data", [])
        if not channels:
            return None

        channel = channels[0]
        stream = channel.get("stream") or {}
        if not stream.get("is_live"):
            return None
        stream_id = stream.get("start_time")
        if not stream_id:
            raise RuntimeError("Kick API не вернул время начала эфира")
        category = channel.get("category") or {}
        return LiveStream(
            stream_id=str(stream_id),
            title=channel.get("stream_title") or "Без названия",
            url=f"https://kick.com/{channel['slug']}",
            game_name=category.get("name") or None,
            thumbnail_url=stream.get("thumbnail") or None,
        )

    async def _download_image(self, url: str) -> Image.Image:
        response = await self.client.get(url)
        response.raise_for_status()
        return Image.open(BytesIO(response.content)).convert("RGB")

    async def twitch_notification_preview(self, stream: LiveStream) -> BytesIO | None:
        """Создаёт карточку из превью эфира, аватара стримера и обложки игры."""
        if not stream.thumbnail_url:
            return None
        try:
            background = await self._download_image(stream.thumbnail_url)
            card = ImageOps.fit(background, (1280, 720), method=Image.Resampling.LANCZOS)
            overlay = Image.new("RGBA", card.size, (0, 0, 0, 85))
            card = Image.alpha_composite(card.convert("RGBA"), overlay)

            if stream.broadcaster_logo_url:
                avatar = await self._download_image(stream.broadcaster_logo_url)
                avatar = ImageOps.fit(avatar, (150, 150), method=Image.Resampling.LANCZOS)
                mask = Image.new("L", (150, 150), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 150, 150), fill=255)
                card.paste(avatar, (48, 522), mask)

            if stream.game_box_url:
                game = await self._download_image(stream.game_box_url)
                game.thumbnail((150, 190), Image.Resampling.LANCZOS)
                x = 1280 - game.width - 48
                y = 720 - game.height - 40
                card.paste(game, (x, y))

            result = BytesIO()
            result.name = "twitch-preview.jpg"
            card.convert("RGB").save(result, format="JPEG", quality=90, optimize=True)
            result.seek(0)
            return result
        except (httpx.HTTPError, OSError, ValueError) as error:
            logger.warning("Не удалось создать карточку Twitch: %s", error)
            return None


def parse_twitch_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host not in {"twitch.tv", "m.twitch.tv"}:
        raise ValueError("Нужна ссылка вида https://twitch.tv/название")

    login = parsed.path.strip("/").split("/", 1)[0].lower()
    if not re.fullmatch(r"[a-z0-9_]{4,25}", login):
        raise ValueError("Не удалось определить канал Twitch из ссылки")
    return login, login, f"https://www.twitch.tv/{login}"


def parse_kick_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host != "kick.com":
        raise ValueError("Нужна ссылка вида https://kick.com/название")
    slug = parsed.path.strip("/").split("/", 1)[0].lower()
    if not re.fullmatch(r"[a-z0-9-]{1,25}", slug):
        raise ValueError("Не удалось определить канал Kick из ссылки")
    return slug


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_ADD, MENU_SUBSCRIPTIONS],
            [MENU_CHECK, MENU_APPEARANCE],
            [MENU_HELP, MENU_CANCEL],
        ],
        resize_keyboard=True,
    )


def clear_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in (
        "wizard",
        "pending_subscription",
        "pending_template",
        "pending_description",
        "awaiting_preview",
        "pending_preview_file_id",
    ):
        context.user_data.pop(key, None)


async def show_main_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "Выбери действие:"
) -> None:
    clear_wizard(context)
    await update.effective_message.reply_text(text, reply_markup=main_menu())


def help_text() -> str:
    return (
        "Я сообщаю о начале Twitch и YouTube-эфиров.\n\n"
        "1. Добавь меня администратором в нужный канал или группу. "
        "Ничего писать там не нужно.\n"
        "2. В личке нажми «Добавить канал» и следуй подсказкам.\n"
        "3. В «Оформлении» можно изменить заголовок уведомления и установить "
        "свою картинку.\n\n"
        "Кнопка «Проверить эфиры» запускает проверку сразу. "
        "Команды /add, /list, /remove, /check, /template и /preview "
        "остались доступны как запасной вариант.\n\n"
        "Первый опрос только запоминает текущий статус: уже идущий эфир "
        "не вызовет уведомление. Следующий новый эфир — вызовет."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(
        update,
        context,
        "Добро пожаловать. Я сообщаю о начале Twitch и YouTube-эфиров.",
    )


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


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat.type != "supergroup" or not chat.is_forum:
        await message.reply_text(
            "Эта команда работает только внутри темы Telegram-форума."
        )
        return
    if not message.message_thread_id:
        await message.reply_text("Открой нужную тему форума и повтори /topic там.")
        return

    member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
    if member.status not in ADMIN_STATUSES:
        await message.reply_text("Выбрать тему может только администратор форума.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.is_configured(chat.id):
        database.connect_chat(
            chat.id, chat.title or str(chat.id), update.effective_user.id
        )
    database.set_notification_thread(chat.id, message.message_thread_id)
    await message.reply_text(
        "Готово. Уведомления для этого форума теперь будут приходить в эту тему."
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
        await message.reply_text(
            "Формат: /add twitch|youtube|kick <ссылка>"
        )
        return

    platform, url = context.args[0].lower(), context.args[1]
    platform = {"twich": "twitch"}.get(platform, platform)
    try:
        if platform == "twitch":
            channel_key, channel_name, channel_url = parse_twitch_url(url)
        elif platform == "youtube":
            channel_key, channel_name, channel_url = await providers.youtube_channel_id(url)
        elif platform == "kick":
            channel_key, channel_name, channel_url = await providers.kick_channel(
                parse_kick_url(url)
            )
        else:
            raise ValueError("Платформа должна быть twitch, youtube или kick")
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


async def begin_subscription_from_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str, url: str
) -> None:
    message = update.effective_message
    database: Database = context.application.bot_data["database"]
    providers: StreamProviders = context.application.bot_data["providers"]
    try:
        if platform == "twitch":
            channel_key, channel_name, channel_url = parse_twitch_url(url)
        elif platform == "youtube":
            channel_key, channel_name, channel_url = await providers.youtube_channel_id(url)
        else:
            channel_key, channel_name, channel_url = await providers.kick_channel(
                parse_kick_url(url)
            )
    except (ValueError, RuntimeError, httpx.HTTPError) as error:
        await message.reply_text(
            f"Не удалось добавить канал: {error}\nПришли корректную ссылку или нажми «Отмена»."
        )
        return

    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(
            update,
            context,
            "Сначала добавь бота администратором в нужный канал или группу, "
            "затем повтори добавление.",
        )
        return

    context.user_data["pending_subscription"] = {
        "platform": platform,
        "channel_key": channel_key,
        "channel_name": channel_name,
        "channel_url": channel_url,
    }
    context.user_data["wizard"] = "add_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"target_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await message.reply_text(
        f"Канал «{channel_name}» найден. Куда отправлять уведомления?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_subscriptions_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    subscriptions = database.list_user_subscriptions(update.effective_user.id)
    if not subscriptions:
        await update.effective_message.reply_text(
            "Подписок пока нет.", reply_markup=main_menu()
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"{subscription['platform'].title()} · {subscription['channel_name']}",
                callback_data=f"subscription:{subscription['id']}",
            )
        ]
        for subscription in subscriptions
    ]
    keyboard.append([InlineKeyboardButton("В меню", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери подписку, чтобы посмотреть её или удалить:",
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


async def preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Свою картинку нужно настроить в личке с ботом."
        )
        return
    context.user_data["awaiting_preview"] = True
    await update.effective_message.reply_text(
        "Отправь мне картинку как фото. Затем выбери канал или группу, "
        "в уведомлениях которых её использовать."
    )


async def receive_preview_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.type != "private":
        return
    if not context.user_data.get("awaiting_preview"):
        await update.effective_message.reply_text(
            "Чтобы установить эту картинку, сначала отправь команду /preview."
        )
        return

    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await update.effective_message.reply_text("Нет доступных каналов или групп.")
        return

    context.user_data["pending_preview_file_id"] = update.effective_message.photo[-1].file_id
    context.user_data.pop("awaiting_preview", None)
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"preview_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    await update.effective_message.reply_text(
        "Выбери канал или группу для этой картинки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_appearance_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.effective_message.reply_text(
        "Оформление уведомлений:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Изменить заголовок", callback_data="appearance:template")],
                [InlineKeyboardButton("Изменить описание", callback_data="appearance:description")],
                [InlineKeyboardButton("Установить картинку", callback_data="appearance:preview")],
                [InlineKeyboardButton("Показать настройки", callback_data="appearance:status")],
                [InlineKeyboardButton("В меню", callback_data="menu:home")],
            ]
        ),
    )


async def choose_template_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, template: str
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    context.user_data["pending_template"] = template
    context.user_data["wizard"] = "template_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"template_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу, для которых изменить заголовок:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_description_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, description: str
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    context.user_data["pending_description"] = description
    context.user_data["wizard"] = "description_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"description_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу, для которых изменить описание:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    database: Database = context.application.bot_data["database"]

    if data == "menu:home":
        await query.edit_message_text("Действие отменено.")
        await show_main_menu(update, context)
        return
    if data == "menu:add":
        clear_wizard(context)
        context.user_data["wizard"] = "add_platform"
        await query.edit_message_text(
            "Выбери платформу:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Twitch", callback_data="add:twitch"),
                        InlineKeyboardButton("YouTube", callback_data="add:youtube"),
                    ],
                    [InlineKeyboardButton("Kick", callback_data="add:kick")],
                    [InlineKeyboardButton("Отмена", callback_data="menu:home")],
                ]
            ),
        )
        return
    if data.startswith("add:"):
        platform = data.removeprefix("add:")
        context.user_data["wizard"] = "add_url"
        context.user_data["add_platform"] = platform
        await query.edit_message_text(
            f"Пришли ссылку на канал {platform.title()}.\n"
            "Например: https://www.twitch.tv/streamer, https://youtube.com/@channel "
            "или https://kick.com/streamer"
        )
        return
    if data == "menu:subscriptions":
        await query.edit_message_text("Список подписок:")
        await show_subscriptions_menu(update, context)
        return
    if data.startswith("subscription:"):
        subscription_id = int(data.removeprefix("subscription:"))
        subscriptions = database.list_user_subscriptions(update.effective_user.id)
        subscription = next(
            (item for item in subscriptions if item["id"] == subscription_id), None
        )
        if not subscription:
            await query.edit_message_text("Подписка не найдена.")
            return
        status = "в эфире" if subscription["active_stream_id"] else "офлайн"
        await query.edit_message_text(
            f"{subscription['platform'].title()} · {subscription['channel_name']}\n"
            f"Получатель: {subscription['chat_title']}\nСтатус: {status}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Удалить", callback_data=f"remove:ask:{subscription_id}"
                        )
                    ],
                    [InlineKeyboardButton("Назад", callback_data="menu:subscriptions")],
                ]
            ),
        )
        return
    if data.startswith("remove:ask:"):
        subscription_id = int(data.removeprefix("remove:ask:"))
        await query.edit_message_text(
            "Удалить эту подписку?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Да, удалить", callback_data=f"remove:confirm:{subscription_id}"
                        ),
                        InlineKeyboardButton(
                            "Нет", callback_data=f"subscription:{subscription_id}"
                        ),
                    ]
                ]
            ),
        )
        return
    if data.startswith("remove:confirm:"):
        subscription_id = int(data.removeprefix("remove:confirm:"))
        removed = database.remove_user_subscription(
            update.effective_user.id, subscription_id
        )
        await query.edit_message_text(
            "Подписка удалена." if removed else "Подписка уже удалена."
        )
        return
    if data == "menu:check":
        await query.edit_message_text("Проверяю каналы…")
        await check_command(update, context)
        return
    if data == "menu:appearance":
        await query.edit_message_text("Открываю настройки оформления.")
        await show_appearance_menu(update, context)
        return
    if data == "appearance:template":
        clear_wizard(context)
        context.user_data["wizard"] = "template_text"
        await query.edit_message_text(
            "Пришли новый заголовок уведомления. Можно использовать {count} — "
            "число новых эфиров."
        )
        return
    if data == "appearance:description":
        clear_wizard(context)
        context.user_data["wizard"] = "description_text"
        await query.edit_message_text(
            "Пришли описание для уведомления: правила, ссылки или другую "
            "информацию. Максимум 350 символов."
        )
        return
    if data == "appearance:preview":
        clear_wizard(context)
        context.user_data["awaiting_preview"] = True
        await query.edit_message_text("Пришли картинку как фото.")
        return
    if data == "appearance:status":
        chats = database.list_user_chats(update.effective_user.id)
        if not chats:
            await query.edit_message_text("Нет доступных каналов или групп.")
            return
        lines = ["Текущие настройки:"]
        for chat in chats:
            settings = database.get_notification_settings(chat["chat_id"])
            preview = "своя картинка" if settings["preview_file_id"] else "автопревью"
            description = (
                f"описание: {settings['notification_description']}"
                if settings["notification_description"]
                else "без описания"
            )
            target = (
                f"тема #{settings['notification_thread_id']}"
                if settings["notification_thread_id"]
                else "общий чат"
            )
            lines.append(
                f"• {chat['title']}: {settings['notification_template']} "
                f"({target}, {preview}, {description})"
            )
        await query.edit_message_text("\n".join(lines))


async def menu_text_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.effective_chat.type != "private":
        return

    text = update.effective_message.text.strip()
    if text == MENU_CANCEL:
        await show_main_menu(update, context, "Действие отменено.")
        return
    if text == MENU_ADD:
        clear_wizard(context)
        context.user_data["wizard"] = "add_platform"
        await update.effective_message.reply_text(
            "Выбери платформу:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Twitch", callback_data="add:twitch"),
                        InlineKeyboardButton("YouTube", callback_data="add:youtube"),
                    ],
                    [InlineKeyboardButton("Kick", callback_data="add:kick")],
                    [InlineKeyboardButton("Отмена", callback_data="menu:home")],
                ]
            ),
        )
        return
    if text == MENU_SUBSCRIPTIONS:
        await show_subscriptions_menu(update, context)
        return
    if text == MENU_CHECK:
        await check_command(update, context)
        return
    if text == MENU_APPEARANCE:
        await show_appearance_menu(update, context)
        return
    if text == MENU_HELP:
        await update.effective_message.reply_text(help_text(), reply_markup=main_menu())
        return

    wizard = context.user_data.get("wizard")
    if wizard == "add_url":
        await begin_subscription_from_url(
            update, context, context.user_data["add_platform"], text
        )
        return
    if wizard == "template_text":
        if len(text) > 300:
            await update.effective_message.reply_text(
                "Текст слишком длинный: максимум 300 символов. Пришли другой."
            )
            return
        await choose_template_target(update, context, text)
        return
    if wizard == "description_text":
        if len(text) > 350:
            await update.effective_message.reply_text(
                "Описание слишком длинное: максимум 350 символов. Пришли другой текст."
            )
            return
        await choose_description_target(update, context, text)
        return

    await update.effective_message.reply_text(
        "Используй кнопки меню ниже.", reply_markup=main_menu()
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
    context.user_data.pop("wizard", None)
    context.user_data.pop("add_platform", None)
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
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Заголовок уведомления сохранён.\n"
        f"Предпросмотр: {template.replace('{count}', '2')}"
    )


async def select_description_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    description = context.user_data.get("pending_description")
    if description is None or not query.data.startswith("description_chat:"):
        await query.edit_message_text(
            "Эта кнопка уже неактуальна. Открой «Оформление» заново."
        )
        return

    try:
        chat_id = int(query.data.removeprefix("description_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.set_notification_description(chat_id, description)
    context.user_data.pop("pending_description", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Описание сохранено. Оно появится в следующем уведомлении."
    )


async def select_preview_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    file_id = context.user_data.get("pending_preview_file_id")
    if not file_id or not query.data.startswith("preview_chat:"):
        await query.edit_message_text("Эта кнопка уже неактуальна. Повтори /preview.")
        return

    try:
        chat_id = int(query.data.removeprefix("preview_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори /preview.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.set_preview_file_id(chat_id, file_id)
    context.user_data.pop("pending_preview_file_id", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Картинка сохранена. Она будет использована в следующем уведомлении."
    )


async def fetch_live_stream(
    providers: StreamProviders, subscription: sqlite3.Row
) -> LiveStream | None:
    if subscription["platform"] == "twitch":
        return await providers.twitch_live(subscription["channel_key"])
    if subscription["platform"] == "youtube":
        return await providers.youtube_live(subscription["channel_key"])
    if subscription["platform"] == "kick":
        return await providers.kick_live(subscription["channel_key"])
    raise RuntimeError(f"Неизвестная платформа {subscription['platform']}")


def format_live_notification(
    notifications: list[tuple[sqlite3.Row, LiveStream]],
    template: str,
    description: str,
) -> str:
    header = html.escape(template.replace("{count}", str(len(notifications))))
    lines = [f"<b>{header}</b>"]
    if description:
        lines.append(html.escape(description))
    hidden_count = 0
    platform_links = []
    for subscription, stream in notifications:
        platform, emoji = {
            "twitch": ("Twitch", "🟣"),
            "youtube": ("YouTube", "🔴"),
            "kick": ("Kick", "🟢"),
        }[subscription["platform"]]
        title = html.escape(
            stream.title[:160] + ("…" if len(stream.title) > 160 else "")
        )
        line = (
            f"• <b>{platform} — {html.escape(subscription['channel_name'])}</b>\n"
            f"{title}"
        )
        platform_link = (
            f"<a href=\"{html.escape(stream.url, quote=True)}\">"
            f"{emoji} {platform}: {html.escape(subscription['channel_name'])}</a>"
        )
        if len(
            "\n\n".join(lines + [line, " · ".join(platform_links + [platform_link])])
        ) > 1000:
            hidden_count += 1
            continue
        lines.append(line)
        platform_links.append(platform_link)

    if hidden_count:
        lines.append(f"…и ещё {hidden_count}.")
    if platform_links:
        lines.append(" · ".join(platform_links))
    return "\n\n".join(lines)


async def active_streams_for_chat(
    application: Application, chat_id: int
) -> list[tuple[sqlite3.Row, LiveStream]]:
    database: Database = application.bot_data["database"]
    providers: StreamProviders = application.bot_data["providers"]
    active_streams = []
    for subscription in database.get_chat_subscriptions(chat_id):
        try:
            stream = await fetch_live_stream(providers, subscription)
        except (RuntimeError, ValueError, httpx.HTTPError, KeyError) as error:
            logger.warning(
                "Не удалось получить эфир для обновления уведомления %s/%s: %s",
                subscription["platform"],
                subscription["channel_key"],
                error,
            )
            continue
        if stream:
            active_streams.append((subscription, stream))
    return active_streams


async def send_or_edit_notification(
    application: Application, chat_id: int
) -> None:
    database: Database = application.bot_data["database"]
    settings = database.get_notification_settings(chat_id)
    if not settings:
        return

    notifications = await active_streams_for_chat(application, chat_id)
    if not notifications:
        database.clear_notification_message(chat_id)
        return

    text = format_live_notification(
        notifications,
        settings["notification_template"],
        settings["notification_description"],
    )
    message_id = settings["notification_message_id"]
    if message_id:
        try:
            if settings["notification_has_photo"]:
                await application.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            return
        except Exception as error:
            logger.warning(
                "Не удалось обновить уведомление в chat_id=%s, создам новое: %s",
                chat_id,
                error,
            )

    preview = settings["preview_file_id"]
    if not preview:
        twitch_stream = next(
            (
                stream
                for subscription, stream in notifications
                if subscription["platform"] == "twitch"
            ),
            None,
        )
        if twitch_stream:
            providers: StreamProviders = application.bot_data["providers"]
            preview = await providers.twitch_notification_preview(twitch_stream)
        if not preview:
            preview = notifications[0][1].thumbnail_url
    thread_kwargs = (
        {"message_thread_id": settings["notification_thread_id"]}
        if settings["notification_thread_id"]
        else {}
    )
    if preview:
        try:
            message = await application.bot.send_photo(
                chat_id=chat_id,
                photo=preview,
                caption=text,
                parse_mode=ParseMode.HTML,
                **thread_kwargs,
            )
            database.set_notification_message(chat_id, message.message_id, has_photo=True)
            return
        except Exception as error:
            logger.warning(
                "Не удалось отправить превью в chat_id=%s, отправляю текст: %s",
                chat_id,
                error,
            )

    message = await application.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        **thread_kwargs,
    )
    database.set_notification_message(chat_id, message.message_id, has_photo=False)


async def delayed_notification(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data
    pending_chats: set[int] = context.application.bot_data[
        "pending_notification_chats"
    ]
    try:
        await send_or_edit_notification(context.application, chat_id)
    finally:
        pending_chats.discard(chat_id)


async def check_streams(
    application: Application,
    *,
    only_subscription_ids: set[int] | None = None,
) -> list[str]:
    database: Database = application.bot_data["database"]
    providers: StreamProviders = application.bot_data["providers"]
    subscriptions = database.get_all_subscriptions()
    results = []
    started_chats: set[int] = set()
    changed_chats: set[int] = set()
    check_youtube = (
        only_subscription_ids is not None
        or time.monotonic() - application.bot_data["last_youtube_check"]
        >= YOUTUBE_POLL_INTERVAL_SECONDS
    )

    for subscription in subscriptions:
        if (
            only_subscription_ids is not None
            and subscription["id"] not in only_subscription_ids
        ):
            continue
        if subscription["platform"] == "youtube" and not check_youtube:
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
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=stream_id,
            )
            started_chats.add(subscription["chat_id"])
            changed_chats.add(subscription["chat_id"])
        elif not stream and subscription["active_stream_id"] is not None:
            database.set_state(
                subscription["id"],
                initialized=True,
                active_stream_id=None,
            )
            changed_chats.add(subscription["chat_id"])

    if check_youtube:
        application.bot_data["last_youtube_check"] = time.monotonic()

    pending_chats: set[int] = application.bot_data["pending_notification_chats"]
    for chat_id in started_chats:
        settings = database.get_notification_settings(chat_id)
        if settings and settings["notification_message_id"]:
            await send_or_edit_notification(application, chat_id)
        elif chat_id not in pending_chats:
            pending_chats.add(chat_id)
            application.job_queue.run_once(
                delayed_notification,
                when=COMBINE_DELAY_SECONDS,
                data=chat_id,
                name=f"combined-notification-{chat_id}",
            )

    for chat_id in changed_chats:
        if not any(
            subscription["active_stream_id"]
            for subscription in database.get_chat_subscriptions(chat_id)
        ):
            database.clear_notification_message(chat_id)

    return results


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await check_streams(context.application)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Необработанная ошибка Telegram: %s", context.error, exc_info=context.error)


async def post_init(application: Application) -> None:
    application.job_queue.run_repeating(
        scheduled_check,
        interval=FAST_POLL_INTERVAL_SECONDS,
        first=5,
        name="stream-status-check",
    )
    logger.info(
        "Проверка Twitch/Kick каждые %d секунд, YouTube каждые %d секунд",
        FAST_POLL_INTERVAL_SECONDS,
        YOUTUBE_POLL_INTERVAL_SECONDS,
    )


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
    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        logger.warning("Kick не настроен: добавь KICK_CLIENT_ID и KICK_CLIENT_SECRET")

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
    application.bot_data["pending_notification_chats"] = set()
    application.bot_data["last_youtube_check"] = 0.0

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
    application.add_handler(CommandHandler("preview", preview_command))
    application.add_handler(CommandHandler("topic", topic_command))
    application.add_handler(MessageHandler(filters.PHOTO, receive_preview_photo))
    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^(menu|add|subscription|remove|appearance):",
        )
    )
    application.add_handler(
        CallbackQueryHandler(select_target_chat, pattern=r"^target_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_template_chat, pattern=r"^template_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_description_chat, pattern=r"^description_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_preview_chat, pattern=r"^preview_chat:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_text_handler)
    )

    logger.info("Бот уведомлений запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
