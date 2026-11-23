import base64
import binascii
import http.cookiejar
import hmac
import ipaddress
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AnyHttpUrl, BaseModel, Field
from yt_dlp.utils import DownloadError

logger = logging.getLogger(__name__)

app = FastAPI(title="Video Metadata API")
_OFFICIAL_RELEASE_SHA256 = (
    "B8:9E:89:B8:44:6E:C2:D5:8F:72:19:3E:17:AC:EE:2E:D8:C8:81:45:87:5B:27:82:46:24:A4:0D:D7:A3:B1:F8"
)
_YOUTUBE_COOKIES_PATH = "/tmp/cookies.txt"
_youtube_cookies_lock = threading.Lock()
_written_youtube_cookies: str | None = None
_youtube_cookies_configured = False
_youtube_cookie_count = 0
_youtube_cookie_parse_error: str | None = None
_NETSCAPE_COOKIE_HEADER = "# Netscape HTTP Cookie File"
_EXTRACT_CACHE_TTL_SECONDS = 10 * 60
_extract_cache_lock = threading.Lock()
_extract_cache: dict[str, tuple[float, ExtractResponse]] = {}

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    url: AnyHttpUrl


class VideoFormat(BaseModel):
    resolution: str
    quality: str
    url: str
    ext: str
    media_format: str = "MP4"
    media_type: str = "video"
    file_size: str | None = None
    http_headers: dict[str, str] = Field(default_factory=dict)
    requires_mux: bool = False
    audio_url: str | None = None
    audio_http_headers: dict[str, str] = Field(default_factory=dict)


class AdConfig(BaseModel):
    ads_enabled: bool
    startio_app_id: str
    latest_version: int
    force_update: bool
    update_reason: str
    website_url: str
    banner: bool
    interstitial: bool
    rewarded: bool


class ExtractResponse(BaseModel):
    title: str
    thumbnail_url: str | None
    duration: float | None
    formats: list[VideoFormat]
    ad_config: AdConfig


def _environment_flag(name: str, default: str) -> bool:
    value = os.getenv(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _is_official_app_signature(signature: str | None) -> bool:
    if signature is None:
        return False
    normalized_signature = signature.strip().upper()
    return normalized_signature.isascii() and hmac.compare_digest(
        normalized_signature,
        _OFFICIAL_RELEASE_SHA256,
    )


def _get_ad_config() -> AdConfig:
    return AdConfig(
        ads_enabled=os.getenv("ADS_ENABLED", "false").lower() == "true",
        startio_app_id=os.getenv("STARTIO_APP_ID", ""),
        latest_version=int(os.getenv("LATEST_VERSION", "1")),
        force_update=os.getenv("FORCE_UPDATE", "true").lower() == "true",
        update_reason=os.getenv("UPDATE_REASON", ""),
        website_url=os.getenv("WEBSITE_URL", "https://example.com"),
        banner=_environment_flag("SHOW_BANNER", "true"),
        interstitial=_environment_flag("SHOW_INTERSTITIAL", "false"),
        rewarded=_environment_flag("SHOW_REWARDED", "false"),
    )


def _is_youtube_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname == "youtu.be" or hostname.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    ) or hostname in {"youtube.com", "youtube-nocookie.com"}


def _is_facebook_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return (
        hostname in {"facebook.com", "fb.com", "fb.watch"}
        or hostname.endswith((".facebook.com", ".fb.com"))
    )


def _resolve_facebook_share_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.path.lower().startswith(("/share/v/", "/share/r/")):
        return url

    class FacebookRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            if not _is_facebook_url(new_url):
                raise urllib.error.HTTPError(
                    new_url,
                    code,
                    "Facebook share URL redirected outside Facebook.",
                    headers,
                    response,
                )
            _ensure_public_url(new_url)
            return super().redirect_request(
                request, response, code, message, headers, new_url
            )

    headers = {
        "User-Agent": yt_dlp.utils.std_headers["User-Agent"],
        "Accept-Language": "en-US,en;q=0.9",
    }
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(FacebookRedirectHandler)
    try:
        with opener.open(request, timeout=8) as response:
            resolved_url = response.geturl()
    except (OSError, urllib.error.URLError, HTTPException, ValueError) as error:
        logger.info("Could not resolve Facebook share URL before extraction: %s", error)
        return url

    return resolved_url if _is_facebook_url(resolved_url) else url


