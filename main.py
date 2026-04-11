import os
import yt_dlp
import tempfile
import subprocess
import instaloader
import requests as req_lib
from flask import Flask, request, jsonify, send_file

# ── ffmpeg path setup (works on Render without root) ──────────────────────────
try:
    import imageio_ffmpeg

    _ffmpeg_dir = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    print(f"[ffmpeg] ✅ Using bundled ffmpeg from: {_ffmpeg_dir}")
except Exception as _e:
    print(f"[ffmpeg] ⚠️ imageio_ffmpeg not found, using system ffmpeg: {_e}")

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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        **extra,
    }

    proxy = os.environ.get("YTDLP_PROXY", "")
    if proxy:
        opts["proxy"] = proxy
        print(f"[yt-dlp] Using proxy: {proxy[:30]}...")

    cookies_path = write_cookies_file(platform)
    if cookies_path:
        opts["cookiefile"] = cookies_path

    return opts


# ── Health Check ──────────────────────────────────────────────────────────────


@app.route("/", methods=["GET"])
def health():
    has_yt_cookies = bool(
        os.environ.get("YOUTUBE_COOKIES")
        or os.path.exists(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "youtube_cookies.txt"
            )
        )
    )
    has_ig_cookies = bool(
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
                "youtube_video": "POST /youtube/video   → streams MP4",
                "youtube_audio": "POST /youtube/audio   → streams MP3",
                "youtube_shorts": "POST /youtube/shorts  → streams MP4",
                "instagram_info": "POST /instagram/info",
                "instagram_video": "POST /instagram/video → streams MP4 (reels)",
                "instagram_image": "POST /instagram/image → streams JPG (posts)",
            },
            "youtube_cookies": "✅ loaded" if has_yt_cookies else "❌ missing",
            "instagram_cookies": "✅ loaded" if has_ig_cookies else "❌ missing",
            "ffmpeg": ffmpeg_status,
            "geo_bypass": "✅ enabled (US)",
            "proxy": "✅ configured" if os.environ.get("YTDLP_PROXY") else "➖ not set",
        }
    )


# ── Helpers ───────────────────────────────────────────────────────────────────


def is_hls_url(url):
    return ".m3u8" in url or "manifest" in url.lower()


def build_video_formats(info):
    quality_order = ["1080p", "720p", "480p", "360p", "240p", "144p"]

    formats = []
    seen = set()
    for f in info.get("formats") or []:
        furl = f.get("url", "")
        if not furl or is_hls_url(furl):
            continue
        if (f.get("vcodec") or "none") == "none":
            continue
        if (f.get("acodec") or "none") == "none":
            continue
        if f.get("ext") not in ("mp4", "webm"):
            continue

        height = f.get("height")
        quality = f.get("format_note") or (f"{height}p" if height else None)
        if not quality or quality in seen:
            continue
        seen.add(quality)

        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        ext = f.get("ext", "mp4")
        formats.append(
            {
                "quality": quality,
                "url": furl,
                "label": f"{quality} {ext.upper()}",
                "size": f"{filesize/(1024*1024):.1f} MB" if filesize else "",
                "ext": ext,
                "is_hls": False,
            }
        )

    if not formats:
        seen_hls = set()
        for f in info.get("formats") or []:
            furl = f.get("url", "")
            if not furl or not is_hls_url(furl):
                continue
            if (f.get("vcodec") or "none") == "none":
                continue
            if f.get("ext") not in ("mp4", "webm"):
                continue

            height = f.get("height")
            if not height:
                continue
            quality = f"{height}p"
            if quality in seen_hls:
                continue
            seen_hls.add(quality)

            formats.append(
                {
                    "quality": quality,
                    "url": furl,
                    "label": f"{quality} HLS",
                    "size": "",
                    "ext": f.get("ext", "mp4"),
                    "is_hls": True,
                }
            )

    formats.sort(
        key=lambda f: (
            quality_order.index(f["quality"]) if f["quality"] in quality_order else 99
        )
    )
    return formats


def sanitize_filename(name):
    return "".join(c for c in name if c.isalnum() or c in " _-").strip() or "media"


