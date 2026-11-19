import unittest
from unittest.mock import patch

import downloader


class SmartFallbackTests(unittest.TestCase):
    """Pastikan halaman yang gagal di satu mesin tetap dicoba mesin lain."""

    def test_site_resolver_failure_falls_back_to_another_engine(self):
        instance = downloader.VideoDownloader()
        handler = {
            "resolve": lambda url: (_ for _ in ()).throw(
                downloader.yt_dlp.utils.DownloadError("halaman berubah")
            ),
            "referer": "https://jwplyr.com/",
        }
        with (
            patch.object(downloader, "detect_site", return_value=handler),
            patch.object(
                instance, "_download_unknown_page", return_value={"title": "ok"}
            ) as fallback,
        ):
            result = instance.download("https://jwplyr.com/d/abcdef")

        self.assertEqual(result, {"title": "ok"})
        fallback.assert_called_once()
        self.assertIsInstance(fallback.call_args.args[1], Exception)

    def test_generic_search_runs_when_ytdlp_finds_no_formats(self):
        instance = downloader.VideoDownloader()
        error = downloader.yt_dlp.utils.DownloadError(
            "ERROR: [generic] page: No video formats found!"
        )
        with (
            patch.object(instance, "_download_via_ytdlp", side_effect=error),
            patch.object(
                downloader,
                "resolve_generic_player",
                return_value=("https://cdn.example/movie.mp4", "Judul", "https://situs/"),
            ),
            patch.object(
                instance,
                "_download_direct",
                return_value={"title": "Judul"},
            ) as direct,
        ):
            result = instance.download("https://situs-halaman-baru.example/tonton/1")

        self.assertEqual(result, {"title": "Judul"})
        direct.assert_called_once_with(
            "https://cdn.example/movie.mp4", "Judul", referer="https://situs/"
        )

    def test_youtube_errors_are_not_replaced_by_generic_search(self):
        instance = downloader.VideoDownloader()
        error = downloader.yt_dlp.utils.DownloadError(
            "ERROR: [youtube] abc: Video unavailable"
        )
        with (
            patch.object(instance, "_download_via_ytdlp", side_effect=error),
            patch.object(downloader, "resolve_generic_player") as generic,
        ):
            with self.assertRaises(downloader.yt_dlp.utils.DownloadError):
                instance.download("https://www.youtube.com/watch?v=abc")

        generic.assert_not_called()

    def test_clear_message_when_no_source_found(self):
        instance = downloader.VideoDownloader()
        with (
            patch.object(
                instance,
                "_download_via_ytdlp",
                side_effect=downloader.yt_dlp.utils.DownloadError("Unsupported URL"),
            ),
            patch.object(
                downloader,
                "resolve_generic_player",
                side_effect=downloader.yt_dlp.utils.DownloadError("kosong"),
            ),
        ):
            with self.assertRaises(downloader.yt_dlp.utils.DownloadError) as raised:
                instance.download("https://situs-baru.example/halaman")

        self.assertIn("Tidak menemukan sumber video", str(raised.exception))


class EmbedUrlTests(unittest.TestCase):
    """Variasi penulisan embed/player yang lazim di situs embed."""

    def test_finds_embed_url_variants(self):
        for page in (
            'var SRC = "/ip1?id=abc";',
            'const SRC = "/ip1?id=abc";',
            'SRC = "/ip1?id=abc";',
        ):
            with self.subTest(page=page):
                self.assertEqual(
                    downloader._find_embed_url(page, "https://jwplyr.com/d/x"),
                    "https://jwplyr.com/ip1?id=abc",
                )

    def test_finds_embed_iframe_then_player_path(self):
        page = '<iframe src="/e/abc123" allowfullscreen></iframe>'
        self.assertEqual(
            downloader._find_embed_url(page, "https://contoh.test/d/abc"),
            "https://contoh.test/e/abc123",
        )
        self.assertEqual(
            downloader._find_player_url(
                'playerPath = "https://contoh.test/stream.php?id=1"',
                "https://contoh.test/e/abc123",
            ),
            "https://contoh.test/stream.php?id=1",
        )

    def test_missing_embed_raises_clear_error(self):
        self.assertIsNone(downloader._find_embed_url("<html></html>", "https://x.test/"))


if __name__ == "__main__":
    unittest.main()
