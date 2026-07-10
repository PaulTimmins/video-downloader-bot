from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import tempfile
import uuid

import yaml
import yt_dlp
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from yt_dlp.utils import sanitize_filename

CONFIG_PATH = os.environ.get(
    "CONFIG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
)

with open(CONFIG_PATH, "r") as f:
    _config = yaml.safe_load(f) or {}

TOKEN = _config["bot_token"]
COOKIES_FILE = _config.get("cookies_file") or None
TEMP_DIR = _config.get("temp_dir") or None
if TEMP_DIR:
    os.makedirs(TEMP_DIR, exist_ok=True)

# Telegram's Bot API caps file uploads from bots at 50MB, and photos sent
# via sendPhoto at 10MB (oversized images still go through as documents).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

URL_RE = re.compile(r"https?://\S+")
SUPPORTED_DOMAINS_RE = re.compile(
    r"(instagram\.com|facebook\.com|fb\.watch|youtube\.com|youtu\.be)",
    re.IGNORECASE,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("reel-bot")


def find_supported_urls(text: str) -> list[str]:
    return [u for u in URL_RE.findall(text) if SUPPORTED_DOMAINS_RE.search(u)]


def _paths_from_processed(ydl: "yt_dlp.YoutubeDL", info: dict) -> list[str]:
    downloads = info.get("requested_downloads")
    if downloads:
        return [d["filepath"] for d in downloads if d.get("filepath")]
    return [ydl.prepare_filename(info)]


def _download_image(ydl: "yt_dlp.YoutubeDL", item: dict, dest_dir: str) -> str | None:
    """Instagram's extractor only ever populates 'formats' for videos, so a
    plain photo post (or a photo inside a carousel/"set") never has anything
    yt-dlp's normal download pipeline will fetch. Grab the highest-res
    thumbnail URL ourselves instead, reusing yt-dlp's own request handling
    (cookies, headers, proxy) via ydl.urlopen."""
    thumbnails = item.get("thumbnails") or []
    if not thumbnails:
        return None
    best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    img_url = best.get("url")
    if not img_url:
        return None

    resp = ydl.urlopen(img_url)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    name = sanitize_filename(str(item.get("id") or uuid.uuid4().hex), restricted=True)
    path = os.path.join(dest_dir, f"{name}{ext}")
    with open(path, "wb") as f:
        f.write(resp.read())
    return path


def download_media(url: str, dest_dir: str) -> list[str]:
    outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": (
            f"best[filesize<{MAX_UPLOAD_BYTES}]/best[height<=720]/best[height<=480]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        # Anonymous requests to YouTube's default "web" client currently
        # require a PO token yt-dlp can't generate; "android" doesn't.
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # process=False skips yt-dlp's format-selection/download pipeline
        # entirely, so a photo-only item (no video formats) doesn't hard-fail
        # extraction before we even get to see the rest of the post/carousel.
        raw = ydl.extract_info(url, download=False, process=False)
        items = list(raw["entries"]) if raw.get("entries") is not None else [raw]

        paths = []
        for item in items:
            if not item:
                continue
            try:
                if item.get("formats") or item.get("url"):
                    # Carry over fields normally set on the outer result so
                    # process_ie_result has what it needs for a bare entry.
                    for key in ("extractor", "extractor_key", "webpage_url"):
                        item.setdefault(key, raw.get(key))
                    processed = ydl.process_ie_result(item, download=True)
                    paths.extend(_paths_from_processed(ydl, processed))
                else:
                    path = _download_image(ydl, item, dest_dir)
                    if path:
                        paths.append(path)
            except Exception as exc:  # noqa: BLE001 - one bad carousel item shouldn't sink the rest
                log.warning("skipping one item of %s: %s", url, exc)
        return paths


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message: Message = update.effective_message
    if not message or not message.text:
        return

    urls = find_supported_urls(message.text)
    if not urls:
        return

    for url in urls:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO
        )
        status = await message.reply_text(f"Downloading…\n{url}")

        with tempfile.TemporaryDirectory(prefix="reel-", dir=TEMP_DIR) as tmp_dir:
            try:
                paths = await asyncio.to_thread(download_media, url, tmp_dir)
            except Exception as exc:  # noqa: BLE001 - surface any download failure to the user
                log.warning("download failed for %s: %s", url, exc)
                await status.edit_text(
                    f"Couldn't download that link:\n{url}\n\nReason: {exc}"
                )
                continue

            if not paths:
                await status.edit_text(f"Nothing downloadable found at:\n{url}")
                continue

            sent_any = False
            skipped = []
            for i, path in enumerate(paths, start=1):
                caption = url if len(paths) == 1 else f"{url} ({i}/{len(paths)})"
                ext = os.path.splitext(path)[1].lower()
                size = os.path.getsize(path)

                try:
                    with open(path, "rb") as f:
                        if ext in IMAGE_EXTS and size <= MAX_PHOTO_BYTES:
                            await message.reply_photo(photo=f, caption=caption)
                        elif ext in VIDEO_EXTS and size <= MAX_UPLOAD_BYTES:
                            await message.reply_video(video=f, caption=caption)
                        elif size <= MAX_UPLOAD_BYTES:
                            await message.reply_document(document=f, caption=caption)
                        else:
                            skipped.append((caption, size))
                            continue
                    sent_any = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("upload failed for %s: %s", path, exc)
                    skipped.append((caption, None))

            if skipped:
                lines = [
                    f"- {c}" + (f" ({s / 1024 / 1024:.1f}MB, over the limit)" if s else " (failed to send)")
                    for c, s in skipped
                ]
                note = "Couldn't send:\n" + "\n".join(lines)
                if sent_any:
                    await status.delete()
                    await message.reply_text(note)
                else:
                    await status.edit_text(note)
            else:
                await status.delete()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send me a link and I'll reply with the media:\n"
        "- Instagram reels, photo posts, and carousels (\"sets\")\n"
        "- Facebook reels/videos\n"
        "- YouTube videos and Shorts"
    )


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex("^/start"), start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info(
        "bot starting (cookies=%s, temp_dir=%s)",
        "yes" if COOKIES_FILE else "no",
        TEMP_DIR or tempfile.gettempdir(),
    )
    app.run_polling()


if __name__ == "__main__":
    main()
