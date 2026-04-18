import os
import shutil
import subprocess
import tempfile
import threading

import requests as req_lib
import yt_dlp
from flask import Flask, request, jsonify, send_file

# ── ffmpeg ─────────────────────────────────────────────────────────────────────
try:
    import imageio_ffmpeg

    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["PATH"] = (
        os.path.dirname(_ffmpeg_exe) + os.pathsep + os.environ.get("PATH", "")
    )
    print(f"[ffmpeg] ✅ {_ffmpeg_exe}")
except Exception as e:
    print(f"[ffmpeg] ⚠️ {e}")

# ── Node.js — find exe and store exact path for yt-dlp js_runtimes ────────────
_node_exe = shutil.which("node") or shutil.which("node.exe")
if not _node_exe:
    for _p in [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        os.path.expanduser(r"~\AppData\Roaming\nvm\current\node.exe"),
        os.path.expanduser(r"~\AppData\Local\Programs\nodejs\node.exe"),
    ]:
        if os.path.exists(_p):
            _node_exe = _p
            break

if _node_exe:
    # Ensure its directory is on PATH for subprocesses
    os.environ["PATH"] = (
        os.path.dirname(_node_exe) + os.pathsep + os.environ.get("PATH", "")
    )
    print(f"[node] ✅ {_node_exe}")
else:
    print("[node] ⚠️ Not found — n-challenge solving will fail")

app = Flask(__name__)
API_SECRET = os.environ.get("API_SECRET", "")


# ── Auth ───────────────────────────────────────────────────────────────────────
def check_auth():
    if not API_SECRET:
        return True
    token = request.headers.get("x-api-secret") or request.args.get("secret")
    return token == API_SECRET


# ── Cookie validity check ──────────────────────────────────────────────────────
def _check_cookie_freshness(cookie_path: str) -> bool:
    """
    Quick heuristic: parse the Netscape cookie file and check if any
    youtube.com session cookies (SAPISID, __Secure-3PAPISID, LOGIN_INFO)
    are present and not obviously expired (expiry > now).
    Returns True if cookies look usable, False if stale/missing.
    """
    import time

    if not cookie_path or not os.path.exists(cookie_path):
        return False
    session_keys = {"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO", "SID", "HSID"}
    now = time.time()
    found, expired = 0, 0
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) != 7:
                    continue
                domain, _, _, _, expiry_str, name, _ = parts
                if "youtube.com" not in domain and "google.com" not in domain:
                    continue
                if name not in session_keys:
                    continue
                found += 1
                try:
                    expiry = int(expiry_str)
                    if expiry != 0 and expiry < now:
                        expired += 1
                except ValueError:
                    pass
        if found == 0:
            print("[cookies] ⚠️  No YouTube session cookies found in file")
            return False
        if expired == found:
            print(f"[cookies] ❌ All {found} session cookies are EXPIRED")
            return False
        print(f"[cookies] ✅ {found - expired}/{found} session cookies are valid")
        return True
    except Exception as e:
        print(f"[cookies] ⚠️  Could not validate cookies: {e}")
        return True  # assume ok if we can't read


# ── Cookies ────────────────────────────────────────────────────────────────────
def _get_cookie_path(platform: str):
    local = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"{platform}_cookies.txt"
    )
    if os.path.exists(local):
        print(f"[cookies] ✅ local {platform}_cookies.txt")
        return local
    env_key = "YOUTUBE_COOKIES" if platform == "youtube" else "INSTAGRAM_COOKIES"
    raw = os.environ.get(env_key, "")
    if not raw.strip():
        print(f"[cookies] ⚠️ no cookies for {platform}")
        return None
    content = raw
    try:
        content = (
            raw.encode("utf-8")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8")
        )
    except Exception:
        pass
    content = content.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in content.split("\n") if l.strip() and not l.startswith("#")]
    valid = [l for l in lines if len(l.split("\t")) == 7]
    print(f"[cookies] {env_key}: {len(lines)} lines, {len(valid)} valid (7-col)")
    if not valid:
        print(f"[cookies] ❌ No valid cookie lines — skipping")
        return None
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        prefix=f"{platform}_cookies_",
        newline="\n",
    )
    tmp.write(content)
    tmp.flush()
    tmp.close()
    print(f"[cookies] ✅ {len(valid)} cookies written → {tmp.name}")
    return tmp.name


