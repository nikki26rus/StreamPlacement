import html
from io import BytesIO
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageOps
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
    5, int(os.getenv("FAST_POLL_INTERVAL_SECONDS", "90"))
)
YOUTUBE_POLL_INTERVAL_SECONDS = max(
    30, int(os.getenv("YOUTUBE_POLL_INTERVAL_SECONDS", "300"))
)
COMBINE_DELAY_SECONDS = max(
    0, int(os.getenv("COMBINE_DELAY_SECONDS", "0"))
)
NOTIFICATION_TIMEZONE = os.getenv("NOTIFICATION_TIMEZONE", "Europe/Moscow")
DB_PATH = Path(os.getenv("DB_PATH", "data/streams.db"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"
TWITCH_GAMES_URL = "https://api.twitch.tv/helix/games"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEO_CATEGORIES_URL = (
    "https://www.googleapis.com/youtube/v3/videoCategories"
)
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_URL = "https://api.kick.com/public/v1/channels"
ADMIN_STATUSES = {"creator", "owner", "administrator"}
MENU_ADD = "➕ Добавить канал"
MENU_SUBSCRIPTIONS = "📺 Мои подписки"
MENU_CHECK = "🔎 Проверить эфиры"
MENU_APPEARANCE = "🎨 Оформление"
MENU_HELP = "ℹ️ Помощь"
MENU_CANCEL = "↩️ Отмена"
DEFAULT_BUTTON_EMOJIS = {
    "twitch": "🟣",
    "youtube": "🔴",
    "kick": "🟢",
}
BUTTON_STYLES = {
    "primary": "Синий",
    "success": "Зелёный",
    "danger": "Красный",
}


@dataclass
class LiveStream:
    stream_id: str
    title: str
    url: str
    game_name: str | None = None
    thumbnail_url: str | None = None
    broadcaster_logo_url: str | None = None
    game_box_url: str | None = None
    started_at: str | None = None


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
                discord_url TEXT,
                button_emojis TEXT NOT NULL DEFAULT '{}',
                button_custom_emoji_ids TEXT NOT NULL DEFAULT '{}',
                button_style TEXT NOT NULL DEFAULT '',
                custom_buttons TEXT NOT NULL DEFAULT '[]',
                blur_preview INTEGER NOT NULL DEFAULT 0,
                preview_platform TEXT NOT NULL DEFAULT 'auto',
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
            "discord_url": "TEXT",
            "button_emojis": "TEXT NOT NULL DEFAULT '{}'",
            "button_custom_emoji_ids": "TEXT NOT NULL DEFAULT '{}'",
            "button_style": "TEXT NOT NULL DEFAULT ''",
            "custom_buttons": "TEXT NOT NULL DEFAULT '[]'",
            "blur_preview": "INTEGER NOT NULL DEFAULT 0",
            "preview_platform": "TEXT NOT NULL DEFAULT 'auto'",
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
        self._migrate_discord_buttons()
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
                   discord_url, button_emojis, button_custom_emoji_ids,
                   button_style, custom_buttons, blur_preview, preview_platform,
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

    def clear_preview_file_id(self, chat_id: int) -> None:
        self.connection.execute(
            "UPDATE chats SET preview_file_id = NULL WHERE chat_id = ?",
            (chat_id,),
        )
        self.connection.commit()

    def set_discord_url(self, chat_id: int, url: str) -> None:
        self.connection.execute(
            "UPDATE chats SET discord_url = ? WHERE chat_id = ?",
            (url, chat_id),
        )
        self.connection.commit()

    def get_button_emojis(self, chat_id: int) -> dict[str, str]:
        row = self.connection.execute(
            "SELECT button_emojis FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        try:
            stored = json.loads(row["button_emojis"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        return {
            platform: str(stored.get(platform) or emoji)
            for platform, emoji in DEFAULT_BUTTON_EMOJIS.items()
        }

    def set_button_emoji(self, chat_id: int, platform: str, emoji: str) -> None:
        emojis = self.get_button_emojis(chat_id)
        emojis[platform] = emoji
        self.connection.execute(
            "UPDATE chats SET button_emojis = ? WHERE chat_id = ?",
            (json.dumps(emojis, ensure_ascii=False), chat_id),
        )
        self.connection.commit()

    def get_button_custom_emoji_ids(self, chat_id: int) -> dict[str, str]:
        row = self.connection.execute(
            "SELECT button_custom_emoji_ids FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        try:
            stored = json.loads(row["button_custom_emoji_ids"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        return {
            platform: str(custom_emoji_id)
            for platform, custom_emoji_id in stored.items()
            if platform in DEFAULT_BUTTON_EMOJIS and custom_emoji_id
        }

    def set_button_custom_emoji_id(
        self, chat_id: int, platform: str, custom_emoji_id: str | None
    ) -> None:
        custom_emoji_ids = self.get_button_custom_emoji_ids(chat_id)
        if custom_emoji_id:
            custom_emoji_ids[platform] = custom_emoji_id
        else:
            custom_emoji_ids.pop(platform, None)
        self.connection.execute(
            "UPDATE chats SET button_custom_emoji_ids = ? WHERE chat_id = ?",
            (json.dumps(custom_emoji_ids), chat_id),
        )
        self.connection.commit()

    def get_button_style(self, chat_id: int) -> str | None:
        row = self.connection.execute(
            "SELECT button_style FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return str(row["button_style"]) if row and row["button_style"] else None

    def set_button_style(self, chat_id: int, style: str) -> None:
        self.connection.execute(
            "UPDATE chats SET button_style = ? WHERE chat_id = ?", (style, chat_id)
        )
        self.connection.commit()

    def get_custom_buttons(self, chat_id: int) -> list[dict[str, str | int]]:
        row = self.connection.execute(
            "SELECT custom_buttons FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        try:
            stored = json.loads(row["custom_buttons"]) if row else []
        except (TypeError, json.JSONDecodeError):
            stored = []
        return [
            {
                "label": item["label"],
                "url": item["url"],
                "group": max(
                    1,
                    min(
                        20,
                        item["group"]
                        if isinstance(item.get("group"), int)
                        else 1,
                    ),
                ),
            }
            for item in stored
            if isinstance(item, dict)
            and isinstance(item.get("label"), str)
            and isinstance(item.get("url"), str)
        ]

    def add_custom_button(
        self, chat_id: int, label: str, url: str, group: int = 1
    ) -> None:
        buttons = self.get_custom_buttons(chat_id)
        buttons.append({"label": label, "url": url, "group": group})
        self.connection.execute(
            "UPDATE chats SET custom_buttons = ? WHERE chat_id = ?",
            (json.dumps(buttons, ensure_ascii=False), chat_id),
        )
        self.connection.commit()

    def remove_custom_button(self, chat_id: int, index: int) -> bool:
        buttons = self.get_custom_buttons(chat_id)
        if not 0 <= index < len(buttons):
            return False
        buttons.pop(index)
        self.connection.execute(
            "UPDATE chats SET custom_buttons = ? WHERE chat_id = ?",
            (json.dumps(buttons, ensure_ascii=False), chat_id),
        )
        self.connection.commit()
        return True

    def set_custom_button_group(self, chat_id: int, index: int, group: int) -> bool:
        buttons = self.get_custom_buttons(chat_id)
        if not 0 <= index < len(buttons):
            return False
        buttons[index]["group"] = group
        self.connection.execute(
            "UPDATE chats SET custom_buttons = ? WHERE chat_id = ?",
            (json.dumps(buttons, ensure_ascii=False), chat_id),
        )
        self.connection.commit()
        return True

    def _migrate_discord_buttons(self) -> None:
        """Переносит старые Discord-кнопки в общий список кастомных кнопок."""
        rows = self.connection.execute(
            "SELECT chat_id, discord_url, custom_buttons FROM chats "
            "WHERE discord_url IS NOT NULL"
        ).fetchall()
        for row in rows:
            try:
                buttons = json.loads(row["custom_buttons"])
            except (TypeError, json.JSONDecodeError):
                buttons = []
            if not isinstance(buttons, list):
                buttons = []
            if not any(
                isinstance(button, dict) and button.get("url") == row["discord_url"]
                for button in buttons
            ):
                buttons.append({"label": "Discord", "url": row["discord_url"]})
            self.connection.execute(
                "UPDATE chats SET custom_buttons = ?, discord_url = NULL "
                "WHERE chat_id = ?",
                (json.dumps(buttons, ensure_ascii=False), row["chat_id"]),
            )

    def get_preview_platform(self, chat_id: int) -> str:
        row = self.connection.execute(
            "SELECT preview_platform FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        platform = str(row["preview_platform"]) if row else "auto"
        return platform if platform in {"auto", "twitch", "youtube", "kick"} else "auto"

    def set_preview_platform(self, chat_id: int, platform: str) -> None:
        if platform not in {"auto", "twitch", "youtube", "kick"}:
            raise ValueError("Неизвестная площадка превью")
        self.connection.execute(
            "UPDATE chats SET preview_platform = ? WHERE chat_id = ?",
            (platform, chat_id),
        )
        self.connection.commit()

    def toggle_preview_blur(self, chat_id: int) -> bool:
        self.connection.execute(
            """
            UPDATE chats SET blur_preview = CASE blur_preview
                WHEN 0 THEN 1 ELSE 0 END
            WHERE chat_id = ?
            """,
            (chat_id,),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT blur_preview FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return bool(row and row["blur_preview"])

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
        self.youtube_category_names: dict[str, str] = {}

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
            started_at=stream.get("started_at") or None,
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
        video_response = await self.client.get(
            YOUTUBE_VIDEOS_URL,
            params={"part": "snippet", "id": video_id, "key": YOUTUBE_API_KEY},
        )
        video_response.raise_for_status()
        video_snippet = (
            (video_response.json().get("items") or [{}])[0].get("snippet") or {}
        )
        category_name = await self._youtube_category_name(
            video_snippet.get("categoryId") or ""
        )
        return LiveStream(
            stream_id=video_id,
            title=snippet.get("title") or "Без названия",
            url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=thumbnail,
            started_at=snippet.get("publishedAt") or None,
            game_name=category_name,
        )

    async def _youtube_category_name(self, category_id: str) -> str | None:
        if not category_id:
            return None
        if category_id in self.youtube_category_names:
            return self.youtube_category_names[category_id]
        response = await self.client.get(
            YOUTUBE_VIDEO_CATEGORIES_URL,
            params={
                "part": "snippet",
                "id": category_id,
                "regionCode": "RU",
                "key": YOUTUBE_API_KEY,
            },
        )
        response.raise_for_status()
        categories = response.json().get("items", [])
        if not categories:
            return None
        name = categories[0]["snippet"].get("title")
        if name:
            self.youtube_category_names[category_id] = name
        return name or None

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
            started_at=stream.get("start_time") or None,
        )

    async def _download_image(self, url: str) -> Image.Image:
        response = await self.client.get(url)
        response.raise_for_status()
        return Image.open(BytesIO(response.content)).convert("RGB")

    async def twitch_notification_preview(
        self, stream: LiveStream, *, blur_background: bool = False
    ) -> BytesIO | None:
        """Создаёт карточку из превью эфира, аватара стримера и обложки игры."""
        if not stream.thumbnail_url:
            return None
        try:
            background = await self._download_image(stream.thumbnail_url)
            card = ImageOps.fit(background, (1280, 720), method=Image.Resampling.LANCZOS)
            if blur_background:
                card = card.filter(ImageFilter.GaussianBlur(radius=18))
            overlay = Image.new("RGBA", card.size, (0, 0, 0, 85))
            card = Image.alpha_composite(card.convert("RGBA"), overlay)

            if stream.broadcaster_logo_url:
                avatar = await self._download_image(stream.broadcaster_logo_url)
                avatar = ImageOps.fit(avatar, (225, 225), method=Image.Resampling.LANCZOS)
                mask = Image.new("L", (225, 225), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 225, 225), fill=255)
                card.paste(avatar, (48, 447), mask)

            if stream.game_box_url:
                game = await self._download_image(stream.game_box_url)
                game.thumbnail((300, 380), Image.Resampling.LANCZOS)
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

    async def thumbnail_notification_preview(
        self, stream: LiveStream, *, blur_background: bool = False
    ) -> BytesIO | None:
        """Готовит превью YouTube или Kick с опциональным блюром фона."""
        if not stream.thumbnail_url:
            return None
        try:
            image = await self._download_image(stream.thumbnail_url)
            card = ImageOps.fit(image, (1280, 720), method=Image.Resampling.LANCZOS)
            if blur_background:
                card = card.filter(ImageFilter.GaussianBlur(radius=18))
            overlay = Image.new("RGBA", card.size, (0, 0, 0, 85))
            card = Image.alpha_composite(card.convert("RGBA"), overlay)
            result = BytesIO()
            result.name = "stream-preview.jpg"
            card.convert("RGB").save(result, format="JPEG", quality=90, optimize=True)
            result.seek(0)
            return result
        except (httpx.HTTPError, OSError, ValueError) as error:
            logger.warning("Не удалось создать карточку превью: %s", error)
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


def parse_discord_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.scheme != "https" or host not in {"discord.gg", "discord.com"}:
        raise ValueError(
            "Нужна ссылка-приглашение вида https://discord.gg/название"
        )
    if host == "discord.com" and not parsed.path.startswith("/invite/"):
        raise ValueError(
            "Нужна ссылка-приглашение вида https://discord.com/invite/название"
        )
    if not parsed.path.strip("/"):
        raise ValueError("Ссылка-приглашение Discord неполная")
    return url


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
        "template_edit_chat_id",
        "description_edit_chat_id",
        "pending_discord_url",
        "emoji_chat_id",
        "emoji_platform",
        "custom_button_chat_id",
        "custom_button_label",
        "custom_button_url",
        "custom_button_index",
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
            "число начавшихся эфиров, {time} — время начала, а также "
            "{titleYT}, {titleTwich}, {titleKick} и {categoryYT}, "
            "{categoryTwich}, {categoryKick}.\n"
            "Пример: /template 🔴 {titleTwich} начал эфир в {time}"
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
                [
                    InlineKeyboardButton(
                        "✏️ Заголовок", callback_data="appearance:template"
                    ),
                    InlineKeyboardButton(
                        "✏️ Описание", callback_data="appearance:description"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼 Своя картинка", callback_data="appearance:preview"
                    ),
                    InlineKeyboardButton(
                        "🗑 Удалить картинку", callback_data="appearance:clear_preview"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🖼 Источник превью", callback_data="appearance:preview_source"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🌫 Блюр превью", callback_data="appearance:blur"
                    ),
                    InlineKeyboardButton(
                        "🔗 Кастомные кнопки",
                        callback_data="appearance:custom_buttons",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🎨 Цвет кнопок", callback_data="appearance:colors"
                    ),
                    InlineKeyboardButton(
                        "😀 Эмодзи", callback_data="appearance:emojis"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "👁 Пример", callback_data="appearance:example"
                    ),
                    InlineKeyboardButton(
                        "ℹ️ Настройки", callback_data="appearance:status"
                    ),
                ],
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


async def choose_template_edit_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"], callback_data=f"template_chat:{chat_row['chat_id']}"
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу, где изменить заголовок:",
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


async def choose_description_edit_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
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
        "Выбери канал или группу, где изменить описание:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_example_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"example_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери настройки какого канала или группы показать:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_discord_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    context.user_data["pending_discord_url"] = url
    context.user_data["wizard"] = "discord_target"
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"discord_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу для Discord-кнопки:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_preview_clear_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"clear_preview_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу, где удалить свою картинку:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_preview_source_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"preview_source_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу для настройки источника превью:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_emoji_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    if not chats:
        await show_main_menu(update, context, "Нет доступных каналов или групп.")
        return

    keyboard = [
        [
            InlineKeyboardButton(
                chat_row["title"],
                callback_data=f"emoji_chat:{chat_row['chat_id']}",
            )
        ]
        for chat_row in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу, для которых изменить эмодзи:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_custom_button_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton(chat["title"], callback_data=f"custom_chat:{chat['chat_id']}")]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу для кастомных кнопок:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_button_color_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton(chat["title"], callback_data=f"color_chat:{chat['chat_id']}")]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу для цвета кнопок:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def choose_blur_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    database: Database = context.application.bot_data["database"]
    chats = database.list_user_chats(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton(chat["title"], callback_data=f"blur_chat:{chat['chat_id']}")]
        for chat in chats
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="menu:home")])
    await update.effective_message.reply_text(
        "Выбери канал или группу, где включить или выключить блюр:",
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
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_template_edit_target(update, context)
        return
    if data == "appearance:description":
        clear_wizard(context)
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_description_edit_target(update, context)
        return
    if data == "appearance:preview":
        clear_wizard(context)
        context.user_data["awaiting_preview"] = True
        await query.edit_message_text("Пришли картинку как фото.")
        return
    if data == "appearance:clear_preview":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_preview_clear_target(update, context)
        return
    if data == "appearance:preview_source":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_preview_source_target(update, context)
        return
    if data == "appearance:emojis":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_emoji_target(update, context)
        return
    if data == "appearance:colors":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_button_color_target(update, context)
        return
    if data == "appearance:custom_buttons":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_custom_button_target(update, context)
        return
    if data == "appearance:blur":
        await query.edit_message_text("Выбираю канал или группу.")
        await choose_blur_target(update, context)
        return
    if data == "appearance:example":
        await query.edit_message_text("Выбираю настройки для примера.")
        await choose_example_target(update, context)
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
            blur = "блюр Twitch-превью" if settings["blur_preview"] else "без блюра"
            custom_buttons = len(database.get_custom_buttons(chat["chat_id"]))
            preview_source = {
                "auto": "автовыбор",
                "twitch": "Twitch",
                "youtube": "YouTube",
                "kick": "Kick",
            }[settings["preview_platform"]]
            lines.append(
                f"• {chat['title']}: {settings['notification_template']} "
                f"({target}, {preview}, источник: {preview_source}, {blur}, "
                f"{description}, кастомных кнопок: {custom_buttons})"
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
    if wizard == "template_edit_text":
        if len(text) > 300:
            await update.effective_message.reply_text(
                "Текст слишком длинный: максимум 300 символов. Пришли другой."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["template_edit_chat_id"]
        database.set_notification_template(chat_id, text)
        clear_wizard(context)
        await update.effective_message.reply_text(
            "Заголовок уведомления обновлён.", reply_markup=main_menu()
        )
        return
    if wizard == "description_text":
        if len(text) > 350:
            await update.effective_message.reply_text(
                "Описание слишком длинное: максимум 350 символов. Пришли другой текст."
            )
            return
        await choose_description_target(update, context, text)
        return
    if wizard == "description_edit_text":
        if len(text) > 350:
            await update.effective_message.reply_text(
                "Описание слишком длинное: максимум 350 символов. Пришли другой текст."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["description_edit_chat_id"]
        database.set_notification_description(chat_id, text)
        clear_wizard(context)
        await update.effective_message.reply_text(
            "Описание уведомления обновлено.", reply_markup=main_menu()
        )
        return
    if wizard == "discord_text":
        try:
            discord_url = parse_discord_url(text)
        except ValueError as error:
            await update.effective_message.reply_text(
                f"{error}\nПришли корректную ссылку или нажми «Отмена»."
            )
            return
        await choose_discord_target(update, context, discord_url)
        return
    if wizard == "emoji_text":
        emoji = text.strip()
        if len(emoji) > 16 or any(character.isspace() for character in emoji):
            await update.effective_message.reply_text(
                "Пришли один эмодзи без пробелов, например 🎮."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["emoji_chat_id"]
        platform_key = context.user_data["emoji_platform"]
        custom_emoji_id = next(
            (
                entity.custom_emoji_id
                for entity in update.effective_message.entities or ()
                if getattr(entity.type, "value", entity.type) == "custom_emoji"
                and entity.custom_emoji_id
            ),
            None,
        )
        if custom_emoji_id:
            database.set_button_custom_emoji_id(
                chat_id, platform_key, custom_emoji_id
            )
        else:
            database.set_button_emoji(chat_id, platform_key, emoji)
            database.set_button_custom_emoji_id(chat_id, platform_key, None)
        platform = platform_key.title()
        clear_wizard(context)
        await update.effective_message.reply_text(
            f"Эмодзи для {platform} сохранён.", reply_markup=main_menu()
        )
        return
    if wizard == "custom_button_label":
        if not text or len(text) > 64:
            await update.effective_message.reply_text(
                "Название кнопки должно содержать от 1 до 64 символов."
            )
            return
        context.user_data["custom_button_label"] = text
        context.user_data["wizard"] = "custom_button_url"
        await update.effective_message.reply_text(
            "Пришли ссылку для этой кнопки (http:// или https://)."
        )
        return
    if wizard == "custom_button_url":
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            await update.effective_message.reply_text(
                "Нужна корректная ссылка, начинающаяся с http:// или https://."
            )
            return
        context.user_data["custom_button_url"] = text
        context.user_data["wizard"] = "custom_button_group"
        await update.effective_message.reply_text(
            "Пришли номер строки от 1 до 20. Кнопки с одинаковым номером "
            "будут расположены в одной строке."
        )
        return
    if wizard == "custom_button_group":
        try:
            group = int(text)
        except ValueError:
            group = 0
        if not 1 <= group <= 20:
            await update.effective_message.reply_text(
                "Пришли номер строки целым числом от 1 до 20."
            )
            return
        database: Database = context.application.bot_data["database"]
        chat_id = context.user_data["custom_button_chat_id"]
        buttons = database.get_custom_buttons(chat_id)
        if len(buttons) >= 20:
            await update.effective_message.reply_text(
                "Можно добавить не более 20 кастомных кнопок."
            )
            return
        database.add_custom_button(
            chat_id,
            context.user_data["custom_button_label"],
            context.user_data["custom_button_url"],
            group,
        )
        clear_wizard(context)
        await update.effective_message.reply_text(
            "Кастомная кнопка сохранена.", reply_markup=main_menu()
        )
        return
    if wizard == "custom_button_group_edit":
        try:
            group = int(text)
        except ValueError:
            group = 0
        if not 1 <= group <= 20:
            await update.effective_message.reply_text(
                "Пришли номер строки целым числом от 1 до 20."
            )
            return
        database: Database = context.application.bot_data["database"]
        changed = database.set_custom_button_group(
            context.user_data["custom_button_chat_id"],
            context.user_data["custom_button_index"],
            group,
        )
        clear_wizard(context)
        await update.effective_message.reply_text(
            "Группа кнопки сохранена." if changed else "Кнопка уже удалена.",
            reply_markup=main_menu(),
        )
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
    if not query.data.startswith("template_chat:"):
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

    if template is None:
        settings = database.get_notification_settings(chat_id)
        context.user_data["wizard"] = "template_edit_text"
        context.user_data["template_edit_chat_id"] = chat_id
        await query.edit_message_text(
            "Текущий заголовок:\n"
            f"{settings['notification_template']}\n\n"
            "Пришли новый заголовок. Доступны {count}, {time}, {titleYT}, "
            "{titleTwich}, {titleKick}, {categoryYT}, {categoryTwich}, "
            "{categoryKick}."
        )
        return

    database.set_notification_template(chat_id, template)
    context.user_data.pop("pending_template", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Заголовок уведомления сохранён.\n"
        "Предпросмотр: "
        f"{template.replace('{count}', '2').replace('{time}', '12:00')}"
    )


async def select_description_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    description = context.user_data.get("pending_description")
    if not query.data.startswith("description_chat:"):
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

    if description is None:
        settings = database.get_notification_settings(chat_id)
        current_description = settings["notification_description"] or "— пустое —"
        context.user_data["wizard"] = "description_edit_text"
        context.user_data["description_edit_chat_id"] = chat_id
        await query.edit_message_text(
            f"Текущее описание:\n{current_description}\n\n"
            "Пришли новое описание (до 350 символов). Доступны {count}, {time}, "
            "{titleYT}, {titleTwich}, {titleKick}, {categoryYT}, "
            "{categoryTwich}, {categoryKick}."
        )
        return

    database.set_notification_description(chat_id, description)
    context.user_data.pop("pending_description", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Описание сохранено. Оно появится в следующем уведомлении."
    )


def link_button(
    label: str,
    url: str,
    emoji: str,
    custom_emoji_id: str | None,
    style: str | None = None,
) -> InlineKeyboardButton:
    text = label if custom_emoji_id else f"{emoji + ' ' if emoji else ''}{label}"
    if custom_emoji_id:
        try:
            return InlineKeyboardButton(
                text,
                url=url,
                icon_custom_emoji_id=custom_emoji_id,
                style=style,
            )
        except TypeError:
            logger.warning(
                "Установлена устаревшая python-telegram-bot без custom emoji в кнопках"
            )
    return InlineKeyboardButton(text, url=url, style=style)


async def select_example_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("example_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Открой «Оформление» заново.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    settings = database.get_notification_settings(chat_id)
    text = format_live_notification(
        [],
        settings["notification_template"],
        settings["notification_description"],
        count_override=1,
    )
    button_emojis = database.get_button_emojis(chat_id)
    custom_emoji_ids = database.get_button_custom_emoji_ids(chat_id)
    button_style = database.get_button_style(chat_id)
    sample_buttons = [
        link_button(
            "Twitch",
            "https://www.twitch.tv/",
            button_emojis["twitch"],
            custom_emoji_ids.get("twitch"),
            button_style,
        ),
        link_button(
            "YouTube",
            "https://www.youtube.com/",
            button_emojis["youtube"],
            custom_emoji_ids.get("youtube"),
            button_style,
        ),
        link_button(
            "Kick",
            "https://kick.com/",
            button_emojis["kick"],
            custom_emoji_ids.get("kick"),
            button_style,
        ),
    ]
    sample_rows = [
        sample_buttons[index : index + 2]
        for index in range(0, len(sample_buttons), 2)
    ]
    sample_rows.extend(
        custom_button_rows(
            database.get_custom_buttons(chat_id),
            button_style,
        )
    )
    sample_keyboard = InlineKeyboardMarkup(sample_rows)
    try:
        if settings["preview_file_id"]:
            await context.bot.send_photo(
                chat_id=update.effective_user.id,
                photo=settings["preview_file_id"],
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=sample_keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=sample_keyboard,
            )
    except Exception as error:
        logger.warning("Не удалось показать пример уведомления: %s", error)
        await query.edit_message_text(f"Не удалось показать пример: {error}")
        return

    await query.edit_message_text(
        "Пример отправлен сюда, в личный чат. Уведомление в канале не публиковалось."
    )


async def select_discord_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    url = context.user_data.get("pending_discord_url")
    if not url or not query.data.startswith("discord_chat:"):
        await query.edit_message_text(
            "Эта кнопка уже неактуальна. Открой «Оформление» заново."
        )
        return

    try:
        chat_id = int(query.data.removeprefix("discord_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.add_custom_button(chat_id, "Discord", url)
    context.user_data.pop("pending_discord_url", None)
    context.user_data.pop("wizard", None)
    await query.edit_message_text(
        "Discord-ссылка сохранена как кастомная кнопка."
    )


async def select_preview_clear_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("clear_preview_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    database.clear_preview_file_id(chat_id)
    await query.edit_message_text(
        "Своя картинка удалена. В следующем уведомлении будет использовано "
        "автоматическое превью."
    )


async def select_preview_source_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("preview_source_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    await query.edit_message_text(
        "Выбери площадку для автоматического превью. Своя загруженная картинка "
        "всегда имеет приоритет.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Автовыбор", callback_data=f"preview_platform:{chat_id}:auto"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Twitch", callback_data=f"preview_platform:{chat_id}:twitch"
                    ),
                    InlineKeyboardButton(
                        "YouTube", callback_data=f"preview_platform:{chat_id}:youtube"
                    ),
                    InlineKeyboardButton(
                        "Kick", callback_data=f"preview_platform:{chat_id}:kick"
                    ),
                ],
            ]
        ),
    )


async def set_preview_platform(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, platform = query.data.split(":", 2)
        chat_id = int(chat_id_text)
    except ValueError:
        await query.edit_message_text("Некорректная настройка. Повтори её.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    try:
        database.set_preview_platform(chat_id, platform)
    except ValueError:
        await query.edit_message_text("Неизвестная площадка.")
        return
    label = {"auto": "автовыбор", "twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}[
        platform
    ]
    await query.edit_message_text(f"Источник автоматического превью: {label}.")


async def select_emoji_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("emoji_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    emojis = database.get_button_emojis(chat_id)
    await query.edit_message_text(
        "Выбери кнопку:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{emojis['twitch']} Twitch",
                        callback_data=f"emoji_platform:{chat_id}:twitch",
                    ),
                    InlineKeyboardButton(
                        f"{emojis['youtube']} YouTube",
                        callback_data=f"emoji_platform:{chat_id}:youtube",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        f"{emojis['kick']} Kick",
                        callback_data=f"emoji_platform:{chat_id}:kick",
                    ),
                ],
            ]
        ),
    )


async def select_emoji_platform(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, platform = query.data.split(":", 2)
        chat_id = int(chat_id_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    if platform not in DEFAULT_BUTTON_EMOJIS:
        await query.edit_message_text("Неизвестная площадка.")
        return

    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return

    context.user_data["wizard"] = "emoji_text"
    context.user_data["emoji_chat_id"] = chat_id
    context.user_data["emoji_platform"] = platform
    await query.edit_message_text(
        f"Пришли новый эмодзи для кнопки {platform.title()}.\n"
        "Можно отправить один Unicode-эмодзи или кастомный эмодзи из Telegram."
    )


async def select_button_color_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("color_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                name, callback_data=f"color_set:{chat_id}:{style}"
            )
        ]
        for style, name in BUTTON_STYLES.items()
    ]
    await query.edit_message_text(
        "Выбери цвет для всех кнопок уведомления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def set_button_color(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, style = query.data.split(":", 2)
        chat_id = int(chat_id_text)
        color_name = BUTTON_STYLES[style]
    except (ValueError, KeyError):
        await query.edit_message_text("Некорректный цвет. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    database.set_button_style(chat_id, style)
    await query.edit_message_text(
        f"Цвет «{color_name}» сохранён для всех кнопок уведомления."
    )


async def select_custom_button_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("custom_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    buttons = database.get_custom_buttons(chat_id)
    keyboard = [
        [InlineKeyboardButton("➕ Добавить кнопку", callback_data=f"custom_add:{chat_id}")]
    ]
    keyboard.extend(
        [
            InlineKeyboardButton(
                f"Строка {button['group']}: {button['label'][:32]}",
                callback_data=f"custom_group:{chat_id}:{index}",
            ),
            InlineKeyboardButton(
                "🗑", callback_data=f"custom_delete:{chat_id}:{index}"
            ),
        ]
        for index, button in enumerate(buttons)
    )
    await query.edit_message_text(
        "Кастомные кнопки: "
        + (str(len(buttons)) if buttons else "пока нет")
        + ". Нажми кнопку, чтобы изменить номер её строки.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def begin_custom_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("custom_add:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    context.user_data["wizard"] = "custom_button_label"
    context.user_data["custom_button_chat_id"] = chat_id
    await query.edit_message_text("Пришли название новой кнопки.")


async def delete_custom_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, index_text = query.data.split(":", 2)
        chat_id, index = int(chat_id_text), int(index_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    deleted = database.remove_custom_button(chat_id, index)
    await query.edit_message_text(
        "Кнопка удалена." if deleted else "Эта кнопка уже удалена."
    )


async def begin_custom_button_group_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, chat_id_text, index_text = query.data.split(":", 2)
        chat_id, index = int(chat_id_text), int(index_text)
    except ValueError:
        await query.edit_message_text("Некорректная кнопка. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    buttons = database.get_custom_buttons(chat_id)
    if not 0 <= index < len(buttons):
        await query.edit_message_text("Эта кнопка уже удалена.")
        return
    context.user_data["wizard"] = "custom_button_group_edit"
    context.user_data["custom_button_chat_id"] = chat_id
    context.user_data["custom_button_index"] = index
    await query.edit_message_text(
        f"Текущая строка: {buttons[index]['group']}.\n"
        "Пришли новый номер строки от 1 до 20."
    )


async def toggle_preview_blur(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    try:
        chat_id = int(query.data.removeprefix("blur_chat:"))
    except ValueError:
        await query.edit_message_text("Некорректный чат. Повтори настройку.")
        return
    database: Database = context.application.bot_data["database"]
    if not database.user_can_access_chat(update.effective_user.id, chat_id):
        await query.edit_message_text("Нет доступа к этому чату.")
        return
    enabled = database.toggle_preview_blur(chat_id)
    await query.edit_message_text(
        "Блюр фона Twitch-превью включён. Аватар и обложка категории не размываются."
        if enabled
        else "Блюр фона Twitch-превью выключен."
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


def notification_start_time(
    notifications: list[tuple[sqlite3.Row, LiveStream]],
) -> str:
    start_times = []
    for _, stream in notifications:
        if not stream.started_at:
            continue
        try:
            start_times.append(
                datetime.fromisoformat(stream.started_at.replace("Z", "+00:00"))
            )
        except ValueError:
            continue
    if not start_times:
        return "сейчас"

    try:
        timezone = ZoneInfo(NOTIFICATION_TIMEZONE)
    except Exception:
        timezone = ZoneInfo("UTC")
    return min(start_times).astimezone(timezone).strftime("%H:%M")


def notification_platform_value(
    notifications: list[tuple[sqlite3.Row, LiveStream]],
    platform: str,
    field: str,
) -> str:
    values = [
        getattr(stream, field)
        for subscription, stream in notifications
        if subscription["platform"] == platform and getattr(stream, field)
    ]
    return " · ".join(values) if values else "—"


def format_live_notification(
    notifications: list[tuple[sqlite3.Row, LiveStream]],
    template: str,
    description: str,
    *,
    count_override: int | None = None,
) -> str:
    replacements = {
        "{count}": str(
            count_override if count_override is not None else len(notifications)
        ),
        "{time}": notification_start_time(notifications),
        "{titleYT}": notification_platform_value(notifications, "youtube", "title"),
        "{titleTwich}": notification_platform_value(notifications, "twitch", "title"),
        "{titleTwitch}": notification_platform_value(notifications, "twitch", "title"),
        "{titleKick}": notification_platform_value(notifications, "kick", "title"),
        "{categoryYT}": notification_platform_value(
            notifications, "youtube", "game_name"
        ),
        "{categoryTwich}": notification_platform_value(
            notifications, "twitch", "game_name"
        ),
        "{categoryTwitch}": notification_platform_value(
            notifications, "twitch", "game_name"
        ),
        "{categoryKick}": notification_platform_value(
            notifications, "kick", "game_name"
        ),
    }
    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)
        description = description.replace(placeholder, value)
    header = html.escape(template)
    lines = [f"<b>{header}</b>"]
    if description:
        lines.append(html.escape(description))
    return "\n\n".join(lines)


def custom_button_rows(
    custom_buttons: list[dict[str, str | int]],
    button_style: str | None,
) -> list[list[InlineKeyboardButton]]:
    groups: dict[int, list[InlineKeyboardButton]] = {}
    for button in custom_buttons:
        group = button["group"] if isinstance(button.get("group"), int) else 1
        groups.setdefault(group, []).append(
            link_button(str(button["label"]), str(button["url"]), "", None, button_style)
        )
    rows = []
    for group in sorted(groups):
        buttons = groups[group]
        rows.extend(buttons[index : index + 8] for index in range(0, len(buttons), 8))
    return rows


def notification_keyboard(
    notifications: list[tuple[sqlite3.Row, LiveStream]],
    button_emojis: dict[str, str],
    button_custom_emoji_ids: dict[str, str],
    button_style: str | None,
    custom_buttons: list[dict[str, str]],
) -> InlineKeyboardMarkup:
    platform_names = {
        "twitch": ("Twitch", button_emojis["twitch"]),
        "youtube": ("YouTube", button_emojis["youtube"]),
        "kick": ("Kick", button_emojis["kick"]),
    }
    platform_counts = {
        platform: sum(
            subscription["platform"] == platform
            for subscription, _ in notifications
        )
        for platform in platform_names
    }
    platform_buttons = []
    for subscription, stream in notifications:
        platform, emoji = platform_names[subscription["platform"]]
        label = platform
        if platform_counts[subscription["platform"]] > 1:
            label += f" · {subscription['channel_name']}"
        platform_buttons.append(
            link_button(
                label,
                stream.url,
                emoji,
                button_custom_emoji_ids.get(subscription["platform"]),
                button_style,
            )
        )
    rows = [
        platform_buttons[index : index + 2]
        for index in range(0, len(platform_buttons), 2)
    ]
    rows.extend(custom_button_rows(custom_buttons, button_style))
    return InlineKeyboardMarkup(rows)


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
    reply_markup = notification_keyboard(
        notifications,
        database.get_button_emojis(chat_id),
        database.get_button_custom_emoji_ids(chat_id),
        settings["button_style"] or None,
        database.get_custom_buttons(chat_id),
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
                    reply_markup=reply_markup,
                )
            else:
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                    reply_markup=reply_markup,
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
        preview_platform = settings["preview_platform"]
        selected_stream = None
        if preview_platform == "auto":
            selected_stream = next(
                (
                    (subscription, stream)
                    for subscription, stream in notifications
                    if subscription["platform"] == "twitch"
                ),
                notifications[0],
            )
        else:
            selected_stream = next(
                (
                    (subscription, stream)
                    for subscription, stream in notifications
                    if subscription["platform"] == preview_platform
                ),
                None,
            )
        if not selected_stream:
            selected_stream = notifications[0]
        if selected_stream:
            subscription, stream = selected_stream
            providers: StreamProviders = application.bot_data["providers"]
            if subscription["platform"] == "twitch":
                preview = await providers.twitch_notification_preview(
                    stream, blur_background=bool(settings["blur_preview"])
                )
            else:
                preview = await providers.thumbnail_notification_preview(
                    stream, blur_background=bool(settings["blur_preview"])
                )
            if not preview:
                preview = stream.thumbnail_url
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
                reply_markup=reply_markup,
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
        reply_markup=reply_markup,
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
        CallbackQueryHandler(select_example_chat, pattern=r"^example_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_discord_chat, pattern=r"^discord_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(
            select_preview_clear_chat, pattern=r"^clear_preview_chat:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            select_preview_source_chat, pattern=r"^preview_source_chat:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(set_preview_platform, pattern=r"^preview_platform:")
    )
    application.add_handler(
        CallbackQueryHandler(select_emoji_chat, pattern=r"^emoji_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(select_emoji_platform, pattern=r"^emoji_platform:")
    )
    application.add_handler(
        CallbackQueryHandler(select_button_color_chat, pattern=r"^color_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(set_button_color, pattern=r"^color_set:")
    )
    application.add_handler(
        CallbackQueryHandler(select_custom_button_chat, pattern=r"^custom_chat:")
    )
    application.add_handler(
        CallbackQueryHandler(begin_custom_button, pattern=r"^custom_add:")
    )
    application.add_handler(
        CallbackQueryHandler(delete_custom_button, pattern=r"^custom_delete:")
    )
    application.add_handler(
        CallbackQueryHandler(
            begin_custom_button_group_edit, pattern=r"^custom_group:"
        )
    )
    application.add_handler(
        CallbackQueryHandler(toggle_preview_blur, pattern=r"^blur_chat:")
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
