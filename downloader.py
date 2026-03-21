import os
import re
import shutil
import base64
import asyncio
import logging
import tempfile
import aiohttp
import yt_dlp
from pathlib import Path
from typing import Optional, Callable, Any

logger = logging.getLogger(__name__)

KINESCOPE_PATTERNS = [
    r"https?://kinescope\.io/([a-zA-Z0-9\-]+)",
    r"https?://(?:[\w-]+\.)?kinescope\.io/(?:embed/)?([a-zA-Z0-9\-]+)",
    r"player\.kinescope\.io/[^\"']*[?&]video_id=([a-zA-Z0-9\-]+)",
]

KINESCOPE_API_BASE = "https://kinescope.io/api/videos"

YOUTUBE_PATTERNS = [
    r"(?:https?://)?(?:www\.)?youtube\.com/watch\?(?:[^&]*&)*v=[\w-]+",
    r"(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+",
    r"(?:https?://)?youtu\.be/[\w-]+",
    r"(?:https?://)?(?:www\.)?youtube\.com/live/[\w-]+",
]

INSTAGRAM_PATTERNS = [
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+",
]

VK_PATTERNS = [
    r"(?:https?://)?(?:www\.)?vk\.com/video[-\d_]+",
    r"(?:https?://)?(?:www\.)?vk\.com/clip[-\d_]+",
    r"(?:https?://)?(?:www\.)?vk\.com/\w+\?(?:[^&]*&)*z=video[-\d_]+",
    r"(?:https?://)?(?:www\.)?vkvideo\.ru/video[-\d_]+",
    r"(?:https?://)?vk\.com/video\?z=video[-\d_]+",
]


def detect_platform(url: str) -> Optional[str]:
    """Returns 'youtube', 'instagram', 'vk', 'kinescope', or None."""
    for pattern in YOUTUBE_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return "youtube"
    for pattern in INSTAGRAM_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return "instagram"
    for pattern in VK_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return "vk"
    if extract_video_id(url):
        return "kinescope"
    return None


def extract_video_id(url: str) -> Optional[str]:
    for pattern in KINESCOPE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# ClearKey DRM helpers
# ---------------------------------------------------------------------------

async def _fetch_m3u8_text(m3u8_url: str, referer: Optional[str] = None) -> str:
    """Download m3u8; if it's a master playlist, follow the highest-bandwidth variant."""
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = "https://kinescope.io"

    async with aiohttp.ClientSession() as session:
        async with session.get(
            m3u8_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
            base = str(resp.url).rsplit("/", 1)[0] + "/"

    # Pick the best variant if this is a master playlist
    best_url: Optional[str] = None
    best_bw = -1
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(m.group(1)) if m else 0
            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                uri = lines[i + 1].strip()
                if bw > best_bw:
                    best_bw = bw
                    best_url = uri if uri.startswith("http") else base + uri

    if best_url:
        headers2: dict[str, str] = {}
        if referer:
            headers2["Referer"] = referer
        async with aiohttp.ClientSession() as session:
            async with session.get(
                best_url, headers=headers2, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                text = await resp.text()

    return text


async def extract_drm_info_from_m3u8(
    m3u8_url: str, referer: Optional[str] = None
) -> tuple[list[str], Optional[str]]:
    """Download the HLS manifest and extract:
    - key IDs (hex) from KEYID= attribute
    - full license URL, including any `token=` already embedded in the URI

    The license URL in the manifest often already contains the token — no
    manual user input needed in that case.

    Returns (key_ids, license_url_with_token_or_none).
    """
    try:
        text = await _fetch_m3u8_text(m3u8_url, referer)
    except Exception as e:
        logger.warning(f"Could not fetch m3u8: {e}")
        return [], None

    key_ids: list[str] = []
    license_url: Optional[str] = None

    for line in text.splitlines():
        if not ("#EXT-X-KEY" in line or "#EXT-X-SESSION-KEY" in line):
            continue

        m = re.search(r"KEYID=0x([0-9a-fA-F]{32})", line)
        if m:
            kid = m.group(1).lower()
            if kid not in key_ids:
                key_ids.append(kid)

        uri_m = re.search(r'URI="([^"]+)"', line)
        if uri_m:
            uri = uri_m.group(1)
            if "kinescope.io" in uri and "clearkey" in uri:
                license_url = uri  # keep last match

    logger.info(
        f"Manifest DRM: {len(key_ids)} key IDs, "
        f"license_url={'found' if license_url else 'not found'}"
    )
    return key_ids, license_url


async def fetch_clearkey_keys(
    license_url: str,
    key_ids: list[str],
    token: str = "",
    referer: Optional[str] = None,
) -> dict[str, str]:
    """Request ClearKey decryption keys from the Kinescope license server.

    Args:
        license_url: URL like https://license.kinescope.io/.../clearkey?token=
        key_ids: List of key IDs in hex (16 bytes = 32 hex chars).
        token: Auth token to fill into the `?token=` placeholder.
        referer: Referer header for the license request.

    Returns:
        dict mapping kid_hex → key_hex.
    """
    if not key_ids:
        return {}

    # Inject token into the license URL
    if token:
        url = re.sub(r"(token=)[^&]*", rf"\g<1>{token}", license_url)
    else:
        url = license_url

    # Encode key IDs to base64url (no padding) as required by W3C ClearKey spec
    kids_b64 = [
        base64.urlsafe_b64encode(bytes.fromhex(kid)).rstrip(b"=").decode()
        for kid in key_ids
    ]
    body = {"kids": kids_b64, "type": "temporary"}

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://kinescope.io",
    }
    if referer:
        headers["Referer"] = referer

    logger.info(f"Requesting ClearKey license from {url}")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)

    result: dict[str, str] = {}
    for entry in data.get("keys", []):
        # base64url decode (add padding)
        kid = base64.urlsafe_b64decode(entry["kid"] + "==").hex()
        key = base64.urlsafe_b64decode(entry["k"] + "==").hex()
        result[kid] = key
        logger.info(f"Got key for KID {kid[:8]}…")

    return result


