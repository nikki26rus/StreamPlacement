"""Authenticated user cabinet served alongside the Telegram polling bot."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from telegram.constants import ParseMode

from config import (
    BOT_TOKEN,
    DB_PATH,
    TELEGRAM_LOGIN_BOT_USERNAME,
    WEB_AUTH_MAX_AGE_SECONDS,
    WEB_COOKIE_SECURE,
)
from constants import BUTTON_STYLES, DEFAULT_BUTTON_EMOJIS, PLATFORM_NAMES
from database import Database
from notifications import fetch_live_stream, format_live_notification
from providers import (
    StreamProviders,
    parse_kick_url,
    parse_public_platform_url,
    parse_twitch_url,
)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
UPLOAD_DIR = Path(os.getenv("WEB_UPLOAD_DIR", "data/web_uploads")).resolve()
COOKIE_NAME = "stream_notifier_session"


class ModeInput(BaseModel):
    mode: str


class SubscriptionInput(BaseModel):
    platform: str
    url: str = Field(min_length=3, max_length=2048)
    chat_id: int | None = None


class AppearanceInput(BaseModel):
    template: str = Field(max_length=1024)
    description: str = Field(max_length=4096)
    preview_platform: str
    blur_preview: bool


class ButtonSettingsInput(BaseModel):
    global_style: str | None = None
    platform_groups: dict[str, int] = {}
    emojis: dict[str, str] = {}
    subscription_labels: dict[str, str] = {}
    subscription_groups: dict[str, int] = {}
    button_styles: dict[str, str] = {}


class CustomButtonInput(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=3, max_length=2048)
    group: int = Field(default=1, ge=1, le=20)
    emoji: str = Field(default="", max_length=16)
    style: str | None = None


class ScheduleItemInput(BaseModel):
    weekday: int = Field(ge=0, le=6)
    time: str
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2048)


def _error(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def _session_signature(payload: str) -> str:
    return hmac.new(BOT_TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _create_session(user_id: int, first_name: str, username: str | None) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = json.dumps(
        {"id": user_id, "name": first_name, "username": username, "exp": int(time.time()) + 86400, "csrf": csrf},
        separators=(",", ":"),
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_session_signature(encoded)}", csrf


def _read_session(request: Request) -> dict:
    value = request.cookies.get(COOKIE_NAME, "")
    try:
        encoded, signature = value.rsplit(".", 1)
        if not hmac.compare_digest(signature, _session_signature(encoded)):
            raise ValueError
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        session = json.loads(payload)
        if not isinstance(session["id"], int) or int(session["exp"]) < time.time():
            raise ValueError
        return session
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise _error(401, "Требуется вход через Telegram.")


def _verify_telegram_login(payload: dict) -> dict:
    if not BOT_TOKEN:
        raise _error(503, "На сервере не настроен BOT_TOKEN.")
    received_hash = str(payload.pop("hash", ""))
    if not received_hash or "id" not in payload or "auth_date" not in payload:
        raise _error(400, "Telegram передал неполные данные входа.")
    try:
        auth_date = int(payload["auth_date"])
    except (TypeError, ValueError):
        raise _error(400, "Некорректная дата авторизации.")
    if auth_date > time.time() + 60 or time.time() - auth_date > WEB_AUTH_MAX_AGE_SECONDS:
        raise _error(401, "Ссылка входа устарела. Войдите ещё раз.")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(payload.items()))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise _error(401, "Telegram Login не прошёл проверку подписи.")
    return payload


def _user(request: Request) -> dict:
    return _read_session(request)


def _csrf(
    session: Annotated[dict, Depends(_user)],
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> dict:
    if not x_csrf_token or not hmac.compare_digest(str(session["csrf"]), x_csrf_token):
        raise _error(403, "Недействительный CSRF-токен.")
    return session


def _db() -> Database:
    return Database(DB_PATH)


def _access(db: Database, user_id: int, chat_id: int) -> None:
    if not db.user_can_access_chat(user_id, chat_id):
        raise _error(404, "Чат не найден или недоступен.")


def _streamer_access(db: Database, user_id: int, chat_id: int) -> None:
    _access(db, user_id, chat_id)
    if db.get_user_mode(user_id) != "streamer":
        raise _error(403, "Эта настройка доступна в режиме стримера.")


def _chat_payload(db: Database, user_id: int) -> list[dict]:
    return [dict(row) for row in db.list_user_chats(user_id)]


def _subscription_payload(db: Database, user_id: int, mode: str | None = None) -> list[dict]:
    return [dict(row) for row in db.list_user_subscriptions(user_id, mode)]


async def _resolve(providers: StreamProviders, platform: str, url: str) -> tuple[str, str, str]:
    if platform == "twitch":
        return parse_twitch_url(url)
    if platform == "youtube":
        return await providers.youtube_channel_id(url)
    if platform == "kick":
        return await providers.kick_channel(parse_kick_url(url))
    if platform in {"vk", "rutube", "instagram", "tiktok"}:
        return await providers.public_channel(platform, parse_public_platform_url(platform, url))
    raise ValueError("Неизвестная платформа.")


def _schedule_text(db: Database, chat_id: int) -> str:
    days = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
    grouped: dict[int, list] = {index: [] for index in range(7)}
    for item in db.list_schedule_items(chat_id):
        grouped[item["weekday"]].append(item)
    lines = ["📅 <b>Расписание на неделю</b>"]
    for day, title in enumerate(days):
        if not grouped[day]:
            continue
        lines.extend(("", f"<b>{title}</b>"))
        for item in grouped[day]:
            lines.append(f"• <b>{item['time']}</b> — {html.escape(item['title'])}")
            if item["description"]:
                lines.append(f"  {html.escape(item['description'])}")
    return "\n".join(lines)


def create_app(bot_application=None) -> FastAPI:
    """Creates the web service. bot_application is supplied by bot.py at runtime."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        app.state.providers = StreamProviders()
        app.state.bot_application = bot_application
        yield
        await app.state.providers.close()

    app = FastAPI(title="Stream Notifier", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

    @app.get("/")
    async def site() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/public-config")
    async def public_config() -> dict:
        return {"telegram_login_bot_username": TELEGRAM_LOGIN_BOT_USERNAME}

    @app.post("/api/auth/telegram")
    async def telegram_auth(payload: dict, response: Response) -> dict:
        verified = _verify_telegram_login(dict(payload))
        try:
            user_id = int(verified["id"])
        except (ValueError, TypeError):
            raise _error(400, "Некорректный идентификатор Telegram.")
        token, csrf = _create_session(user_id, str(verified.get("first_name") or "Пользователь"), verified.get("username"))
        response.set_cookie(
            COOKIE_NAME, token, httponly=True, secure=WEB_COOKIE_SECURE, samesite="lax", max_age=86400, path="/"
        )
        return {"user": {"id": user_id, "name": verified.get("first_name", "Пользователь")}, "csrf": csrf}

    @app.post("/api/auth/logout")
    async def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/me")
    async def me(session: Annotated[dict, Depends(_user)], db: Annotated[Database, Depends(_db)]) -> dict:
        return {
            "user": {"id": session["id"], "name": session["name"], "username": session.get("username")},
            "csrf": session["csrf"],
            "mode": db.get_user_mode(session["id"]),
            "chats": _chat_payload(db, session["id"]),
            "platforms": PLATFORM_NAMES,
        }

    @app.put("/api/mode")
    async def set_mode(data: ModeInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        if data.mode not in {"streamer", "subscriber"}:
            raise _error(422, "Выберите режим «стример» или «подписчик».")
        db.set_user_mode(session["id"], data.mode)
        return {"mode": data.mode}

    @app.get("/api/subscriptions")
    async def list_subscriptions(session: Annotated[dict, Depends(_user)], db: Annotated[Database, Depends(_db)]) -> dict:
        return {"items": _subscription_payload(db, session["id"]), "chats": _chat_payload(db, session["id"])}

    @app.post("/api/subscriptions")
    async def add_subscription(data: SubscriptionInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)], request: Request) -> dict:
        mode = db.get_user_mode(session["id"])
        if mode not in {"streamer", "subscriber"}:
            raise _error(409, "Сначала выберите режим.")
        chat_id = session["id"] if mode == "subscriber" else data.chat_id
        if not isinstance(chat_id, int):
            raise _error(422, "Выберите подключённый чат для уведомлений.")
        if mode == "streamer":
            _streamer_access(db, session["id"], chat_id)
        elif chat_id != session["id"]:
            raise _error(403, "Подписчик может использовать только свой личный чат.")
        if not db.is_configured(chat_id):
            db.connect_chat(chat_id, "Личные уведомления" if mode == "subscriber" else "Подключённый чат", session["id"])
        try:
            key, name, canonical_url = await _resolve(request.app.state.providers, data.platform, data.url)
            item_id = db.add_subscription(chat_id, data.platform, key, name, canonical_url)
        except (ValueError, httpx.HTTPError) as error:
            raise _error(422, f"Не удалось добавить канал: {error}")
        except Exception as error:
            if "UNIQUE constraint failed" in str(error):
                raise _error(409, "Этот канал уже добавлен в выбранный чат.")
            raise
        return {"id": item_id}

    @app.delete("/api/subscriptions/{subscription_id}")
    async def delete_subscription(subscription_id: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        if not db.remove_user_subscription(session["id"], subscription_id, db.get_user_mode(session["id"])):
            raise _error(404, "Подписка не найдена.")
        return {"ok": True}

    @app.get("/api/chats/{chat_id}/appearance")
    async def get_appearance(chat_id: int, session: Annotated[dict, Depends(_user)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        settings = db.get_notification_settings(chat_id)
        if not settings:
            raise _error(404, "Настройки чата не найдены.")
        result = dict(settings)
        result["blur_preview"] = bool(result["blur_preview"])
        result["preview_file_note"] = bool(result.get("preview_file_id"))
        return result

    @app.put("/api/chats/{chat_id}/appearance")
    async def save_appearance(chat_id: int, data: AppearanceInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if data.preview_platform not in {"auto", *PLATFORM_NAMES}:
            raise _error(422, "Некорректный источник превью.")
        db.set_notification_template(chat_id, data.template)
        db.set_notification_description(chat_id, data.description)
        db.set_preview_platform(chat_id, data.preview_platform)
        current = bool(db.get_notification_settings(chat_id)["blur_preview"])
        if current != data.blur_preview:
            db.toggle_preview_blur(chat_id)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/appearance/reset")
    async def reset_appearance(chat_id: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        db.reset_notification_settings(chat_id)
        return {"ok": True}

    @app.get("/api/chats/{chat_id}/buttons")
    async def get_buttons(chat_id: int, session: Annotated[dict, Depends(_user)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        settings = db.get_notification_settings(chat_id)
        return {
            "global_style": settings["button_style"] or None,
            "emojis": db.get_button_emojis(chat_id),
            "platform_groups": db.get_platform_button_groups(chat_id),
            "subscription_labels": db.get_subscription_button_labels(chat_id),
            "subscription_groups": db.get_subscription_button_groups(chat_id),
            "button_styles": db.get_button_styles(chat_id),
            "custom_buttons": db.get_custom_buttons(chat_id),
            "subscriptions": [dict(row) for row in db.list_subscriptions(chat_id)],
            "styles": BUTTON_STYLES,
        }

    @app.put("/api/chats/{chat_id}/buttons")
    async def save_buttons(chat_id: int, data: ButtonSettingsInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if data.global_style and data.global_style not in BUTTON_STYLES:
            raise _error(422, "Некорректный цвет кнопок.")
        db.set_button_style(chat_id, data.global_style or "")
        for platform, group in data.platform_groups.items():
            db.set_platform_button_group(chat_id, platform, group)
        for platform, emoji in data.emojis.items():
            if platform in PLATFORM_NAMES and emoji.strip():
                db.set_button_emoji(chat_id, platform, emoji.strip())
        owned = {str(item["id"]) for item in db.list_subscriptions(chat_id)}
        for item_id, label in data.subscription_labels.items():
            if item_id in owned:
                db.set_subscription_button_label(chat_id, int(item_id), label.strip())
        for item_id, group in data.subscription_groups.items():
            if item_id in owned:
                db.set_subscription_button_group(chat_id, int(item_id), group)
        for key, style in data.button_styles.items():
            if style and style not in BUTTON_STYLES:
                raise _error(422, "Некорректный индивидуальный цвет.")
            db.set_individual_button_style(chat_id, key, style or None)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/buttons/custom")
    async def add_custom_button(chat_id: int, data: CustomButtonInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        parsed = urlparse(data.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _error(422, "Ссылка кнопки должна начинаться с http:// или https://.")
        db.add_custom_button(chat_id, data.label.strip(), data.url, data.group)
        index = len(db.get_custom_buttons(chat_id)) - 1
        db.set_custom_button_emoji(chat_id, index, data.emoji.strip(), None)
        db.set_individual_button_style(chat_id, f"custom:{index}", data.style)
        return {"index": index}

    @app.put("/api/chats/{chat_id}/buttons/custom/{index}")
    async def edit_custom_button(chat_id: int, index: int, data: CustomButtonInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        parsed = urlparse(data.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _error(422, "Ссылка кнопки должна начинаться с http:// или https://.")
        if (
            not db.set_custom_button_label(chat_id, index, data.label.strip())
            or not db.set_custom_button_url(chat_id, index, data.url)
            or not db.set_custom_button_group(chat_id, index, data.group)
        ):
            raise _error(404, "Кнопка не найдена.")
        db.set_custom_button_emoji(chat_id, index, data.emoji.strip(), None)
        db.set_individual_button_style(chat_id, f"custom:{index}", data.style)
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}/buttons/custom/{index}")
    async def delete_custom_button(chat_id: int, index: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if not db.remove_custom_button(chat_id, index):
            raise _error(404, "Кнопка не найдена.")
        return {"ok": True}

    @app.get("/api/chats/{chat_id}/schedule")
    async def get_schedule(chat_id: int, session: Annotated[dict, Depends(_user)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        return {
            "items": [dict(row) for row in db.list_schedule_items(chat_id)],
            "image_url": db.get_schedule_web_image_url(chat_id),
            "has_telegram_image": bool(db.get_schedule_image_file_id(chat_id)),
            "thread_id": db.get_schedule_thread_id(chat_id),
            "preview": _schedule_text(db, chat_id),
        }

    @app.post("/api/chats/{chat_id}/schedule/items")
    async def add_schedule_item(chat_id: int, data: ScheduleItemInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        return {"id": db.add_schedule_item(chat_id, data.weekday, data.time, data.title.strip(), data.description.strip())}

    @app.put("/api/chats/{chat_id}/schedule/items/{item_id}")
    async def edit_schedule_item(chat_id: int, item_id: int, data: ScheduleItemInput, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if not db.update_schedule_item(chat_id, item_id, data.weekday, data.time, data.title.strip(), data.description.strip()):
            raise _error(404, "Пункт расписания не найден.")
        return {"ok": True}

    @app.delete("/api/chats/{chat_id}/schedule/items/{item_id}")
    async def delete_schedule_item(chat_id: int, item_id: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if not db.delete_schedule_item(chat_id, item_id):
            raise _error(404, "Пункт расписания не найден.")
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/schedule/image")
    async def upload_schedule_image(chat_id: int, image: Annotated[UploadFile, File()], session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise _error(422, "Загрузите JPG, PNG или WebP.")
        content = await image.read()
        if not content or len(content) > 8 * 1024 * 1024:
            raise _error(422, "Файл должен быть не больше 8 МБ.")
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[image.content_type]
        filename = f"schedule-{chat_id}-{secrets.token_hex(8)}{extension}"
        destination = UPLOAD_DIR / filename
        destination.write_bytes(content)
        old = db.get_schedule_web_image_path(chat_id)
        db.set_schedule_web_image_path(chat_id, filename)
        if old:
            (UPLOAD_DIR / Path(old).name).unlink(missing_ok=True)
        return {"url": f"/uploads/{filename}"}

    @app.delete("/api/chats/{chat_id}/schedule/image")
    async def delete_schedule_image(chat_id: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        old = db.get_schedule_web_image_path(chat_id)
        db.set_schedule_web_image_path(chat_id, None)
        if old:
            (UPLOAD_DIR / Path(old).name).unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/chats/{chat_id}/schedule/publish")
    async def publish_schedule(chat_id: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)], request: Request) -> dict:
        _streamer_access(db, session["id"], chat_id)
        if not db.list_schedule_items(chat_id):
            raise _error(422, "Добавьте хотя бы один пункт перед публикацией.")
        application = request.app.state.bot_application
        loop = application.bot_data.get("event_loop") if application else None
        if not application or not loop:
            raise _error(503, "Публикация доступна только когда бот запущен вместе с сайтом.")
        text, thread_id = _schedule_text(db, chat_id), db.get_schedule_thread_id(chat_id)
        kwargs = {"message_thread_id": thread_id} if thread_id else {}

        async def telegram_call(coroutine):
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, loop))

        try:
            image_path = db.get_schedule_web_image_path(chat_id)
            telegram_image = db.get_schedule_image_file_id(chat_id)
            if image_path and (UPLOAD_DIR / Path(image_path).name).is_file() and len(text) <= 1024:
                with (UPLOAD_DIR / Path(image_path).name).open("rb") as image:
                    message = await telegram_call(
                        application.bot.send_photo(
                            chat_id, image, caption=text, parse_mode=ParseMode.HTML, **kwargs
                        )
                    )
                db.set_schedule_message(chat_id, message.message_id, has_photo=True)
            elif telegram_image and len(text) <= 1024:
                message = await telegram_call(
                    application.bot.send_photo(
                        chat_id, telegram_image, caption=text, parse_mode=ParseMode.HTML, **kwargs
                    )
                )
                db.set_schedule_message(chat_id, message.message_id, has_photo=True)
            else:
                message = await telegram_call(
                    application.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, **kwargs)
                )
                db.set_schedule_message(chat_id, message.message_id, has_photo=False)
        except Exception as error:
            raise _error(502, f"Telegram не принял публикацию: {error}")
        return {"ok": True, "message": "Расписание опубликовано в Telegram."}

    @app.get("/api/chats/{chat_id}/preview")
    async def notification_preview(chat_id: int, session: Annotated[dict, Depends(_user)], db: Annotated[Database, Depends(_db)]) -> dict:
        _streamer_access(db, session["id"], chat_id)
        settings = db.get_notification_settings(chat_id)
        sample = [("sample", type("Stream", (), {"title": "Тестовый эфир", "game_name": "Категория", "started_at": None})())]
        subscription = {"platform": "twitch"}
        text = format_live_notification([(subscription, sample[0][1])], settings["notification_template"], settings["notification_description"])
        return {"text": text, "buttons": db.get_custom_buttons(chat_id), "subscriptions": [dict(item) for item in db.list_subscriptions(chat_id)]}

    @app.post("/api/subscriptions/{subscription_id}/check")
    async def manual_check(subscription_id: int, session: Annotated[dict, Depends(_csrf)], db: Annotated[Database, Depends(_db)], request: Request) -> dict:
        owned = next((row for row in db.list_user_subscriptions(session["id"]) if row["id"] == subscription_id), None)
        if not owned:
            raise _error(404, "Подписка не найдена.")
        row = next((row for row in db.get_all_subscriptions() if row["id"] == subscription_id), None)
        if not row:
            raise _error(404, "Подписка не найдена.")
        try:
            stream = await fetch_live_stream(request.app.state.providers, row)
        except (RuntimeError, ValueError, httpx.HTTPError, KeyError) as error:
            raise _error(502, f"Проверка источника не удалась: {error}")
        return {"live": bool(stream), "title": stream.title if stream else None, "url": stream.url if stream else None, "category": stream.game_name if stream else None}

    return app
