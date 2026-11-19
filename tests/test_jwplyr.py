import unittest
import urllib.error
from unittest.mock import patch

import downloader


class JwplyrDownloadTests(unittest.TestCase):
    def test_detects_jwplyr_url_with_query_parameters(self):
        handler = downloader.detect_site(
            "https://jwplyr.com/d/xl3jcx8adspy?path=d&id=xl3jcx8adspy"
        )
        self.assertIsNotNone(handler)
        self.assertEqual(handler["name"], "jwplyr")

    def test_reports_unreachable_resolved_media_server(self):
        downloader_instance = downloader.VideoDownloader()
        unreachable = urllib.error.URLError(TimeoutError("timed out"))
        with (
            patch.object(
                downloader, "detect_site",
                return_value={
                    "resolve": lambda url: ("https://mp4-01.overfetch.video/a/movie.mp4", "A movie"),
                    "referer": "https://jwplyr.com/",
                },
            ),
            patch.object(downloader_instance, "_download_direct", side_effect=unreachable),
        ):
            with self.assertRaises(downloader.yt_dlp.utils.DownloadError) as raised:
                downloader_instance.download("https://jwplyr.com/d/xl3jcx8adspy")

        message = str(raised.exception)
        self.assertIn("Sumber video ditemukan", message)
        self.assertIn("mp4-01.overfetch.video", message)
        self.assertEqual(downloader.friendly_error(raised.exception), message)

    def test_preserves_http_error_from_resolved_media_server(self):
        downloader_instance = downloader.VideoDownloader()
        http_error = urllib.error.HTTPError("https://media.example/video.mp4", 403, "Forbidden", {}, None)
        with (
            patch.object(
                downloader, "detect_site",
                return_value={
                    "resolve": lambda url: ("https://media.example/video.mp4", "A movie"),
                    "referer": "https://jwplyr.com/",
                },
            ),
            patch.object(downloader_instance, "_download_direct", side_effect=http_error),
        ):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                downloader_instance.download("https://jwplyr.com/d/xl3jcx8adspy")

        self.assertEqual(raised.exception.code, 403)
        http_error.close()


if __name__ == "__main__":
    unittest.main()
