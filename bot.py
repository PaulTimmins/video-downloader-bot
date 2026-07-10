import asyncio
import logging
import os
import re
import tempfile
import uuid

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

# Telegram's Bot API caps uploads at 50MB unless you run your own local
# Bot API server (see README). We ask yt-dlp to prefer formats under that.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

URL_RE = re.compile(r"https?://\S+")
SUPPORTED_DOMAINS_RE = re.compile(
    r"(instagram\.com|facebook\.com|fb\.watch)", re.IGNORECASE
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("reel-bot")


def find_supported_urls(text: str) -> list[str]:
    return [u for u in URL_RE.findall(text) if SUPPORTED_DOMAINS_RE.search(u)]


def download_video(url: str, dest_dir: str) -> str:
    outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        "format": f"best[filesize<{MAX_UPLOAD_BYTES}]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
    }
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


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
                path = await asyncio.to_thread(download_video, url, tmp_dir)
            except Exception as exc:  # noqa: BLE001 - surface any download failure to the user
                log.warning("download failed for %s: %s", url, exc)
                await status.edit_text(
                    f"Couldn't download that link:\n{url}\n\nReason: {exc}"
                )
                continue

            size = os.path.getsize(path)
            if size > MAX_UPLOAD_BYTES:
                await status.edit_text(
                    f"Downloaded but it's {size / 1024 / 1024:.1f}MB, over Telegram's "
                    f"50MB bot upload limit, so I can't attach it:\n{url}"
                )
                continue

            try:
                with open(path, "rb") as video_file:
                    await message.reply_video(
                        video=video_file,
                        caption=url,
                        filename=f"{uuid.uuid4().hex}.mp4",
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("upload failed for %s: %s", url, exc)
                await status.edit_text(f"Downloaded but failed to send:\n{url}\n\n{exc}")
                continue

            await status.delete()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Send me an Instagram or Facebook reel link and I'll reply with the video."
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
