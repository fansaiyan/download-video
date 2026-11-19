"""Fallback terbatas untuk menemukan media pada halaman player HTML.

Tidak menjalankan JavaScript, melewati challenge browser, atau mengunduh seluruh situs.
"""

import base64
import html
from html.parser import HTMLParser
import json
import re
import urllib.parse

import yt_dlp

MEDIA_RE = re.compile(r"\.(?:mp4|m4v|webm|mkv|mov|avi|flv|ts|m3u8)(?:[?#]|$)", re.I)
DIRECT_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?\.(?:mp4|m4v|webm|mkv|mov|avi|flv|ts|m3u8)(?:\?[^\s\"'<>\\]*)?", re.I
)
PLAYER_KEY_RE = re.compile(
    r"\b(?:videoUrl|video_url|fileUrl|file_url|streamUrl|stream_url|contentUrl|hls|mp4|file|src|sources?)\s*[:=]\s*['\"]([^'\"]+)['\"]", re.I
)
B64_RE = re.compile(r"\b(?:atob\s*\(|data-(?:src|source|video)\s*=\s*)['\"]([A-Za-z0-9+/]{24,}={0,2})['\"]", re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
PACKED_RE = re.compile(r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*[dr]\s*\)", re.I)
PACKED_ARGS_RE = re.compile(
    r"\}\s*\(\s*(?P<q1>['\"])(?P<payload>(?:\\.|(?!(?P=q1)).)*)(?P=q1)\s*,\s*"
    r"(?P<radix>\d+)\s*,\s*(?P<count>\d+)\s*,\s*"
    r"(?P<q2>['\"])(?P<symbols>(?:\\.|(?!(?P=q2)).)*)(?P=q2)"
    r"\s*\.split\s*\(\s*['\"]\|['\"]\s*\)", re.S
)


class _PlayerHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.media = []
        self.frames = []
        self.scripts = []
        self._jsonld = False
        self._json_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("video", "source"):
            self.media.append(attrs.get("src") or attrs.get("data-src"))
        elif tag == "meta" and (attrs.get("property") or attrs.get("name") or "").lower() in (
            "og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"
        ):
            self.media.append(attrs.get("content"))
        elif tag == "iframe":
            self.frames.append((attrs.get("src") or attrs.get("data-src"), "allowfullscreen" in attrs))
        elif tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self._jsonld = True
            self._json_parts = []

    def handle_data(self, data):
        if self._jsonld:
            self._json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._jsonld:
            self.scripts.append("".join(self._json_parts))
            self._jsonld = False


def _unescape_js(value):
    # Decode escapes only, never eval executable code from a website.
    value = re.sub(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))",
                   lambda m: chr(int(m.group(1) or m.group(2), 16)), value)
    return re.sub(r"\\([/\\\"'])", r"\1", value)


def _media_url(value, base):
    if not isinstance(value, str):
        return None
    value = html.unescape(_unescape_js(value.strip()))
    if not value or value.lower().startswith(("javascript:", "data:", "blob:")):
        return None
    try:
        absolute = urllib.parse.urljoin(base, value)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            return None
        return absolute if MEDIA_RE.search(parts.path) else None
    except ValueError:
        return None


def _unpack(code):
    match = PACKED_ARGS_RE.search(code)
    if not match:
        return ""
    radix, count = int(match['radix']), int(match['count'])
    symbols = _unescape_js(match['symbols']).split('|')
    # Bounds prevent malformed input from causing huge loops or base-1 division.
    if not 2 <= radix <= 62 or count > 10000 or count > len(symbols):
        return ""
    digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    table = {}
    for index in range(count):
        number, key = index, ""
        while number:
            key = digits[number % radix] + key
            number //= radix
        table[key or "0"] = symbols[index] or key or "0"
    return re.sub(r"\b\w+\b", lambda m: table.get(m.group(), m.group()),
                  _unescape_js(match['payload']))


def _json_media(obj, found, base, depth=0):
    if depth > 8:
        return
    if isinstance(obj, list):
        for item in obj[:100]:
            _json_media(item, found, base, depth + 1)
    elif isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, str):
                media = _media_url(value, base)
                if media:
                    found.append(media)
            elif isinstance(value, (dict, list)):
                _json_media(value, found, base, depth + 1)


