import unittest
from unittest.mock import patch

import downloader


class DirectMediaUrlTests(unittest.TestCase):
    def test_direct_media_url_skips_html_parsing(self):
        instance = downloader.VideoDownloader()
        with (
            patch.object(
                instance,
                "_download_via_ytdlp",
                side_effect=downloader.yt_dlp.utils.DownloadError("Unsupported URL"),
            ),
            patch.object(downloader, "resolve_generic_player") as generic,
            patch.object(
                instance,
                "_download_media",
                return_value={"title": "film"},
            ) as media,
        ):
            result = instance.download("https://cdn.example.test/berkas/Film_Hebat.mp4")

        self.assertEqual(result, {"title": "film"})
        generic.assert_not_called()
        media.assert_called_once_with(
            "https://cdn.example.test/berkas/Film_Hebat.mp4",
            "Film_Hebat",
            referer="https://cdn.example.test/",
        )


if __name__ == "__main__":
    unittest.main()
