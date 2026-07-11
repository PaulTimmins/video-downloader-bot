from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re
import tempfile
import threading
import uuid

import yaml
import yt_dlp
import yt_dlp.extractor.instagram as _ig_extractor
from telegram import InputMediaPhoto, InputMediaVideo, Message, Update
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

# Telegram albums (sendMediaGroup) allow at most 10 items per call.
MEDIA_GROUP_MAX = 10

URL_RE = re.compile(r"https?://\S+")
SUPPORTED_DOMAINS_RE = re.compile(
    r"(instagram\.com|facebook\.com|fb\.watch|youtube\.com|youtu\.be|tiktok\.com)",
    re.IGNORECASE,
)

# Guards the monkeypatch below so concurrent Instagram downloads (across
# different chats) can't race on the same patched method.
_ig_capture_lock = threading.Lock()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx logs the full request URL at INFO level, which for Telegram's Bot API
# means "https://api.telegram.org/bot<TOKEN>/...' - i.e. the bot token itself.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reel-bot")


def find_supported_urls(text: str) -> list[str]:
    return [u for u in URL_RE.findall(text) if SUPPORTED_DOMAINS_RE.search(u)]


def _paths_from_processed(ydl: "yt_dlp.YoutubeDL", info: dict) -> list[dict]:
    """Telegram renders a video with a wrong (often square) preview box
    unless width/height are passed explicitly with the upload, since it
    can't always probe them itself - so carry yt-dlp's own metadata through
    alongside each file path instead of just returning bare paths."""
    downloads = info.get("requested_downloads")
    if downloads:
        return [
            {
                "path": d["filepath"],
                "width": d.get("width") or info.get("width"),
                "height": d.get("height") or info.get("height"),
                "duration": info.get("duration"),
            }
            for d in downloads
            if d.get("filepath")
        ]
    return [{
        "path": ydl.prepare_filename(info),
        "width": info.get("width"),
        "height": info.get("height"),
        "duration": info.get("duration"),
    }]


def _best_candidate(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))


def _download_image(
    ydl: "yt_dlp.YoutubeDL", item: dict, dest_dir: str, raw_media: dict | None = None
) -> dict | None:
    """Fetch the full-resolution photo for an item that has no video
    formats. Prefers the raw Instagram media-info candidates (real
    resolution); falls back to whatever yt-dlp's own (often low-res or
    absent) 'thumbnails' list has if that lookup wasn't available."""
    candidates = ((raw_media or {}).get("image_versions2") or {}).get("candidates") or []
    best = _best_candidate(candidates) or _best_candidate(item.get("thumbnails") or [])
    img_url = (best or {}).get("url")
    if not img_url:
        return None

    resp = ydl.urlopen(img_url)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    raw_id = (
        (raw_media or {}).get("code")
        or (raw_media or {}).get("pk")
        or item.get("id")
        or uuid.uuid4().hex
    )
    name = sanitize_filename(str(raw_id), restricted=True)
    path = os.path.join(dest_dir, f"{name}{ext}")
    with open(path, "wb") as f:
        f.write(resp.read())
    return {"path": path, "width": best.get("width"), "height": best.get("height"), "duration": None}


