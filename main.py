import os
import yt_dlp
import tempfile
import subprocess
import instaloader
from flask import Flask, request, jsonify, send_file

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
    return jsonify(
        {
            "status": "ok",
            "service": "VidiFlow Media API",
            "endpoints": {
                "youtube_info": "POST /youtube/info",
                "youtube_video": "POST /youtube/video  → streams MP4 file",
                "youtube_audio": "POST /youtube/audio  → streams MP3 file",
                "instagram_info": "POST /instagram/info",
            },
            "youtube_cookies": "✅ loaded" if has_yt_cookies else "❌ missing",
            "instagram_cookies": "✅ loaded" if has_ig_cookies else "❌ missing",
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
    """Strip characters unsafe for filenames."""
    return "".join(c for c in name if c.isalnum() or c in " _-").strip() or "video"


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


# ── YOUTUBE: /youtube/audio  → returns MP3 file ───────────────────────────────


@app.route("/youtube/audio", methods=["POST"])
def youtube_audio():
    """
    Downloads the best audio stream, converts to MP3 via ffmpeg,
    and streams the MP3 file back to the client.

    Body: { "url": "https://youtube.com/watch?v=..." }
    Response: audio/mpeg file attachment
    """
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_audio_")
    output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    try:
        # yt-dlp downloads best audio and post-processes to MP3 via ffmpeg
        ydl_opts = {
            **get_ydl_opts("youtube"),
            "skip_download": False,  # we DO want to download
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

        title = info.get("title", "audio")
        safe_title = sanitize_filename(title)

        # Find the produced .mp3 file in the temp dir
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

        print(f"[Audio] ✅ MP3 ready: {mp3_file} ({os.path.getsize(mp3_file)} bytes)")

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
        # Cleanup temp dir in background after response (best-effort)
        import threading
        import shutil

        def _cleanup():
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        threading.Thread(target=_cleanup, daemon=True).start()


# ── YOUTUBE: /youtube/video  → returns MP4 file ───────────────────────────────


@app.route("/youtube/video", methods=["POST"])
def youtube_video():
    """
    Downloads best video+audio for the requested quality, muxes to MP4
    via ffmpeg, and streams the MP4 file back to the client.

    Body: { "url": "...", "quality": "720p" }  (quality optional, default 720p)
    Response: video/mp4 file attachment
    """
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    quality = data.get("quality", "720p").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Map quality label to max height for yt-dlp format selector
    height_map = {
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
        "240p": 240,
        "144p": 144,
    }
    max_height = height_map.get(quality, 720)

    tmp_dir = tempfile.mkdtemp(prefix="vidiflow_video_")
    output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")

    try:
        # Format selector:
        #   - best mp4 up to max_height with audio, or
        #   - best video-only up to max_height + best audio, merged to mp4
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
            "merge_output_format": "mp4",  # ffmpeg merges to MP4
            "postprocessors": [
                {
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }
            ],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title = info.get("title", "video")
        safe_title = sanitize_filename(title)

        # Find the produced .mp4 file
        mp4_file = None
        for fname in os.listdir(tmp_dir):
            if fname.endswith(".mp4"):
                mp4_file = os.path.join(tmp_dir, fname)
                break

        # Fallback: any video file
        if not mp4_file:
            for fname in os.listdir(tmp_dir):
                fpath = os.path.join(tmp_dir, fname)
                if os.path.isfile(fpath):
                    mp4_file = fpath
                    break

        if not mp4_file or not os.path.exists(mp4_file):
            return (
                jsonify(
                    {
                        "error": "MP4 download/merge failed — ffmpeg may not be installed."
                    }
                ),
                500,
            )

        print(
            f"[Video] ✅ MP4 ready: {mp4_file} ({os.path.getsize(mp4_file)/1024/1024:.1f} MB)"
        )

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
        import threading
        import shutil

        def _cleanup():
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

        threading.Thread(target=_cleanup, daemon=True).start()


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
        print(f"[Instagram] Trying yt-dlp for: {url}")
        opts = get_ydl_opts("instagram")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"error": "Could not extract Instagram info"}), 404

        thumbnail = info.get("thumbnail", "")
        formats = []
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

        if not formats:
            return jsonify({"error": "No video found in this Instagram post"}), 404

        print(f"[Instagram] ✅ yt-dlp success, formats: {len(formats)}")

        return jsonify(
            {
                "success": True,
                "type": "video",
                "url": url,
                "title": info.get("title", "")
                or info.get("description", "")
                or "Instagram Video",
                "author": info.get("uploader", "") or info.get("channel", ""),
                "thumbnail": thumbnail,
                "duration": info.get("duration", 0),
                "formats": formats,
                "defaultUrl": formats[0]["url"],
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

        # Retry without cookies, mobile UA
        try:
            print("[Instagram] Retrying without cookies...")
            basic_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "geo_bypass": True,
                "geo_bypass_country": "US",
                "http_headers": {
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                },
            }
            proxy = os.environ.get("YTDLP_PROXY", "")
            if proxy:
                basic_opts["proxy"] = proxy

            with yt_dlp.YoutubeDL(basic_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if info and (info.get("url") or info.get("formats")):
                formats = []
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

                if formats:
                    print("[Instagram] ✅ Retry success")
                    return jsonify(
                        {
                            "success": True,
                            "type": "video",
                            "url": url,
                            "title": info.get("title", "") or "Instagram Video",
                            "author": info.get("uploader", ""),
                            "thumbnail": info.get("thumbnail", ""),
                            "duration": info.get("duration", 0),
                            "formats": formats,
                            "defaultUrl": formats[0]["url"],
                        }
                    )
        except Exception as retry_e:
            print(f"[Instagram] Retry failed: {retry_e}")

        return (
            jsonify({"error": f"Could not extract Instagram video: {msg[:200]}"}),
            500,
        )

    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


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
