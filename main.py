import hmac
import ipaddress
import logging
import os
import re
import socket
import threading
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
    url: str
    media_format: str = "MP4"
    media_type: str = "video"
    file_size: str | None = None
    http_headers: dict[str, str] = Field(default_factory=dict)


class AdConfig(BaseModel):
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
        banner=_environment_flag("SHOW_BANNER", "true"),
        interstitial=_environment_flag("SHOW_INTERSTITIAL", "false"),
        rewarded=_environment_flag("SHOW_REWARDED", "false"),
    )


def _is_youtube_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname == "youtu.be" or hostname.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    ) or hostname in {"youtube.com", "youtube-nocookie.com"}


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
    raw_cookie_content = os.getenv("YOUTUBE_COOKIES")
    if raw_cookie_content is None:
        return None
    cookie_content = raw_cookie_content.strip()
    if (
        len(cookie_content) >= 2
        and cookie_content[0] == cookie_content[-1]
        and cookie_content[0] in {"'", '"'}
    ):
        cookie_content = cookie_content[1:-1].strip()
    cookie_content = _reconstruct_flattened_cookies(cookie_content)

    with _youtube_cookies_lock:
        if (
            cookie_content == _written_youtube_cookies
            and os.path.isfile(_YOUTUBE_COOKIES_PATH)
        ):
            return _YOUTUBE_COOKIES_PATH
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
        return _YOUTUBE_COOKIES_PATH


def _reconstruct_flattened_cookies(content: str) -> str:
    if "\n" in content or "\r" in content:
        return content.replace("\r\n", "\n").replace("\r", "\n")

    fields = content.split()
    cookie_rows = []
    index = 0
    while index + 6 < len(fields):
        domain, include_subdomains, path, secure, expiry = fields[index : index + 5]
        if (
            include_subdomains.upper() in {"TRUE", "FALSE"}
            and path.startswith("/")
            and secure.upper() in {"TRUE", "FALSE"}
            and expiry.isdigit()
        ):
            cookie_rows.append("\t".join(fields[index : index + 7]))
            index += 7
        else:
            index += 1

    if not cookie_rows:
        return content
    return "# Netscape HTTP Cookie File\n" + "\n".join(cookie_rows) + "\n"


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
        return f"Audio ({media_format})"

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
    size = item.get("filesize") or item.get("filesize_approx")
    if not isinstance(size, (int, float)) or size <= 0:
        bitrate = item.get("tbr")
        if isinstance(bitrate, (int, float)) and bitrate > 0 and duration:
            size = bitrate * 1000 * duration / 8
    if not isinstance(size, (int, float)) or size <= 0:
        return None

    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    unit_index = 0
    while value >= 1000 and unit_index < len(units) - 1:
        value /= 1000
        unit_index += 1
    return f"{value:.1f} {units[unit_index]}"


def _format_http_headers(item: dict, referer_url: str) -> dict[str, str]:
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
    return {
        "User-Agent": user_agent or yt_dlp.utils.std_headers["User-Agent"],
        "Referer": referer or referer_url,
    }


@app.get("/ad-config", response_model=AdConfig)
def get_ad_config() -> AdConfig:
    return _get_ad_config()


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
    _ensure_public_url(url)

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
    }
    extraction_url = _strip_youtube_tracking(url)
    if _is_youtube_url(extraction_url):
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["mweb", "android_vr", "tv_embedded", "web"],
            }
        }
        cookie_file = _write_youtube_cookies()
        if cookie_file is not None:
            options["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(extraction_url, download=False)
    except DownloadError as error:
        clean_error = _clean_ytdlp_error(error)
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
    audio_formats = []
    for item in source_formats:
        media_url = item.get("url")
        protocol = str(item.get("protocol") or "").lower()
        if (
            protocol not in {"http", "https"}
            or not isinstance(media_url, str)
            or not media_url
            or protocol in {"m3u8", "m3u8_native"}
            or urlsplit(media_url).path.lower().endswith(".m3u8")
        ):
            continue

        extension = str(item.get("ext") or "").lower()
        has_video = item.get("vcodec") not in {None, "none"}
        has_audio = item.get("acodec") not in {None, "none"}
        if extension == "mp4":
            progressive_mp4.append(item)
        elif extension in {"mp3", "m4a"} and has_audio and not has_video:
            audio_formats.append(item)

    selected_items = [
        (item, "MP4", "video") for item in progressive_mp4
    ] + [(item, str(item.get("ext")).upper(), "audio") for item in audio_formats]
    if not progressive_mp4:
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
                (fallback_item, fallback_extension, fallback_media_type)
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
                    (info, fallback_extension, fallback_media_type)
                ]

    best_formats: dict[str, tuple[tuple[float, float], VideoFormat]] = {}
    for item, media_format, media_type in selected_items:
        resolution = _resolution_label(item, media_format, media_type)
        file_size = _format_file_size(item, duration)
        candidate = VideoFormat(
            resolution=resolution,
            url=item["url"],
            media_format=media_format,
            media_type=media_type,
            file_size=file_size,
            http_headers=_format_http_headers(item, referer_url),
        )
        rank = (
            float(item.get("tbr") or 0),
            float(item.get("filesize") or item.get("filesize_approx") or 0),
        )
        current = best_formats.get(resolution)
        if current is None or rank > current[0]:
            best_formats[resolution] = (rank, candidate)

    formats = [format_option for _, format_option in best_formats.values()]
    formats.sort(
        key=lambda item: (
            item.media_format != "MP4",
            _format_height({"format_note": item.resolution}) or 0,
            item.media_format,
        ),
        reverse=False,
    )

    return ExtractResponse(
        title=info.get("title") or "Untitled video",
        thumbnail_url=info.get("thumbnail"),
        duration=duration,
        formats=formats,
        ad_config=_get_ad_config(),
    )
