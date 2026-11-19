"""
Video Downloader App
--------------------
Aplikasi sederhana untuk download video dari website cukup dengan memasukkan URL.
Dibangun di atas yt-dlp, yang mendukung ribuan situs (YouTube, Facebook, Instagram,
TikTok, Twitter/X, Vimeo, Dailymotion, dan banyak lagi), termasuk situs yang penuh iklan.

Fitur:
- Antarmuka grafis (GUI tkinter) dan baris perintah (CLI)
- Batch download: tempel banyak URL sekaligus (satu per baris)
- Cookies dari browser: otomatis memakai sesi login Chrome/Edge/Firefox/dll. (termasuk profil, mis. chrome:Profile 1)
- SponsorBlock: otomatis lewati segmen iklan/sponsor di YouTube (butuh FFmpeg)
- Retry otomatis & timeout agar tahan terhadap jaringan tidak stabil
- Pilihan kualitas: terbaik, 1080p, 720p, 480p, 360p, hanya audio
- Deteksi FFmpeg dengan fallback otomatis
- Pesan error yang ramah dalam bahasa Indonesia
- **Beberapa video bersamaan** (maksimal 5) dengan tabel progres per video
- Dukungan khusus **filecoster.com** dan **jwplyr.com** (link `/d/`, `/e/`, folder `/f/`, termasuk pagination)
- Tombol **Batal** untuk menghentikan semua unduhan & sisa antrean

Cara pakai:
    python downloader.py                                  # buka GUI
    python downloader.py --url "https://..."              # unduh 1 video
    python downloader.py --url "https://..." --url "..."  # unduh beberapa video
    python downloader.py --file daftar.txt                # unduh dari file daftar URL
"""

import argparse
import html
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import ttk, filedialog, messagebox

import yt_dlp

from generic_player import resolve_generic_player as _resolve_generic_player


# FFmpeg dibutuhkan untuk menggabungkan video+audio dan memotong segmen iklan.
HAS_FFMPEG = shutil.which("ffmpeg") is not None


# Deteksi JavaScript Runtime untuk menyelesaikan JS challenge (mis. YouTube EJS)
def _detect_js_runtime():
    """Deteksi runtime JavaScript yang tersedia (Deno, Node, QuickJS, Bun).

    yt-dlp membutuhkan runtime JS eksternal untuk menyelesaikan challenge YouTube.
    Deno aktif otomatis secara default di yt-dlp, sedangkan Node/QuickJS/Bun
    perlu didaftarkan eksplisit lewat opsi js_runtimes.
    """
    if shutil.which("deno"):
        return {"deno": {}}
    if shutil.which("node"):
        return {"node": {}}
    if shutil.which("qjs") or shutil.which("quickjs"):
        return {"quickjs": {}}
    if shutil.which("bun"):
        return {"bun": {}}
    return None


JS_RUNTIMES_OPT = _detect_js_runtime()


def parse_browser_spec(spec):
    """Pisahkan ``chrome:Profile 1`` menjadi ("chrome", "Profile 1")."""
    text = (spec or "").strip()
    if not text or text.lower() == "none":
        return None, None
    for separator in (":", "/"):
        if separator in text:
            name, profile = text.split(separator, 1)
            name = name.strip().lower()
            profile = profile.strip()
            if not name:
                return None, None
            if profile.lower() in ("", "default"):
                profile = None
            return name, profile
    return text.lower(), None


def cookies_from_browser_option(spec):
    """Ubah spek cookies menjadi tuple yang diterima yt-dlp.

    Spek boleh berupa nama browser saja (``chrome``) atau dengan profil
    (``chrome:Profile 1``). Mengembalikan None bila cookies tidak dipakai.
    """
    name, profile = parse_browser_spec(spec)
    if not name:
        return None
    return (name, profile) if profile else (name,)


# ---------------------------------------------------------------------------
# Konfigurasi default untuk menghindari iklan / pembatasan situs
# ---------------------------------------------------------------------------
DEFAULT_OPTS = {
    "noplaylist": True,          # nilai default; diatur per unduhan di _build_opts()
    "ignoreerrors": False,
    "no_warnings": False,
    "restrictfilenames": True,   # aman untuk nama file di Windows
    "windowsfilenames": True,
    "socket_timeout": 30,        # batas waktu koneksi (detik)
    "retries": 10,               # ulangi saat gagal
    "fragment_retries": 10,
    "file_access_retries": 5,
    # User-Agent agar situs tidak mudah memblokir bot
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        "Referer": "https://www.google.com/",
    },
}

# Pemetaan pilihan kualitas ke format yt-dlp (saat FFmpeg tersedia)
QUALITY_MAP = {
    "terbaik": "bestvideo+bestaudio/best",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "audio only": "bestaudio/best",
}

# Pemetaan kualitas saat FFmpeg TIDAK tersedia (hanya satu file, tanpa merge)
QUALITY_MAP_NO_FFMPEG = {
    "terbaik": "best[height<=1080]/best",
    "1080p": "best[height<=1080]/best",
    "720p": "best[height<=720]/best",
    "480p": "best[height<=480]/best",
    "360p": "best[height<=360]/best",
    "audio only": "bestaudio/best",
}

BROWSERS = ["none", "chrome", "edge", "firefox", "brave", "opera", "chromium", "vivaldi"]


# Pola URL halaman filecoster.com, mis. https://filecoster.com/d/<id> atau /e/<id>
FILECOSTER_URL_RE = re.compile(
    r"https?://(?:www\.)?filecoster\.com/(?:d|e)/([A-Za-z0-9]+)", re.IGNORECASE
)
# Pola halaman folder, mis. https://filecoster.com/f/<id> (berisi daftar video)
FILECOSTER_FOLDER_RE = re.compile(
    r"https?://(?:www\.)?filecoster\.com/f/([A-Za-z0-9]+)", re.IGNORECASE
)
FILECOSTER_UA = DEFAULT_OPTS["http_headers"]["User-Agent"]

# Pola URL jwplyr.com (platform embed yang strukturnya sama dengan filecoster),
# mis. https://jwplyr.com/d/<id>, /e/<id>, atau folder /f/<id>
JWPLYR_URL_RE = re.compile(
    r"https?://(?:www\.)?jwplyr\.com/(?:d|e)/([A-Za-z0-9]+)", re.IGNORECASE
)
JWPLYR_FOLDER_RE = re.compile(
    r"https?://(?:www\.)?jwplyr\.com/f/([A-Za-z0-9]+)", re.IGNORECASE
)

# Pola URL halaman slicedrive.cloud (SPA dengan #/), mis. https://cdn2.slicedrive.cloud/#/k3491Tk.mp4
SLICEDRIVE_URL_RE = re.compile(
    r"https?://(?:[^/]+\.)?slicedrive\.cloud(?:[/#]|$)", re.IGNORECASE
)

# Ekstensi/media yang bisa diunduh langsung (termasuk HLS)
MEDIA_EXT_RE = re.compile(
    r"\.(?:mp4|m4v|webm|mkv|mov|avi|flv|ts|m3u8)(?:[?#]|$)", re.IGNORECASE
)

# Folder penyimpanan default: <folder project>/video
DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "video")

# Pengaturan unduhan paralel (mempercepat unduhan)
DEFAULT_THREADS = 8
MAX_CONCURRENT_DOWNLOADS = 5   # maksimal video yang diunduh bersamaan (GUI)
MIN_PARALLEL_SIZE = 2 * 1024 * 1024   # di bawah 2 MB, paralel tidak sepadan
MIN_CHUNK_SIZE = 512 * 1024           # minimal 512 KB per bagian
READ_CHUNK = 1024 * 1024              # ukuran baca per iterasi (1 MB)
MAX_RETRIES_PER_PART = 5


def is_unsupported_error(exc):
    """True jika yt-dlp tidak dapat mengekstrak format dan fallback HTML aman dicoba."""
    text = str(exc).lower()
    return any(marker in text for marker in (
        "unsupported url",
        "no suitable extractor",
    )) or ("[generic]" in text and "no video formats found" in text)


def format_size(num_bytes):
    """Format ukuran byte menjadi teks ringkas (B/KB/MB/GB/TB)."""
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "?"
    if value < 1024:
        return f"{value:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
    return f"{value:.2f} TB"


def format_eta(seconds):
    """Format ETA (detik) menjadi teks yang mudah dibaca."""
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}j {minutes:02d}m {sec:02d}s"


