import os
import yt_dlp
import tempfile
import subprocess
import instaloader
import requests as req_lib
from flask import Flask, request, jsonify, send_file

# ── ffmpeg path setup (bundled via imageio-ffmpeg, no root needed) ────────────
try:
    import imageio_ffmpeg

    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_dir = os.path.dirname(_ffmpeg_exe)
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    print(f"[ffmpeg] ✅ Using bundled ffmpeg: {_ffmpeg_exe}")
except Exception as _e:
    print(f"[ffmpeg] ⚠️ imageio_ffmpeg not available, trying system ffmpeg: {_e}")

app = Flask(__name__)

API_SECRET = os.environ.get("API_SECRET", "")

# ── Auth ──────────────────────────────────────────────────────────────────────


def check_auth():
    if not API_SECRET:
        return True
    token = request.headers.get("x-api-secret") or request.args.get("secret")
    return token == API_SECRET


# ── Cookies ───────────────────────────────────────────────────────────────────


def write_cookies_file(platform="youtube"):
    env_key = "YOUTUBE_COOKIES" if platform == "youtube" else "INSTAGRAM_COOKIES"
    cookies_content = os.environ.get(env_key, "")
    # Fix Windows-style line endings
    cookies_content = cookies_content.replace("\r\n", "\n").replace("\r", "\n")

    print(f"[cookies] {env_key} length: {len(cookies_content)} chars")
    print(f"[cookies] First 80 chars: {cookies_content[:80]!r}")

    local_cookies = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"{platform}_cookies.txt"
    )
    if os.path.exists(local_cookies):
        print(f"[yt-dlp] Using local {platform}_cookies.txt")
        return local_cookies
    if cookies_content.strip():
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix=f"{platform}_cookies_"
        )
        tmp.write(cookies_content)
        tmp.flush()
        tmp.close()
        print(f"[yt-dlp] Using {env_key} env var → {tmp.name}")
        return tmp.name
    print(f"[yt-dlp] ⚠️ No cookies found for {platform}")
    return None


# ── yt-dlp Options ────────────────────────────────────────────────────────────


def get_ydl_opts(platform="youtube", extra={}):
    proxy = os.environ.get("YTDLP_PROXY", "")
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "geo_bypass": True,
        "geo_bypass_country": "IN",
        "nocheckcertificate": True,
        "retries": 3,
        # 🔥 CRITICAL FIX
        "js_runtimes": {"node": {}},
        "extractor_args": {
            "youtube": {
                "player_client": ["android"],
                # "skip": ["webpage", "configs"],
                # "skip": ["webpage", "configs"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11)",
        },
    }

    opts.update(extra)

    if proxy:
        opts["proxy"] = proxy
        print(f"[yt-dlp] Using proxy: {proxy[:30]}...")

    cookies_path = write_cookies_file(platform)
    if cookies_path:
        opts["cookiefile"] = cookies_path

    return opts


# ── Helpers ───────────────────────────────────────────────────────────────────


def is_hls_url(url):
    return ".m3u8" in url or "manifest" in url.lower()


def sanitize_filename(name):
    return "".join(c for c in name if c.isalnum() or c in " _-").strip() or "media"


def cleanup_dir(tmp_dir):
    import threading, shutil

    def _rm():
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    threading.Thread(target=_rm, daemon=True).start()


def build_video_formats(info: dict):
    formats = info.get("formats") or []

    # 🔥 fallback if formats empty
    if not formats and info.get("url"):
        return [
            {
                "quality": "auto",
                "ext": info.get("ext"),
                "url": info.get("url"),
            }
        ]

    out = []
    seen = set()

    for f in formats:
        height = f.get("height")
        url = f.get("url")

        if not height or not url:
            continue

        label = f"{height}p"
        if label in seen:
            continue

        seen.add(label)

        out.append(
            {
                "quality": label,
                "ext": f.get("ext"),
                "url": url,
            }
        )

    return sorted(out, key=lambda x: int(x["quality"][:-1]), reverse=True)