def download_media(url: str, dest_dir: str) -> list[dict]:
    outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": outtmpl,
        # Prefer h264: some platforms (confirmed on TikTok) serve their
        # h265/bytevc1 rendition of a video with no audio track at all even
        # though yt-dlp's metadata claims one, while the h264 rendition of
        # the same video has real audio. H264 also has universally solid
        # playback support, unlike HEVC which some Telegram clients render
        # incorrectly. Only steer toward a smaller format when a platform
        # actually reports filesize (mainly YouTube); a height cap here
        # would silently kick in on every Instagram/Facebook/TikTok link
        # (they rarely report filesize) and risk the same wrong-rendition
        # problem this is fixing.
        "format": (
            f"best[vcodec=h264][filesize<={MAX_UPLOAD_BYTES}]/best[vcodec=h264]"
            f"/best[filesize<={MAX_UPLOAD_BYTES}]/best"
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

    def run(captured_media: list[dict]) -> list[dict]:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # process=False skips yt-dlp's format-selection/download pipeline
            # entirely, so a photo-only item (no video formats) doesn't
            # hard-fail extraction before we see the rest of the post.
            raw = ydl.extract_info(url, download=False, process=False)
            items = list(raw["entries"]) if raw.get("entries") is not None else [raw]

            downloaded = []
            for i, item in enumerate(items):
                if not item:
                    continue
                try:
                    if item.get("formats") or item.get("url"):
                        # Carry over fields normally set on the outer result
                        # so process_ie_result has what it needs for a bare
                        # entry.
                        for key in ("extractor", "extractor_key", "webpage_url"):
                            item.setdefault(key, raw.get(key))
                        processed = ydl.process_ie_result(item, download=True)
                        downloaded.extend(_paths_from_processed(ydl, processed))
                    else:
                        raw_media = captured_media[i] if i < len(captured_media) else None
                        entry = _download_image(ydl, item, dest_dir, raw_media)
                        if entry:
                            downloaded.append(entry)
                except Exception as exc:  # noqa: BLE001 - one bad carousel item shouldn't sink the rest
                    log.warning("skipping one item of %s: %s", url, exc)
            return downloaded

    if "instagram.com" not in url.lower():
        return run([])

    # yt-dlp's Instagram extractor throws away each photo's real
    # image_versions2.candidates before returning its result to us - it only
    # preserves resolution data for videos, which is why photo items came
    # through as tiny square crops (or nothing). Intercept the raw API data
    # right where the extractor itself fetches it (same request, same
    # cookies/session that's already proven to work) before it gets
    # discarded, instead of guessing at a separate endpoint ourselves.
    captured_media = []
    original_extract_product_media = _ig_extractor.InstagramBaseIE._extract_product_media

    def _capturing_extract_product_media(self, product_media):
        captured_media.append(product_media)
        return original_extract_product_media(self, product_media)

    with _ig_capture_lock:
        _ig_extractor.InstagramBaseIE._extract_product_media = _capturing_extract_product_media
        try:
            return run(captured_media)
        finally:
            _ig_extractor.InstagramBaseIE._extract_product_media = original_extract_product_media


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
                downloaded = await asyncio.to_thread(download_media, url, tmp_dir)
            except Exception as exc:  # noqa: BLE001 - surface any download failure to the user
                log.warning("download failed for %s: %s", url, exc)
                await status.edit_text(
                    f"Couldn't download that link:\n{url}\n\nReason: {exc}"
                )
                continue

            if not downloaded:
                await status.edit_text(f"Nothing downloadable found at:\n{url}")
                continue

            # Classify each downloaded file: photos/videos within Telegram's
            # per-type size limits can go in one album (sendMediaGroup);
            # anything else (oversized, or an unrecognized type) is sent as
            # its own document reply instead, since albums can't mix in
            # documents alongside photos/videos.
            groupable = []  # (kind, entry) for "photo" or "video"
            singles = []  # (label, path) sent individually as documents
            skipped = []  # (label, size_or_None) that couldn't be sent

            for entry in downloaded:
                path = entry["path"]
                ext = os.path.splitext(path)[1].lower()
                size = os.path.getsize(path)
                label = f"{url} ({os.path.basename(path)})" if len(downloaded) > 1 else url

                if ext in IMAGE_EXTS and size <= MAX_PHOTO_BYTES:
                    groupable.append(("photo", entry))
                elif ext in VIDEO_EXTS and size <= MAX_UPLOAD_BYTES:
                    groupable.append(("video", entry))
                elif size <= MAX_UPLOAD_BYTES:
                    singles.append((label, path))
                else:
                    skipped.append((label, size))

            sent_any = False

            if len(groupable) == 1:
                kind, entry = groupable[0]
                path = entry["path"]
                try:
                    with open(path, "rb") as f:
                        if kind == "photo":
                            await message.reply_photo(photo=f, caption=url)
                        else:
                            await message.reply_video(
                                video=f,
                                caption=url,
                                width=entry.get("width"),
                                height=entry.get("height"),
                                duration=entry.get("duration"),
                                supports_streaming=True,
                            )
                    sent_any = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("upload failed for %s: %s", path, exc)
                    skipped.append((url, None))
            elif groupable:
                for start in range(0, len(groupable), MEDIA_GROUP_MAX):
                    chunk = groupable[start : start + MEDIA_GROUP_MAX]
                    files = [open(entry["path"], "rb") for _, entry in chunk]
                    try:
                        media = [
                            (InputMediaPhoto(media=f, caption=url if start == 0 and i == 0 else None)
                             if kind == "photo" else
                             InputMediaVideo(
                                 media=f,
                                 caption=url if start == 0 and i == 0 else None,
                                 width=entry.get("width"),
                                 height=entry.get("height"),
                                 duration=entry.get("duration"),
                                 supports_streaming=True,
                             ))
                            for i, ((kind, entry), f) in enumerate(zip(chunk, files))
                        ]
                        await message.reply_media_group(media=media)
                        sent_any = True
                    except Exception as exc:  # noqa: BLE001
                        log.warning("album send failed for %s: %s", url, exc)
                        skipped.append((f"{url} (album)", None))
                    finally:
                        for f in files:
                            f.close()

            for label, path in singles:
                try:
                    with open(path, "rb") as f:
                        await message.reply_document(document=f, caption=label)
                    sent_any = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("upload failed for %s: %s", path, exc)
                    skipped.append((label, None))

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
        "- YouTube videos and Shorts\n"
        "- TikTok videos"
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
