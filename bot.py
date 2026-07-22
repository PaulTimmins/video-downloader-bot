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
import yt_dlp.extractor.bluesky as _bsky_extractor
import yt_dlp.extractor.instagram as _ig_extractor
import yt_dlp.extractor.twitter as _tw_extractor
from telegram import InputMediaPhoto, InputMediaVideo, Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from yt_dlp.utils import sanitize_filename, update_url_query

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
    r"\b(?:instagram\.com|facebook\.com|fb\.watch|youtube\.com|youtu\.be|tiktok\.com"
    r"|twitter\.com|x\.com|bsky\.app)\b",
    re.IGNORECASE,
)

# Guards the monkeypatches below so concurrent downloads (across different
# chats) can't race on the same patched method.
_capture_lock = threading.Lock()

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


def _fetch_and_save_image(
    ydl: "yt_dlp.YoutubeDL",
    img_url: str | None,
    dest_dir: str,
    name_hint: str,
    width: int | None = None,
    height: int | None = None,
) -> dict | None:
    if not img_url:
        return None
    resp = ydl.urlopen(img_url)
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    ext = mimetypes.guess_extension(content_type) or ".jpg"
    if ext == ".jpe":
        ext = ".jpg"
    name = sanitize_filename(str(name_hint), restricted=True)
    path = os.path.join(dest_dir, f"{name}{ext}")
    with open(path, "wb") as f:
        f.write(resp.read())
    return {"path": path, "width": width, "height": height, "duration": None}


def _download_image(
    ydl: "yt_dlp.YoutubeDL", item: dict, dest_dir: str, raw_media: dict | None = None
) -> dict | None:
    """Fetch the full-resolution photo for an Instagram item with no video
    formats, using the raw product_media captured via monkeypatch (real
    image_versions2 resolution data yt-dlp itself discards for photos).
    Deliberately does NOT fall back to yt-dlp's generic 'thumbnails' field -
    on other platforms (confirmed on Twitter) that can hold low-res
    card/link-preview art on an unrelated entry, not real post content."""
    candidates = ((raw_media or {}).get("image_versions2") or {}).get("candidates") or []
    best = _best_candidate(candidates)
    if not best:
        return None
    raw_id = (
        (raw_media or {}).get("code")
        or (raw_media or {}).get("pk")
        or item.get("id")
        or uuid.uuid4().hex
    )
    return _fetch_and_save_image(ydl, best.get("url"), dest_dir, raw_id, best.get("width"), best.get("height"))


def _twitter_photo_candidates(status: dict) -> list[dict]:
    """Real per-image entries from a tweet (and its quoted tweet, if any).
    yt-dlp's Twitter extractor filters photo-type media out of
    extended_entities entirely - it only ever builds entries for
    video/gif - so these never reach us any other way."""
    out = []
    for root in (status, status.get("quoted_status") or {}):
        for media in (root.get("extended_entities") or {}).get("media") or []:
            if media.get("type") != "photo":
                continue
            media_url = media.get("media_url_https") or media.get("media_url")
            if not media_url:
                continue
            orig = media.get("original_info") or {}
            out.append({
                "url": update_url_query(media_url, {"name": "orig"}),
                "width": orig.get("width"),
                "height": orig.get("height"),
                "id": media.get("id_str") or media.get("id"),
            })
    return out