def _strip_youtube_tracking(url: str) -> str:
    if not _is_youtube_url(url):
        return url

    parsed = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != "si"
    ]
    hostname = (parsed.hostname or "").lower()
    path_segments = [segment for segment in parsed.path.split("/") if segment]

    video_id = None
    if hostname == "youtu.be" and path_segments:
        video_id = path_segments[0]
    elif (
        hostname.endswith("youtube.com")
        and len(path_segments) >= 2
        and path_segments[0] == "shorts"
    ):
        video_id = path_segments[1]

    if video_id:
        query = [("v", video_id)] + [
            (key, value) for key, value in query if key != "v"
        ]
        return urlunsplit(
            ("https", "www.youtube.com", "/watch", urlencode(query), "")
        )

    if hostname.endswith("youtube.com") and parsed.path.rstrip("/") == "/watch":
        return urlunsplit(
            ("https", "www.youtube.com", "/watch", urlencode(query), "")
        )

    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _write_youtube_cookies() -> str | None:
    global _written_youtube_cookies
    global _youtube_cookies_configured
    global _youtube_cookie_count
    global _youtube_cookie_parse_error

    raw_cookie_content = os.getenv("YOUTUBE_COOKIES")
    cookie_content = _strip_cookie_quotes(raw_cookie_content or "")
    cookie_content = _decode_base64_cookie_content(cookie_content)
    if not cookie_content:
        with _youtube_cookies_lock:
            _written_youtube_cookies = None
            _youtube_cookies_configured = False
            _youtube_cookie_count = 0
            _youtube_cookie_parse_error = None
        return None
    cookie_content = _reconstruct_flattened_cookies(cookie_content)

    with _youtube_cookies_lock:
        _youtube_cookies_configured = True

        if (
            cookie_content == _written_youtube_cookies
            and os.path.isfile(_YOUTUBE_COOKIES_PATH)
        ):
            return (
                _YOUTUBE_COOKIES_PATH
                if _youtube_cookie_count > 0 and _youtube_cookie_parse_error is None
                else None
            )
        try:
            file_descriptor = os.open(
                _YOUTUBE_COOKIES_PATH,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as cookie_file:
                cookie_file.write(cookie_content)
            os.chmod(_YOUTUBE_COOKIES_PATH, 0o600)
        except OSError:
            logger.exception("Could not write the YouTube cookies file")
            raise HTTPException(
                status_code=500,
                detail="Unable to prepare YouTube authentication cookies.",
            ) from None

        _written_youtube_cookies = cookie_content
        cookie_jar = http.cookiejar.MozillaCookieJar(_YOUTUBE_COOKIES_PATH)
        try:
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except (http.cookiejar.LoadError, OSError, UnicodeError, ValueError) as error:
            _youtube_cookie_count = 0
            _youtube_cookie_parse_error = str(error)[:300] or "Invalid Netscape cookie file."
        else:
            _youtube_cookie_count = len(cookie_jar)
            _youtube_cookie_parse_error = None

        return (
            _YOUTUBE_COOKIES_PATH
            if _youtube_cookie_count > 0 and _youtube_cookie_parse_error is None
            else None
        )


def _strip_cookie_quotes(content: str) -> str:
    content = content.strip()
    if (
        len(content) >= 2
        and content[0] == content[-1]
        and content[0] in {"'", '"'}
    ):
        return content[1:-1].strip()
    return content


def _decode_base64_cookie_content(content: str) -> str:
    compact_content = "".join(content.split())
    if not compact_content or not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", compact_content):
        return content

    padded_content = compact_content + "=" * (-len(compact_content) % 4)
    try:
        decoded = base64.b64decode(padded_content, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return content

    normalized_decoded = _reconstruct_flattened_cookies(decoded)
    normalized_lines = normalized_decoded.splitlines()
    has_header = decoded.lstrip().startswith(_NETSCAPE_COOKIE_HEADER)
    has_cookie_rows = any(
        _is_netscape_cookie_row(line)
        for line in normalized_lines[1:]
        if not line.startswith("#")
    )
    if has_header or has_cookie_rows:
        return _strip_cookie_quotes(decoded)
    return content


def _is_netscape_cookie_row(line: str) -> bool:
    parts = line.strip().split(None, 6)
    return len(parts) == 7 and _is_cookie_row_start(parts, 0)


def _is_cookie_row_start(fields: list[str], index: int) -> bool:
    return (
        index + 4 < len(fields)
        and fields[index + 1].upper() in {"TRUE", "FALSE"}
        and fields[index + 2].startswith("/")
        and fields[index + 3].upper() in {"TRUE", "FALSE"}
        and re.fullmatch(r"-?\d+", fields[index + 4]) is not None
    )


def _reconstruct_flattened_cookies(content: str) -> str:
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    content = re.sub(
        r"^# Netscape HTTP Cookie File(?:\s+|$)",
        "",
        content,
        count=1,
    )

    comments = []
    cookie_rows = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            comments.append(line)
            continue

        fields = line.split()
        row_starts = [
            index
            for index in range(len(fields) - 4)
            if _is_cookie_row_start(fields, index)
        ]
        if len(row_starts) > 1 and row_starts[0] == 0:
            for index, start in enumerate(row_starts):
                end = (
                    row_starts[index + 1]
                    if index + 1 < len(row_starts)
                    else len(fields)
                )
                parts = " ".join(fields[start:end]).strip().split(None, 6)
                cookie_rows.append("\t".join(parts))
            continue

        parts = line.strip().split(None, 6)
        cookie_rows.append("\t".join(parts))

    return "\n".join([_NETSCAPE_COOKIE_HEADER, *comments, *cookie_rows]) + "\n"


def _clean_ytdlp_error(error: DownloadError) -> str:
    message = str(error).strip()
    message = re.sub(r"\x1b\[[0-9;]*m", "", message)
    message = re.sub(r"^(?:ERROR:\s*)+", "", message, flags=re.IGNORECASE)
    message = re.sub(r"https?://\S+", "[URL]", message)
    message = re.sub(r"\s+", " ", message).strip()
    return message[:300] or "yt-dlp could not extract video metadata."


def _format_height(item: dict) -> int | None:
    height = item.get("height")
    if isinstance(height, (int, float)) and height > 0:
        return int(height)

    resolution = item.get("resolution")
    note = item.get("format_note")
    format_id = item.get("format_id")
    clues = " ".join(
        str(value) for value in (resolution, note, format_id) if value is not None
    ).lower()
    match = re.search(r"\b\d{3,4}\s*p\b", clues)
    if match:
        return int(re.search(r"\d+", match.group()).group())

    match = re.search(r"\b\d{2,4}\s*x\s*(\d{2,4})\b", clues)
    if match:
        return int(match.group(1))

    if re.search(r"\bsd\b", clues):
        return 480
    if re.search(r"\bhd\b", clues):
        return 720

    width = item.get("width")
    if isinstance(width, (int, float)) and width > 0:
        width = int(width)
        if width <= 640:
            return 360
        if width <= 854:
            return 480
        if width <= 960:
            return 540
        if width <= 1280:
            return 720
        if width <= 1920:
            return 1080
        if width <= 2560:
            return 1440
        return 2160
    return None


def _resolution_label(item: dict, media_format: str, media_type: str) -> str:
    if media_type == "audio":
        return "Audio"

    height = _format_height(item)
    if height is None:
        return "SD (quality unavailable)"
    if height >= 2160:
        return f"UHD ({height}p)"
    if height >= 1440:
        return f"2K ({height}p)"
    if height >= 1080:
        return f"Full HD ({height}p)"
    if height >= 720:
        return f"HD ({height}p)"
    return f"SD ({height}p)"


def _format_file_size(item: dict, duration: float | None) -> str | None:
    size = _format_size_bytes(item, duration)
    if size is None:
        return None

    units = ("B", "KB", "MB", "GB", "TB")
    value = size
    unit_index = 0
    while value >= 1000 and unit_index < len(units) - 1:
        value /= 1000
        unit_index += 1
    return f"{value:.1f} {units[unit_index]}"


def _format_size_bytes(item: dict, duration: float | None) -> float | None:
    size = item.get("filesize") or item.get("filesize_approx")
    if not isinstance(size, (int, float)) or size <= 0:
        bitrate = item.get("tbr")
        if isinstance(bitrate, (int, float)) and bitrate > 0 and duration:
            size = bitrate * 1000 * duration / 8
    if not isinstance(size, (int, float)) or size <= 0:
        return None
    return float(size)


def _format_http_headers(
    item: dict,
    referer_url: str,
    *,
    facebook: bool = False,
) -> dict[str, str]:
    raw_headers = item.get("http_headers")
    headers = raw_headers if isinstance(raw_headers, dict) else {}
    user_agent = next(
        (
            value
            for name, value in headers.items()
            if isinstance(name, str)
            and name.lower() == "user-agent"
            and isinstance(value, str)
        ),
        None,
    )
    referer = next(
        (
            value
            for name, value in headers.items()
            if isinstance(name, str)
            and name.lower() == "referer"
            and isinstance(value, str)
        ),
        None,
    )
    result = {
        "User-Agent": user_agent or yt_dlp.utils.std_headers["User-Agent"],
        "Referer": (
            "https://www.facebook.com/"
            if facebook
            else referer or referer_url
        ),
    }
    if facebook:
        result["Accept"] = "*/*"
        result["Connection"] = "keep-alive"
    return result


def _get_cached_extract_response(url: str) -> ExtractResponse | None:
    now = time.monotonic()
    with _extract_cache_lock:
        cached = _extract_cache.get(url)
        if cached is None:
            return None
        expires_at, response = cached
        if expires_at <= now:
            del _extract_cache[url]
            return None
        return response.copy(deep=True, update={"ad_config": _get_ad_config()})


def _cache_extract_response(url: str, response: ExtractResponse) -> None:
    now = time.monotonic()
    with _extract_cache_lock:
        expired_urls = [
            cached_url
            for cached_url, (expires_at, _) in _extract_cache.items()
            if expires_at <= now
        ]
        for cached_url in expired_urls:
            del _extract_cache[cached_url]
        _extract_cache[url] = (
            now + _EXTRACT_CACHE_TTL_SECONDS,
            response.copy(deep=True),
        )


@app.get("/ad-config", response_model=AdConfig)
def get_ad_config() -> AdConfig:
    return _get_ad_config()


@app.get("/cookie-status")
def get_cookie_status() -> dict[str, bool | int | str | None]:
    _write_youtube_cookies()
    with _youtube_cookies_lock:
        return {
            "cookies_configured": _youtube_cookies_configured,
            "youtube_cookie_count": _youtube_cookie_count,
            "parse_error": _youtube_cookie_parse_error,
        }


def _ensure_public_url(url: str) -> None:
    hostname = urlsplit(url).hostname
    if not hostname:
        raise HTTPException(status_code=400, detail="A valid public video URL is required.")

    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Local URLs are not supported.")

    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="The video URL host could not be resolved.") from None

    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise HTTPException(status_code=400, detail="Only publicly reachable video URLs are supported.")


@app.post("/extract", response_model=ExtractResponse)
def extract_video(
    request: ExtractRequest,
    x_app_signature: str | None = Header(default=None),
) -> ExtractResponse | JSONResponse:
    if _environment_flag("ENFORCE_SIGNATURE", "true") and not _is_official_app_signature(
        x_app_signature
    ):
        return JSONResponse(
            status_code=403,
            content={"error": "Modified APK detected. Please use the official app."},
        )

    url = str(request.url)
    extraction_url = _strip_youtube_tracking(url)
    _ensure_public_url(url)
    if _is_facebook_url(extraction_url):
        extraction_url = _resolve_facebook_share_url(extraction_url)
    cached_response = _get_cached_extract_response(extraction_url)
    if cached_response is not None:
        return cached_response

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "writesubtitles": False,
        "socket_timeout": 12,
        "check_formats": False,
        "ignore_no_formats_error": True,
    }
    is_facebook = _is_facebook_url(extraction_url)
    if is_facebook:
        options["http_headers"] = {
            "User-Agent": yt_dlp.utils.std_headers["User-Agent"],
            "Referer": "https://www.facebook.com/",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    cookie_file = None
    if _is_youtube_url(extraction_url):
        cookie_file = _write_youtube_cookies()
        if cookie_file is not None:
            options["cookiefile"] = cookie_file
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["web_safari", "mweb", "web"],
            }
        }

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(extraction_url, download=False)
    except DownloadError as error:
        if _is_youtube_url(extraction_url):
            retry_options = options.copy()
            retry_options.pop("cookiefile", None)
            retry_options["extractor_args"] = {
                "youtube": {
                    "player_client": ["android_vr", "mweb"],
                }
            }
            try:
                with yt_dlp.YoutubeDL(retry_options) as ydl:
                    info = ydl.extract_info(extraction_url, download=False)
            except DownloadError as retry_error:
                error = retry_error
            else:
                error = None

        if error is not None:
            clean_error = _clean_ytdlp_error(error)
            if is_facebook:
                clean_error = f"Facebook extraction failed: {clean_error}"
            logger.error("yt-dlp extraction failed: %s", clean_error)
            raise HTTPException(
                status_code=502,
                detail=clean_error,
            ) from None

    if not isinstance(info, dict):
        raise HTTPException(
            status_code=502,
            detail="The video service returned no metadata for the provided URL.",
        )

    duration = info.get("duration")
    if not isinstance(duration, (int, float)):
        duration = None
    referer_url = info.get("webpage_url")
    if not isinstance(referer_url, str) or not referer_url:
        referer_url = extraction_url
    source_formats = info.get("formats") or []
    progressive_mp4 = []
    video_only_mp4 = []
    audio_only_mp4 = []
    legacy_audio_formats = []
    for item in source_formats:
        media_url = item.get("url")
        if not isinstance(media_url, str) or not media_url:
            continue
        protocol = str(item.get("protocol") or "").lower()
        parsed_media_url = urlsplit(media_url)
        if (
            protocol not in {"http", "https"}
            or parsed_media_url.scheme.lower() not in {"http", "https"}
            or parsed_media_url.path.lower().endswith(".m3u8")
        ):
            continue

        extension = str(item.get("ext") or "").lower()
        has_video = item.get("vcodec") != "none"
        has_audio = item.get("acodec") != "none"
        if extension == "mp4":
            if has_video and has_audio:
                progressive_mp4.append(item)
            elif has_video:
                video_only_mp4.append(item)
            elif has_audio:
                audio_only_mp4.append(item)
        elif (
            extension == "m4a"
            and has_audio
            and item.get("vcodec") in {None, "none"}
        ):
            audio_only_mp4.append(item)
        elif extension == "mp3" and has_audio and not has_video:
            legacy_audio_formats.append((item, "MP3"))

    selected_items = [
        (item, "MP4", "video", None, "mp4")
        for item in progressive_mp4
    ]

    def format_rank(item: dict) -> tuple[float, float]:
        return (
            float(item.get("tbr") or 0),
            float(item.get("filesize") or item.get("filesize_approx") or 0),
        )

    facebook_has_progressive = is_facebook and bool(progressive_mp4)
    if facebook_has_progressive:
        progressive_mp4.sort(key=format_rank, reverse=True)
        selected_items = [
            (item, "MP4", "video", None, "mp4")
            for item in progressive_mp4
        ]

    best_audio = max(audio_only_mp4, key=format_rank, default=None)
    if not facebook_has_progressive:
        if best_audio is not None:
            selected_items.append((best_audio, "M4A", "audio", None, "m4a"))
        else:
            selected_items.extend(
                (item, media_format, "audio", None, media_format.lower())
                for item, media_format in legacy_audio_formats
            )

    if not facebook_has_progressive:
        video_only_by_height: dict[int, dict] = {}
        for item in video_only_mp4:
            height = _format_height(item)
            codec = str(item.get("vcodec") or "").lower()
            if (
                height is None
                or height < 360
                or (codec and not codec.startswith(("avc1", "h264")))
            ):
                continue
            current = video_only_by_height.get(height)
            rank = format_rank(item)
            current_rank = format_rank(current) if current is not None else None
            if current_rank is None or rank > current_rank:
                video_only_by_height[height] = item
        selected_items.extend(
            (item, "MP4", "video", best_audio, "mp4")
            for item in video_only_by_height.values()
        )

    if not selected_items:
        playable_formats = [
            item
            for item in source_formats
            if item.get("protocol") in {"http", "https"}
            and isinstance(item.get("url"), str)
            and item.get("url")
            and not urlsplit(item["url"]).path.lower().endswith(".m3u8")
            and item.get("protocol") not in {"m3u8", "m3u8_native"}
        ]
        playable_formats.sort(
            key=lambda item: (
                item.get("vcodec") in {None, "none"},
                item.get("height") or 0,
                item.get("tbr") or 0,
                item.get("filesize") or item.get("filesize_approx") or 0,
            ),
            reverse=True,
        )
        if playable_formats:
            fallback_item = playable_formats[0]
            fallback_extension = str(fallback_item.get("ext") or "MP4").upper()
            fallback_media_type = (
                "audio"
                if fallback_item.get("vcodec") == "none"
                or fallback_extension in {"MP3", "M4A"}
                else "video"
            )
            selected_items = [
                (
                    fallback_item,
                    fallback_extension,
                    fallback_media_type,
                    None,
                    str(fallback_item.get("ext") or fallback_extension).lower(),
                )
            ]
        else:
            top_level_url = info.get("url")
            if (
                isinstance(top_level_url, str)
                and top_level_url
                and not urlsplit(top_level_url).path.lower().endswith(".m3u8")
            ):
                fallback_extension = str(info.get("ext") or "MP4").upper()
                fallback_media_type = (
                    "audio"
                    if info.get("vcodec") == "none"
                    or fallback_extension in {"MP3", "M4A"}
                    else "video"
                )
                selected_items = [
                    (
                        info,
                        fallback_extension,
                        fallback_media_type,
                        None,
                        str(info.get("ext") or fallback_extension).lower(),
                    )
                ]

    formats_by_url: dict[str, VideoFormat] = {}
    for item, media_format, media_type, audio_item, extension in selected_items:
        resolution = _resolution_label(item, media_format, media_type)
        if audio_item is not None:
            video_size = _format_size_bytes(item, duration)
            audio_size = _format_size_bytes(audio_item, duration)
            file_size = (
                _format_file_size({"filesize": video_size + audio_size}, None)
                if video_size is not None and audio_size is not None
                else None
            )
        else:
            file_size = _format_file_size(item, duration)
        candidate = VideoFormat(
            resolution=resolution,
            quality=resolution,
            url=item["url"],
            ext=extension,
            media_format=media_format,
            media_type=media_type,
            file_size=file_size,
            http_headers=_format_http_headers(
                item,
                referer_url,
                facebook=is_facebook,
            ),
            requires_mux=audio_item is not None,
            audio_url=audio_item.get("url") if audio_item is not None else None,
            audio_http_headers=(
                _format_http_headers(
                    audio_item,
                    referer_url,
                    facebook=is_facebook,
                )
                if audio_item is not None
                else {}
            ),
        )
        formats_by_url.setdefault(item["url"], candidate)

    formats = list(formats_by_url.values())
    if not facebook_has_progressive:
        formats.sort(
            key=lambda item: (
                item.media_format != "MP4",
                _format_height({"format_note": item.resolution}) or 0,
                item.media_format,
            ),
            reverse=False,
        )

    response = ExtractResponse(
        title=info.get("title") or "Untitled video",
        thumbnail_url=info.get("thumbnail"),
        duration=duration,
        formats=formats,
        ad_config=_get_ad_config(),
    )
    _cache_extract_response(extraction_url, response)
    return response
