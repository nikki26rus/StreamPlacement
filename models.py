"""Модели предметной области."""

from dataclasses import dataclass


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