def _bluesky_photo_candidates(post: dict) -> list[dict]:
    """Real per-image entries from a Bluesky post (and recordWithMedia
    quote posts). yt-dlp's Bluesky extractor only ever builds entries for
    app.bsky.embed.video/external - there's no image-embed handling at
    all, so these never reach us any other way."""
    out = []
    for embed in (post.get("embed"), (post.get("embed") or {}).get("media")):
        if not embed or embed.get("$type") != "app.bsky.embed.images#view":
            continue
        for img in embed.get("images") or []:
            if not img.get("fullsize"):
                continue
            ar = img.get("aspectRatio") or {}
            out.append({"url": img["fullsize"], "width": ar.get("width"), "height": ar.get("height")})
    return out


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

    def run(captured_media: list[dict], on_no_video=None) -> list[dict]:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # process=False skips yt-dlp's format-selection/download pipeline
            # entirely, so a photo-only item (no video formats) doesn't
            # hard-fail extraction before we see the rest of the post.
            try:
                raw = ydl.extract_info(url, download=False, process=False)
            except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as exc:
                # A photo-only post: several extractors (Instagram single
                # posts, Twitter/X photo tweets, Bluesky image posts) raise
                # outright instead of returning the image data they already
                # fetched. The relevant monkeypatch below already captured
                # it; use that instead of giving up.
                if on_no_video:
                    result = on_no_video(ydl, exc)
                    if result is not None:
                        return result
                raise
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

    if re.search(r"\binstagram\.com\b", url, re.IGNORECASE):
        # yt-dlp's Instagram extractor calls _extract_product_media for
        # every item - carousel entries and single-photo posts alike - but
        # for a photo with no video formats it raises right afterward
        # without ever handing us the result, discarding the (perfectly
        # good) image data it just built. Intercept the raw product_media
        # dict right where the extractor builds it (same request, same
        # cookies/session already proven to work) before it gets lost,
        # instead of guessing at separate endpoints.
        captured_media = []
        original = _ig_extractor.InstagramBaseIE._extract_product_media

        def _capturing_extract_product_media(self, product_media):
            captured_media.append(product_media)
            return original(self, product_media)

        def on_no_video(ydl, exc):
            if "no video in this post" not in str(exc).lower() or not captured_media:
                return None
            entry = _download_image(ydl, {}, dest_dir, captured_media[-1])
            # None here means we genuinely found no usable image data (not
            # just "downloaded zero images") - let the original, more
            # informative yt-dlp error propagate instead of masking it with
            # a generic "nothing downloadable" message.
            return [entry] if entry else None

        with _capture_lock:
            _ig_extractor.InstagramBaseIE._extract_product_media = _capturing_extract_product_media
            try:
                return run(captured_media, on_no_video)
            finally:
                _ig_extractor.InstagramBaseIE._extract_product_media = original

    if re.search(r"\b(?:twitter\.com|x\.com)\b", url, re.IGNORECASE):
        # yt-dlp's Twitter extractor filters photo-type media out of a
        # tweet's extended_entities entirely (it only builds entries for
        # video/gif), so a photo-only tweet raises "No video could be
        # found" without ever exposing the image URLs it already has.
        # Intercept the raw tweet data at _extract_status, the exact point
        # it's fetched, and pull the real photo entries out ourselves.
        captured_status = []
        original = _tw_extractor.TwitterIE._extract_status

        def _capturing_extract_status(self, twid):
            status = original(self, twid)
            captured_status.append(status)
            return status

        def on_no_video(ydl, exc):
            if "no video could be found" not in str(exc).lower() or not captured_status:
                return None
            candidates = _twitter_photo_candidates(captured_status[-1])
            if not candidates:
                # No real photo data either - not a photo-only tweet, just a
                # genuine failure (suspended, deleted, etc). Let the
                # original error propagate instead of masking it.
                return None
            downloaded = []
            for i, c in enumerate(candidates):
                entry = _fetch_and_save_image(
                    ydl, c["url"], dest_dir, c.get("id") or f"photo{i}", c.get("width"), c.get("height"),
                )
                if entry:
                    downloaded.append(entry)
            return downloaded

        with _capture_lock:
            _tw_extractor.TwitterIE._extract_status = _capturing_extract_status
            try:
                return run([], on_no_video)
            finally:
                _tw_extractor.TwitterIE._extract_status = original

    if re.search(r"\bbsky\.app\b", url, re.IGNORECASE):
        # Same situation again: yt-dlp's Bluesky extractor only ever builds
        # entries for video/external-link embeds - there's no image-embed
        # handling at all, so a photo-only post raises "No video could be
        # found" despite the (public, unauthenticated) API response it just
        # got already containing the real image URLs.
        captured_post = []
        original = _bsky_extractor.BlueskyIE._extract_post

        def _capturing_extract_post(self, handle, post_id):
            post = original(self, handle, post_id)
            captured_post.append(post)
            return post

        def on_no_video(ydl, exc):
            if "no video could be found" not in str(exc).lower() or not captured_post:
                return None
            candidates = _bluesky_photo_candidates(captured_post[-1])
            if not candidates:
                # No real photo data either - not an image-only post, just a
                # genuine failure. Let the original error propagate instead
                # of masking it.
                return None
            downloaded = []
            for i, c in enumerate(candidates):
                entry = _fetch_and_save_image(ydl, c["url"], dest_dir, f"photo{i}", c.get("width"), c.get("height"))
                if entry:
                    downloaded.append(entry)
            return downloaded

        with _capture_lock:
            _bsky_extractor.BlueskyIE._extract_post = _capturing_extract_post
            try:
                return run([], on_no_video)
            finally:
                _bsky_extractor.BlueskyIE._extract_post = original

    return run([])


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
                log.info("nothing downloadable found at %s", url)
                await status.delete()
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
        "- TikTok videos\n"
        "- Twitter/X photos and videos\n"
        "- Bluesky photos and videos"
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