def friendly_error(exc):
    """Ubah error teknis yt-dlp menjadi pesan yang mudah dipahami."""
    text = str(exc)
    low = text.lower()
    # yt-dlp membungkus error HTTP dengan "Unable to download webpage".
    # Periksa kode HTTP lebih dulu agar penolakan server tidak dianggap error DNS.
    status = getattr(exc, "code", None)
    http_match = re.search(r"\bHTTP(?:\s+Error)?\s*:?\s*(\d{3})\b", text, re.I)
    if http_match:
        status = int(http_match.group(1))
    if status == 403:
        return (
            "Server menolak akses (HTTP 403 Forbidden). Tautan mungkin kedaluwarsa "
            "atau membutuhkan sesi login / halaman pemutar asal. Coba tempel URL "
            "halaman pemutar, bukan URL CDN langsung."
        )
    if status == 401:
        return "Server meminta autentikasi (HTTP 401). Gunakan halaman asal dan sesi login yang valid."
    if status in (404, 410):
        return f"Video tidak ditemukan atau tautan sudah tidak berlaku (HTTP {status})."
    if status == 429:
        return "Terlalu banyak permintaan (HTTP 429). Tunggu lalu coba lagi dengan lebih sedikit video/koneksi paralel."
    if isinstance(status, int) and 500 <= status <= 599:
        return f"Server situs sedang bermasalah (HTTP {status}). Coba lagi nanti."
    if "getaddrinfo" in low or "connection" in low or "timed out" in low or "10060" in low:
        return (
            "Gagal terhubung ke server video. Server mungkin sedang bermasalah atau "
            "jalur jaringan ke server tersebut terputus. Coba beberapa saat lagi atau "
            "gunakan jaringan/VPN lain. Periksa juga koneksi internet kamu."
        )
    if "sign in to confirm" in low or "age" in low or "login" in low or "private" in low:
        return (
            "Video membutuhkan login / verifikasi umur. Pilih browser pada opsi "
            "'Cookies dari browser' agar yt-dlp memakai sesi login browser kamu."
        )
    if "ffmpeg" in low:
        return (
            "FFmpeg tidak ditemukan. Install FFmpeg agar video dan audio dapat "
            "digabung. Lihat README untuk caranya."
        )
    if "video unavailable" in low or "not available" in low or "does not exist" in low:
        if "youtube" in low:
            return (
                "Video YouTube tidak tersedia. Pastikan URL video benar, video tidak "
                "dihapus/di-private, atau coba pilih 'Cookies dari browser' bila video "
                "memerlukan akun/verifikasi."
            )
        return "Video tidak tersedia atau tautan sudah tidak valid."
    if "unsupported url" in low:
        return "Situs ini belum didukung oleh yt-dlp."
    if "requested format is not available" in low:
        return "Kualitas yang dipilih tidak tersedia untuk video ini. Coba pilih 'terbaik'."
    if "filecoster" in low:
        return (
            "Gagal membaca halaman filecoster.com. Buka dulu link di browser untuk "
            "memastikan masih aktif, lalu coba lagi. Jika tetap gagal, situs mungkin "
            "sedang mengubah halaman/iklan."
        )
    if "slicedrive" in low:
        return (
            "Gagal membaca daftar video slicedrive. Pastikan link masih aktif dan "
            "video tersedia (bukan link yang sudah dihapus), lalu coba lagi."
        )
    if "sumber video ditemukan, tetapi server file" in low:
        return text
    if "jwplyr" in low:
        return (
            "Gagal membaca halaman jwplyr. Pastikan link masih aktif dan video "
            "tersedia (bukan link yang sudah dihapus), lalu coba lagi."
        )
    return text


# ---------------------------------------------------------------------------
# Dukungan khusus filecoster.com (situs file-hosting dengan iklan)
# ---------------------------------------------------------------------------
def is_filecoster(url):
    return bool(FILECOSTER_URL_RE.search(url or ""))


def _js_unescape(value):
    """Unescape escape sequence JS sederhana (mis. \\u0026 menjadi &)."""
    return (
        value.replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u003f", "?")
        .replace("\\/", "/")
    )


def _http_get(url, referer=None, timeout=30, retries=4):
    headers = {
        "User-Agent": FILECOSTER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        "Connection": "keep-alive",
    }
    if referer:
        headers["Referer"] = referer

    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500:  # 4xx tidak akan membaik dengan retry
                raise
        except urllib.error.URLError as exc:
            last_error = exc
        time.sleep(min(1.5 * (attempt + 1), 5))

    raise last_error


def _find_embed_url(page, page_url):
    """Cari URL iframe/embed dari variasi kode yang umum di situs embed."""
    patterns = (
        # var/let/const SRC = "...", SRC = "...", src: "..."
        r"(?:var|let|const)\s+(?:SRC|src)\s*=\s*['\"]([^'\"]+)['\"]",
        r"(?<![\w.])SRC\s*=\s*['\"]([^'\"]+)['\"]",
        r"['\"]?(?:embedUrl|embed_url|playerUrl|player_url)['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, page)
        if match:
            return urllib.parse.urljoin(page_url, _js_unescape(match.group(1)))

    # Beberapa halaman menulis iframe embed langsung di HTML.
    for tag in re.findall(r"<iframe\b[^>]*>", page, flags=re.IGNORECASE):
        match = re.search(r"\bsrc\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = urllib.parse.urljoin(page_url, html.unescape(_js_unescape(match.group(1))))
        if re.search(r"/(?:e|embed|player|video|watch)(?:[/?]|$)", candidate, re.I):
            return candidate
    return None


def _find_player_url(embed, embed_url):
    """Cari URL player dari variabel playerPath atau konfigurasi player lain."""
    for pattern in (
        r"playerPath\s*=\s*[\"']([^\"']+)[\"']",
        r"['\"]?playerPath['\"]?\s*:\s*['\"]([^'\"]+)['\"]",
        r"(?:player_?url|video_?url|stream_?url)['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
    ):
        match = re.search(pattern, embed, flags=re.IGNORECASE)
        if match:
            return urllib.parse.urljoin(embed_url, _js_unescape(match.group(1)))
    return None


def _resolve_embed_page(url, label, timeout=30):
    """Alur umum situs embed: halaman -> SRC -> playerPath -> <source>.

    Dipakai oleh situs yang memakai struktur player sama (filecoster, jwplyr).
    """
    page = _http_get(url, timeout=timeout)

    embed_url = _find_embed_url(page, url)
    if not embed_url:
        raise yt_dlp.utils.DownloadError(
            f"{label}: embed player tidak ditemukan (halaman mungkin berubah)."
        )

    embed = _http_get(embed_url, referer=url, timeout=timeout)
    player_url = _find_player_url(embed, embed_url)
    if not player_url:
        raise yt_dlp.utils.DownloadError(
            f"{label}: link player tidak ditemukan (mungkin butuh cookies / iklan)."
        )

    player = _http_get(player_url, referer=embed_url, timeout=timeout)
    match = re.search(r"<source[^>]+src=[\"']([^\"']+)[\"']", player)
    if not match:
        match = re.search(r"<video[^>]+src=[\"']([^\"']+)[\"']", player)
    if match:
        media_url = match.group(1)
    else:
        # Manifest HLS/DASH lebih baik diserahkan ke yt-dlp agar semua kualitas terbaca.
        fallback = re.search(
            r"https?://[^\"'\\\s]+\.(?:mp4|m3u8|mpd)[^\"'\\\s]*", player
        )
        if not fallback:
            raise yt_dlp.utils.DownloadError(
                f"{label}: URL video tidak ditemukan (mungkin butuh cookies / iklan)."
            )
        media_url = fallback.group(0)
    media_url = urllib.parse.urljoin(
        player_url, html.unescape(_js_unescape(media_url))
    )

    title_match = re.search(r"<title>(.*?)</title>", player, re.S) or re.search(
        r"<title>(.*?)</title>", page, re.S
    )
    title = html.unescape(title_match.group(1)).strip() if title_match else "video"
    return media_url, title


def resolve_filecoster(url, timeout=30):
    """Ubah halaman filecoster.com menjadi (url_media_langsung, judul)."""
    return _resolve_embed_page(url, "filecoster", timeout=timeout)


def resolve_jwplyr(url, timeout=30):
    """Ubah halaman jwplyr.com menjadi (url_media_langsung, judul)."""
    return _resolve_embed_page(url, "jwplyr", timeout=timeout)


def is_filecoster_folder(url):
    return bool(FILECOSTER_FOLDER_RE.search(url or ""))


def _expand_embed_folder(url, label, timeout=30):
    """Ambil semua URL video (/d/ atau /e/) dari halaman folder /f/<id>.

    Mendukung pagination (?p=N). Mengembalikan daftar URL unik.
    """
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    first_page = _http_get(url, timeout=timeout)
    page_numbers = [int(n) for n in re.findall(r"[?&]p=(\d+)", first_page)]
    max_page = max(page_numbers) if page_numbers else 1

    pages = [first_page]
    for page_number in range(2, max_page + 1):
        try:
            pages.append(
                _http_get(f"{base}?p={page_number}", referer=url, timeout=timeout)
            )
        except Exception:
            continue  # lewati halaman yang gagal, lanjutkan yang lain

    seen = set()
    videos = []
    for page in pages:
        for match in re.finditer(
            r'href=["\'](/[de]/([A-Za-z0-9]+))["\']', page
        ):
            path, video_id = match.group(1), match.group(2)
            if video_id in seen:
                continue
            seen.add(video_id)
            videos.append(urllib.parse.urljoin(origin + "/", path))

    if not videos:
        raise yt_dlp.utils.DownloadError(
            f"{label}: folder tidak berisi video yang bisa diunduh."
        )
    return videos


def expand_filecoster_folder(url, timeout=30):
    """Ambil semua URL video dari halaman folder filecoster /f/<id>."""
    return _expand_embed_folder(url, "filecoster", timeout=timeout)


def expand_jwplyr_folder(url, timeout=30):
    """Ambil semua URL video dari halaman folder jwplyr /f/<id>."""
    return _expand_embed_folder(url, "jwplyr", timeout=timeout)


# ---------------------------------------------------------------------------
# Dukungan khusus slicedrive.cloud (SPA + daftar videos.json)
# ---------------------------------------------------------------------------
def is_slicedrive(url):
    return bool(SLICEDRIVE_URL_RE.search(url or ""))


def _slicedrive_video_id(url):
    """Ambil ID video dari URL, mendukung format #/<id> maupun /<id>."""
    parts = urllib.parse.urlsplit(url)
    raw = parts.fragment or parts.path
    raw = urllib.parse.unquote(raw).lstrip("/")
    return raw.split("?")[0]


def resolve_slicedrive(url, timeout=30):
    """Ubah URL slicedrive.cloud menjadi (url_media_langsung, judul).

    Halaman SPA mengambil daftar video dari videos.json, lalu memutar field 'cdn'
    milik entri yang id-nya cocok. Kita lakukan hal yang sama tanpa browser.
    """
    parts = urllib.parse.urlsplit(url)
    video_id = _slicedrive_video_id(url)
    if not video_id:
        raise yt_dlp.utils.DownloadError(
            "slicedrive: ID video tidak ditemukan di URL."
        )

    origin = f"{parts.scheme}://{parts.netloc}"
    json_url = urllib.parse.urljoin(origin + "/", "videos.json")
    try:
        data = json.loads(_http_get(json_url, referer=origin + "/", timeout=timeout))
    except json.JSONDecodeError:
        raise yt_dlp.utils.DownloadError(
            "slicedrive: daftar videos.json tidak valid / tidak dapat dibaca."
        )

    videos = data.get("videos") if isinstance(data, dict) else data
    if not isinstance(videos, list):
        raise yt_dlp.utils.DownloadError(
            "slicedrive: format videos.json tidak dikenal."
        )

    entry = next(
        (
            item
            for item in videos
            if isinstance(item, dict) and item.get("id") == video_id
        ),
        None,
    )
    if entry is None:
        # Coba cocokkan tanpa memperhatikan ekstensi / huruf besar-kecil
        target = video_id.lower()
        entry = next(
            (
                item
                for item in videos
                if isinstance(item, dict)
                and str(item.get("id", "")).lower() in (target, target + ".mp4")
            ),
            None,
        )
    if entry is None or not entry.get("cdn"):
        raise yt_dlp.utils.DownloadError(
            f"slicedrive: video '{video_id}' tidak ditemukan di daftar videos.json."
        )

    media_url = html.unescape(entry["cdn"])
    title = re.sub(r"\.(mp4|m3u8|webm|mkv)$", "", video_id, flags=re.IGNORECASE)
    return media_url, title or "video"


# Layanan short link yang perlu diikuti redirect-nya ke URL asli.
# Bandingkan dengan hostname tanpa "www." (termasuk subdomain).
SHORTENER_HOSTS = {
    "bit.ly", "bitly.com", "t.co", "tinyurl.com", "tiny.cc", "goo.gl",
    "is.gd", "v.gd", "cutt.ly", "s.id", "rebrand.ly", "shorturl.at",
    "ow.ly", "buff.ly", "rb.gy", "lnkd.in", "t.ly", "short.link",
    "shrtco.de", "9qr.de", "kutt.it", "srt.am", "clck.ru", "u.to",
    "vk.cc", "dlvr.it", "ift.tt", "trib.al", "adf.ly", "sh.st",
    "ouo.io", "linkvertise.com", "linkvertise.net", "boost.ink",
    "mboost.me", "safelinku.net", "gplinks.co", "gplinks.in",
    "tnlink.in", "za.gl", "zee.gl", "link.sbstck.com", "share.whapi.io",
    "spotify.link", "apple.co", "amzn.to", "amzn.eu", "fb.me",
    "instagr.am", "redd.it", "shorte.st", "tiny.one", "urlz.fr",
    "shorturl.gg", "n9.cl", "l1nq.com", "surl.li", "tny.im", "clck.pro",
    # short link dari situs konten (mengarah ke halaman video asli)
    "vt.tiktok.com", "vm.tiktok.com", "fb.watch", "dai.ly", "pin.it",
    "b23.tv", "xhslink.com", "v.douyin.com",
}

# Batas jumlah lompatan redirect agar tidak berputar tanpa henti
REDIRECT_MAX_HOPS = 8
_REDIRECT_CACHE = {}


def _url_host(url):
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_short_url(url):
    """True jika URL memakai layanan short link yang dikenal."""
    host = _url_host(url)
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == short or host.endswith("." + short) for short in SHORTENER_HOSTS)


def _unwrap_query_redirect(url):
    """Ambil URL asli dari link wrapper, mis. youtube.com/redirect?q=... .

    Tidak melakukan request jaringan; hanya membaca parameter URL.
    """
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.lower()

    is_youtube = host.endswith("youtube.com") or host.endswith("youtube-nocookie.com")
    is_google = re.fullmatch(r"google\.[a-z.]+", host) is not None

    wrapper = (
        (is_youtube and path.rstrip("/").endswith("/redirect"))
        or (is_google and path.rstrip("/").endswith("/url"))
        or host in ("l.facebook.com", "lm.facebook.com")
        or host == "out.reddit.com"
        or host == "href.li"
        or (host == "duckduckgo.com" and path.startswith("/l/"))
        or (host == "steamcommunity.com" and "linkfilter" in path)
    )
    if not wrapper:
        return None

    raw = urllib.parse.unquote(parts.query)
    match = re.search(r"https?://[^\s&\"'<>]+", raw)
    return match.group(0) if match else None


def _extract_html_redirect(body, base_url):
    """Cari redirect di dalam HTML: meta refresh atau window.location."""
    for tag in re.findall(r"<meta\b[^>]*>", body, flags=re.IGNORECASE):
        if "refresh" not in tag.lower():
            continue
        match = re.search(r"url\s*=\s*([^\s\"'>]+|'[^']*'|\"[^\"]*\")", tag, re.IGNORECASE)
        if match:
            target = html.unescape(match.group(1).strip("\"'"))
            return urllib.parse.urljoin(base_url, target)

    # Halaman perantara shortener (mis. is.gd / v.gd) memakai link "biglink"
    for tag in re.findall(r"<a\b[^>]*>", body, flags=re.IGNORECASE):
        if "biglink" not in tag.lower():
            continue
        match = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.IGNORECASE)
        if match:
            return urllib.parse.urljoin(base_url, html.unescape(match.group(1)))

    match = re.search(
        r"location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']"
        r"|location\.replace\(\s*[\"']([^\"']+)[\"']",
        body,
        flags=re.IGNORECASE,
    )
    if match:
        target = html.unescape(match.group(1) or match.group(2))
        return urllib.parse.urljoin(base_url, target)
    return None