async def build_patched_m3u8(
    m3u8_url: str,
    clearkey_map: dict[str, str],
    referer: Optional[str] = None,
    tmpdir: Optional[str] = None,
) -> Optional[str]:
    """Download the master m3u8, follow the best-quality variant, patch key URIs
    with inline `data:` URIs carrying the actual AES-128 key bytes, and write the
    patched playlist to a temp file.

    Returns the path to the patched m3u8 file (caller must delete it).
    """
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = "https://kinescope.io"

    async with aiohttp.ClientSession() as session:
        # ── 1. Fetch master playlist ──────────────────────────────────────
        async with session.get(
            m3u8_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            resp.raise_for_status()
            master_text = await resp.text()
            base_url = str(resp.url).rsplit("/", 1)[0] + "/"

        # ── 2. If it's a master playlist, pick the highest-bandwidth variant ─
        best_variant_url: Optional[str] = None
        best_bw = -1
        for i, line in enumerate(master_text.splitlines()):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(m.group(1)) if m else 0
                lines = master_text.splitlines()
                if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    uri = lines[i + 1].strip()
                    if bw > best_bw:
                        best_bw = bw
                        best_variant_url = (
                            uri if uri.startswith("http") else base_url + uri
                        )

        if best_variant_url:
            async with session.get(
                best_variant_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                resp.raise_for_status()
                media_text = await resp.text()
                media_base = str(resp.url).rsplit("/", 1)[0] + "/"
        else:
            # Already a media playlist
            media_text = master_text
            media_base = base_url

    # ── 3. Patch #EXT-X-KEY lines ─────────────────────────────────────────
    patched_lines: list[str] = []
    for line in media_text.splitlines():
        if "#EXT-X-KEY" in line or "#EXT-X-SESSION-KEY" in line:
            kid_m = re.search(r"KEYID=0x([0-9a-fA-F]{32})", line)
            if kid_m:
                kid = kid_m.group(1).lower()
                key_hex = clearkey_map.get(kid)
                if key_hex:
                    key_bytes = bytes.fromhex(key_hex)
                    data_uri = (
                        "data:text/plain;base64,"
                        + base64.b64encode(key_bytes).decode()
                    )
                    # Replace the URI="..." with the inline data URI
                    line = re.sub(r'URI="[^"]*"', f'URI="{data_uri}"', line)
                    # Strip unsupported attributes that confuse ffmpeg/yt-dlp
                    for attr in ("KEYFORMAT", "KEYFORMATVERSIONS", "KEYID"):
                        line = re.sub(rf",{attr}=[^,\n]*", "", line)
                    logger.info(f"Patched key URI for KID {kid[:8]}…")
        patched_lines.append(line)

    # Resolve relative segment URLs so yt-dlp can find them
    resolved_lines: list[str] = []
    for line in patched_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("http"):
            line = media_base + stripped
        resolved_lines.append(line)

    patched_text = "\n".join(resolved_lines)

    # ── 4. Write to a temp file ───────────────────────────────────────────
    fd, path = tempfile.mkstemp(suffix=".m3u8", dir=tmpdir)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(patched_text)

    logger.info(f"Patched m3u8 written to {path}")
    return path


# ---------------------------------------------------------------------------
# Core download
# ---------------------------------------------------------------------------

INSTAGRAM_COOKIES_FILE = os.getenv("INSTAGRAM_COOKIES_FILE", "")

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
if not _FFMPEG_AVAILABLE:
    logger.warning("ffmpeg не найден — используется однофайловый формат без объединения дорожек")


def download_video(
    url: str,
    output_dir: str,
    progress_hook: Optional[Callable] = None,
    referer: Optional[str] = None,
    title: Optional[str] = None,
    platform: Optional[str] = None,
) -> Optional[str]:
    if title:
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)
        output_template = os.path.join(output_dir, f"{safe_title}.%(ext)s")
    else:
        output_template = os.path.join(output_dir, "%(title)s.%(ext)s")

    if _FFMPEG_AVAILABLE:
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    else:
        fmt = "best[ext=mp4]/best"

    ydl_opts: dict[str, Any] = {
        "outtmpl": output_template,
        "format": fmt,
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }

    if _FFMPEG_AVAILABLE:
        ydl_opts["merge_output_format"] = "mp4"

    if referer:
        headers = {"Referer": referer}
        if platform == "kinescope":
            headers["Origin"] = "https://kinescope.io"
        ydl_opts["http_headers"] = headers

    if platform == "instagram" and INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
        ydl_opts["cookiefile"] = INSTAGRAM_COOKIES_FILE
        logger.info(f"Using Instagram cookies from {INSTAGRAM_COOKIES_FILE}")

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
    title: Optional[str] = None,
    platform: Optional[str] = None,
) -> Optional[str]:
    last_progress: dict[str, float] = {}

    def progress_hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            if total and progress_callback:
                percent = downloaded / total * 100
                if abs(percent - last_progress.get("percent", 0)) >= 5:
                    last_progress["percent"] = percent
                    asyncio.get_event_loop().call_soon_threadsafe(
                        lambda p=percent, dl=downloaded, tot=total: asyncio.ensure_future(
                            progress_callback(p, dl, tot)
                        )
                    )

    loop = asyncio.get_event_loop()
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    return await loop.run_in_executor(
        None,
        lambda: download_video(
            url,
            output_dir,
            progress_hook if progress_callback else None,
            referer,
            title,
            platform,
        ),
    )