def yt_error_response(msg):
    print(f"[yt-dlp ERROR] {msg}")  # ← yeh add karo
    msg_lower = msg.lower()
    # "cookies" word sirf tab trigger kare jab actual YouTube bot check fail ho
    if "sign in" in msg or "confirm your age" in msg_lower:
        return (
            jsonify(
                {"error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var."}
            ),
            403,
        )
    if "bot" in msg_lower and ("detected" in msg_lower or "check" in msg_lower):
        return (
            jsonify(
                {"error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var."}
            ),
            403,
        )
    if "private video" in msg_lower:
        return jsonify({"error": "This video is private."}), 403
    if "age" in msg_lower and "restrict" in msg_lower:
        return jsonify({"error": "Age-restricted video."}), 403
    if "not available" in msg_lower:
        return jsonify({"error": "Video not available in this region."}), 404
    # Default — actual error dikhao debug ke liye
    return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500


# ── Health Check ──────────────────────────────────────────────────────────────


@app.route("/", methods=["GET"])
def health():
    has_yt = bool(
        os.environ.get("YOUTUBE_COOKIES")
        or os.path.exists(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt"
            )
        )
    )
    has_ig = bool(
        os.environ.get("INSTAGRAM_COOKIES")
        or os.path.exists(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "instagram_cookies.txt"
            )
        )
    )
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        ffmpeg_status = "✅ available"
    except Exception:
        ffmpeg_status = "❌ not found"
    return jsonify(
        {
            "status": "ok",
            "service": "VidiFlow Media API",
            "endpoints": {
                "youtube_info": "POST /youtube/info",
                "youtube_audio": "POST /youtube/audio   → streams MP3",
                "youtube_video": "POST /youtube/video   → streams MP4",
                "youtube_shorts": "POST /youtube/shorts  → streams MP4",
                "instagram_info": "POST /instagram/info",
                "instagram_video": "POST /instagram/video → streams MP4",
                "instagram_image": "POST /instagram/image → streams JPG",
            },
            "youtube_cookies": "✅ loaded" if has_yt else "❌ missing",
            "instagram_cookies": "✅ loaded" if has_ig else "❌ missing",
            "ffmpeg": ffmpeg_status,
            "geo_bypass": "✅ enabled (US)",
            "proxy": "✅ configured" if os.environ.get("YTDLP_PROXY") else "➖ not set",
        }
    )


# ── YOUTUBE: /youtube/info ────────────────────────────────────────────────────


@app.route("/youtube/info", methods=["POST"])
def youtube_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts("youtube")) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "Could not extract video info"}), 404
        thumbnail = info.get("thumbnail") or ""
        if not thumbnail and info.get("thumbnails"):
            thumbs = sorted(
                info["thumbnails"], key=lambda t: t.get("preference", 0) or 0
            )
            thumbnail = thumbs[-1].get("url", "")
        formats = build_video_formats(info)
        return jsonify(
            {
                "success": True,
                "videoId": info.get("id", ""),
                "title": info.get("title", ""),
                "author": info.get("uploader", "") or info.get("channel", ""),
                "thumbnail": thumbnail,
                "duration": info.get("duration", 0),
                "formats": formats,
            }
        )
    except yt_dlp.utils.DownloadError as e:
        return yt_error_response(str(e))
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


# ── YOUTUBE: /youtube/audio → MP3 ────────────────────────────────────────────


