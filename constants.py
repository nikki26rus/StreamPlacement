"""Неизменяемые значения, используемые в интерфейсе и провайдерах."""

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
    "vk": "🔵",
    "rutube": "🟠",
    "instagram": "🟣",
    "tiktok": "⚫",
}
PLATFORM_NAMES = {
    "twitch": "Twitch",
    "youtube": "YouTube",
    "kick": "Kick",
    "vk": "VK",
    "rutube": "Rutube",
    "instagram": "Instagram",
    "tiktok": "TikTok",
}
SCRAPE_PLATFORMS = {"youtube", "vk", "rutube", "instagram", "tiktok"}
SLOW_SCRAPE_PLATFORMS = {"instagram", "tiktok"}
BUTTON_STYLES = {
    "primary": "Синий",
    "success": "Зелёный",
    "danger": "Красный",
}