def _follow_http_redirect(url, timeout=15):
    """Ikuti redirect HTTP / meta refresh / JS pada halaman short link."""
    headers = {
        "User-Agent": FILECOSTER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = response.geturl()
        if final.rstrip("/") != url.rstrip("/"):
            return final

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            return final

        body = response.read(32768).decode(
            response.headers.get_content_charset() or "utf-8", errors="replace"
        )

    return _extract_html_redirect(body, final) or final


def normalize_url(url):
    """Pastikan URL memiliki skema (default https://) dan bersih dari spasi."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url.lstrip("/")
    return url


def resolve_redirects(url, timeout=15, max_hops=REDIRECT_MAX_HOPS):
    """Ubah short link / link wrapper menjadi URL aslinya.

    Mendukung redirect HTTP, halaman meta refresh / JavaScript, serta wrapper
    seperti youtube.com/redirect?q=... dan google.com/url?q=...
    URL biasa dikembalikan apa adanya tanpa request tambahan.
    """
    url = normalize_url(url)
    if not url:
        return ""
    if url in _REDIRECT_CACHE:
        return _REDIRECT_CACHE[url]

    original = url
    current = url
    seen = set()
    try:
        for _ in range(max_hops):
            if current in seen:
                break
            seen.add(current)

            unwrapped = _unwrap_query_redirect(current)
            if unwrapped and unwrapped != current:
                current = unwrapped
                continue

            if not is_short_url(current):
                break

            try:
                next_url = _follow_http_redirect(current, timeout=timeout)
            except Exception:
                break
            if not next_url or next_url == current:
                break
            current = next_url
    finally:
        _REDIRECT_CACHE[original] = current

    return current


# ---------------------------------------------------------------------------
# Resolver generik untuk halaman pemutar HTML (fallback yt-dlp)
# ---------------------------------------------------------------------------
def _http_post_json(url, payload, referer=None, timeout=30):
    """Kirim JSON ke API same-origin milik situs dan baca respons JSON-nya."""
    headers = {
        "User-Agent": FILECOSTER_UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    }
    parts = urllib.parse.urlsplit(url)
    if parts.scheme and parts.netloc:
        headers["Origin"] = f"{parts.scheme}://{parts.netloc}"
    if referer:
        headers["Referer"] = referer

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(response.read().decode(charset, errors="replace"))


def resolve_generic_player(url, timeout=30):
    """Cari URL media langsung dari halaman pemutar HTML generik.

    Dipakai sebagai fallback ketika yt-dlp tidak mengenali extractor atau tidak
    menemukan format di halaman. Mencari sumber <video>/<source>, meta tag,
    JSON-LD, konfigurasi player JS, packed code, base64, embed iframe, serta
    API JSON milik halaman (mis. POST /api/stream).
    Mengembalikan (url_media, judul, referer).
    """
    return _resolve_generic_player(
        url, fetch=_http_get, timeout=timeout, post_json=_http_post_json
    )


# ---------------------------------------------------------------------------
# Registry situs khusus
# ---------------------------------------------------------------------------
# Setiap entri berisi: pola URL, fungsi resolve (halaman -> media langsung),
# dan opsional folder_pattern + expand untuk halaman berisi daftar video.
# Untuk menambah situs baru, cukup panggil register_site(...) di bawah.
SITE_HANDLERS = []


def register_site(name, patterns, resolve=None, folder_pattern=None, expand=None, referer=None):
    """Daftarkan penanganan khusus sebuah situs.

    patterns       : regex (atau daftar regex) yang cocok dengan URL situs
    resolve        : fungsi(url) -> (url_media_langsung, judul)
    folder_pattern : regex halaman yang berisi daftar video (opsional)
    expand         : fungsi(url) -> daftar URL video di dalam folder (opsional)
    referer        : string header Referer, atau fungsi(url) -> string
    """
    if hasattr(patterns, "search"):
        patterns = [patterns]
    entry = {
        "name": name,
        "patterns": list(patterns),
        "resolve": resolve,
        "folder_pattern": folder_pattern,
        "expand": expand,
        "referer": referer,
    }
    SITE_HANDLERS.append(entry)
    return entry


def detect_site(url):
    """Kembalikan entri registry yang cocok dengan URL, atau None."""
    url = url or ""
    for handler in SITE_HANDLERS:
        if any(pattern.search(url) for pattern in handler["patterns"]):
            return handler
    return None


def _slicedrive_referer(url):
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


# Situs dengan halaman video tunggal + halaman folder
register_site(
    "filecoster",
    patterns=[FILECOSTER_URL_RE, FILECOSTER_FOLDER_RE],
    resolve=resolve_filecoster,
    folder_pattern=FILECOSTER_FOLDER_RE,
    expand=expand_filecoster_folder,
    referer="https://filecoster.com/",
)

# Platform embed dengan struktur player yang sama seperti filecoster
register_site(
    "jwplyr",
    patterns=[JWPLYR_URL_RE, JWPLYR_FOLDER_RE],
    resolve=resolve_jwplyr,
    folder_pattern=JWPLYR_FOLDER_RE,
    expand=expand_jwplyr_folder,
    referer="https://jwplyr.com/",
)

# SPA yang mengambil daftar video dari videos.json
register_site(
    "slicedrive",
    patterns=[SLICEDRIVE_URL_RE],
    resolve=resolve_slicedrive,
    referer=_slicedrive_referer,
)


# ---------------------------------------------------------------------------
# Perluasan URL (playlist / daftar / folder)
# ---------------------------------------------------------------------------
def expand_playlist(url, cookies_from_browser=None, limit=500):
    """Ambil semua URL video dari halaman playlist/daftar (mis. YouTube list).

    Memakai yt-dlp dalam mode "flat" agar cepat dan tidak mengunduh apa pun.
    Jika URL ternyata bukan playlist, kembalikan URL aslinya.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
    }
    if JS_RUNTIMES_OPT:
        opts["js_runtimes"] = dict(JS_RUNTIMES_OPT)
    if cookies_from_browser and cookies_from_browser != "none":
        cookies_option = cookies_from_browser_option(cookies_from_browser)
        if cookies_option:
            opts["cookiesfrombrowser"] = cookies_option

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries") if isinstance(info, dict) else None
    if not entries:
        return [url]

    urls = []
    for entry in entries:
        if not entry or len(urls) >= limit:
            continue
        if entry.get("_type") in ("playlist", "multi_video"):
            continue
        candidate = entry.get("url") or entry.get("webpage_url")
        if not candidate or not str(candidate).startswith("http"):
            continue
        urls.append(candidate)

    return urls or [url]


