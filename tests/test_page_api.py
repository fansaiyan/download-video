import unittest

import generic_player


class PageCodeTests(unittest.TestCase):
    def test_code_from_path_segment(self):
        self.assertEqual(
            generic_player._page_code("https://situs.test/e/uDerL2IEn1Y9n", ""),
            "uDerL2IEn1Y9n",
        )

    def test_code_from_query_parameter(self):
        self.assertEqual(
            generic_player._page_code("https://situs.test/watch?id=abc12345", ""),
            "abc12345",
        )

    def test_static_segment_is_not_treated_as_code(self):
        self.assertEqual(
            generic_player._page_code("https://situs.test/watch", ""), ""
        )


class ApiPayloadTests(unittest.TestCase):
    def test_codec_key_is_not_mistaken_for_file_code(self):
        self.assertEqual(generic_player._api_payload_value("codecs", "abc"), [])
        self.assertEqual(generic_player._api_payload_value("filecode", "abc"), "abc")

    def test_unknown_key_is_skipped(self):
        self.assertIsNone(generic_player._api_payload_value("somethingelse", "abc"))

    def test_extracts_payload_keys_from_stringify(self):
        page = (
            'fetch(url,{body:JSON.stringify({filecode:filecode,device:detectDevice(),'
            "codecs:cs})})"
        )
        self.assertEqual(
            generic_player._api_payload_keys(page), ["filecode", "device", "codecs"]
        )


class PageApiResolverTests(unittest.TestCase):
    PAGE = (
        '<html><title>Judul Halaman</title><script>'
        'let apiURL="/api/stream";'
        "fetch(apiURL,{method:'POST',body:JSON.stringify({filecode:filecode,"
        "device:'web',codecs:[]})})"
        "</script></html>"
    )
    URL = "https://situs.test/e/uDerL2IEn1Y9n"

    def test_resolves_media_from_json_api_response(self):
        calls = []

        def post(url, payload, referer=None):
            calls.append((url, payload, referer))
            return {
                "title": "Judul Video",
                "streaming_url": "https://cdn.test/hls/master.m3u8?token=abc",
                "subtitles": None,
            }

        result = generic_player.resolve_via_page_api(self.PAGE, self.URL, post)

        self.assertEqual(
            result,
            ("https://cdn.test/hls/master.m3u8?token=abc", "Judul Video"),
        )
        self.assertEqual(calls[0][0], "https://situs.test/api/stream")
        self.assertEqual(calls[0][1]["filecode"], "uDerL2IEn1Y9n")
        self.assertEqual(calls[0][1]["device"], "web")
        self.assertEqual(calls[0][1]["codecs"], [])
        self.assertEqual(calls[0][2], self.URL)

    def test_falls_back_to_page_title_when_api_title_empty(self):
        def post(url, payload, referer=None):
            return {"title": "", "streaming_url": "https://cdn.test/v.mp4"}

        result = generic_player.resolve_via_page_api(self.PAGE, self.URL, post)
        self.assertEqual(result, ("https://cdn.test/v.mp4", ""))

    def test_ignores_cross_origin_api_endpoint(self):
        page = self.PAGE.replace("/api/stream", "https://evil.test/api/stream")
        called = []
        generic_player.resolve_via_page_api(
            page, self.URL, lambda *a, **k: called.append(a) or {}
        )
        self.assertEqual(called, [])

    def test_returns_none_when_response_has_no_media(self):
        result = generic_player.resolve_via_page_api(
            self.PAGE, self.URL, lambda *a, **k: {"ok": True}
        )
        self.assertIsNone(result)

    def test_returns_none_without_post_helper(self):
        self.assertIsNone(
            generic_player.resolve_via_page_api(self.PAGE, self.URL, None)
        )

    def test_recursive_json_search_ignores_thumbnail_images(self):
        def post(url, payload, referer=None):
            return {
                "data": {
                    "thumbnail": "https://cdn.test/thumb.png",
                    "sources": [{"file": "https://cdn.test/video/movie.mp4"}],
                }
            }

        result = generic_player.resolve_via_page_api(self.PAGE, self.URL, post)
        self.assertEqual(result[0], "https://cdn.test/video/movie.mp4")


class GenericResolveApiIntegrationTests(unittest.TestCase):
    def test_resolve_generic_player_uses_api_probe(self):
        url = "https://situs.test/e/abc12345"

        def fetch(page_url, referer=None, timeout=30):
            return PageApiResolverTests.PAGE

        def post(api_url, payload, referer=None):
            return {"streaming_url": "https://cdn.test/hls/master.m3u8"}

        result = generic_player.resolve_generic_player(
            url, fetch, post_json=post
        )
        self.assertEqual(
            result, ("https://cdn.test/hls/master.m3u8", "Judul Halaman", url)
        )


if __name__ == "__main__":
    unittest.main()