API_ENDPOINT_RE = re.compile(
    r"['\"]((?:https?://[^'\"\s<>]+)?/(?:api|ajax)/[^'\"\s<>]*"
    r"(?:stream|video|player|source|media|file|embed|watch)[^'\"\s<>]*)['\"]",
    re.I,
)
STRINGIFY_KEYS_RE = re.compile(r"JSON\.stringify\s*\(\s*\{([^{}]*)\}", re.S)
CODE_PARAM_NAMES = ("filecode", "file_code", "code", "id", "vid", "video_id", "slug")


def _page_code(page_url, page):
    """Kode video dari URL (segmen terakhir) atau query param yang lazim."""
    parts = urllib.parse.urlsplit(page_url)
    query = urllib.parse.parse_qs(parts.query)
    for name in CODE_PARAM_NAMES:
        if query.get(name) and query[name][0].strip():
            return query[name][0].strip()
    segments = [segment for segment in parts.path.split("/") if segment]
    static_names = {
        "watch", "video", "videos", "stream", "embed", "index", "home",
        "player", "e", "d", "v", "f", "media", "play",
    }
    if segments:
        last = segments[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", last) and last.lower() not in static_names:
            return last
    return ""


def _api_payload_keys(page):
    """Ambil nama field JSON yang dikirim halaman ke API (bila terlihat)."""
    keys = []
    for match in STRINGIFY_KEYS_RE.finditer(page):
        for key in re.findall(r"([A-Za-z_$][\w$]*)\s*:", match.group(1)):
            if key not in keys:
                keys.append(key)
    return keys


def _api_payload_value(key, code):
    """Isi nilai field payload berdasarkan namanya; None berarti dilewati."""
    low = key.lower()
    # Urutan penting: "codecs" mengandung "code" tetapi bukan kode video.
    if "codec" in low:
        return []
    if any(name in low for name in ("filecode", "file_code", "code", "video_id", "vid", "slug")):
        return code
    if low in ("id", "file", "fileid", "file_id"):
        return code
    if "device" in low:
        return "web"
    if "mobile" in low or "touch" in low:
        return False
    if "lang" in low:
        return "en"
    if low in ("tz", "timezone"):
        return ""
    return None


def resolve_via_page_api(page, page_url, post_json):
    """Coba API JSON milik halaman (pola umum pemutar berbasis server).

    Banyak situs tidak menaruh URL video di HTML, tetapi meminta server lewat
    endpoint seperti /api/stream. Fungsi ini hanya memanggil endpoint
    same-origin yang jelas berkaitan dengan video, lalu membaca URL media dari
    respons JSON. Mengembalikan (media_url, judul) atau None.
    """
    if post_json is None:
        return None
    parts = urllib.parse.urlsplit(page_url)
    if not parts.scheme or not parts.hostname:
        return None
    code = _page_code(page_url, page)
    if not code:
        return None
    keys = _api_payload_keys(page)

    endpoints = []
    for match in API_ENDPOINT_RE.finditer(page):
        endpoint = urllib.parse.urljoin(page_url, html.unescape(match.group(1)))
        endpoint_parts = urllib.parse.urlsplit(endpoint)
        # Hanya same-origin, agar tidak mengirim data ke pihak ketiga.
        if (endpoint_parts.scheme, endpoint_parts.netloc) != (parts.scheme, parts.netloc):
            continue
        if endpoint not in endpoints:
            endpoints.append(endpoint)

    for endpoint in endpoints[:3]:
        payload = {}
        for key in keys:
            value = _api_payload_value(key, code)
            if value is not None:
                payload[key] = value
        if not payload:
            payload = {"filecode": code}

        try:
            response = post_json(endpoint, payload, referer=page_url)
        except Exception:
            continue
        if not isinstance(response, (dict, list)):
            continue

        found = []
        _json_media(response, found, page_url)
        if not found:
            continue
        title = response.get("title") if isinstance(response, dict) else ""
        if not isinstance(title, str) or not title.strip():
            title = ""
        return found[0], title
    return None


def _extract(page, page_url):
    parser = _PlayerHTML()
    parser.feed(page)
    found = []
    for value in parser.media:
        media = _media_url(value, page_url)
        if media:
            found.append(media)
    for raw in parser.scripts[:10]:
        try:
            _json_media(json.loads(raw), found, page_url)
        except (ValueError, RecursionError):
            pass

    # Baca konfigurasi player dan JavaScript yang dipadatkan tanpa mengeksekusinya.
    blocks = [page]
    for match in list(PACKED_RE.finditer(page))[:3]:
        unpacked = _unpack(page[match.start():match.start() + 100_000])
        if unpacked:
            blocks.append(unpacked)
    # Compatibility: wrapper pages sometimes build a CDN URL using the filename
    # in their own URL rather than writing a complete media URL in the page.
    filename = urllib.parse.unquote(page_url.split('?', 1)[0].rsplit('/', 1)[-1])
    if MEDIA_RE.search(filename):
        for match in re.finditer(
            r"(?:VIDEO_BASE|VIDEO_URL|CDN_URL|BASE_URL|baseUrl|cdnUrl|base_url|cdn)"
            r"\s*=\s*['\"](https?://[^'\"]+)['\"]", page, re.I
        ):
            media = _media_url(match.group(1).rstrip('/') + '/' + filename, page_url)
            if media:
                found.append(media)
    for block in blocks:
        block = _unescape_js(block)
        for match in PLAYER_KEY_RE.finditer(block):
            media = _media_url(match.group(1), page_url)
            if media:
                found.append(media)
        for match in B64_RE.finditer(block):
            try:
                decoded = base64.b64decode(match.group(1), validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                continue
            media = _media_url(decoded, page_url)
            if media:
                found.append(media)
        # Iframe URL is handled by the parser/traversal below. Strip raw iframe
        # tags before URL scanning so a URL embedded inside a `data:` iframe is
        # never treated as a real media source.
        scan_block = re.sub(r"<iframe\b[^>]*>", "", block, flags=re.I)
        for match in DIRECT_URL_RE.finditer(scan_block):
            # Abaikan URL yang merupakan bagian dari URI berbahaya (javascript:, data:, blob:)
            boundary = max(
                scan_block.rfind('"', 0, match.start()),
                scan_block.rfind("'", 0, match.start()),
                scan_block.rfind(">", 0, match.start()),
                scan_block.rfind(" ", 0, match.start()),
                scan_block.rfind("\t", 0, match.start()),
                scan_block.rfind("\n", 0, match.start()),
            )
            leading = scan_block[boundary + 1:match.start()].strip().lower()
            if re.match(r"^(?:javascript|data|blob):", leading):
                continue
            media = _media_url(match.group(), page_url)
            if media:
                found.append(media)

    title = TITLE_RE.search(page)
    title = html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else ""
    return list(dict.fromkeys(found)), parser.frames, title


def resolve_generic_player(url, fetch, timeout=30, max_pages=5, max_depth=2, post_json=None):
    """Kembalikan (URL media, judul, referer halaman yang memuat media).

    Hanya iframe yang tampak seperti player ditelusuri, paling jauh dua tingkat.
    Referer adalah URL halaman pemutar, bukan domain induk atau URL media.
    """
    pending = [(url, None, 0)]
    visited = set()
    while pending and len(visited) < max_pages:
        page_url, parent, depth = pending.pop(0)
        if page_url in visited:
            continue
        visited.add(page_url)
        try:
            page = fetch(page_url, referer=parent, timeout=timeout)
        except Exception:
            if depth == 0:
                raise
            continue  # iframe iklan/rusak tidak menghalangi iframe player lain
        found, frames, title = _extract(page, page_url)
        if found:
            return found[0], title or "video", page_url

        # Sebagian pemutar tidak menulis URL media di HTML, melainkan meminta
        # server lewat API JSON (mis. POST /api/stream). Coba itu sebelum iframe.
        try:
            api_result = resolve_via_page_api(page, page_url, post_json)
        except Exception:
            api_result = None
        if api_result:
            media_url, api_title = api_result
            return media_url, api_title or title or "video", page_url

        if depth >= max_depth:
            continue
        for src, fullscreen in frames[:20]:
            if not src:
                continue
            frame = urllib.parse.urljoin(page_url, html.unescape(src))
            parts = urllib.parse.urlsplit(frame)
            if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
                continue
            # Jangan ikuti iframe iklan/analytics; player umumnya allowfullscreen
            # atau memiliki path embed/player yang jelas.
            if not fullscreen and not re.search(r"/(?:embed|e|player|video|watch)(?:/|\?|$)", frame, re.I):
                continue
            if frame not in visited and len(pending) + len(visited) < max_pages:
                pending.append((frame, page_url, depth + 1))
    raise yt_dlp.utils.DownloadError("Tidak menemukan tautan video di halaman pemutar atau embed-nya.")
