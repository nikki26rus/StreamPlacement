"""Доставка и оформление уведомлений об активных эфирах."""

import html
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from config import NOTIFICATION_TIMEZONE
from constants import PLATFORM_NAMES
from database import Database
from models import LiveStream
from providers import StreamProviders


logger = logging.getLogger(__name__)


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


async def fetch_live_stream(
    providers: StreamProviders, subscription: sqlite3.Row
) -> LiveStream | None:
    if subscription["platform"] == "twitch":
        return await providers.twitch_live(subscription["channel_key"])
    if subscription["platform"] == "youtube":
        return await providers.youtube_live(subscription["channel_key"])
    if subscription["platform"] == "kick":
        return await providers.kick_live(subscription["channel_key"])
    if subscription["platform"] in {"vk", "rutube", "instagram", "tiktok"}:
        return await providers.public_live(
            subscription["platform"], subscription["channel_key"]
        )
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
        "{titleVK}": notification_platform_value(notifications, "vk", "title"),
        "{titleRutube}": notification_platform_value(notifications, "rutube", "title"),
        "{titleInstagram}": notification_platform_value(notifications, "instagram", "title"),
        "{titleTikTok}": notification_platform_value(notifications, "tiktok", "title"),
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
        "{categoryVK}": notification_platform_value(notifications, "vk", "game_name"),
        "{categoryRutube}": notification_platform_value(notifications, "rutube", "game_name"),
        "{categoryInstagram}": notification_platform_value(
            notifications, "instagram", "game_name"
        ),
        "{categoryTikTok}": notification_platform_value(
            notifications, "tiktok", "game_name"
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
            link_button(
                str(button["label"]),
                str(button["url"]),
                str(button.get("emoji") or ""),
                button.get("custom_emoji_id"),
                button_style,
            )
        )
    rows = []
    for group in sorted(groups):
        buttons = groups[group]
        rows.extend(buttons[index : index + 8] for index in range(0, len(buttons), 8))
    return rows


def notification_keyboard(
    subscriptions: list[sqlite3.Row],
    button_emojis: dict[str, str],
    button_custom_emoji_ids: dict[str, str],
    button_style: str | None,
    button_styles: dict[str, str],
    custom_buttons: list[dict[str, str]],
    platform_groups: dict[str, int],
    subscription_groups: dict[str, int],
    subscription_labels: dict[str, str],
) -> InlineKeyboardMarkup:
    platform_names = {
        platform: (name, button_emojis[platform])
        for platform, name in PLATFORM_NAMES.items()
    }
    platform_counts = {
        platform: sum(
            subscription["platform"] == platform
            for subscription in subscriptions
        )
        for platform in platform_names
    }
    groups: dict[int, list[InlineKeyboardButton]] = {}
    for subscription in subscriptions:
        platform, emoji = platform_names[subscription["platform"]]
        label = subscription_labels.get(str(subscription["id"]), platform)
        if (
            str(subscription["id"]) not in subscription_labels
            and platform_counts[subscription["platform"]] > 1
        ):
            label += f" · {subscription['channel_name']}"
        group = subscription_groups.get(
            str(subscription["id"]), platform_groups[subscription["platform"]]
        )
        groups.setdefault(group, []).append(
            link_button(
                label,
                subscription["channel_url"],
                emoji,
                button_custom_emoji_ids.get(subscription["platform"]),
                button_styles.get(
                    f"subscription:{subscription['id']}", button_style
                ),
            )
        )
    for index, button in enumerate(custom_buttons):
        group = button["group"] if isinstance(button.get("group"), int) else 1
        groups.setdefault(group, []).append(
            link_button(
                str(button["label"]),
                str(button["url"]),
                str(button.get("emoji") or ""),
                button.get("custom_emoji_id"),
                button_styles.get(f"custom:{index}", button_style),
            )
        )
    rows = []
    for group in sorted(groups):
        buttons = groups[group]
        rows.extend(buttons[index : index + 8] for index in range(0, len(buttons), 8))
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
        database.get_chat_subscriptions(chat_id),
        database.get_button_emojis(chat_id),
        database.get_button_custom_emoji_ids(chat_id),
        settings["button_style"] or None,
        database.get_button_styles(chat_id),
        database.get_custom_buttons(chat_id),
        database.get_platform_button_groups(chat_id),
        database.get_subscription_button_groups(chat_id),
        database.get_subscription_button_labels(chat_id),
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