def expand_url(url, playlist=False, cookies_from_browser=None):
    """Perluas satu URL input menjadi daftar URL yang akan diunduh.

    - Handler situs dengan folder (mis. filecoster /f/) -> semua video di folder
    - Opsi playlist aktif -> semua video di playlist/daftar
    - Selain itu -> URL itu sendiri
    """
    url = resolve_redirects((url or "").strip())
    if not url:
        return []

    handler = detect_site(url)
    if handler:
        folder_pattern = handler.get("folder_pattern")
        if handler.get("expand") and folder_pattern and folder_pattern.search(url):
            return handler["expand"](url)
        return [url]

    if playlist:
        return expand_playlist(url, cookies_from_browser=cookies_from_browser)
    return [url]


def sanitize_title(title):
    """Bersihkan judul agar aman dipakai sebagai nama file Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title or "video").strip().strip(".")
    return cleaned[:150] or "video"


def unique_path(path):
    """Hindari menimpa file yang sudah ada."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    index = 1
    while os.path.exists(f"{base} ({index}){ext}"):
        index += 1
    return f"{base} ({index}){ext}"


class VideoDownloader:
    def __init__(
        self,
        output_dir=None,
        quality="terbaik",
        cookies_from_browser=None,
        sponsorblock=False,
        progress_callback=None,
        threads=DEFAULT_THREADS,
        playlist=False,
    ):
        self.output_dir = output_dir or DEFAULT_OUTPUT_DIR
        os.makedirs(self.output_dir, exist_ok=True)
        self.quality = quality
        self.cookies_from_browser = (
            cookies_from_browser
            if cookies_from_browser and cookies_from_browser != "none"
            else None
        )
        self.sponsorblock = sponsorblock
        self.progress_callback = progress_callback
        self.threads = max(1, int(threads or 1))
        self.playlist = bool(playlist)
        self._stop = False

    def stop(self):
        self._stop = True

    # -- internal ---------------------------------------------------------
    def _emit(self, **kwargs):
        if self.progress_callback:
            self.progress_callback(**kwargs)

    def _progress_hook(self, d):
        if self._stop:
            raise yt_dlp.utils.DownloadError("Download dihentikan oleh pengguna")

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            self._emit(
                percent=round(percent, 1),
                downloaded=downloaded,
                total=total,
                speed=d.get("speed", 0),
                eta=d.get("eta", 0),
            )
        elif d["status"] == "finished":
            self._emit(
                percent=100,
                downloaded=d.get("total_bytes", 0),
                total=d.get("total_bytes", 0),
                speed=0,
                eta=0,
                message="Selesai diunduh, sedang memproses file...",
            )

    def _build_opts(self):
        opts = dict(DEFAULT_OPTS)
        if JS_RUNTIMES_OPT:
            opts["js_runtimes"] = dict(JS_RUNTIMES_OPT)
        opts["noplaylist"] = not self.playlist
        opts["outtmpl"] = os.path.join(self.output_dir, "%(title)s.%(ext)s")
        opts["progress_hooks"] = [self._progress_hook]

        if HAS_FFMPEG:
            opts["format"] = QUALITY_MAP.get(self.quality, QUALITY_MAP["terbaik"])
            opts["merge_output_format"] = "mp4"
            postprocessors = [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
                {"key": "FFmpegMetadata", "add_metadata": True},
            ]
            if self.sponsorblock:
                # Lewati segmen iklan/sponsor di YouTube (butuh FFmpeg untuk memotong)
                postprocessors += [
                    {
                        "key": "SponsorBlock",
                        "categories": ["sponsor", "selfpromo", "interaction"],
                        "when": "after_filter",
                    },
                    {
                        "key": "ModifyChapters",
                        "remove_sponsor_segments": ["sponsor", "selfpromo", "interaction"],
                    },
                ]
            opts["postprocessors"] = postprocessors
        else:
            # Tanpa FFmpeg: unduh satu file yang sudah tergabung, tanpa merge
            opts["format"] = QUALITY_MAP_NO_FFMPEG.get(
                self.quality, QUALITY_MAP_NO_FFMPEG["terbaik"]
            )

        if self.cookies_from_browser:
            cookies_option = cookies_from_browser_option(self.cookies_from_browser)
            if cookies_option:
                opts["cookiesfrombrowser"] = cookies_option

        # Percepat unduhan HLS/DASH dengan mengunduh beberapa fragmen sekaligus
        if self.threads > 1:
            opts["concurrent_fragment_downloads"] = self.threads

        return opts

    # -- publik -----------------------------------------------------------
    def download(self, url):
        if not url or not url.strip():
            raise ValueError("URL tidak boleh kosong")
        url = resolve_redirects(url.strip())

        # Situs dengan penanganan khusus (melewati iklan/embed).
        # Jika resolver situs berubah/rusak, coba yt-dlp lalu pencari generik,
        # bukan langsung menyerah.
        handler = detect_site(url)
        if handler:
            try:
                media_url, title = handler["resolve"](url)
            except Exception as handler_error:
                self._emit(
                    message="Resolver situs khusus gagal; mencoba mesin lain..."
                )
                return self._download_unknown_page(url, handler_error)

            self._emit(message=f"Tautan video ditemukan: {title}")
            referer = handler.get("referer")
            if callable(referer):
                referer = referer(url)
            referer = referer or url
            return self._download_media(media_url, title, referer=referer)

        return self._download_unknown_page(url)

    def _download_media(self, media_url, title, referer=None):
        """Unduh URL media hasil resolver; stream manifest diserahkan ke yt-dlp."""
        if media_url.split("?")[0].lower().endswith((".m3u8", ".mpd")):
            return self._download_via_ytdlp(media_url, referer=referer, title=title)
        try:
            return self._download_direct(media_url, title, referer=referer)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            # HTTP 4xx/5xx tetap memakai pesan status HTTP yang spesifik.
            if isinstance(exc, urllib.error.HTTPError):
                raise
            host = urllib.parse.urlsplit(media_url).hostname or "server media"
            raise yt_dlp.utils.DownloadError(
                f"Sumber video ditemukan, tetapi server file {host} tidak dapat "
                "dihubungi. Server atau jalur jaringan ke server tersebut mungkin "
                "bermasalah. Coba putar video di browser pada jaringan yang sama, "
                "lalu coba lagi nanti."
            ) from exc

    def _download_unknown_page(self, url, reason=None):
        """Alur pintar untuk halaman yang tidak punya resolver situs khusus.

        1. Coba yt-dlp (ratusan situs + YouTube).
        2. Jika belum didukung/tidak menemukan format, baca HTML & player page.
        3. Jika keduanya gagal, berikan pesan yang menjelaskan penyebabnya.
        """
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        is_youtube = (
            hostname == "youtube.com" or hostname.endswith(".youtube.com")
            or hostname in ("youtu.be", "youtube-nocookie.com")
            or hostname.endswith(".youtube-nocookie.com")
        )

        try:
            return self._download_via_ytdlp(url)
        except Exception as exc:
            if is_youtube:
                # Error YouTube (video privat/verifikasi/challenge) tidak boleh
                # diganti pencarian HTML generik.
                raise
            if reason is None and not is_unsupported_error(exc):
                raise
            last_error = exc

        self._emit(
            message="Membaca halaman dan konfigurasi player untuk mencari sumber video..."
        )

        # URL yang memang berupa file media langsung tidak perlu dibaca sebagai
        # HTML; server mungkin hanya menolak permintaan HEAD dari yt-dlp.
        media_path = urllib.parse.urlsplit(url).path
        if MEDIA_EXT_RE.search(media_path):
            filename = urllib.parse.unquote(media_path.rsplit("/", 1)[-1])
            title = os.path.splitext(filename)[0] or "video"
            parts = urllib.parse.urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}/"
            self._emit(message="URL adalah file media langsung; mencoba mengunduh langsung...")
            return self._download_media(url, title, referer=origin)

        try:
            media_url, title, referer = resolve_generic_player(url)
        except Exception as exc:
            raise yt_dlp.utils.DownloadError(
                "Tidak menemukan sumber video di halaman tersebut. Halaman mungkin "
                "memuat video lewat JavaScript kompleks, butuh login/cookies, atau "
                "videonya sudah tidak tersedia."
            ) from (last_error or exc)

        self._emit(message=f"Tautan video ditemukan: {title}")
        return self._download_media(media_url, title, referer=referer)

    def expand(self, url):
        """Perluas URL (folder/playlist) menjadi daftar URL video."""
        resolved = resolve_redirects(url)
        if resolved != (url or "").strip():
            self._emit(message=f"URL dialihkan ke: {resolved}")
        return expand_url(
            resolved,
            playlist=self.playlist,
            cookies_from_browser=self.cookies_from_browser,
        )

    def _download_via_ytdlp(self, url, referer=None, title=None):
        opts = self._build_opts()
        if title:
            # Gunakan judul halaman yang ramah (bukan "master" dari playlist HLS)
            base_title = re.sub(
                r"\.(mp4|m3u8|webm|mkv|mov|avi|flv|ts)$",
                "",
                title.strip(),
                flags=re.IGNORECASE,
            )
            opts["outtmpl"] = os.path.join(
                self.output_dir, sanitize_title(base_title) + ".%(ext)s"
            )
        if referer:
            opts["http_headers"] = {
                **DEFAULT_OPTS["http_headers"],
                "Referer": referer,
            }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise yt_dlp.utils.DownloadError(
                    "Tidak ada video yang bisa diunduh dari URL ini"
                )
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    raise yt_dlp.utils.DownloadError("Playlist kosong / tidak ada video")
                info = entries[0]

            try:
                filename = ydl.prepare_filename(info)
            except Exception:
                filename = info.get("title", "video")

            return {
                "title": title or info.get("title"),
                "filename": filename,
                "extractor": info.get("extractor"),
                "duration": info.get("duration"),
            }

    def _download_direct(self, media_url, title, referer=None):
        """Unduh URL media langsung (mp4) dengan progress, Batal, dan paralel."""
        # Hindari ekstensi ganda, mis. judul "video.mp4" -> "video.mp4" (bukan .mp4.mp4)
        ext_match = re.search(
            r"\.(mp4|m4v|webm|mkv|mov|avi|flv|ts)(?:[?#]|$)", media_url, re.IGNORECASE
        )
        ext = ext_match.group(1).lower() if ext_match else "mp4"
        base_title = re.sub(
            r"\.(mp4|m3u8|webm|mkv|mov|avi|flv|ts)$",
            "",
            (title or "").strip(),
            flags=re.IGNORECASE,
        )
        dest = unique_path(
            os.path.join(self.output_dir, f"{sanitize_title(base_title)}.{ext}")
        )
        headers = {
            "User-Agent": FILECOSTER_UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
        }
        if referer:
            headers["Referer"] = referer

        total, supports_ranges = self._probe_media(media_url, headers)
        threads = self.threads
        if total:
            threads = min(threads, max(1, total // MIN_CHUNK_SIZE))

        if threads > 1 and supports_ranges and total >= MIN_PARALLEL_SIZE:
            try:
                self._emit(message=f"Mengunduh dengan {threads} koneksi paralel...")
                downloaded = self._download_parallel(
                    media_url, dest, headers, total, threads
                )
            except yt_dlp.utils.DownloadError:
                raise
            except Exception:
                # Server menolak Range / jaringan bermasalah -> kembali ke mode biasa
                self._emit(message="Mode paralel gagal, beralih ke unduhan biasa...")
                downloaded = self._download_single(media_url, dest, headers)
        else:
            downloaded = self._download_single(media_url, dest, headers)

        self._emit(
            percent=100,
            downloaded=downloaded,
            total=downloaded,
            speed=0,
            eta=0,
            message="Selesai diunduh",
        )
        return {
            "title": title,
            "filename": dest,
            "extractor": "direct",
            "duration": None,
        }

    @staticmethod
    def _probe_media(media_url, headers):
        """Cek ukuran file & dukungan HTTP Range lewat HEAD."""
        total = 0
        supports_ranges = False
        try:
            request = urllib.request.Request(media_url, method="HEAD", headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                total = int(response.headers.get("Content-Length") or 0)
                accept = (response.headers.get("Accept-Ranges") or "").lower()
                supports_ranges = "bytes" in accept
        except (urllib.error.HTTPError, ValueError):
            pass
        # Error koneksi/timeout tidak ditelan: biarkan pemanggil melaporkan
        # bahwa server media tidak dapat dihubungi, bukan menunggu unduhan penuh.
        return total, supports_ranges

    def _download_single(self, media_url, dest, headers):
        """Unduhan satu koneksi (fallback)."""
        request = urllib.request.Request(media_url, headers=headers)
        downloaded = 0
        start = time.time()
        with urllib.request.urlopen(request, timeout=60) as response, open(dest, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            while True:
                if self._stop:
                    out.close()
                    self._abort(dest)
                    raise yt_dlp.utils.DownloadError(
                        "Download dihentikan oleh pengguna"
                    )
                chunk = response.read(READ_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)

                elapsed = max(time.time() - start, 0.001)
                speed = downloaded / elapsed
                eta = int((total - downloaded) / speed) if total and speed else 0
                percent = (downloaded / total * 100) if total else 0
                self._emit(
                    percent=round(percent, 1),
                    downloaded=downloaded,
                    total=total,
                    speed=speed,
                    eta=eta,
                )
        return downloaded

    def _download_parallel(self, media_url, dest, headers, total, threads):
        """Unduh satu file dengan beberapa koneksi memakai HTTP Range."""
        # Bagi file menjadi beberapa rentang byte
        step = total // threads
        ranges = []
        start = 0
        for index in range(threads):
            end = total - 1 if index == threads - 1 else start + step - 1
            ranges.append((start, end))
            start = end + 1

        downloaded = 0
        lock = threading.Lock()
        stop_event = threading.Event()
        errors = []
        start_time = time.time()
        last_emit = [0.0]

        # Siapkan file seukuran total agar tiap thread menulis di posisinya
        with open(dest, "wb") as preallocate:
            preallocate.truncate(total)
        file_handle = open(dest, "r+b")

        def emit_progress(force=False):
            now = time.time()
            if not force and now - last_emit[0] < 0.3:
                return
            last_emit[0] = now
            with lock:
                done = downloaded
            elapsed = max(now - start_time, 0.001)
            speed = done / elapsed
            eta = int((total - done) / speed) if speed else 0
            percent = (done / total * 100) if total else 0
            self._emit(
                percent=round(percent, 1),
                downloaded=done,
                total=total,
                speed=speed,
                eta=eta,
            )

        def worker(part_start, part_end):
            nonlocal downloaded
            position = part_start
            attempt = 0
            while position <= part_end and not stop_event.is_set():
                if self._stop:
                    stop_event.set()
                    return
                try:
                    range_headers = dict(headers)
                    range_headers["Range"] = f"bytes={position}-{part_end}"
                    request = urllib.request.Request(media_url, headers=range_headers)
                    with urllib.request.urlopen(request, timeout=60) as response:
                        if response.status != 206:
                            raise RuntimeError("Server tidak mendukung HTTP Range")
                        while position <= part_end and not stop_event.is_set():
                            if self._stop:
                                stop_event.set()
                                return
                            chunk = response.read(
                                min(READ_CHUNK, part_end - position + 1)
                            )
                            if not chunk:
                                break
                            with lock:
                                file_handle.seek(position)
                                file_handle.write(chunk)
                                downloaded += len(chunk)
                            position += len(chunk)
                            emit_progress()
                    attempt = 0
                except Exception as exc:
                    if self._stop:
                        stop_event.set()
                        return
                    attempt += 1
                    if attempt > MAX_RETRIES_PER_PART:
                        errors.append(exc)
                        stop_event.set()
                        return
                    time.sleep(min(2 * attempt, 5))

        workers = [
            threading.Thread(target=worker, args=(start, end), daemon=True)
            for start, end in ranges
        ]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join()

        file_handle.close()

        if self._stop:
            self._abort(dest)
            raise yt_dlp.utils.DownloadError("Download dihentikan oleh pengguna")
        if errors:
            raise RuntimeError(str(errors[0]))

        emit_progress(force=True)
        return downloaded

    @staticmethod
    def _abort(dest):
        try:
            os.remove(dest)
        except OSError:
            pass

    def download_many(self, urls):
        """Unduh banyak URL. Mengembalikan (hasil, daftar_error).

        Setiap URL diperluas lebih dulu (folder situs khusus / playlist) menjadi
        daftar video yang akan diunduh berurutan.
        """
        expanded = []
        errors = []

        for url in urls:
            url = (url or "").strip()
            if not url:
                continue
            try:
                children = self.expand(url)
            except Exception as exc:
                errors.append((url, exc))
                continue
            if len(children) > 1:
                self._emit(message=f"Ditemukan {len(children)} video dari: {url}")
            expanded.extend(children)

        results = []
        total = len(expanded)
        for index, url in enumerate(expanded, start=1):
            if self._stop:
                break
            self._emit(message=f"[{index}/{total}] Mengunduh: {url}")
            try:
                results.append(self.download(url))
            except Exception as exc:  # lanjutkan ke URL berikutnya
                errors.append((url, exc))
        return results, errors


# ---------------------------------------------------------------------------
# Antarmuka Baris Perintah (CLI)
# ---------------------------------------------------------------------------
def run_cli(urls, output_dir, quality, cookies, sponsorblock, threads=DEFAULT_THREADS, playlist=False):
    downloader = VideoDownloader(
        output_dir=output_dir,
        quality=quality,
        cookies_from_browser=cookies,
        sponsorblock=sponsorblock,
        threads=threads,
        playlist=playlist,
    )

    def on_progress(**kwargs):
        msg = kwargs.get("message")
        if msg:
            print(f"\n{msg}")
            return
        if kwargs.get("percent") is not None:
            percent = kwargs["percent"]
            downloaded = kwargs.get("downloaded") or 0
            total = kwargs.get("total") or 0
            speed = kwargs.get("speed") or 0
            if total:
                size_text = f"{format_size(downloaded):>10} / {format_size(total):>10}"
            else:
                size_text = f"{format_size(downloaded):>10} terunduh"
            speed_text = f"{format_size(speed) + '/s':>11}" if speed else " " * 11
            bar = "#" * int(percent // 3)
            print(
                f"\rUnduhan: {percent:5.1f}% | {size_text} | {speed_text} | "
                f"ETA {format_eta(kwargs.get('eta', 0)):>7} | {bar:<34}",
                end="",
            )

    downloader.progress_callback = on_progress
    results, errors = downloader.download_many(urls)

    print("\n")
    if results:
        print(f"Berhasil mengunduh {len(results)} video:")
        for r in results:
            print(f"   - {r['title']}  ->  {r['filename']}")
    if errors:
        print(f"\nGagal mengunduh {len(errors)} URL:")
        for url, exc in errors:
            print(f"   - {url}\n     {friendly_error(exc)}")
        sys.exit(1)
    if not results and not errors:
        print("Tidak ada yang diunduh.")


# ---------------------------------------------------------------------------
# Antarmuka Grafis (GUI)
# ---------------------------------------------------------------------------
class DownloadSession:
    """Satu sesi antrean: URL yang diperluas + beberapa unduhan bersamaan."""

    def __init__(self, session_id, max_workers):
        self.id = session_id
        self.max_workers = max_workers
        self.url_queue = queue.Queue()
        self.task_queue = queue.Queue()
        self.lock = threading.Lock()
        self.pending_tasks = 0
        self.cancelled = False
        self.dispatcher_running = True
        self.next_task_id = 0
        self.downloaders = {}
        self.worker_threads = []


class DownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Downloader")
        self.root.geometry("800x720")
        self.root.resizable(True, True)

        self._queue_lock = threading.Lock()
        self._session = None
        self._next_session_id = 0
        self.ui_queue = queue.Queue()
        self._cancel_requested = False
        self._session_success = 0
        self._session_fail = 0
        self._session_done = 0
        self._session_total = 0
        self._active_rows = {}
        self._task_stats = {}
        self._build_ui()
        self.root.after(100, self._poll_ui)

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # Peringatan FFmpeg
        if not HAS_FFMPEG:
            warn = ttk.Label(
                self.root,
                text=(
                    "⚠ FFmpeg tidak terdeteksi — video+audio belum bisa digabung & "
                    "SponsorBlock nonaktif. Lihat README untuk instalasi FFmpeg."
                ),
                foreground="#b35900",
                wraplength=720,
            )
            warn.pack(anchor="w", **pad)

        # Peringatan runtime JS (dibutuhkan YouTube versi terbaru)
        if not JS_RUNTIMES_OPT:
            ttk.Label(
                self.root,
                text=(
                    "⚠ Runtime JavaScript (Node.js/Deno) tidak terdeteksi — "
                    "unduhan YouTube dapat gagal. Lihat README untuk instalasi."
                ),
                foreground="#b35900",
                wraplength=720,
            ).pack(anchor="w", **pad)

        # URL (bisa banyak baris)
        ttk.Label(self.root, text="URL Video (satu per baris, bisa banyak):").pack(
            anchor="w", **pad
        )
        url_frame = ttk.Frame(self.root)
        url_frame.pack(fill="both", expand=False, **pad)
        self.url_text = tk.Text(url_frame, height=4, wrap="none", undo=True)
        self.url_text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(url_frame, orient="vertical", command=self.url_text.yview)
        scroll.pack(side="right", fill="y")
        self.url_text.configure(yscrollcommand=scroll.set)

        # Folder tujuan
        folder_frame = ttk.Frame(self.root)
        folder_frame.pack(fill="x", **pad)
        ttk.Label(folder_frame, text="Simpan ke:").pack(side="left")
        self.dir_var = tk.StringVar(value=DEFAULT_OUTPUT_DIR)
        ttk.Entry(folder_frame, textvariable=self.dir_var, width=55).pack(
            side="left", fill="x", expand=True, padx=(6, 6)
        )
        ttk.Button(folder_frame, text="Browse", command=self._browse).pack(side="left")

        # Opsi: kualitas
        quality_frame = ttk.LabelFrame(self.root, text="Kualitas")
        quality_frame.pack(fill="x", **pad)
        self.quality_var = tk.StringVar(value="terbaik")
        for label in QUALITY_MAP:
            ttk.Radiobutton(
                quality_frame, text=label, value=label, variable=self.quality_var
            ).pack(side="left", padx=4)

        # Opsi: cookies & sponsorblock
        extra = ttk.Frame(self.root)
        extra.pack(fill="x", **pad)

        ttk.Label(extra, text="Cookies dari browser:").pack(side="left")
        self.cookies_var = tk.StringVar(value="none")
        ttk.Combobox(
            extra,
            textvariable=self.cookies_var,
            values=BROWSERS,
            state="readonly",
            width=12,
        ).pack(side="left", padx=(6, 16))

        self.sponsor_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            extra, text="Lewati iklan/sponsor YouTube (SponsorBlock)", variable=self.sponsor_var
        ).pack(side="left")

        # Opsi: playlist
        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill="x", **pad)
        self.playlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            list_frame,
            text="Unduh seluruh playlist/daftar (mis. YouTube list)",
            variable=self.playlist_var,
        ).pack(anchor="w")

        # Opsi: percepatan unduhan
        perf = ttk.Frame(self.root)
        perf.pack(fill="x", **pad)
        ttk.Label(perf, text="Koneksi paralel:").pack(side="left")
        self.threads_var = tk.IntVar(value=DEFAULT_THREADS)
        ttk.Spinbox(
            perf, from_=1, to=32, textvariable=self.threads_var, width=5
        ).pack(side="left", padx=(6, 16))
        ttk.Label(perf, text="Video bersamaan:").pack(side="left")
        self.concurrent_var = tk.IntVar(value=MAX_CONCURRENT_DOWNLOADS)
        ttk.Spinbox(
            perf,
            from_=1,
            to=MAX_CONCURRENT_DOWNLOADS,
            textvariable=self.concurrent_var,
            width=5,
        ).pack(side="left", padx=(6, 8))
        ttk.Label(perf, text=f"(maks. {MAX_CONCURRENT_DOWNLOADS})").pack(side="left")

        # Tombol aksi
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=10)
        self.download_btn = ttk.Button(
            btn_frame, text="⬇ Unduh Video", command=self._start_download
        )
        self.download_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(
            btn_frame, text="✖ Batal", command=self._cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=6)


        # Progress bar + status
        self.progress = ttk.Progressbar(
            self.root, orient="horizontal", length=700, mode="determinate"
        )
        self.progress.pack(**pad)
        self.status_var = tk.StringVar(value="Siap")
        ttk.Label(self.root, textvariable=self.status_var, wraplength=760).pack(
            anchor="w", **pad
        )

        # Daftar unduhan yang sedang berjalan (maks. 5)
        active_frame = ttk.LabelFrame(self.root, text="Unduhan aktif")
        active_frame.pack(fill="x", **pad)
        self.active_tree = ttk.Treeview(
            active_frame,
            columns=("video", "progress", "size", "speed", "eta"),
            show="headings",
            height=MAX_CONCURRENT_DOWNLOADS,
            selectmode="none",
        )
        self.active_tree.heading("video", text="Video")
        self.active_tree.heading("progress", text="Progres")
        self.active_tree.heading("size", text="Ukuran")
        self.active_tree.heading("speed", text="Kecepatan")
        self.active_tree.heading("eta", text="ETA")
        self.active_tree.column("video", width=280, anchor="w", stretch=True)
        self.active_tree.column("progress", width=70, anchor="center", stretch=False)
        self.active_tree.column("size", width=175, anchor="e", stretch=False)
        self.active_tree.column("speed", width=100, anchor="e", stretch=False)
        self.active_tree.column("eta", width=80, anchor="e", stretch=False)
        self.active_tree.pack(fill="x", padx=6, pady=(2, 6))

        # Log
        ttk.Label(self.root, text="Log:").pack(anchor="w", **pad)
        self.log_text = tk.Text(self.root, height=4, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=False, padx=12, pady=(0, 8))

        self._setup_text_context_menus()

    # -- menu klik kanan & clipboard -------------------------------------
    def _setup_text_context_menus(self):
        """Menu klik kanan: tempel/potong/salin/pilih semua untuk input URL."""
        self.url_menu = tk.Menu(self.root, tearoff=0)
        self.url_menu.add_command(
            label="Tempel",
            accelerator="Ctrl+V",
            command=lambda: self._paste_text(self.url_text),
        )
        self.url_menu.add_command(
            label="Potong",
            accelerator="Ctrl+X",
            command=lambda: self._cut_text(self.url_text),
        )
        self.url_menu.add_command(
            label="Salin",
            accelerator="Ctrl+C",
            command=lambda: self._copy_text(self.url_text),
        )
        self.url_menu.add_separator()
        self.url_menu.add_command(
            label="Pilih Semua",
            accelerator="Ctrl+A",
            command=lambda: self._select_all(self.url_text),
        )
        self.url_menu.add_command(
            label="Urungkan",
            accelerator="Ctrl+Z",
            command=lambda: self._undo_text(self.url_text),
        )
        self.url_text.bind("<Button-3>", self._show_url_menu)
        self.url_text.bind("<Control-a>", self._on_select_all)

        # Log: hanya salin & pilih semua (log bersifat read-only)
        self.log_menu = tk.Menu(self.root, tearoff=0)
        self.log_menu.add_command(
            label="Salin",
            accelerator="Ctrl+C",
            command=lambda: self._copy_text(self.log_text),
        )
        self.log_menu.add_command(
            label="Pilih Semua",
            accelerator="Ctrl+A",
            command=lambda: self._select_all(self.log_text),
        )
        self.log_text.bind("<Button-3>", self._show_log_menu)
        self.log_text.bind("<Control-a>", self._on_select_all)

    def _show_url_menu(self, event):
        # Pindahkan kursor ke posisi klik, lalu tampilkan menu
        self.url_text.mark_set("insert", f"@{event.x},{event.y}")
        try:
            self.url_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.url_menu.grab_release()
        return "break"

    def _show_log_menu(self, event):
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()
        return "break"

    def _on_select_all(self, event):
        self._select_all(event.widget)
        return "break"

    @staticmethod
    def _selected_text(widget):
        try:
            return widget.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def _copy_text(self, widget):
        text = self._selected_text(widget)
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _cut_text(self, widget):
        if not self._selected_text(widget):
            return
        self._copy_text(widget)
        widget.delete("sel.first", "sel.last")

    def _paste_text(self, widget):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        widget.insert("insert", text)

    @staticmethod
    def _select_all(widget):
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "1.0")
        widget.see("insert")

    @staticmethod
    def _undo_text(widget):
        try:
            widget.edit_undo()
        except tk.TclError:
            pass

    def _log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _browse(self):
        directory = filedialog.askdirectory(initialdir=self.dir_var.get())
        if directory:
            self.dir_var.set(directory)

    def _cancel(self):
        session = self._session
        if session is None:
            return

        with session.lock:
            session.cancelled = True
            session.dispatcher_running = False
            # Kosongkan antrean yang belum diproses
            for pending_queue in (session.url_queue, session.task_queue):
                while True:
                    try:
                        pending_queue.get_nowait()
                    except queue.Empty:
                        break
            active_downloaders = list(session.downloaders.values())

        for downloader in active_downloaders:
            downloader.stop()

        self._cancel_requested = True
        self.status_var.set("Menghentikan semua unduhan & membatalkan antrean...")
        self._log("Permintaan berhenti diterima; antrean dibatalkan.")

    def _start_download(self):
        urls = [
            line.strip()
            for line in self.url_text.get("1.0", "end").splitlines()
            if line.strip()
        ]
        if not urls:
            messagebox.showwarning("Peringatan", "Masukkan minimal satu URL video.")
            return

        # Reset area URL segera agar bisa langsung paste URL berikutnya
        self.url_text.delete("1.0", "end")
        self._enqueue_urls(urls)

    def _enqueue_urls(self, urls):
        """Masukkan URL ke antrean unduhan dengan pengaturan yang aktif."""
        if not urls:
            return

        # Simpan pengaturan saat ini; berlaku untuk URL yang diantrekan sekarang
        settings = {
            "output_dir": self.dir_var.get(),
            "quality": self.quality_var.get(),
            "cookies_from_browser": self.cookies_var.get(),
            "sponsorblock": self.sponsor_var.get(),
            "threads": self.threads_var.get(),
            "playlist": self.playlist_var.get(),
        }

        try:
            max_workers = int(self.concurrent_var.get())
        except (tk.TclError, ValueError):
            max_workers = MAX_CONCURRENT_DOWNLOADS
        max_workers = max(1, min(MAX_CONCURRENT_DOWNLOADS, max_workers))

        with self._queue_lock:
            session = self._session
            new_session = False
            if session is not None:
                # Cek & titipkan URL dalam satu kunci yang sama dengan dispatcher,
                # agar URL tidak hilang saat dispatcher baru saja berhenti.
                with session.lock:
                    if session.dispatcher_running:
                        session.max_workers = max(session.max_workers, max_workers)
                        for url in urls:
                            session.url_queue.put((url, settings))
                    else:
                        session = None
            if session is None:
                session = DownloadSession(self._next_session_id, max_workers)
                self._next_session_id += 1
                self._session = session
                self._cancel_requested = False
                new_session = True
                with session.lock:
                    for url in urls:
                        session.url_queue.put((url, settings))

        if new_session:
            self._reset_session_ui()
            self.dispatcher_thread = threading.Thread(
                target=self._dispatcher_loop, args=(session,), daemon=True
            )
            self.dispatcher_thread.start()
        self._ensure_workers(session)

        self.cancel_btn.state(["!disabled"])
        self._log(f"Ditambahkan ke antrean: {len(urls)} URL")
        self.status_var.set(
            f"{len(urls)} URL ditambahkan — memperbarui antrean..."
        )



    def _reset_session_ui(self):
        """Siapkan tampilan untuk sesi antrean baru."""
        self._session_success = 0
        self._session_fail = 0
        self._session_done = 0
        self._session_total = 0
        self.progress["value"] = 0
        self.status_var.set("Menyiapkan antrean...")
        self.active_tree.delete(*self.active_tree.get_children())
        self._active_rows.clear()
        self._task_stats.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _ensure_workers(self, session):
        """Pastikan jumlah thread unduhan sesuai batas (maksimal 5)."""
        with session.lock:
            session.worker_threads = [
                thread for thread in session.worker_threads if thread.is_alive()
            ]
            while len(session.worker_threads) < session.max_workers:
                thread = threading.Thread(
                    target=self._worker_loop, args=(session,), daemon=True
                )
                session.worker_threads.append(thread)
                thread.start()

    def _dispatcher_loop(self, session):
        """Perluas URL (folder/playlist) lalu isi antrean video."""
        while True:
            with session.lock:
                if session.cancelled:
                    session.dispatcher_running = False
                    break
                try:
                    raw_url, settings = session.url_queue.get_nowait()
                except queue.Empty:
                    raw_url = None
                if raw_url is None and session.pending_tasks == 0:
                    # Tandai berhenti di dalam kunci yang sama, supaya URL baru
                    # tidak bisa diselipkan ke antrean yang sudah mati.
                    session.dispatcher_running = False
                    break

            if raw_url is None:
                time.sleep(0.1)
                continue

            expander = VideoDownloader(
                progress_callback=lambda **kw: self._gui_progress(
                    session.id, None, **kw
                ),
                **settings,
            )
            try:
                targets = expander.expand(raw_url)
            except Exception as exc:
                if not session.cancelled:
                    self.ui_queue.put(("expand_error", session.id, raw_url, exc))
                continue

            if len(targets) > 1:
                self.ui_queue.put(
                    (
                        "session_message",
                        session.id,
                        f"Ditemukan {len(targets)} video dari: {raw_url}",
                    )
                )

            with session.lock:
                if session.cancelled:
                    break
                for target in targets:
                    session.next_task_id += 1
                    session.task_queue.put(
                        {
                            "id": session.next_task_id,
                            "url": target,
                            "settings": settings,
                        }
                    )
                    session.pending_tasks += 1
                added = len(targets)

            if added:
                self.ui_queue.put(("tasks_added", session.id, added))
            self._ensure_workers(session)

        with self._queue_lock:
            is_current = self._session is session
        if is_current:
            self.ui_queue.put(("queue_empty", session.id))

    def _worker_loop(self, session):
        """Ambil satu video dari antrean dan unduh (berjalan paralel)."""
        while True:
            with session.lock:
                if session.cancelled and session.task_queue.empty():
                    return
                try:
                    task = session.task_queue.get_nowait()
                except queue.Empty:
                    if not session.dispatcher_running:
                        return
                    task = None

            if task is None:
                time.sleep(0.1)
                continue

            task_id = task["id"]
            url = task["url"]
            downloader = VideoDownloader(
                progress_callback=lambda **kw: self._gui_progress(
                    session.id, task_id, **kw
                ),
                **task["settings"],
            )
            with session.lock:
                # Daftarkan lalu cek Batal dalam kunci yang sama: kalau Batal
                # datang lebih dulu, downloader ini tetap ikut dihentikan.
                session.downloaders[task_id] = downloader
                cancelled = session.cancelled

            if cancelled:
                downloader.stop()
                with session.lock:
                    session.downloaders.pop(task_id, None)
                    session.pending_tasks = max(0, session.pending_tasks - 1)
                continue

            self.ui_queue.put(("task_start", session.id, task_id, url))

            result = None
            error = None
            try:
                result = downloader.download(url)
            except Exception as exc:
                if not session.cancelled:
                    error = exc

            self.ui_queue.put(
                ("task_done", session.id, task_id, url, result, error)
            )
            with session.lock:
                session.downloaders.pop(task_id, None)
                session.pending_tasks = max(0, session.pending_tasks - 1)

    @staticmethod
    def _describe_file(filename):
        """Tambahkan ukuran file ke nama file bila tersedia."""
        try:
            if filename and os.path.isfile(filename):
                return f"{filename} ({format_size(os.path.getsize(filename))})"
        except OSError:
            pass
        return filename or ""

    @staticmethod
    def _short_label(text, limit=60):
        """Ringkas URL/judul agar muat di kolom daftar unduhan."""
        text = re.sub(r"^https?://", "", (text or "").strip())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _on_task_start(self, task_id, url):
        iid = f"t{task_id}"
        self.active_tree.insert(
            "",
            "end",
            iid=iid,
            values=(self._short_label(url), "0.0%", "-", "-", "-"),
        )
        self._active_rows[task_id] = iid
        self._log(f"Mulai: {url}")
        self.cancel_btn.state(["!disabled"])
        self._refresh_status()

    def _on_task_title(self, task_id, title):
        iid = self._active_rows.get(task_id)
        if not iid:
            return
        values = list(self.active_tree.item(iid, "values"))
        if values:
            values[0] = self._short_label(title)
            self.active_tree.item(iid, values=values)

    def _on_task_progress(self, task_id, percent, downloaded, total, speed, eta):
        iid = self._active_rows.get(task_id)
        if not iid:
            return
        values = list(self.active_tree.item(iid, "values"))
        values[1] = f"{percent:.1f}%"
        values[2] = (
            f"{format_size(downloaded)} / {format_size(total)}"
            if total
            else f"{format_size(downloaded)} / ?"
        )
        values[3] = f"{format_size(speed)}/s" if speed else "-"
        values[4] = format_eta(eta) if eta else "-"
        self.active_tree.item(iid, values=values)
        self._task_stats[task_id] = (downloaded, total, speed)
        self._refresh_status()

    def _on_task_done(self, task_id, url, result, error):
        iid = self._active_rows.pop(task_id, None)
        if iid:
            try:
                self.active_tree.delete(iid)
            except tk.TclError:
                pass
        self._task_stats.pop(task_id, None)
        self._session_done += 1
        if result is not None:
            self._session_success += 1
            self._log(
                f"✔ {result['title']}  ->  {self._describe_file(result['filename'])}"
            )
        elif error is not None:
            self._session_fail += 1
            self._log(f"✖ {url}\n   {friendly_error(error)}")

        if self._session is not None:
            self._update_overall_progress()
            self._refresh_status()

    def _on_expand_error(self, url, exc):
        self._session_fail += 1
        self._log(f"✖ {url}\n   {friendly_error(exc)}")

    def _update_overall_progress(self):
        """Progress bar = jumlah video selesai / total video yang diketahui."""
        if self._session_total:
            value = self._session_done / self._session_total * 100
            self.progress["value"] = min(100.0, value)
        else:
            self.progress["value"] = 0

    def _refresh_status(self):
        """Status agregat dari semua unduhan yang sedang berjalan."""
        active = len(self._active_rows)
        if not active and not self._session_total:
            return

        downloaded_sum = sum(item[0] for item in self._task_stats.values())
        total_sum = sum(item[1] for item in self._task_stats.values() if item[1])
        speed_sum = sum(item[2] for item in self._task_stats.values())

        parts = []
        if active:
            parts.append(f"Aktif {active}")
        if total_sum:
            parts.append(
                f"{format_size(downloaded_sum)} / {format_size(total_sum)}"
            )
        elif downloaded_sum:
            parts.append(f"{format_size(downloaded_sum)} terunduh")
        if speed_sum:
            parts.append(f"{format_size(speed_sum)}/s")
        if self._session_total:
            parts.append(f"Selesai {self._session_done}/{self._session_total}")
        if parts:
            self.status_var.set(" | ".join(parts))

    def _on_queue_empty(self, session_id):
        with self._queue_lock:
            if self._session is not None and self._session.id == session_id:
                self._session = None

        self.cancel_btn.state(["disabled"])
        self.active_tree.delete(*self.active_tree.get_children())
        self._active_rows.clear()
        self._task_stats.clear()

        if self._cancel_requested:
            self._cancel_requested = False
            self.progress["value"] = 0
            self.status_var.set("Dihentikan")
            self._log("Unduhan dihentikan; antrean dibatalkan.")
            return

        ok = self._session_success
        fail = self._session_fail
        self.progress["value"] = 100 if ok else 0

        if fail and not ok:
            self.status_var.set("Gagal — lihat log")
        elif fail:
            self.status_var.set(f"Selesai: {ok} berhasil, {fail} gagal")
        elif ok:
            self.status_var.set(f"Selesai! {ok} video berhasil diunduh.")
        else:
            self.status_var.set("Selesai — antrean kosong")

        if ok or fail:
            self._log(f"Ringkasan sesi: {ok} berhasil, {fail} gagal.")

    def _gui_progress(self, session_id, task_id=None, percent=None, downloaded=None,
                      total=None, speed=None, eta=None, message=None):
        # Dipanggil dari thread worker -> kirim event ke antrean UI (thread-safe)
        if message:
            if task_id is not None and message.startswith("Tautan video ditemukan:"):
                title = message.split(":", 1)[1].strip()
                self.ui_queue.put(("task_title", session_id, task_id, title))
            self.ui_queue.put(("task_message", session_id, message))
            return
        if percent is not None:
            self.ui_queue.put(
                (
                    "task_progress",
                    session_id,
                    task_id,
                    percent,
                    downloaded or 0,
                    total or 0,
                    speed or 0,
                    eta or 0,
                )
            )

    def _poll_ui(self):
        """Proses event dari worker di main thread (dipanggil berkala)."""
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                session_id = event[1] if len(event) > 1 else None

                # Abaikan event dari sesi lama (mis. setelah Batal lalu mulai lagi)
                current = self._session
                if current is None or session_id != current.id:
                    continue

                if kind == "task_start":
                    self._on_task_start(event[2], event[3])
                elif kind == "task_title":
                    self._on_task_title(event[2], event[3])
                elif kind == "task_progress":
                    _, _, task_id, percent, downloaded, total, speed, eta = event
                    self._on_task_progress(
                        task_id, percent, downloaded, total, speed, eta
                    )
                elif kind == "task_message":
                    self._log(event[2])
                elif kind == "task_done":
                    self._on_task_done(event[2], event[3], event[4], event[5])
                elif kind == "tasks_added":
                    self._session_total += event[2]
                    self._update_overall_progress()
                    self._refresh_status()
                elif kind == "session_message":
                    self._log(event[2])
                elif kind == "expand_error":
                    self._on_expand_error(event[2], event[3])
                elif kind == "queue_empty":
                    self._on_queue_empty(session_id)
        except queue.Empty:
            pass

        try:
            self.root.after(100, self._poll_ui)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Titik masuk
