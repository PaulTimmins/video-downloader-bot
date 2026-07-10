from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import tempfile
import uuid

import yaml
import yt_dlp
from telegram import InputMediaPhoto, InputMediaVideo, Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from yt_dlp.networking import Request
from yt_dlp.utils import decode_base_n, sanitize_filename

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
    r"(instagram\.com|facebook\.com|fb\.watch|youtube\.com|youtu\.be)",
    re.IGNORECASE,
)

# Same encoding yt-dlp's Instagram extractor uses to turn a post's shortcode
# into its internal numeric media id ("pk").
_IG_SHORTCODE_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_IG_SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel|tv)/([^/?#]+)", re.IGNORECASE)
# Same public app id yt-dlp's Instagram extractor sends with its own API calls.
_IG_API_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "198387",
    "X-IG-WWW-Claim": "0",
    "Origin": "https://www.instagram.com",
    "Accept": "*/*",
}

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


def _instagram_shortcode_to_pk(shortcode: str) -> int:
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    return decode_base_n(shortcode, table=_IG_SHORTCODE_CHARS)


def _fetch_instagram_raw_media(ydl: "yt_dlp.YoutubeDL", url: str) -> list[dict]:
    """yt-dlp's Instagram extractor discards each photo's real
    image_versions2.candidates before returning its result - it only keeps
    resolution info for videos, so photo-only items come back as low-res
    square crops (or nothing at all). Re-fetch the same media-info endpoint
    the extractor itself uses, reusing its cookies/session, to get the real
    per-item candidate list."""
    match = _IG_SHORTCODE_RE.search(url)
    if not match:
        return []
    pk = _instagram_shortcode_to_pk(match.group(1))
    resp = ydl.urlopen(Request(
        f"https://i.instagram.com/api/v1/media/{pk}/info/", headers=_IG_API_HEADERS,
    ))
    data = json.loads(resp.read())
    item = (data.get("items") or [None])[0]
    if not item:
        return []
    return item.get("carousel_media") or [item]


def _best_candidate_url(candidates: list[dict]) -> str | None:
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    return best.get("url")


def _download_image(
    ydl: "yt_dlp.YoutubeDL", item: dict, dest_dir: str, raw_media: dict | None = None
) -> str | None:
    """Fetch the full-resolution photo for an item that has no video
    formats. Prefers the raw Instagram media-info candidates (real
    resolution); falls back to whatever yt-dlp's own (often low-res or
    absent) 'thumbnails' list has if that lookup wasn't available."""
    candidates = ((raw_media or {}).get("image_versions2") or {}).get("candidates") or []
    img_url = _best_candidate_url(candidates) or _best_candidate_url(item.get("thumbnails") or [])
    if not img_url:
        return None

    resp = ydl.urlopen(img_url)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    raw_id = (raw_media or {}).get("code") or (raw_media or {}).get("id") or item.get("id") or uuid.uuid4().hex
    name = sanitize_filename(str(raw_id), restricted=True)
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

        raw_media_list = []
        if "instagram.com" in url.lower():
            try:
                raw_media_list = _fetch_instagram_raw_media(ydl, url)
            except Exception as exc:  # noqa: BLE001 - fall back to yt-dlp's (weaker) data
                log.warning("couldn't fetch raw Instagram media info for %s: %s", url, exc)

        paths = []
        for i, item in enumerate(items):
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
                    raw_media = raw_media_list[i] if i < len(raw_media_list) else None
                    path = _download_image(ydl, item, dest_dir, raw_media)
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

            # Classify each downloaded file: photos/videos within Telegram's
            # per-type size limits can go in one album (sendMediaGroup);
            # anything else (oversized, or an unrecognized type) is sent as
            # its own document reply instead, since albums can't mix in
            # documents alongside photos/videos.
            groupable = []  # (kind, path) for "photo" or "video"
            singles = []  # paths sent individually as documents
            skipped = []  # (label, size_or_None) that couldn't be sent

            for path in paths:
                ext = os.path.splitext(path)[1].lower()
                size = os.path.getsize(path)
                label = f"{url} ({os.path.basename(path)})" if len(paths) > 1 else url

                if ext in IMAGE_EXTS and size <= MAX_PHOTO_BYTES:
                    groupable.append(("photo", path))
                elif ext in VIDEO_EXTS and size <= MAX_UPLOAD_BYTES:
                    groupable.append(("video", path))
                elif size <= MAX_UPLOAD_BYTES:
                    singles.append((label, path))
                else:
                    skipped.append((label, size))

            sent_any = False

            if len(groupable) == 1:
                kind, path = groupable[0]
                try:
                    with open(path, "rb") as f:
                        if kind == "photo":
                            await message.reply_photo(photo=f, caption=url)
                        else:
                            await message.reply_video(video=f, caption=url)
                    sent_any = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("upload failed for %s: %s", path, exc)
                    skipped.append((url, None))
            elif groupable:
                for start in range(0, len(groupable), MEDIA_GROUP_MAX):
                    chunk = groupable[start : start + MEDIA_GROUP_MAX]
                    files = [open(path, "rb") for _, path in chunk]
                    try:
                        media = [
                            (InputMediaPhoto if kind == "photo" else InputMediaVideo)(
                                media=f, caption=url if start == 0 and i == 0 else None
                            )
                            for i, ((kind, _), f) in enumerate(zip(chunk, files))
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
