import unittest

from bot import parse_public_platform_url, public_channel_url
from providers import parse_public_live_page


class PublicPlatformUrlTests(unittest.TestCase):
    def test_parses_supported_platform_urls(self) -> None:
        self.assertEqual(
            parse_public_platform_url("vk", "https://vk.com/club123"),
            "club123",
        )
        self.assertEqual(
            parse_public_platform_url("rutube", "https://rutube.ru/channel/12345/"),
            "12345",
        )
        self.assertEqual(
            parse_public_platform_url("instagram", "https://instagram.com/example/"),
            "example",
        )
        self.assertEqual(
            parse_public_platform_url("tiktok", "https://tiktok.com/@example/live"),
            "example",
        )

    def test_builds_live_urls(self) -> None:
        self.assertEqual(
            public_channel_url("instagram", "example", live=True),
            "https://www.instagram.com/example/live/",
        )
        self.assertEqual(
            public_channel_url("tiktok", "example", live=True),
            "https://www.tiktok.com/@example/live/",
        )

    def test_extracts_tiktok_live_page(self) -> None:
        stream = parse_public_live_page(
            "tiktok",
            (
                '<meta property="og:title" content="Live title">'
                '<meta property="og:image" content="https://image.example/cover.jpg">'
                '"status":2,"room_id":"123456789"'
            ),
            "https://www.tiktok.com/@example/live/",
        )
        self.assertIsNotNone(stream)
        assert stream is not None
        self.assertEqual(stream.stream_id, "123456789")
        self.assertEqual(stream.title, "Live title")

    def test_ignores_offline_page(self) -> None:
        self.assertIsNone(
            parse_public_live_page(
                "instagram",
                '"is_live":false,"broadcast_id":"123456789"',
                "https://www.instagram.com/example/live/",
            )
        )


if __name__ == "__main__":
    unittest.main()