def download_mp4(url, quality="720p", prefix="vidiflow_video_"):
    """
    Shared helper: downloads + merges video to MP4.
    Returns (mp4_filepath, safe_title, tmp_dir).
    Caller must cleanup tmp_dir.
    """
    height_map = {
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
        "240p": 240,
        "144p": 144,
    }
    max_height = height_map.get(quality, 720)

    tmp_dir = tempfile.mkdtemp(prefix=prefix)
    output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    fmt = (
        f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={max_height}]+bestaudio"
        f"/best[height<={max_height}]"
        f"/best"
    )

    ydl_opts = {
        **get_ydl_opts("youtube"),
        "skip_download": False,
        "format": fmt,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

    title = info.get("title", "video")
    safe_title = sanitize_filename(title)

    mp4_file = None
    for fname in os.listdir(tmp_dir):
        if fname.endswith(".mp4"):
            mp4_file = os.path.join(tmp_dir, fname)
            break
    if not mp4_file:
        for fname in os.listdir(tmp_dir):
            fpath = os.path.join(tmp_dir, fname)
            if os.path.isfile(fpath):
                mp4_file = fpath
                break

    return mp4_file, safe_title, tmp_dir


def cleanup_dir(tmp_dir):
    import threading, shutil

    def _rm():
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    threading.Thread(target=_rm, daemon=True).start()


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
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower() or "cookies" in msg.lower():
            return (
                jsonify(
                    {
                        "error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var on Render."
                    }
                ),
                403,
            )
        if "Private video" in msg:
            return jsonify({"error": "This video is private."}), 403
        if "age" in msg.lower():
            return jsonify({"error": "Age-restricted video."}), 403
        if "not available" in msg.lower():
            return (
                jsonify(
                    {"error": f"Video not available in this region. Full: {msg[:300]}"}
                ),
                404,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


# ── YOUTUBE: /youtube/audio  → MP3 ───────────────────────────────────────────


@app.route("/youtube/audio", methods=["POST"])
def youtube_audio():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_audio_")
    output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    try:
        ydl_opts = {
            **get_ydl_opts("youtube"),
            "skip_download": False,
            "format": "bestaudio/best",
            "outtmpl": output_template,
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

        mp3_file = None
        for fname in os.listdir(tmp_dir):
            if fname.endswith(".mp3"):
                mp3_file = os.path.join(tmp_dir, fname)
                break

        if not mp3_file or not os.path.exists(mp3_file):
            return (
                jsonify(
                    {"error": "MP3 conversion failed — ffmpeg may not be installed."}
                ),
                500,
            )

        print(f"[Audio] ✅ {mp3_file} ({os.path.getsize(mp3_file)} bytes)")

        return send_file(
            mp3_file,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=f"{safe_title}.mp3",
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower() or "cookies" in msg.lower():
            return (
                jsonify(
                    {"error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var."}
                ),
                403,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── YOUTUBE: /youtube/video  → MP4 ───────────────────────────────────────────


@app.route("/youtube/video", methods=["POST"])
def youtube_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = None
    try:
        mp4_file, safe_title, tmp_dir = download_mp4(
            url, quality, prefix="vidiflow_video_"
        )

        if not mp4_file or not os.path.exists(mp4_file):
            return (
                jsonify(
                    {
                        "error": "MP4 download/merge failed — ffmpeg may not be installed."
                    }
                ),
                500,
            )

        print(f"[Video] ✅ {mp4_file} ({os.path.getsize(mp4_file)/1024/1024:.1f} MB)")

        return send_file(
            mp4_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}_{quality}.mp4",
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower() or "cookies" in msg.lower():
            return (
                jsonify(
                    {
                        "error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var on Render."
                    }
                ),
                403,
            )
        if "Private video" in msg:
            return jsonify({"error": "This video is private."}), 403
        if "age" in msg.lower():
            return jsonify({"error": "Age-restricted video."}), 403
        if "not available" in msg.lower():
            return (
                jsonify(
                    {"error": f"Video not available in this region. Full: {msg[:300]}"}
                ),
                404,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        if tmp_dir:
            cleanup_dir(tmp_dir)


# ── YOUTUBE: /youtube/shorts  → MP4 ──────────────────────────────────────────
# Shorts are just normal YouTube videos with a /shorts/ URL.
# yt-dlp handles them identically — this endpoint normalizes the URL
# and forwards to the same download logic.


@app.route("/youtube/shorts", methods=["POST"])
def youtube_shorts():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Normalize shorts URL → standard watch URL so yt-dlp is happy
    if "/shorts/" in url:
        video_id = url.split("/shorts/")[-1].split("?")[0]
        url = f"https://www.youtube.com/watch?v={video_id}"
        print(f"[Shorts] Normalized URL → {url}")

    tmp_dir = None
    try:
        mp4_file, safe_title, tmp_dir = download_mp4(
            url, quality, prefix="vidiflow_shorts_"
        )

        if not mp4_file or not os.path.exists(mp4_file):
            return (
                jsonify(
                    {
                        "error": "Shorts MP4 download failed — ffmpeg may not be installed."
                    }
                ),
                500,
            )

        print(f"[Shorts] ✅ {mp4_file} ({os.path.getsize(mp4_file)/1024/1024:.1f} MB)")

        return send_file(
            mp4_file,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"{safe_title}_short.mp4",
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower() or "cookies" in msg.lower():
            return (
                jsonify(
                    {
                        "error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var on Render."
                    }
                ),
                403,
            )
        if "Private video" in msg:
            return jsonify({"error": "This video is private."}), 403
        if "age" in msg.lower():
            return jsonify({"error": "Age-restricted video."}), 403
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        if tmp_dir:
            cleanup_dir(tmp_dir)


# ── INSTAGRAM: /instagram/info ────────────────────────────────────────────────
# Returns metadata + format list. Does NOT download.
# Use /instagram/video or /instagram/image to actually download.


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

        # Detect media type
        # If there are video formats → it's a reel/video post
        # If only thumbnail/image → it's a photo post
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

        print(f"[Instagram] ✅ type={media_type}, formats={len(formats)}")

        return jsonify(
            {
                "success": True,
                "type": media_type,  # "video" or "image"
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
        print(f"[Instagram] yt-dlp failed: {msg[:200]}")
        if "login" in msg.lower() or "private" in msg.lower():
            return (
                jsonify(
                    {"error": "This Instagram account is private or requires login."}
                ),
                403,
            )
        if "not found" in msg.lower():
            return (
                jsonify(
                    {
                        "error": "Instagram post not found. Make sure the account is public."
                    }
                ),
                404,
            )
        return jsonify({"error": f"Could not extract Instagram info: {msg[:200]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


# ── INSTAGRAM: /instagram/video  → MP4 (reels/videos) ────────────────────────


@app.route("/instagram/video", methods=["POST"])
def instagram_video():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_ig_video_")
    output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    try:
        ydl_opts = {
            **get_ydl_opts("instagram"),
            "skip_download": False,
            "format": "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best",
            "outtmpl": output_template,
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

        mp4_file = None
        for fname in os.listdir(tmp_dir):
            if fname.endswith(".mp4"):
                mp4_file = os.path.join(tmp_dir, fname)
                break
        if not mp4_file:
            for fname in os.listdir(tmp_dir):
                fpath = os.path.join(tmp_dir, fname)
                if os.path.isfile(fpath):
                    mp4_file = fpath
                    break

        if not mp4_file or not os.path.exists(mp4_file):
            return jsonify({"error": "Instagram video download failed."}), 500

        print(
            f"[IG Video] ✅ {mp4_file} ({os.path.getsize(mp4_file)/1024/1024:.1f} MB)"
        )

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
        if "not found" in msg.lower():
            return (
                jsonify(
                    {
                        "error": "Instagram post not found. Make sure the account is public."
                    }
                ),
                404,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500
    finally:
        cleanup_dir(tmp_dir)


# ── INSTAGRAM: /instagram/image  → JPG (photo posts) ─────────────────────────


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
        # First extract info to get the thumbnail / image URL
        with yt_dlp.YoutubeDL(get_ydl_opts("instagram")) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"error": "Could not extract Instagram post info"}), 404

        # For image posts, yt-dlp exposes the image via thumbnail
        image_url = info.get("thumbnail", "")

        # If it's actually a video post, reject
        has_video = any(
            (f.get("vcodec") or "none") != "none"
            for f in (info.get("formats") or [])
            if f.get("url")
        )
        if has_video:
            return (
                jsonify(
                    {
                        "error": "This post contains a video, not an image. Use /instagram/video instead."
                    }
                ),
                400,
            )

        if not image_url:
            return jsonify({"error": "No image found in this Instagram post."}), 404

        safe_title = sanitize_filename(
            info.get("title", "") or info.get("description", "") or "instagram_post"
        )

        # Download the image via requests
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.instagram.com/",
        }
        img_response = req_lib.get(image_url, headers=headers, timeout=30)
        if img_response.status_code != 200:
            return (
                jsonify(
                    {
                        "error": f"Failed to download image (HTTP {img_response.status_code})"
                    }
                ),
                500,
            )

        # Detect extension from content-type
        content_type = img_response.headers.get("Content-Type", "image/jpeg")
        ext = "jpg"
        if "png" in content_type:
            ext = "png"
        elif "webp" in content_type:
            ext = "webp"

        img_path = os.path.join(tmp_dir, f"{safe_title}.{ext}")
        with open(img_path, "wb") as f:
            f.write(img_response.content)

        print(f"[IG Image] ✅ {img_path} ({len(img_response.content)} bytes)")

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


# ── DEBUG: /youtube/debug ─────────────────────────────────────────────────────


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

        raw_formats = []
        for f in info.get("formats") or []:
            furl = f.get("url", "")
            raw_formats.append(
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

        return jsonify({"total_formats": len(raw_formats), "formats": raw_formats})

    except Exception as e:
        return jsonify({"error": str(e)[:500]}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
