"""
YouTube Live -> Telegram bot (sirf apne channel ke liye)

Setup:
  pip install pyrogram tgcrypto yt-dlp
  ffmpeg install hona zaroori hai (apt install ffmpeg)

Environment variables:
  API_ID, API_HASH   -> https://my.telegram.org se
  BOT_TOKEN          -> @BotFather se
  OWNER_ID           -> tumhara Telegram user id (sirf tum bot use kar sako)
  CHANNEL_ID         -> tumhara YouTube channel id (UCxxxxxxxx...) auto-detect ke liye
  UPLOAD_CHAT_ID     -> jis chat/channel me video bhejni hai (bot ko admin banao)
  COOKIES_FILE       -> (optional) cookies.txt agar members-only/age-restricted ho

Commands:
  /live <youtube_url>   -> us live ko record karo aur khatam hone pe upload karo
  /watch                -> apne channel ki live auto-detect karna shuru
  /stop                 -> auto-detect band
"""
import asyncio
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
# OWNER_ID me ek ya comma se kai Telegram user id ho sakte hain (jaise 111,222)
OWNER_IDS = [int(x) for x in os.environ["OWNER_ID"].replace(" ", "").split(",") if x]
OWNER_ID = OWNER_IDS[0]  # default upload chat / auto-watch ke status messages yahin jaate hain
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
UPLOAD_CHAT_ID = int(os.environ.get("UPLOAD_CHAT_ID", OWNER_ID))
COOKIES_FILE = os.environ.get("COOKIES_FILE")

QUALITIES = [360, 480, 720, 1080]
DOWNLOAD_DIR = Path("downloads")
TG_LIMIT = 2 * 1024 * 1024 * 1024 - 50 * 1024 * 1024  # ~1.95 GB safe limit

Path("data").mkdir(exist_ok=True)
app = Client("ytlivebot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir="data")
owner_only = filters.user(OWNER_IDS)

active_videos: set[str] = set()
watch_task: asyncio.Task | None = None


def base_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    return opts


def record_quality(url: str, height: int, out_dir: Path) -> Path | None:
    """Ek quality me live record karta hai (blocking; thread me chalao)."""
    opts = base_opts() | {
        "format": f"bv*[height<={height}]+ba/b[height<={height}]",
        "live_from_start": True,
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"%(title).80s_{height}p.%(ext)s"),
        "retries": 20,
        "fragment_retries": 50,
        "concurrent_fragment_downloads": 4,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        return path if path.exists() else None


def split_file(path: Path) -> list[Path]:
    """2GB se badi file ho to ffmpeg se parts me todta hai."""
    if path.stat().st_size <= TG_LIMIT:
        return [path]
    import subprocess

    pattern = str(path.with_name(path.stem + "_part%03d.mp4"))
    # ~ approx 30 min parts; zarurat ho to segment_time kam/zyada karo
    subprocess.run(
        ["ffmpeg", "-i", str(path), "-c", "copy", "-map", "0",
         "-f", "segment", "-segment_time", "1800", "-reset_timestamps", "1", pattern],
        check=True,
    )
    path.unlink()
    return sorted(path.parent.glob(path.stem + "_part*.mp4"))


async def upload(path: Path, caption: str):
    for part in split_file(path):
        await app.send_video(
            UPLOAD_CHAT_ID,
            str(part),
            caption=f"{caption}\n{part.name}",
            supports_streaming=True,
        )


async def process_live(url: str, status_chat: int):
    info = await asyncio.to_thread(
        lambda: yt_dlp.YoutubeDL(base_opts()).extract_info(url, download=False)
    )
    vid = info["id"]
    if vid in active_videos:
        return
    active_videos.add(vid)
    title = info.get("title", vid)
    out_dir = DOWNLOAD_DIR / vid
    out_dir.mkdir(parents=True, exist_ok=True)

    await app.send_message(status_chat, f"Recording shuru: {title}")
    try:
        # saari qualities parallel me record hoti hain
        results = await asyncio.gather(
            *[asyncio.to_thread(record_quality, url, q, out_dir) for q in QUALITIES],
            return_exceptions=True,
        )
        for q, res in zip(QUALITIES, results):
            if isinstance(res, Exception) or res is None:
                await app.send_message(status_chat, f"{q}p fail: {res}")
                continue
            await upload(res, f"{title} [{q}p]")
        await app.send_message(status_chat, "Sab upload ho gaya.")
    finally:
        active_videos.discard(vid)
        shutil.rmtree(out_dir, ignore_errors=True)


async def watch_loop():
    live_url = f"https://www.youtube.com/channel/{CHANNEL_ID}/live"
    while True:
        try:
            info = await asyncio.to_thread(
                lambda: yt_dlp.YoutubeDL(base_opts()).extract_info(live_url, download=False)
            )
            if info.get("is_live") and info["id"] not in active_videos:
                asyncio.create_task(process_live(info["webpage_url"], OWNER_ID))
        except Exception:
            pass  # abhi live nahi hai
        await asyncio.sleep(60)


@app.on_message(filters.command("live") & owner_only)
async def cmd_live(_, m: Message):
    if len(m.command) < 2:
        return await m.reply("Usage: /live <youtube_url>")
    asyncio.create_task(process_live(m.command[1], m.chat.id))
    await m.reply("Ok, queue me daal diya.")


@app.on_message(filters.command("watch") & owner_only)
async def cmd_watch(_, m: Message):
    global watch_task
    if not CHANNEL_ID:
        return await m.reply("CHANNEL_ID env variable set karo.")
    if watch_task and not watch_task.done():
        return await m.reply("Already watching.")
    watch_task = asyncio.create_task(watch_loop())
    await m.reply("Channel watch shuru. Live aate hi record hoga.")


@app.on_message(filters.command("stop") & owner_only)
async def cmd_stop(_, m: Message):
    if watch_task:
        watch_task.cancel()
    await m.reply("Watch band.")


class HealthHandler(BaseHTTPRequestHandler):
    """Render port check + UptimeRobot ping ke liye."""

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self._ok()

    def log_message(self, *args):
        pass  # logs shaant rakho


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


if __name__ == "__main__":
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    start_health_server()
    app.run()
