import os
import yt_dlp
import tempfile
import instaloader
from flask import Flask, request, jsonify

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
    """Write cookies from env var to temp file."""

    env_key = "YOUTUBE_COOKIES" if platform == "youtube" else "INSTAGRAM_COOKIES"
    cookies_content = os.environ.get(env_key, "")

    # Check local cookies.txt first (for localhost dev)
    local_cookies = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"{platform}_cookies.txt"
    )
    if os.path.exists(local_cookies):
        print(f"[yt-dlp] Using local {platform}_cookies.txt")
        return local_cookies

    # Write env var to temp file (for Render production)
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

    # Optional proxy support — set YTDLP_PROXY env var on Render if needed
    # e.g. YTDLP_PROXY=socks5://user:pass@host:1080
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
            "youtube_cookies": "✅ loaded" if has_yt_cookies else "❌ missing",
            "instagram_cookies": "✅ loaded" if has_ig_cookies else "❌ missing",
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

        # Get thumbnail
        thumbnail = info.get("thumbnail") or ""
        if not thumbnail and info.get("thumbnails"):
            thumbs = sorted(
                info["thumbnails"], key=lambda t: t.get("preference", 0) or 0
            )
            thumbnail = thumbs[-1].get("url", "")

        # Build formats list
        quality_order = ["1080p", "720p", "480p", "360p", "240p", "144p"]
        formats = []
        seen_qualities = set()

        for f in info.get("formats") or []:
            if not f.get("url"):
                continue
            if f.get("vcodec") == "none" or f.get("acodec") == "none":
                continue
            if f.get("ext") != "mp4":
                continue

            quality = f.get("format_note") or ""
            height = f.get("height")
            if height and not quality:
                quality = f"{height}p"
            if not quality or quality in seen_qualities:
                continue
            seen_qualities.add(quality)

            filesize = f.get("filesize") or f.get("filesize_approx") or 0
            size_str = f"{filesize / (1024*1024):.1f} MB" if filesize else ""

            formats.append(
                {
                    "quality": quality,
                    "url": f["url"],
                    "label": f"{quality} MP4",
                    "size": size_str,
                    "ext": "mp4",
                }
            )

        def quality_sort_key(f):
            try:
                return quality_order.index(f["quality"])
            except ValueError:
                return 99

        formats.sort(key=quality_sort_key)

        return jsonify(
            {
                "success": True,
                "videoId": info.get("id", ""),
                "title": info.get("title", ""),
                "author": info.get("uploader", "") or info.get("channel", ""),
                "thumbnail": thumbnail,
                "duration": info.get("duration", 0),
                "formats": formats,
                "defaultUrl": formats[0]["url"] if formats else "",
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
                    {
                        "error": "Video not available in this region. Try setting YTDLP_PROXY env var."
                    }
                ),
                404,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


# ── YOUTUBE: /youtube/audio ───────────────────────────────────────────────────


@app.route("/youtube/audio", methods=["POST"])
def youtube_audio():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        opts = get_ydl_opts("youtube", {"format": "bestaudio[ext=m4a]/bestaudio/best"})

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"error": "Could not extract audio info"}), 404

        audio_url = None
        audio_ext = "mp3"
        bitrate = ""

        for f in reversed(info.get("formats") or []):
            if f.get("vcodec") == "none" and f.get("url"):
                abr = f.get("abr") or 0
                audio_url = f["url"]
                audio_ext = f.get("ext", "mp3")
                bitrate = f"{int(abr)}kbps" if abr else "128kbps"
                break

        if not audio_url:
            audio_url = info.get("url")
            audio_ext = info.get("ext", "mp3")

        if not audio_url:
            return jsonify({"error": "No audio stream found"}), 404

        thumbnail = info.get("thumbnail", "")
        if not thumbnail and info.get("thumbnails"):
            thumbs = sorted(
                info["thumbnails"], key=lambda t: t.get("preference", 0) or 0
            )
            thumbnail = thumbs[-1].get("url", "")

        return jsonify(
            {
                "success": True,
                "videoId": info.get("id", ""),
                "audioUrl": audio_url,
                "format": audio_ext,
                "bitrate": bitrate or "128kbps",
                "title": info.get("title", ""),
                "author": info.get("uploader", "") or info.get("channel", ""),
                "thumbnail": thumbnail,
                "duration": info.get("duration", 0),
            }
        )

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower():
            return (
                jsonify(
                    {"error": "YouTube bot check failed. Add YOUTUBE_COOKIES env var."}
                ),
                403,
            )
        return jsonify({"error": f"yt-dlp error: {msg[:300]}"}), 500
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:200]}"}), 500


# ── INSTAGRAM: /instagram/info ────────────────────────────────────────────────


@app.route("/instagram/info", methods=["POST"])
def instagram_info():
    if not check_auth():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Strategy A: yt-dlp (works for public posts)
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
            if f.get("url") and f.get("vcodec") != "none":
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
            formats = [
                {
                    "quality": "HD",
                    "url": info["url"],
                    "label": "HD",
                }
            ]

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

        # Strategy B: Try without cookies
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
                    if f.get("url") and f.get("vcodec") != "none":
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