@app.route("/youtube/audio", methods=["POST"])
def youtube_audio():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_audio_")
    try:
        ydl_opts = {
            **get_ydl_opts("youtube"),
            "skip_download": False,
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        safe_title = sanitize_filename(info.get("title", "audio"))
        mp3_file = next(
            (
                os.path.join(tmp_dir, f)
                for f in os.listdir(tmp_dir)
                if f.endswith(".mp3")
            ),
            None,
        )

        if not mp3_file or not os.path.exists(mp3_file):
            return jsonify({"error": "MP3 conversion failed — ffmpeg error."}), 500

        print(f"[Audio] ✅ {mp3_file} ({os.path.getsize(mp3_file)} bytes)")
        return send_file(
            mp3_file,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{safe_title}.mp3",
        )

    except yt_dlp.utils.DownloadError as e:
        return yt_error_response(str(e))
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── YOUTUBE: /youtube/video → MP4 ────────────────────────────────────────────


@app.route("/youtube/video", methods=["POST"])
def youtube_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_video_")
    try:
        height_map = {
            "2160p": 2160,
            "1440p": 1440,
            "1080p": 1080,
            "720p": 720,
            "480p": 480,
            "360p": 360,
            "240p": 240,
            "144p": 144,
        }
        max_height = height_map.get(quality, 720)
        # fmt = f"bv*[height<={max_height}]+ba/" f"b[height<={max_height}]/b/best"
        fmt = f"best[height<={max_height}]/best"
        ydl_opts = {
            **get_ydl_opts("youtube"),
            "skip_download": False,
            "format": fmt,
            "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        safe_title = sanitize_filename(info.get("title", "video"))
        mp4_file = next(
            (
                os.path.join(tmp_dir, f)
                for f in os.listdir(tmp_dir)
                if f.endswith(".mp4")
            ),
            None,
        )
        if not mp4_file:
            mp4_file = next(
                (
                    os.path.join(tmp_dir, f)
                    for f in os.listdir(tmp_dir)
                    if os.path.isfile(os.path.join(tmp_dir, f))
                ),
                None,
            )

        if not mp4_file or not os.path.exists(mp4_file):
            return jsonify({"error": "MP4 download failed — ffmpeg error."}), 500

        print(f"[Video] ✅ {mp4_file} ({os.path.getsize(mp4_file)/1024/1024:.1f} MB)")
        return send_file(
            mp4_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}_{quality}.mp4",
        )

    except yt_dlp.utils.DownloadError as e:
        return yt_error_response(str(e))
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── YOUTUBE: /youtube/shorts → MP4 ───────────────────────────────────────────


@app.route("/youtube/shorts", methods=["POST"])
def youtube_shorts():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    if "/shorts/" in url:
        video_id = url.split("/shorts/")[-1].split("?")[0]
        url = f"https://www.youtube.com/watch?v={video_id}"
        print(f"[Shorts] Normalized → {url}")

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_shorts_")
    try:
        height_map = {
            "1080p": 1080,
            "720p": 720,
            "480p": 480,
            "360p": 360,
            "240p": 240,
            "144p": 144,
        }
        max_height = height_map.get(quality, 720)
        # fmt = f"bv*[height<={max_height}]+ba/" f"b[height<={max_height}]/b"
        fmt = f"best[height<={max_height}]/best"
        ydl_opts = {
            **get_ydl_opts("youtube"),
            "skip_download": False,
            "format": fmt,
            "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        safe_title = sanitize_filename(info.get("title", "short"))
        mp4_file = next(
            (
                os.path.join(tmp_dir, f)
                for f in os.listdir(tmp_dir)
                if f.endswith(".mp4")
            ),
            None,
        )
        if not mp4_file:
            mp4_file = next(
                (
                    os.path.join(tmp_dir, f)
                    for f in os.listdir(tmp_dir)
                    if os.path.isfile(os.path.join(tmp_dir, f))
                ),
                None,
            )

        if not mp4_file or not os.path.exists(mp4_file):
            return jsonify({"error": "Shorts download failed."}), 500

        print(f"[Shorts] ✅ {mp4_file} ({os.path.getsize(mp4_file)/1024/1024:.1f} MB)")
        return send_file(
            mp4_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}_short.mp4",
        )

    except yt_dlp.utils.DownloadError as e:
        return yt_error_response(str(e))
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── INSTAGRAM: /instagram/info ────────────────────────────────────────────────


@app.route("/instagram/info", methods=["POST"])
def instagram_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        print(f"[Instagram] Fetching info: {url}")
        with yt_dlp.YoutubeDL(get_ydl_opts("instagram")) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "Could not extract Instagram info"}), 404

        thumbnail = info.get("thumbnail", "")
        has_video = any(
            (f.get("vcodec") or "none") != "none"
            for f in (info.get("formats") or [])
            if f.get("url")
        )
        media_type = "video" if has_video else "image"
        formats = []
        if has_video:
            for f in info.get("formats") or []:
                if f.get("url") and (f.get("vcodec") or "none") != "none":
                    height = f.get("height") or 0
                    formats.append(
                        {
                            "quality": f"{height}p" if height else "HD",
                            "url": f["url"],
                            "label": f"{height}p" if height else "HD",
                            "height": height,
                        }
                    )
            formats.sort(key=lambda x: x.get("height", 0), reverse=True)
            if not formats and info.get("url"):
                formats = [{"quality": "HD", "url": info["url"], "label": "HD"}]

        return jsonify(
            {
                "success": True,
                "type": media_type,
                "url": url,
                "title": info.get("title", "")
                or info.get("description", "")
                or "Instagram Post",
                "author": info.get("uploader", "") or info.get("channel", ""),
                "thumbnail": thumbnail,
                "duration": info.get("duration", 0),
                "formats": formats,
                "defaultUrl": formats[0]["url"] if formats else thumbnail,
            }
        )
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return (
                jsonify(
                    {"error": "This Instagram account is private or requires login."}
                ),
                403,
            )
        if "not found" in msg.lower():
            return jsonify({"error": "Instagram post not found."}), 404
        return jsonify({"error": f"Could not extract: {msg[:200]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


# ── INSTAGRAM: /instagram/video → MP4 ────────────────────────────────────────


@app.route("/instagram/video", methods=["POST"])
def instagram_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_ig_video_")
    try:
        ydl_opts = {
            **get_ydl_opts("instagram"),
            "skip_download": False,
            "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
            "outtmpl": os.path.join(tmp_dir, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        safe_title = sanitize_filename(
            info.get("title", "") or info.get("description", "") or "reel"
        )
        mp4_file = next(
            (
                os.path.join(tmp_dir, f)
                for f in os.listdir(tmp_dir)
                if f.endswith(".mp4")
            ),
            None,
        )
        if not mp4_file:
            mp4_file = next(
                (
                    os.path.join(tmp_dir, f)
                    for f in os.listdir(tmp_dir)
                    if os.path.isfile(os.path.join(tmp_dir, f))
                ),
                None,
            )

        if not mp4_file or not os.path.exists(mp4_file):
            return jsonify({"error": "Instagram video download failed."}), 500

        return send_file(
            mp4_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}.mp4",
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "login" in msg.lower() or "private" in msg.lower():
            return (
                jsonify(
                    {"error": "This Instagram account is private or requires login."}
                ),
                403,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── INSTAGRAM: /instagram/image → JPG ────────────────────────────────────────


@app.route("/instagram/image", methods=["POST"])
def instagram_image():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_ig_image_")
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts("instagram")) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "Could not extract Instagram post info"}), 404

        has_video = any(
            (f.get("vcodec") or "none") != "none"
            for f in (info.get("formats") or [])
            if f.get("url")
        )
        if has_video:
            return (
                jsonify(
                    {
                        "error": "This post contains a video. Use /instagram/video instead."
                    }
                ),
                400,
            )

        image_url = info.get("thumbnail", "")
        if not image_url:
            return jsonify({"error": "No image found in this Instagram post."}), 404

        safe_title = sanitize_filename(
            info.get("title", "") or info.get("description", "") or "instagram_post"
        )
        img_response = req_lib.get(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.instagram.com/",
            },
            timeout=30,
        )
        if img_response.status_code != 200:
            return (
                jsonify(
                    {
                        "error": f"Failed to download image (HTTP {img_response.status_code})"
                    }
                ),
                500,
            )

        content_type = img_response.headers.get("Content-Type", "image/jpeg")
        ext = (
            "png"
            if "png" in content_type
            else "webp" if "webp" in content_type else "jpg"
        )
        img_path = os.path.join(tmp_dir, f"{safe_title}.{ext}")
        with open(img_path, "wb") as f:
            f.write(img_response.content)

        mime_map = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
        return send_file(
            img_path,
            mimetype=mime_map.get(ext, "image/jpeg"),
            as_attachment=True,
            download_name=f"{safe_title}.{ext}",
        )

    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── DEBUG ─────────────────────────────────────────────────────────────────────


@app.route("/youtube/debug", methods=["POST"])
def youtube_debug():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts("youtube")) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return jsonify({"error": "No info"}), 404
        raw = []
        for f in info.get("formats") or []:
            furl = f.get("url", "")
            raw.append(
                {
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "vcodec": f.get("vcodec"),
                    "acodec": f.get("acodec"),
                    "height": f.get("height"),
                    "format_note": f.get("format_note"),
                    "abr": f.get("abr"),
                    "is_hls": is_hls_url(furl),
                    "has_url": bool(furl),
                }
            )
        return jsonify({"total_formats": len(raw), "formats": raw})
    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