class NeedTokenError(Exception):
    """Raised when the m3u8 manifest has no embedded token and one must be provided."""
    pass


async def async_download_with_clearkey(
    hls_url: str,
    output_dir: str,
    clearkey_url: str,
    token: str = "",
    referer: Optional[str] = None,
    title: Optional[str] = None,
    progress_callback: Optional[Callable] = None,
) -> Optional[str]:
    """Full ClearKey download pipeline:
    1. Extract key IDs and (hopefully) a token-bearing license URL from the m3u8.
    2. Fall back to clearkey_url + user-supplied token if the manifest has none.
    3. Fetch keys from license server.
    4. Patch m3u8 with inline keys.
    5. Download using patched m3u8.

    Raises NeedTokenError if no token could be found automatically and none was supplied.
    """
    # Step 1 – parse manifest; it may already contain the token in the URI
    key_ids, manifest_license_url = await extract_drm_info_from_m3u8(hls_url, referer)

    if not key_ids:
        raise ValueError(
            "Не удалось извлечь key ID из манифеста. "
            "Возможно, видео использует другой тип шифрования."
        )

    # Prefer the license URL from the manifest (token already embedded) over the
    # fallback URL from the JSON (which has an empty token= placeholder).
    effective_license_url = manifest_license_url or clearkey_url

    # If neither source provided a token, ask the user
    has_token_in_url = "token=" in effective_license_url and not effective_license_url.endswith("token=")
    if not has_token_in_url and not token:
        raise NeedTokenError(
            "Токен не найден в манифесте. Пожалуйста, предоставьте токен вручную."
        )

    # Step 2 – fetch keys
    clearkey_map = await fetch_clearkey_keys(effective_license_url, key_ids, token, referer)
    if not clearkey_map:
        raise ValueError(
            "Лицензионный сервер не вернул ключи. "
            "Проверьте правильность токена."
        )

    # Step 3 – patch m3u8
    patched_path = await build_patched_m3u8(hls_url, clearkey_map, referer, output_dir)
    if not patched_path:
        raise RuntimeError("Не удалось создать patched m3u8.")

    try:
        # Step 4 – download from patched manifest (local file:// path)
        return await async_download_video(
            url=f"file://{patched_path}",
            output_dir=output_dir,
            progress_callback=progress_callback,
            referer=referer,
            title=title,
        )
    finally:
        cleanup_file(patched_path)


# ---------------------------------------------------------------------------
# JSON player-state parser
# ---------------------------------------------------------------------------

def parse_kinescope_json(data: dict) -> dict:
    """Extract download info from a Kinescope player state JSON.

    Returns a dict with keys:
        hls_url      – signed m3u8 URL (required)
        title        – video title (optional)
        video_id     – video UUID (optional)
        referrer     – page referrer (optional)
        clearkey_url – ClearKey DRM license URL (optional)
    """
    result: dict[str, Any] = {}

    playlist = (
        data.get("options", {}).get("playlist")
        or data.get("rawOptions", {}).get("playlist")
        or []
    )

    if playlist:
        item = playlist[0]
        sources = item.get("sources", {})
        hls_src = (
            sources.get("hls", {}).get("src")
            or sources.get("shakahls", {}).get("src")
        )
        if hls_src:
            result["hls_url"] = hls_src

        result["title"] = item.get("title") or item.get("meta", {}).get("title")
        result["video_id"] = item.get("id")

        clearkey = item.get("drm", {}).get("clearkey", {}).get("licenseUrl")
        if clearkey:
            result["clearkey_url"] = clearkey

    state_video_id = data.get("state", {}).get("videoId")
    if state_video_id:
        result["video_id"] = state_video_id

    result["referrer"] = data.get("referrer")
    result["embed_url"] = data.get("url")

    return result


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

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
