import os
import re
import asyncio
import logging
import aiohttp
import yt_dlp
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

KINESCOPE_PATTERNS = [
    r"https?://kinescope\.io/([a-zA-Z0-9]+)",
    r"https?://(?:[\w-]+\.)?kinescope\.io/(?:embed/)?([a-zA-Z0-9]+)",
    r"player\.kinescope\.io/[^\"']*[?&]video_id=([a-zA-Z0-9]+)",
]

KINESCOPE_API_BASE = "https://kinescope.io/api/videos"
KINESCOPE_EMBED_BASE = "https://kinescope.io/embed"


def extract_video_id(url: str) -> Optional[str]:
    for pattern in KINESCOPE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


async def get_video_info(video_id: str) -> Optional[dict]:
    url = f"{KINESCOPE_API_BASE}/{video_id}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.warning(f"Could not fetch video info from API: {e}")
    return None


def download_video(
    url: str,
    output_dir: str,
    progress_hook: Optional[Callable] = None,
    referer: Optional[str] = None,
) -> Optional[str]:
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }

    if referer:
        ydl_opts["http_headers"] = {"Referer": referer}

    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                filename = ydl.prepare_filename(info)
                if not os.path.exists(filename):
                    filename = filename.rsplit(".", 1)[0] + ".mp4"
                return filename if os.path.exists(filename) else None
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Download error: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during download: {e}")
        raise


async def async_download_video(
    url: str,
    output_dir: str,
    progress_callback: Optional[Callable] = None,
    referer: Optional[str] = None,
) -> Optional[str]:
    last_progress = {}

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total and progress_callback:
                percent = downloaded / total * 100
                if abs(percent - last_progress.get("percent", 0)) >= 5:
                    last_progress["percent"] = percent
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda p=percent: asyncio.ensure_future(
                            progress_callback(p, downloaded, total)
                        )
                    )

    loop = asyncio.get_event_loop()
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    return await loop.run_in_executor(
        None,
        lambda: download_video(url, output_dir, progress_hook if progress_callback else None, referer),
    )


def build_kinescope_url(video_id: str) -> str:
    return f"https://kinescope.io/{video_id}"


def get_file_size_mb(filepath: str) -> float:
    return os.path.getsize(filepath) / (1024 * 1024)


def cleanup_file(filepath: str) -> None:
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            logger.info(f"Cleaned up: {filepath}")
    except Exception as e:
        logger.warning(f"Failed to cleanup {filepath}: {e}")
