import json
import sqlite3
from pathlib import Path

from constants import BUTTON_STYLES, DEFAULT_BUTTON_EMOJIS, PLATFORM_NAMES


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
                button_styles TEXT NOT NULL DEFAULT '{}',
                custom_buttons TEXT NOT NULL DEFAULT '[]',
                platform_button_groups TEXT NOT NULL DEFAULT '{}',
                subscription_button_groups TEXT NOT NULL DEFAULT '{}',
                subscription_button_labels TEXT NOT NULL DEFAULT '{}',
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
                platform TEXT NOT NULL CHECK(
                    platform IN (
                        'twitch', 'youtube', 'kick', 'vk', 'rutube',
                        'instagram', 'tiktok'
                    )
                ),
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
            "button_styles": "TEXT NOT NULL DEFAULT '{}'",
            "custom_buttons": "TEXT NOT NULL DEFAULT '[]'",
            "platform_button_groups": "TEXT NOT NULL DEFAULT '{}'",
            "subscription_button_groups": "TEXT NOT NULL DEFAULT '{}'",
            "subscription_button_labels": "TEXT NOT NULL DEFAULT '{}'",
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
        if "'tiktok'" not in subscriptions_sql:
            self.connection.execute("ALTER TABLE subscriptions RENAME TO subscriptions_old")
            self.connection.executescript(
                """
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    platform TEXT NOT NULL CHECK(
                        platform IN (
                            'twitch', 'youtube', 'kick', 'vk', 'rutube',
                            'instagram', 'tiktok'
                        )
                    ),
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
                   button_style, button_styles, custom_buttons, platform_button_groups,
                   blur_preview, preview_platform,
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

    def get_button_styles(self, chat_id: int) -> dict[str, str]:
        row = self.connection.execute(
            "SELECT button_styles FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        try:
            stored = json.loads(row["button_styles"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        return {
            str(key): str(style)
            for key, style in stored.items()
            if style in BUTTON_STYLES
        }

    def set_individual_button_style(
        self, chat_id: int, button_key: str, style: str | None
    ) -> None:
        styles = self.get_button_styles(chat_id)
        if style:
            if style not in BUTTON_STYLES:
                raise ValueError("Некорректный цвет кнопки")
            styles[button_key] = style
        else:
            styles.pop(button_key, None)
        self.connection.execute(
            "UPDATE chats SET button_styles = ? WHERE chat_id = ?",
            (json.dumps(styles, ensure_ascii=False), chat_id),
        )
        self.connection.commit()

    def get_platform_button_groups(self, chat_id: int) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT platform_button_groups FROM chats WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        try:
            stored = json.loads(row["platform_button_groups"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        return {
            platform: max(1, min(20, int(stored.get(platform, index + 1))))
            if str(stored.get(platform, index + 1)).isdigit()
            else index + 1
            for index, platform in enumerate(PLATFORM_NAMES)
        }

    def set_platform_button_group(self, chat_id: int, platform: str, group: int) -> None:
        if platform not in PLATFORM_NAMES or not 1 <= group <= 20:
            raise ValueError("Некорректная группа кнопки")
        groups = self.get_platform_button_groups(chat_id)
        groups[platform] = group
        self.connection.execute(
            "UPDATE chats SET platform_button_groups = ? WHERE chat_id = ?",
            (json.dumps(groups, ensure_ascii=False), chat_id),
        )
        self.connection.commit()

    def get_subscription_button_groups(self, chat_id: int) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT subscription_button_groups FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        try:
            stored = json.loads(row["subscription_button_groups"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        return {
            str(subscription_id): int(group)
            for subscription_id, group in stored.items()
            if str(group).isdigit() and 1 <= int(group) <= 20
        }

    def set_subscription_button_group(
        self, chat_id: int, subscription_id: int, group: int
    ) -> None:
        if not 1 <= group <= 20:
            raise ValueError("Некорректная группа кнопки")
        groups = self.get_subscription_button_groups(chat_id)
        groups[str(subscription_id)] = group
        self.connection.execute(
            "UPDATE chats SET subscription_button_groups = ? WHERE chat_id = ?",
            (json.dumps(groups, ensure_ascii=False), chat_id),
        )
        self.connection.commit()

    def get_subscription_button_labels(self, chat_id: int) -> dict[str, str]:
        row = self.connection.execute(
            "SELECT subscription_button_labels FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        try:
            stored = json.loads(row["subscription_button_labels"]) if row else {}
        except (TypeError, json.JSONDecodeError):
            stored = {}
        return {
            str(subscription_id): label.strip()
            for subscription_id, label in stored.items()
            if isinstance(label, str) and label.strip()
        }

    def set_subscription_button_label(
        self, chat_id: int, subscription_id: int, label: str
    ) -> None:
        labels = self.get_subscription_button_labels(chat_id)
        labels[str(subscription_id)] = label
        self.connection.execute(
            "UPDATE chats SET subscription_button_labels = ? WHERE chat_id = ?",
            (json.dumps(labels, ensure_ascii=False), chat_id),
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
                "emoji": item.get("emoji", "")
                if isinstance(item.get("emoji", ""), str)
                else "",
                "custom_emoji_id": item.get("custom_emoji_id")
                if isinstance(item.get("custom_emoji_id"), str)
                else None,
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

    def set_custom_button_label(self, chat_id: int, index: int, label: str) -> bool:
        buttons = self.get_custom_buttons(chat_id)
        if not 0 <= index < len(buttons):
            return False
        buttons[index]["label"] = label
        self.connection.execute(
            "UPDATE chats SET custom_buttons = ? WHERE chat_id = ?",
            (json.dumps(buttons, ensure_ascii=False), chat_id),
        )
        self.connection.commit()
        return True

    def set_custom_button_emoji(
        self,
        chat_id: int,
        index: int,
        emoji: str,
        custom_emoji_id: str | None,
    ) -> bool:
        buttons = self.get_custom_buttons(chat_id)
        if not 0 <= index < len(buttons):
            return False
        buttons[index]["emoji"] = emoji
        buttons[index]["custom_emoji_id"] = custom_emoji_id
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
        return platform if platform in {"auto", *PLATFORM_NAMES} else "auto"

    def set_preview_platform(self, chat_id: int, platform: str) -> None:
        if platform not in {"auto", *PLATFORM_NAMES}:
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

    def reset_notification_settings(self, chat_id: int) -> None:
        """Сбрасывает оформление и параметры уведомлений, не затрагивая подписки."""
        self.connection.execute(
            """
            UPDATE chats
            SET notification_template = '🔴 Новые эфиры: {count}',
                notification_description = '',
                preview_file_id = NULL,
                discord_url = NULL,
                button_emojis = '{}',
                button_custom_emoji_ids = '{}',
                button_style = '',
                button_styles = '{}',
                custom_buttons = '[]',
                platform_button_groups = '{}',
                subscription_button_groups = '{}',
                subscription_button_labels = '{}',
                blur_preview = 0,
                preview_platform = 'auto',
                notification_thread_id = NULL,
                notification_message_id = NULL,
                notification_has_photo = 0
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