# ── yt-dlp base opts ───────────────────────────────────────────────────────────
def _base_opts() -> dict:
    proxy = os.environ.get("YTDLP_PROXY", "")
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "nocheckcertificate": True,
        "retries": 5,
        "socket_timeout": 30,
        # ── Critical: explicitly hand yt-dlp the Node.js path so it can solve
        #    the n-challenge (throttling param). Without this, yt-dlp finds node
        #    via PATH but the new EJS-based solver also needs it registered here.
        "js_runtimes": {"node": {"path": _node_exe}} if _node_exe else {"node": {}},
        # Allow fetching the EJS challenge-solver script from npm/GitHub if needed
        "remote_components": ["ejs:npm", "ejs:github"],
    }
    if proxy:
        opts["proxy"] = proxy
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        print(f"[proxy] {proxy[:50]}...")
    return opts


# ── YouTube client chain ───────────────────────────────────────────────────────
#
#  Now that js_runtimes is explicitly set, Node.js WILL solve the n-challenge.
#  Chain strategy:
#   1. web_embedded — supports cookies, no PO token needed, n-challenge solved by node
#   2. web          — standard client, cookies, n-challenge solved by node
#   3. ios          — no cookies (by design), pre-signed URLs, no n-challenge needed
#   4. android      — same as ios, different UA
#
#  Each tuple: (client_name, skip_protocols, use_cookies)

_YT_CLIENT_CHAIN = [
    ("web_embedded", [], True),
    ("web", [], True),
    ("ios", [], False),  # no cookies — pre-signed stream URLs
    ("android", [], False),  # no cookies — pre-signed stream URLs
]

_UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
_UA_IOS = "com.google.ios.youtube/19.29.1 CFNetwork/1490.0.4 Darwin/23.2.0"
_UA_ANDROID = (
    "com.google.android.youtube/19.29.37 (Linux; U; Android 13; en_US; Pixel 7) gzip"
)


def _yt_opts_for_client(
    client: str, skip_protos: list, use_cookies: bool, extra: dict = {}
) -> dict:
    opts = _base_opts()
    extractor_args: dict = {"player_client": [client]}
    if skip_protos:
        extractor_args["skip"] = skip_protos

    ua = (
        _UA_IOS
        if client == "ios"
        else _UA_ANDROID if client == "android" else _UA_DESKTOP
    )

    opts.update(
        {
            "extractor_args": {"youtube": extractor_args},
            "http_headers": {"User-Agent": ua},
            "geo_bypass": True,
            "geo_bypass_country": "US",
        }
    )
    opts.update(extra)

    if use_cookies:
        cp = _get_cookie_path("youtube")
        if cp:
            opts["cookiefile"] = cp

    return opts


def _extract_yt(url: str, extra: dict = {}, download: bool = False):
    """
    Try each client in the fallback chain.
    Returns (info, client_used) or raises the last exception.
    """
    cp = _get_cookie_path("youtube")
    if cp and not _check_cookie_freshness(cp):
        print(
            "[cookies] ❌ Stale cookies detected — bot-check likely. Re-export cookies!"
        )

    last_exc = None
    for client, skip_protos, use_cookies in _YT_CLIENT_CHAIN:
        try:
            print(f"[yt-dlp] Trying client: {client}")
            opts = _yt_opts_for_client(client, skip_protos, use_cookies, extra)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=download)
            if not info:
                continue
            # Reject stubs-only / image-only responses
            fmts = info.get("formats") or []
            has_real = any(
                f.get("url") and f.get("vcodec", "none") != "none" for f in fmts
            ) or (not fmts and info.get("url"))
            if download or has_real or info.get("url"):
                print(f"[yt-dlp] ✅ client={client}")
                return info, client
            print(f"[yt-dlp] ⚠️ client={client} returned no real formats, trying next…")
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            print(f"[yt-dlp] ❌ client={client}: {str(e)[:120]}")
            last_exc = e
            if (
                "private" in msg
                or "copyright" in msg
                or ("age" in msg and "restrict" in msg)
            ):
                raise
            continue
        except Exception as e:
            print(f"[yt-dlp] ❌ client={client} unexpected: {e}")
            last_exc = e
            continue
    raise last_exc or yt_dlp.utils.DownloadError("All clients failed")


