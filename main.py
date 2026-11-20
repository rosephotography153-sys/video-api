import hmac
import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

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

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
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

    formats = []
    eligible_formats = [
        item
        for item in info.get("formats") or []
        if item.get("ext") == "mp4"
        and item.get("protocol") in {"http", "https"}
        and item.get("url")
        and item.get("acodec") != "none"
        and item.get("vcodec") != "none"
    ]
    for item in sorted(
        eligible_formats,
        key=lambda item: (item.get("height") or 0, item.get("width") or 0),
        reverse=True,
    ):
        height = item.get("height")
        resolution = item.get("resolution") or (f"{item.get('width')}x{height}" if height else "unknown")
        formats.append(VideoFormat(resolution=resolution, url=item["url"]))

    return ExtractResponse(
        title=info.get("title") or "Untitled video",
        thumbnail_url=info.get("thumbnail"),
        duration=info.get("duration"),
        formats=formats,
        ad_config=_get_ad_config(),
    )
