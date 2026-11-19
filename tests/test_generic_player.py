import base64
import unittest
from urllib.error import HTTPError

import yt_dlp

import generic_player


class FakeFetch:
    """Small deterministic fetch replacement used by resolver tests."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url, referer=None, timeout=30):
        self.calls.append((url, referer, timeout))
        result = self.pages[url]
        if isinstance(result, BaseException):
            raise result
        return result


class GenericPlayerExtractTests(unittest.TestCase):
    def test_extracts_video_source_and_supported_meta_tags(self):
        page = """
        <html>
          <head><title>Example &amp; Clip</title></head>
          <body>
            <video src="/media/main.mp4"></video>
            <video><source data-src="https://cdn.example.test/backup.webm?token=1"></video>
            <meta property="og:video" content="https://cdn.example.test/open.mp4">
            <meta property="og:video:url" content="https://cdn.example.test/url.m3u8">
            <meta property="og:video:secure_url" content="https://cdn.example.test/secure.mp4">
            <meta name="twitter:player:stream" content="https://cdn.example.test/twitter.m4v">
          </body>
        </html>
        """

        found, frames, title = generic_player._extract(
            page, "https://site.example.test/watch/page"
        )

        self.assertEqual(
            found,
            [
                "https://site.example.test/media/main.mp4",
                "https://cdn.example.test/backup.webm?token=1",
                "https://cdn.example.test/open.mp4",
                "https://cdn.example.test/url.m3u8",
                "https://cdn.example.test/secure.mp4",
                "https://cdn.example.test/twitter.m4v",
            ],
        )
        self.assertEqual(frames, [])
        self.assertEqual(title, "Example & Clip")

    def test_extracts_json_ld_video_urls_recursively(self):
        page = """
        <script type="application/ld+json">
        {
          "@type": "VideoObject",
          "name": "A video",
          "contentUrl": "/videos/main.mp4",
          "subjectOf": {
            "url": "https://cdn.example.test/alternate.m3u8"
          },
          "image": "https://cdn.example.test/poster.jpg"
        }
        </script>
        """

        found, _, _ = generic_player._extract(
            page, "https://site.example.test/article"
        )

        self.assertEqual(
            found,
            [
                "https://site.example.test/videos/main.mp4",
                "https://cdn.example.test/alternate.m3u8",
            ],
        )

    def test_extracts_jw_player_file_and_sources_configuration(self):
        page = """
        <script>
          jwplayer("movie").setup({
            playlist: [{
              title: "Movie",
              sources: [
                {file: "https://video.example.test/hls/movie.m3u8", type: "application/x-mpegURL"},
                {file: "https://video.example.test/mp4/movie.mp4", label: "720p"}
              ]
            }]
          });
        </script>
        """

        found, _, _ = generic_player._extract(page, "https://site.example.test/player")

        self.assertEqual(
            found,
            [
                "https://video.example.test/hls/movie.m3u8",
                "https://video.example.test/mp4/movie.mp4",
            ],
        )

    def test_extracts_escaped_javascript_urls(self):
        page = r'''<script>
          const config = {videoUrl: "https:\/\/cdn.example.test\/escaped\/movie.m3u8?x=1\u0026y=2"};
        </script>'''

        found, _, _ = generic_player._extract(page, "https://site.example.test/player")

        self.assertEqual(
            found,
            ["https://cdn.example.test/escaped/movie.m3u8?x=1&y=2"],
        )

    def test_extracts_base64_encoded_media_url(self):
        encoded = base64.b64encode(
            b"https://private-cdn.example.test/media/hidden_video.mp4"
        ).decode("ascii")
        page = '<div class="player" data-source="%s"></div>' % encoded

        found, _, _ = generic_player._extract(page, "https://site.example.test/player")

        self.assertEqual(
            found,
            ["https://private-cdn.example.test/media/hidden_video.mp4"],
        )

    def test_extracts_media_from_packed_javascript(self):
        page = r'''<script>
        eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\b'+c.toString(a)+'\\b','g'),k[c]);return p}('0 1="2://3/4/5.6";',7,7,'var|videoUrl|https|cdn.example.test|video|master|m3u8'.split('|'),0,{}))
        </script>'''

        found, _, _ = generic_player._extract(page, "https://site.example.test/player")

        self.assertEqual(found, ["https://cdn.example.test/video/master.m3u8"])

    def test_rejects_non_http_media_schemes(self):
        for value in (
            "javascript:https://cdn.example.test/movie.mp4",
            "data:video/mp4;base64,AAAA",
            "blob:https://site.example.test/1234",
        ):
            with self.subTest(value=value):
                self.assertIsNone(
                    generic_player._media_url(value, "https://site.example.test/player")
                )

        page = """
        <video src="javascript:alert(1)"></video>
        <source src="data:video/mp4;base64,AAAA"></source>
        <meta property="og:video" content="blob:https://site.example.test/id">
        """
        found, _, _ = generic_player._extract(page, "https://site.example.test/player")
        self.assertEqual(found, [])

    def test_does_not_salvage_http_urls_from_forbidden_schemes(self):
        for value in (
            "javascript:https://bad.example.test/movie.mp4",
            "data:video/mp4,https://bad.example.test/movie.mp4",
            "blob:https://bad.example.test/movie.mp4",
        ):
            with self.subTest(value=value):
                page = '<video src="%s"></video>' % value
                found, _, _ = generic_player._extract(
                    page, "https://site.example.test/player"
                )
                self.assertEqual(found, [])


class GenericPlayerResolveTests(unittest.TestCase):
    def test_follows_nested_player_iframes_and_passes_referers(self):
        root = "https://site.example.test/watch"
        first = "https://player.example.test/embed/one"
        second = "https://player.example.test/player/two"
        fetch = FakeFetch(
            {
                root: '<iframe src="%s" allowfullscreen></iframe>' % first,
                first: '<iframe src="%s"></iframe>' % second,
                second: (
                    "<title>Nested title</title>"
                    '<video src="/media/final.mp4"></video>'
                ),
            }
        )

        result = generic_player.resolve_generic_player(
            root, fetch, timeout=7, max_pages=5, max_depth=2
        )

        self.assertEqual(
            result,
            ("https://player.example.test/media/final.mp4", "Nested title", second),
        )
        self.assertEqual(
            fetch.calls,
            [
                (root, None, 7),
                (first, root, 7),
                (second, first, 7),
            ],
        )

    def test_bad_iframe_does_not_prevent_later_player_iframe(self):
        root = "https://site.example.test/watch"
        broken = "https://ads.example.test/embed/broken"
        player = "https://player.example.test/embed/good"
        fetch = FakeFetch(
            {
                root: (
                    '<iframe src="%s" allowfullscreen></iframe>'
                    '<iframe src="%s" allowfullscreen></iframe>'
                    % (broken, player)
                ),
                broken: ConnectionError("ad server unavailable"),
                player: '<source src="https://cdn.example.test/good.mp4">',
            }
        )

        result = generic_player.resolve_generic_player(root, fetch)

        self.assertEqual(
            result,
            ("https://cdn.example.test/good.mp4", "video", player),
        )
        self.assertEqual([call[0] for call in fetch.calls], [root, broken, player])

    def test_does_not_revisit_cyclic_iframes(self):
        root = "https://site.example.test/watch"
        child = "https://player.example.test/embed/child"
        fetch = FakeFetch(
            {
                root: '<iframe src="%s" allowfullscreen></iframe>' % child,
                child: '<iframe src="%s" allowfullscreen></iframe>' % root,
            }
        )

        with self.assertRaises(yt_dlp.utils.DownloadError):
            generic_player.resolve_generic_player(root, fetch, max_pages=10)

        self.assertEqual([call[0] for call in fetch.calls], [root, child])

    def test_honors_max_depth(self):
        root = "https://site.example.test/watch"
        first = "https://player.example.test/embed/one"
        second = "https://player.example.test/embed/two"
        fetch = FakeFetch(
            {
                root: '<iframe src="%s" allowfullscreen></iframe>' % first,
                first: '<iframe src="%s" allowfullscreen></iframe>' % second,
                second: '<video src="https://cdn.example.test/final.mp4">',
            }
        )

        with self.assertRaises(yt_dlp.utils.DownloadError):
            generic_player.resolve_generic_player(root, fetch, max_depth=1)

        self.assertEqual([call[0] for call in fetch.calls], [root, first])

    def test_honors_max_pages(self):
        root = "https://site.example.test/watch"
        first = "https://player.example.test/embed/one"
        second = "https://player.example.test/embed/two"
        fetch = FakeFetch(
            {
                root: '<iframe src="%s" allowfullscreen></iframe>' % first,
                first: '<iframe src="%s" allowfullscreen></iframe>' % second,
                second: '<video src="https://cdn.example.test/final.mp4">',
            }
        )

        with self.assertRaises(yt_dlp.utils.DownloadError):
            generic_player.resolve_generic_player(root, fetch, max_pages=2)

        self.assertEqual([call[0] for call in fetch.calls], [root, first])

    def test_preserves_root_fetch_http_error(self):
        root = "https://site.example.test/watch"
        error = HTTPError(root, 503, "Service Unavailable", None, None)
        self.addCleanup(error.close)
        fetch = FakeFetch({root: error})

        with self.assertRaises(HTTPError) as raised:
            generic_player.resolve_generic_player(root, fetch)

        self.assertIs(raised.exception, error)
        self.assertEqual(fetch.calls, [(root, None, 30)])

    def test_skips_javascript_data_and_blob_iframes(self):
        root = "https://site.example.test/watch"
        page = '''
        <iframe src="javascript:alert(1)" allowfullscreen></iframe>
        <iframe src="data:text/html;base64,PGgxPkFkPC9oMT4=" allowfullscreen></iframe>
        <iframe src="blob:https://site.example.test/frame-id" allowfullscreen></iframe>
        '''
        fetch = FakeFetch({root: page})

        with self.assertRaises(yt_dlp.utils.DownloadError):
            generic_player.resolve_generic_player(root, fetch)

        self.assertEqual(fetch.calls, [(root, None, 30)])


if __name__ == "__main__":
    unittest.main()
