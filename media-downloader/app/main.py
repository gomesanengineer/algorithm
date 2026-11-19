"""Media Grabber: download YouTube video/audio and capture PDF/PPT documents as PDF."""
import asyncio
import ipaddress
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .document import capture_document

app = FastAPI(title="Media Grabber")
STATIC = Path(__file__).parent / "static"

MAX_DURATION = int(os.getenv("MAX_VIDEO_SECONDS", "7200"))  # 2 hours
COOKIES_FILE = os.getenv("YTDLP_COOKIES")  # optional Netscape cookies.txt path


class LinkRequest(BaseModel):
    url: str
    format: str | None = None  # "mp4" | "mp3" for video
    quality: str | None = None  # "best" | "1080" | "720" | "480" | "360"
    start: str | None = None  # optional clip start, e.g. "8", "0:08", "00:00:08"
    end: str | None = None  # optional clip end


def validate_public_url(url: str) -> str:
    """Accept only http(s) URLs that resolve to public addresses (blocks SSRF)."""
    url = url.strip()
    parsed = urlparse(url)
    if os.getenv("ALLOW_PRIVATE_URLS") == "1" and parsed.scheme in ("http", "https"):
        return url  # local testing only
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Please paste a full http(s) link.")
    if parsed.hostname.lower() in ("localhost",) or parsed.hostname.lower().endswith((".local", ".internal")):
        raise HTTPException(400, "Links to private or local addresses are not allowed.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        if os.getenv("HTTPS_PROXY"):  # behind an egress proxy that resolves names itself
            return url
        raise HTTPException(400, f"Could not resolve host {parsed.hostname}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(400, "Links to private or local addresses are not allowed.")
    return url


def safe_name(name: str, ext: str) -> str:
    name = re.sub(r"[^\w\-. ()\[\]]+", "_", name).strip(" ._") or "download"
    return f"{name[:120]}.{ext}"


def send_and_cleanup(path: Path, filename: str, tmpdir: str, media_type: str) -> FileResponse:
    return FileResponse(
        path,
        filename=filename,
        media_type=media_type,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


# ---------------------------------------------------------------- video
def _duration_filter(info, *, incomplete=False):
    dur = info.get("duration")
    if dur and dur > MAX_DURATION:
        return f"Video is longer than the {MAX_DURATION // 60} minute limit."
    return None

def parse_time(value: str | None) -> float | None:
    """'90', '1:30', '00:01:30', '1:30.5' -> seconds. Empty -> None."""
    if value is None or not str(value).strip():
        return None
    parts = str(value).strip().split(":")
    if len(parts) > 3:
        raise HTTPException(400, f"Invalid time '{value}'. Use seconds, MM:SS or HH:MM:SS.")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise HTTPException(400, f"Invalid time '{value}'. Use seconds, MM:SS or HH:MM:SS.")
    if any(n < 0 for n in nums):
        raise HTTPException(400, "Times cannot be negative.")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _ytdlp_download(url: str, fmt: str, quality: str, outdir: str,
                    start: float | None = None, end: float | None = None) -> tuple[Path, str]:
    import yt_dlp
    from yt_dlp.utils import download_range_func

    opts: dict = {
        "outtmpl": os.path.join(outdir, "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "restrictfilenames": False,
        # a clip of a long video is fine; only limit full-length downloads
        "match_filter": _duration_filter if start is None and end is None else None,
    }
    if start is not None or end is not None:
        # same as: --download-sections "*START-END" --force-keyframes-at-cuts
        opts["download_ranges"] = download_range_func(None, [(start or 0, end if end is not None else float("inf"))])
        opts["force_keyframes_at_cuts"] = True
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
        ]
    else:
        height = "" if quality in (None, "", "best") else f"[height<={int(quality)}]"
        opts["format"] = (
            f"bv*{height}[ext=mp4]+ba[ext=m4a]/b{height}[ext=mp4]/bv*{height}+ba/b{height}/b"
        )
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None or ((info.get("duration") or 0) > MAX_DURATION and start is None and end is None):
            raise HTTPException(400, f"Video is longer than the {MAX_DURATION // 60} minute limit.")
        title = info.get("title") or "video"

    files = [p for p in Path(outdir).iterdir() if p.suffix.lower() == f".{fmt}"]
    if not files:
        raise HTTPException(500, "Download finished but no output file was produced.")
    return max(files, key=lambda p: p.stat().st_size), title


@app.post("/api/video")
async def download_video(req: LinkRequest):
    fmt = (req.format or "mp4").lower()
    if fmt not in ("mp4", "mp3"):
        raise HTTPException(400, "Format must be mp4 or mp3.")
    start, end = parse_time(req.start), parse_time(req.end)
    if start is not None and end is not None and end <= start:
        raise HTTPException(400, "End time must be after start time.")
    url = validate_public_url(req.url)
    tmpdir = tempfile.mkdtemp(prefix="vid-")
    try:
        path, title = await asyncio.to_thread(_ytdlp_download, url, fmt, req.quality or "best", tmpdir, start, end)
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception as e:  # yt-dlp errors
        shutil.rmtree(tmpdir, ignore_errors=True)
        msg = str(e).replace("\x1b[0;31m", "").replace("\x1b[0m", "")
        if "Sign in to confirm" in msg:
            msg += " — YouTube is blocking this server's IP. Set YTDLP_COOKIES (see README)."
        raise HTTPException(400, msg[:500])
    media = "audio/mpeg" if fmt == "mp3" else "video/mp4"
    return send_and_cleanup(path, safe_name(title, fmt), tmpdir, media)


# ---------------------------------------------------------------- documents
@app.post("/api/document")
async def download_document(req: LinkRequest):
    url = validate_public_url(req.url)
    tmpdir = tempfile.mkdtemp(prefix="doc-")
    try:
        path, title, method = await capture_document(url, Path(tmpdir), validate_public_url)
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(400, f"Could not capture document: {str(e)[:400]}")
    resp = send_and_cleanup(path, safe_name(title, "pdf"), tmpdir, "application/pdf")
    resp.headers["X-Capture-Method"] = method
    return resp


@app.get("/api/health")
async def health():
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg")), "libreoffice": bool(shutil.which("soffice"))}


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
