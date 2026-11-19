# Video Downloader

Aplikasi pengunduh video berbasis [yt-dlp](https://github.com/yt-dlp/yt-dlp). Tempel URL halaman apa pun, aplikasi mencari sumber video di dalamnya dan mengunduhnya.

## Fitur

- **Ribuan situs**: YouTube, Facebook, Instagram, TikTok, Twitter/X, Vimeo, Dailymotion, dan lainnya (via yt-dlp)
- **Resolver khusus**: `filecoster.com`, `jwplyr.com`, `slicedrive.cloud` (melewati iklan/embed)
- **Fallback generik**: bila yt-dlp tidak mengenali URL, aplikasi memindai HTML/JSON/embed (`iframe`, `og:video`, HLS `.m3u8`, MP4, dll.) lalu mengunduh sumber yang ditemukan
- **Cookies dari browser**: pakai sesi login Chrome/Edge/Firefox/Brave/Opera/Vivaldi/Chromium (termasuk profil, mis. `chrome:Profile 1`) untuk situs yang butuh login/verifikasi umur
- **SponsorBlock**: lewati segmen iklan/sponsor YouTube (butuh FFmpeg)
- **Batch download**: tempel banyak URL sekaligus (satu per baris) atau dari file
- **Banyak video bersamaan**: GUI maks 5 video, bisa batalkan antrean dan tambah URL saat mengunduh
- **Playlist/daftar**: opsi unduh seluruh playlist (mis. YouTube list)
- **Retry & timeout**: lebih tahan jaringan tidak stabil
- **Kualitas**: terbaik, 1080p, 720p, 480p, 360p, hanya audio
- **CLI & GUI** (Bahasa Indonesia)

**Batasan:** bukan jaminan semua website bisa diunduh. Halaman yang butuh JavaScript kompleks, login/cookies tertentu, CAPTCHA/anti-bot, atau DRM mungkin belum tertangani. Fallback belum memakai sesi cookies browser. Gunakan hanya untuk video yang kamu berhak unduh.

## Persyaratan

- Python 3.8+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [FFmpeg](https://ffmpeg.org/download.html) (disarankan, untuk merge video+audio dan SponsorBlock)

### Instalasi

```bash
# Install yt-dlp
pip install yt-dlp

# Install FFmpeg (Windows, via winget)
winget install ffmpeg

# Atau download FFmpeg manual dan tambahkan ke PATH
```

Tanpa FFmpeg, aplikasi tetap bisa mengunduh satu file yang sudah tergabung (kualitas/format terbatas).

## Cara Pakai

### Antarmuka Grafis (GUI)

```bash
python downloader.py
```

1. Tempel URL video ke kotak teks (bisa lebih dari satu, satu URL per baris)
2. Pilih folder penyimpanan (default `video/`)
3. Pilih kualitas video
4. Jika situs butuh login, pilih browser pada **Cookies dari browser** (bisa ketik profil, mis. `chrome:Profile 1`)
5. (Opsional) Centang **Lewati iklan/sponsor YouTube** jika butuh
6. Klik **⬇ Mulai Download**

### Baris Perintah (CLI)

```bash
# Download satu video
python downloader.py --url "https://www.youtube.com/watch?v=xxxxx"

# Download beberapa video sekaligus
python downloader.py --url "https://..." --url "https://..." --url "https://..."

# Download dari file daftar URL (satu URL per baris)
python downloader.py --file daftar.txt

# Tentukan kualitas, folder output, dan cookies browser
python downloader.py --url "https://www.youtube.com/watch?v=xxx" --output D:\Downloads --quality 720p --cookies chrome --sponsorblock
```

| Opsi | Keterangan |
|------|------------|
| `--url` | URL video (boleh diulang untuk banyak video) |
| `--file` | File teks berisi daftar URL |
| `--output` | Folder penyimpanan |
| `--quality` | `terbaik`, `1080p`, `720p`, `480p`, `360p`, `audio only` |
| `--cookies` | Browser sumber cookies: `none`, `chrome`, `edge`, `firefox`, `brave`, `opera`, `chromium`, `vivaldi`; boleh disertai profil, mis. `chrome:Profile 1` |
| `--sponsorblock` | Lewati segmen iklan/sponsor YouTube |
| `--threads` | Jumlah koneksi paralel (default `8`, `1` = mode biasa) |
| `--playlist` | Unduh seluruh playlist/daftar, bukan hanya satu video |
| `--gui` | Buka antarmuka grafis |

## Situs dengan Banyak Iklan / Butuh Login

- **Cara termudah**: pada GUI pilih browser yang biasa kamu pakai di **Cookies dari browser** (mis. `chrome`), atau tambahkan `--cookies chrome` di CLI. yt-dlp akan memakai sesi login browser tersebut secara otomatis.
- **Banyak profil Chrome/Edge/Brave**: bila login ada di profil selain `Default`, pilih atau ketik profilnya, mis. `chrome:Profile 1`.
- **Alternatif**: ekspor cookies ke file `cookies.txt` (ekstensi browser seperti *Get cookies.txt LOCALLY*), lalu tambahkan di `DEFAULT_OPTS` pada `downloader.py`:
  ```python
  "cookiefile": "cookies.txt",
  ```

> Catatan: browser harus dalam keadaan login ke situs tersebut. Chrome/Edge mengunci file cookies saat berjalan, jadi cookies biasanya hanya bisa dibaca setelah browser ditutup sepenuhnya (termasuk ikon di tray).

## Dukungan Khusus: filecoster.com

`filecoster.com` adalah situs *file hosting* dengan halaman iklan/embed. Resolver khusus otomatis:

1. Membaca halaman `/d/<id>` atau `/e/<id>` (atau folder `/f/<id>`)
2. Mengambil embed player dari JavaScript halaman
3. Mengambil URL video langsung (`.mp4`) dari player
4. Mengunduh file tersebut langsung dengan progress bar

```bash
python downloader.py --url "https://filecoster.com/d/xxxxx"
python downloader.py --url "https://filecoster.com/f/xxxxx"
```

## Dukungan Khusus: jwplyr.com & slicedrive.cloud

Resolver khusus juga menangani halaman `jwplyr.com` (`/d/`, `/e/`, `/f/`) dan `slicedrive.cloud`. Cukup tempel URL halaman seperti biasa.

## Pemecahan Masalah

| Masalah | Solusi |
|---------|--------|
| `Server menolak akses (HTTP 403 Forbidden)` | Tempel URL halaman pemutar, bukan URL file CDN langsung. Coba isi **Cookies dari browser**. |
| `getaddrinfo failed` | Cek internet/DNS/firewall. |
| `Video unavailable` | Tautan tidak valid atau video sudah dihapus. |
| `Sign in to confirm...` | Butuh login/verifikasi umur. Gunakan opsi **Cookies dari browser**. |
| `ffmpeg not found` | Install FFmpeg (`winget install ffmpeg`). |
| Format tidak tersedia | Coba kualitas `terbaik`. |
| `filecoster: ...` / `jwplyr: ...` / `slicedrive: ...` | Situs berubah/link mati. Pastikan link masih aktif di browser. |

## Catatan Hukum

Gunakan aplikasi ini hanya untuk konten yang kamu berhak unduh. Tanggung jawab penggunaan ada pada pengguna.