# ── Instagram opts ─────────────────────────────────────────────────────────────
def _ig_opts(extra: dict = {}) -> dict:
    opts = _base_opts()
    opts.update(
        {
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15"
                ),
            },
        }
    )
    opts.update(extra)
    cp = _get_cookie_path("instagram")
    if cp:
        opts["cookiefile"] = cp
    return opts


# ── Helpers ────────────────────────────────────────────────────────────────────
def sanitize(name: str) -> str:
    return (
        "".join(c for c in (name or "") if c.isalnum() or c in " _-").strip() or "media"
    )


def cleanup(path: str):
    def _rm():
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    threading.Thread(target=_rm, daemon=True).start()


def find_file(folder: str, ext: str):
    for f in os.listdir(folder):
        if f.lower().endswith(f".{ext}"):
            return os.path.join(folder, f)
    for f in os.listdir(folder):
        p = os.path.join(folder, f)
        if os.path.isfile(p):
            return p
    return None


def build_formats(info: dict) -> list:
    fmts = info.get("formats") or []
    if not fmts and info.get("url"):
        return [{"quality": "auto", "ext": info.get("ext", "mp4"), "url": info["url"]}]
    out, seen = [], set()
    for f in fmts:
        h, url = f.get("height"), f.get("url")
        if not h or not url:
            continue
        label = f"{h}p"
        if label in seen:
            continue
        seen.add(label)
        out.append({"quality": label, "ext": f.get("ext", "mp4"), "url": url})
    return sorted(out, key=lambda x: int(x["quality"][:-1]), reverse=True)


def yt_err(msg: str):
    print(f"[ERROR] {msg}")
    m = msg.lower()
    if "sign in" in m or "bot" in m or "confirm" in m:
        return jsonify({"error": "YouTube bot check — re-export cookies"}), 403
    if "private" in m:
        return jsonify({"error": "Video is private"}), 403
    if "age" in m and "restrict" in m:
        return jsonify({"error": "Age-restricted"}), 403
    if "not available" in m:
        return jsonify({"error": "Not available in this region"}), 404
    if "copyright" in m:
        return jsonify({"error": "Blocked by copyright"}), 403
    if "format" in m and "available" in m:
        return jsonify({"error": "No downloadable formats found"}), 404
    return jsonify({"error": f"yt-dlp: {msg[:400]}"}), 500


HEIGHT_MAP = {
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "144p": 144,
}


# ── Health ─────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    node = shutil.which("node")
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        ffmpeg_ok = True
    except Exception:
        ffmpeg_ok = False
    base = os.path.dirname(os.path.abspath(__file__))
    yt_cookie_file = os.path.join(base, "youtube_cookies.txt")
    yt_cookie_status = (
        (
            "✅ local (valid)"
            if _check_cookie_freshness(yt_cookie_file)
            else "❌ local (EXPIRED — re-export!)"
        )
        if os.path.exists(yt_cookie_file)
        else ("✅ env" if os.environ.get("YOUTUBE_COOKIES") else "❌ missing")
    )
    return jsonify(
        {
            "status": "ok",
            "ffmpeg": "✅" if ffmpeg_ok else "❌",
            "node": (
                f"✅ {node}"
                if node
                else "❌ not found (ios/mweb clients used — no JS needed)"
            ),
            "yt_client_chain": [c for (c, _, __) in _YT_CLIENT_CHAIN],
            "yt_cookies": yt_cookie_status,
            "ig_cookies": (
                "✅ local"
                if os.path.exists(os.path.join(base, "instagram_cookies.txt"))
                else ("✅ env" if os.environ.get("INSTAGRAM_COOKIES") else "❌ missing")
            ),
            "proxy": "✅" if os.environ.get("YTDLP_PROXY") else "➖",
            "endpoints": {
                "youtube_info": "POST /youtube/info",
                "youtube_audio": "POST /youtube/audio",
                "youtube_video": "POST /youtube/video",
                "youtube_shorts": "POST /youtube/shorts",
                "instagram_info": "POST /instagram/info",
                "instagram_video": "POST /instagram/video",
                "instagram_image": "POST /instagram/image",
                "debug_formats": "POST /youtube/debug",
            },
        }
    )


