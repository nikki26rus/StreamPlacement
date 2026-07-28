"""Конфигурация приложения, получаемая из переменных окружения BotHost."""

import os
from pathlib import Path


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "").strip()
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "").strip()
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "").strip()
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "").strip()
STREAM_PROXY_URL = os.getenv("STREAM_PROXY_URL", "").strip()
INSTAGRAM_COOKIE = os.getenv("INSTAGRAM_COOKIE", "").strip()
TIKTOK_COOKIE = os.getenv("TIKTOK_COOKIE", "").strip()
SCRAPE_USER_AGENT = os.getenv(
    "SCRAPE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
).strip()
FAST_POLL_INTERVAL_SECONDS = max(
    5, int(os.getenv("FAST_POLL_INTERVAL_SECONDS", "90"))
)
YOUTUBE_POLL_INTERVAL_SECONDS = max(
    30, int(os.getenv("YOUTUBE_POLL_INTERVAL_SECONDS", "300"))
)
SLOW_SCRAPE_POLL_INTERVAL_SECONDS = max(
    60, int(os.getenv("SLOW_SCRAPE_POLL_INTERVAL_SECONDS", "300"))
)
COMBINE_DELAY_SECONDS = max(
    0, int(os.getenv("COMBINE_DELAY_SECONDS", "0"))
)
NOTIFICATION_TIMEZONE = os.getenv("NOTIFICATION_TIMEZONE", "Europe/Moscow")
DB_PATH = Path(os.getenv("DB_PATH", "data/streams.db"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip()
WEB_PORT = max(1, min(65535, int(os.getenv("WEB_PORT", "8080"))))
WEB_AUTH_MAX_AGE_SECONDS = max(
    60, int(os.getenv("WEB_AUTH_MAX_AGE_SECONDS", "86400"))
)
WEB_COOKIE_SECURE = os.getenv("WEB_COOKIE_SECURE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
TELEGRAM_LOGIN_BOT_USERNAME = os.getenv("TELEGRAM_LOGIN_BOT_USERNAME", "").lstrip("@")
WEB_ENABLED = os.getenv("WEB_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
