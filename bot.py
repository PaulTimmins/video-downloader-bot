import asyncio
import logging
import os
import re
import tempfile

import yaml
import yt_dlp
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters

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


def _paths_from_info(ydl: "yt_dlp.YoutubeDL", info: dict) -> list[str]:
    """Resolve the downloaded file path(s) for a single-item result or a
    carousel/playlist result (e.g. an Instagram "set" with multiple photos
    and/or videos in one post)."""
    items = list(info["entries"]) if info.get("entries") is not None else [info]

    paths = []
    for item in items:
        if not item:
            continue
        downloads = item.get("requested_downloads")
        if downloads:
            paths.extend(d["filepath"] for d in downloads if d.get("filepath"))
        else:
            paths.append(ydl.prepare_filename(item))
    return paths


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
        info = ydl.extract_info(url, download=True)
        return _paths_from_info(ydl, info)


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