# ---------------------------------------------------------------------------



def main():
    parser = argparse.ArgumentParser(description="Download video dari website via URL")
    parser.add_argument(
        "--url",
        action="append",
        help="URL video yang akan diunduh (boleh diulang untuk banyak video)",
    )
    parser.add_argument(
        "--file",
        help="File teks berisi daftar URL (satu URL per baris)",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Folder tempat menyimpan video (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--quality",
        default="terbaik",
        choices=list(QUALITY_MAP.keys()),
        help="Kualitas video (default: terbaik)",
    )
    parser.add_argument(
        "--cookies",
        default="none",
        metavar="BROWSER[:PROFIL]",
        help=(
            "Ambil cookies dari browser ini (untuk situs yang butuh login). "
            "Boleh disertai profil, mis. chrome:Profile 1"
        ),
    )
    parser.add_argument(
        "--sponsorblock",
        action="store_true",
        help="Lewati segmen iklan/sponsor YouTube (butuh FFmpeg)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=(
            "Jumlah koneksi paralel untuk mempercepat unduhan "
            f"(default: {DEFAULT_THREADS}, 1 = mode biasa)"
        ),
    )
    parser.add_argument(
        "--playlist",
        action="store_true",
        help="Unduh seluruh playlist/daftar, bukan hanya satu video",
    )
    parser.add_argument("--gui", action="store_true", help="Buka antarmuka grafis")

    args = parser.parse_args()

    urls = list(args.url or [])
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                urls += [line.strip() for line in f if line.strip()]
        except OSError as exc:
            print(f"Gagal membaca file daftar URL: {exc}")
            sys.exit(1)

    if urls:
        run_cli(
            urls,
            args.output,
            args.quality,
            args.cookies,
            args.sponsorblock,
            args.threads,
            args.playlist,
        )
    else:
        root = tk.Tk()
        DownloaderApp(root)
        root.mainloop()


if __name__ == "__main__":
    main()
