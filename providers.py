"""Клиенты и функции разбора стриминговых провайдеров."""

import html
from io import BytesIO
import logging
import re
import time
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from config import (
    INSTAGRAM_COOKIE,
    KICK_CLIENT_ID,
    KICK_CLIENT_SECRET,
    REQUEST_TIMEOUT,
    SCRAPE_USER_AGENT,
    STREAM_PROXY_URL,
    TIKTOK_COOKIE,
    TWITCH_CLIENT_ID,
    TWITCH_CLIENT_SECRET,
)
from constants import (
    KICK_CHANNELS_URL,
    KICK_TOKEN_URL,
    PLATFORM_NAMES,
    TWITCH_GAMES_URL,
    TWITCH_STREAMS_URL,
    TWITCH_TOKEN_URL,
    TWITCH_USERS_URL,
)
from models import LiveStream


logger = logging.getLogger(__name__)


def parse_public_live_page(platform: str, body: str, url: str) -> LiveStream | None:
    """Извлекает минимальные данные эфира из публичной HTML/JSON-страницы."""
    live_patterns = {
        "vk": r'"(?:is_live|isLive|live)":(?:true|1)',
        "rutube": r'"(?:is_livestream|isLive|live)":(?:true|1)',
        "instagram": r'"(?:is_live|isLiveBroadcast|broadcast_status)":"?(?:true|LIVE|live|1)"?',
        "tiktok": r'"(?:status|is_live|isLive)":(?:"?2"?|true|1)',
    }
    if not re.search(live_patterns[platform], body):
        return None
    stream_id_match = re.search(
        r'"(?:room_id|broadcast_id|video_id|live_id|id)":"?([A-Za-z0-9_-]{6,})"?',
        body,
    )
    if not stream_id_match:
        return None
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', body)
    image_match = re.search(r'<meta property="og:image" content="([^"]+)"', body)
    return LiveStream(
        stream_id=stream_id_match.group(1),
        title=html.unescape(title_match.group(1)) if title_match else "Прямой эфир",
        url=url,
        thumbnail_url=html.unescape(image_match.group(1)) if image_match else None,
    )


class StreamProviders:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        proxy_kwargs = {"proxy": STREAM_PROXY_URL} if STREAM_PROXY_URL else {}
        self.scrape_client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": SCRAPE_USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
            **proxy_kwargs,
        )
        self.twitch_access_token: str | None = None
        self.twitch_token_expires_at = 0.0
        self.kick_access_token: str | None = None
        self.kick_token_expires_at = 0.0

    async def close(self) -> None:
        await self.client.aclose()
        await self.scrape_client.aclose()

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
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        if host not in {"youtube.com", "m.youtube.com"}:
            raise ValueError("Нужна ссылка на канал YouTube")

        path = parsed.path.strip("/")
        channel_id = ""
        if path.startswith("channel/"):
            channel_id = path.split("/", 1)[1].split("/", 1)[0]
        elif path.startswith("@"):
            response = await self.scrape_client.get(f"https://www.youtube.com/{path.split('/', 1)[0]}")
            response.raise_for_status()
            match = re.search(r'"externalId":"(UC[\w-]+)"', response.text)
            if not match:
                raise ValueError("Не удалось определить ID канала YouTube")
            channel_id = match.group(1)
        else:
            raise ValueError(
                "Поддерживаются ссылки вида youtube.com/channel/UC... "
                "или youtube.com/@название"
            )
        response = await self.scrape_client.get(
            f"https://www.youtube.com/channel/{channel_id}"
        )
        response.raise_for_status()
        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', response.text)
        title = html.unescape(title_match.group(1)) if title_match else channel_id
        return channel_id, title, f"https://www.youtube.com/channel/{channel_id}"

    async def youtube_live(self, channel_id: str) -> LiveStream | None:
        response = await self.scrape_client.get(
            f"https://www.youtube.com/channel/{channel_id}/live"
        )
        response.raise_for_status()
        canonical = re.search(
            r'<link rel="canonical" href="https?://www\.youtube\.com/watch\?v=([^"&]+)',
            response.text,
        )
        if not canonical or '"isLiveContent":true' not in response.text:
            return None
        video_id = canonical.group(1)
        title_match = re.search(r'"title":"([^"]+)"', response.text)
        thumbnail_match = re.search(r'"thumbnail":\{"thumbnails":\[\{"url":"([^"]+)', response.text)
        return LiveStream(
            stream_id=video_id,
            title=html.unescape(title_match.group(1)) if title_match else "Без названия",
            url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=thumbnail_match.group(1).replace(r"\u0026", "&") if thumbnail_match else None,
        )

    async def public_channel(self, platform: str, key: str) -> tuple[str, str, str]:
        url = public_channel_url(platform, key)
        headers = {}
        if platform == "instagram" and INSTAGRAM_COOKIE:
            headers["Cookie"] = INSTAGRAM_COOKIE
        if platform == "tiktok" and TIKTOK_COOKIE:
            headers["Cookie"] = TIKTOK_COOKIE
        response = await self.scrape_client.get(url, headers=headers)
        response.raise_for_status()
        title_match = re.search(r'<meta property="og:title" content="([^"]+)"', response.text)
        name = html.unescape(title_match.group(1)) if title_match else key
        return key, name, url

    async def public_live(self, platform: str, key: str) -> LiveStream | None:
        url = public_channel_url(platform, key, live=True)
        headers = {}
        if platform == "instagram" and INSTAGRAM_COOKIE:
            headers["Cookie"] = INSTAGRAM_COOKIE
        if platform == "tiktok" and TIKTOK_COOKIE:
            headers["Cookie"] = TIKTOK_COOKIE
        response = await self.scrape_client.get(url, headers=headers)
        response.raise_for_status()
        return parse_public_live_page(platform, response.text, str(response.url))

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


def parse_public_platform_url(platform: str, url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.strip("/")
    hosts = {
        "vk": {"vk.com", "vkvideo.ru"},
        "rutube": {"rutube.ru"},
        "instagram": {"instagram.com"},
        "tiktok": {"tiktok.com"},
    }
    if platform not in hosts or host not in hosts[platform]:
        raise ValueError(f"Нужна ссылка на канал {PLATFORM_NAMES[platform]}")
    if platform == "tiktok":
        key = path.removeprefix("@").split("/", 1)[0]
    elif platform == "rutube" and path.startswith("channel/"):
        key = path.split("/", 2)[1]
    else:
        key = path.split("/", 1)[0].removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", key):
        raise ValueError("Не удалось определить канал по ссылке")
    return key.lower()


def public_channel_url(platform: str, key: str, *, live: bool = False) -> str:
    base = {
        "vk": f"https://vk.com/{key}",
        "rutube": f"https://rutube.ru/channel/{key}/",
        "instagram": f"https://www.instagram.com/{key}/",
        "tiktok": f"https://www.tiktok.com/@{key}",
    }[platform]
    return f"{base.rstrip('/')}/live/" if live and platform in {"instagram", "tiktok"} else base


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