# ── YouTube info ───────────────────────────────────────────────────────────────
@app.route("/youtube/info", methods=["POST"])
def youtube_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        info, client = _extract_yt(url, download=False)
        thumb = info.get("thumbnail") or ""
        if not thumb and info.get("thumbnails"):
            thumb = sorted(
                info["thumbnails"], key=lambda t: t.get("preference", 0) or 0
            )[-1].get("url", "")
        return jsonify(
            {
                "success": True,
                "client_used": client,
                "videoId": info.get("id", ""),
                "title": info.get("title", ""),
                "author": info.get("uploader") or info.get("channel", ""),
                "thumbnail": thumb,
                "duration": info.get("duration", 0),
                "formats": build_formats(info),
            }
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


# ── YouTube audio ──────────────────────────────────────────────────────────────
@app.route("/youtube/audio", methods=["POST"])
def youtube_audio():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    tmp = tempfile.mkdtemp(prefix="vf_audio_")
    try:
        extra = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        info, _ = _extract_yt(url, extra=extra, download=True)
        f = find_file(tmp, "mp3")
        if not f:
            return jsonify({"error": "MP3 conversion failed — check ffmpeg"}), 500
        print(f"[Audio] ✅ {os.path.getsize(f):,} bytes")
        return send_file(
            f,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title', 'audio'))}.mp3",
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ── YouTube video ──────────────────────────────────────────────────────────────
@app.route("/youtube/video", methods=["POST"])
def youtube_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    h = HEIGHT_MAP.get(quality, 720)
    tmp = tempfile.mkdtemp(prefix="vf_video_")
    try:
        extra = {
            "format": (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]/best"
            ),
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        info, _ = _extract_yt(url, extra=extra, download=True)
        f = find_file(tmp, "mp4")
        if not f:
            return jsonify({"error": "Download failed"}), 500
        print(f"[Video] ✅ {os.path.getsize(f)/1024/1024:.1f} MB")
        return send_file(
            f,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title', 'video'))}_{quality}.mp4",
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ── YouTube shorts ─────────────────────────────────────────────────────────────
@app.route("/youtube/shorts", methods=["POST"])
def youtube_shorts():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    if "/shorts/" in url:
        url = (
            f"https://www.youtube.com/watch?v={url.split('/shorts/')[1].split('?')[0]}"
        )
    h = HEIGHT_MAP.get(quality, 720)
    tmp = tempfile.mkdtemp(prefix="vf_shorts_")
    try:
        extra = {
            "format": (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]/best"
            ),
            "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        info, _ = _extract_yt(url, extra=extra, download=True)
        f = find_file(tmp, "mp4")
        if not f:
            return jsonify({"error": "Download failed"}), 500
        return send_file(
            f,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title', 'short'))}_short.mp4",
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_err(str(e))
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ── Instagram info ─────────────────────────────────────────────────────────────
@app.route("/instagram/info", methods=["POST"])
def instagram_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    try:
        with yt_dlp.YoutubeDL(_ig_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "No info"}), 404
        fmts = info.get("formats") or []
        has_video = any(
            (f.get("vcodec") or "none") != "none" for f in fmts if f.get("url")
        )
        formats = []
        if has_video:
            for f in fmts:
                if f.get("url") and (f.get("vcodec") or "none") != "none":
                    h = f.get("height") or 0
                    formats.append(
                        {
                            "quality": f"{h}p" if h else "HD",
                            "url": f["url"],
                            "height": h,
                        }
                    )
            formats.sort(key=lambda x: x.get("height", 0), reverse=True)
            if not formats and info.get("url"):
                formats = [{"quality": "HD", "url": info["url"], "height": 0}]
        thumb = info.get("thumbnail", "")
        return jsonify(
            {
                "success": True,
                "type": "video" if has_video else "image",
                "title": info.get("title")
                or info.get("description")
                or "Instagram Post",
                "author": info.get("uploader") or info.get("channel", ""),
                "thumbnail": thumb,
                "duration": info.get("duration", 0),
                "formats": formats,
                "defaultUrl": formats[0]["url"] if formats else thumb,
            }
        )
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return jsonify({"error": "Private or login required"}), 403
        return jsonify({"error": msg[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


# ── Instagram video ────────────────────────────────────────────────────────────
@app.route("/instagram/video", methods=["POST"])
def instagram_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    tmp = tempfile.mkdtemp(prefix="vf_ig_")
    try:
        opts = _ig_opts(
            {
                "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
                "outtmpl": os.path.join(tmp, "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
                "postprocessors": [
                    {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
                ],
            }
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        f = find_file(tmp, "mp4")
        if not f:
            return jsonify({"error": "Download failed"}), 500
        return send_file(
            f,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{sanitize(info.get('title') or 'reel')}.mp4",
        )
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return jsonify({"error": "Private or login required"}), 403
        return jsonify({"error": msg[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ── Instagram image ────────────────────────────────────────────────────────────
@app.route("/instagram/image", methods=["POST"])
def instagram_image():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    tmp = tempfile.mkdtemp(prefix="vf_ig_img_")
    try:
        with yt_dlp.YoutubeDL(_ig_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "No info"}), 404
        fmts = info.get("formats") or []
        if any((f.get("vcodec") or "none") != "none" for f in fmts if f.get("url")):
            return jsonify({"error": "Video post — use /instagram/video"}), 400
        img_url = info.get("thumbnail", "")
        if not img_url:
            return jsonify({"error": "No image found"}), 404
        r = req_lib.get(
            img_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.instagram.com/",
            },
            timeout=30,
        )
        if r.status_code != 200:
            return jsonify({"error": f"HTTP {r.status_code}"}), 500
        ct = r.headers.get("Content-Type", "image/jpeg")
        ext = "png" if "png" in ct else "webp" if "webp" in ct else "jpg"
        safe = sanitize(info.get("title") or info.get("description") or "post")
        path = os.path.join(tmp, f"{safe}.{ext}")
        with open(path, "wb") as fh:
            fh.write(r.content)
        mime = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
            ext, "image/jpeg"
        )
        return send_file(
            path, mimetype=mime, as_attachment=True, download_name=f"{safe}.{ext}"
        )
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500
    finally:
        cleanup(tmp)


# ── Debug ──────────────────────────────────────────────────────────────────────
@app.route("/youtube/debug", methods=["POST"])
def youtube_debug():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400
    results = {}
    for client, skip_protos, use_cookies in _YT_CLIENT_CHAIN:
        try:
            opts = _yt_opts_for_client(client, skip_protos, use_cookies)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = info.get("formats") or []
            results[client] = {
                "ok": True,
                "total_formats": len(fmts),
                "video_formats": sum(
                    1
                    for f in fmts
                    if f.get("vcodec", "none") != "none" and f.get("url")
                ),
                "formats": [
                    {
                        "id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "height": f.get("height"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "note": f.get("format_note"),
                        "has_url": bool(f.get("url")),
                    }
                    for f in fmts
                ],
            }
        except Exception as e:
            results[client] = {"ok": False, "error": str(e)[:200]}
    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
