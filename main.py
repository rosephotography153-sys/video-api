import hmac
import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AnyHttpUrl, BaseModel
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
    file_size: str | None = None


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


def _upgrade_ytdlp() -> None:
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--upgrade",
                "--pre",
                "yt-dlp",
            ]
        )
    except OSError:
        logger.exception("Could not start the background yt-dlp upgrade")
        return

    logger.info("Started background yt-dlp upgrade (pid=%s)", process.pid)
    return_code = process.wait()
    if return_code:
        logger.error("The background yt-dlp upgrade exited with code %s", return_code)
    else:
        logger.info("The background yt-dlp upgrade completed")


@app.on_event("startup")
def start_ytdlp_upgrade() -> None:
    threading.Thread(
        target=_upgrade_ytdlp,
        name="yt-dlp-upgrade",
        daemon=True,
    ).start()


def _is_youtube_url(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname == "youtu.be" or hostname.endswith(
        (".youtube.com", ".youtube-nocookie.com")
    ) or hostname in {"youtube.com", "youtube-nocookie.com"}


def _strip_youtube_tracking(url: str) -> str:
    if not _is_youtube_url(url):
        return url

    parsed = urlsplit(url)
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "si"]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _write_youtube_cookies() -> str | None:
    global _written_youtube_cookies
    cookie_content = os.getenv("YOUTUBE_COOKIES")
    if cookie_content is None:
        return None

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


def _resolution_label(item: dict, media_format: str) -> str:
    if media_format != "MP4":
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
    }
    extraction_url = _strip_youtube_tracking(url)
    if _is_youtube_url(extraction_url):
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "ios", "tv_embedded", "web"],
            }
        }
        cookie_file = _write_youtube_cookies()
        if cookie_file is not None:
            options["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(extraction_url, download=False)
    except DownloadError:
        logger.error("yt-dlp could not extract video metadata")
        raise HTTPException(
            status_code=502,
            detail="Unable to extract video information from the provided URL.",
        ) from None

    if not isinstance(info, dict):
        raise HTTPException(
            status_code=502,
            detail="The video service returned no metadata for the provided URL.",
        )

    duration = info.get("duration")
    if not isinstance(duration, (int, float)):
        duration = None
    best_formats: dict[str, tuple[tuple[float, float], VideoFormat]] = {}
    for item in info.get("formats") or []:
        if item.get("protocol") not in {"http", "https"} or not item.get("url"):
            continue

        extension = item.get("ext")
        has_video = bool(item.get("vcodec")) and item.get("vcodec") != "none"
        has_audio = bool(item.get("acodec")) and item.get("acodec") != "none"
        if extension == "mp4" and has_video:
            media_format = "MP4"
        elif extension in {"mp3", "m4a"} and has_audio and not has_video:
            media_format = extension.upper()
        else:
            continue

        resolution = _resolution_label(item, media_format)
        file_size = _format_file_size(item, duration)
        candidate = VideoFormat(
            resolution=resolution,
            url=item["url"],
            media_format=media_format,
            file_size=file_size,
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
