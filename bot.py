from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
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
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
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

# Persists per-chat preferences (currently just the reply-to-sender toggle)
# across restarts. JSON keyed by chat id.
SETTINGS_FILE = _config.get("settings_file") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chat_settings.json"
)

# Telegram's Bot API caps file uploads from bots at 50MB, and photos sent
# via sendPhoto at 10MB (oversized images still go through as documents).
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}

# Telegram albums (sendMediaGroup) allow at most 10 items per call.
MEDIA_GROUP_MAX = 10

# Telegram limits: media captions max 1024 chars, text messages max 4096.
# Kept slightly under to leave headroom (the API counts UTF-16 units, so
# emoji etc. can push a 1024-"char" string over).
CAPTION_LIMIT = 1000
TEXT_MESSAGE_LIMIT = 4000

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


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f) or {}
    except (FileNotFoundError, ValueError):
        return {}


# In-memory settings, mutated only from the (single-threaded) asyncio event
# loop, so no lock is needed. Written through to disk on every change.
_SETTINGS = _load_settings()


def _save_settings() -> None:
    tmp = f"{SETTINGS_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(_SETTINGS, f)
    os.replace(tmp, SETTINGS_FILE)  # atomic, so a crash mid-write can't corrupt it


def chat_replies_enabled(chat_id: int) -> bool:
    """Whether the bot should reply to (and thus notify) the original poster.
    Defaults to True for chats that haven't set a preference."""
    return _SETTINGS.get(str(chat_id), {}).get("reply", True)


def set_chat_replies(chat_id: int, enabled: bool) -> None:
    _SETTINGS.setdefault(str(chat_id), {})["reply"] = enabled
    _save_settings()


def find_supported_urls(text: str) -> list[str]:
    return [u for u in URL_RE.findall(text) if SUPPORTED_DOMAINS_RE.search(u)]


def _probe_video_dimensions(path: str) -> tuple[int, int] | None:
    """Read the real *display* width/height straight from the downloaded
    file with ffprobe. yt-dlp's format metadata is often missing or wrong
    for some platforms (e.g. Facebook), which makes Telegram fall back to a
    near-square preview box; and phone videos are frequently stored
    landscape with a rotation flag, so the coded dimensions need swapping to
    get the displayed orientation. Returns None if ffprobe isn't available
    or can't read the file, so callers fall back to yt-dlp's metadata."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        streams = (json.loads(proc.stdout or "{}").get("streams")) or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not streams:
        return None
    stream = streams[0]
    width, height = stream.get("width"), stream.get("height")
    if not width or not height:
        return None

    rotation = 0
    tag = (stream.get("tags") or {}).get("rotate")  # older ffmpeg
    if tag is not None:
        try:
            rotation = int(tag)
        except (TypeError, ValueError):
            pass
    for side_data in stream.get("side_data_list") or []:  # newer ffmpeg (Display Matrix)
        if side_data.get("rotation") is not None:
            try:
                rotation = int(side_data["rotation"])
            except (TypeError, ValueError):
                pass
    if abs(rotation) % 180 == 90:
        width, height = height, width
    return width, height


def _entry_from_file(path: str, info: dict, fmt: dict | None = None) -> dict:
    """Build a media entry, preferring ffprobe's real display dimensions and
    falling back to yt-dlp's metadata when ffprobe isn't available."""
    fmt = fmt or {}
    probed = _probe_video_dimensions(path)
    if probed:
        width, height = probed
    else:
        width = fmt.get("width") or info.get("width")
        height = fmt.get("height") or info.get("height")
    return {"path": path, "width": width, "height": height, "duration": info.get("duration")}


def _paths_from_processed(ydl: "yt_dlp.YoutubeDL", info: dict) -> list[dict]:
    """Telegram renders a video with a wrong (often square) preview box
    unless width/height are passed explicitly with the upload, since it
    can't always probe them itself - so carry each file's real dimensions
    through alongside its path instead of just returning bare paths."""
    downloads = info.get("requested_downloads")
    if downloads:
        return [
            _entry_from_file(d["filepath"], info, d)
            for d in downloads
            if d.get("filepath")
        ]
    return [_entry_from_file(ydl.prepare_filename(info), info)]


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


def _dig(obj, *keys):
    """Safe nested dict lookup; returns None if any level is missing."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _twitter_post_text(status: dict | None) -> str | None:
    if not status:
        return None
    # note_tweet holds the full text of long-form (premium) posts; full_text
    # is the standard field. Both preserve the original newlines, unlike
    # yt-dlp's own 'description' which flattens them to spaces.
    return (
        _dig(status, "note_tweet", "note_tweet_results", "result", "text")
        or status.get("full_text")
        or status.get("text")
    )


def _bluesky_post_text(post: dict | None) -> str | None:
    return _dig(post, "record", "text")


def _instagram_post_text(product_info: dict | None) -> str | None:
    if not product_info:
        return None
    caption = product_info.get("caption")
    if isinstance(caption, dict):
        return caption.get("text")
    if isinstance(caption, str):
        return caption
    return None


def build_caption(text: str | None, url: str) -> tuple[str, str | None]:
    """Combine the post text and source URL into a media caption. If the
    combined length would exceed Telegram's caption limit, return just the
    URL as the caption plus the full text as 'overflow' to send as separate
    message(s), so long posts aren't truncated."""
    text = (text or "").strip()
    if not text:
        return url, None
    full = f"{text}\n\n{url}"
    if len(full) <= CAPTION_LIMIT:
        return full, None
    return url, text


def _chunk_text(text: str, size: int = TEXT_MESSAGE_LIMIT) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def download_media(url: str, dest_dir: str) -> dict:
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
        # instead of guessing at separate endpoints. Also capture the parent
        # product_info (via _extract_product) for the post's caption text.
        captured_media = []
        captured_product = []
        original_media = _ig_extractor.InstagramBaseIE._extract_product_media
        # Capturing the caption is a nice-to-have; guard it so a future
        # yt-dlp rename of _extract_product can't break image downloading.
        original_product = getattr(_ig_extractor.InstagramBaseIE, "_extract_product", None)

        def _capturing_extract_product_media(self, product_media):
            captured_media.append(product_media)
            return original_media(self, product_media)

        def _capturing_extract_product(self, product_info, *a, **k):
            captured_product.append(product_info)
            return original_product(self, product_info, *a, **k)

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
            if original_product:
                _ig_extractor.InstagramBaseIE._extract_product = _capturing_extract_product
            try:
                entries = run(captured_media, on_no_video)
            finally:
                _ig_extractor.InstagramBaseIE._extract_product_media = original_media
                if original_product:
                    _ig_extractor.InstagramBaseIE._extract_product = original_product
        text = _instagram_post_text(captured_product[-1]) if captured_product else None
        return {"entries": entries, "text": text}

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
                entries = run([], on_no_video)
            finally:
                _tw_extractor.TwitterIE._extract_status = original
        text = _twitter_post_text(captured_status[-1]) if captured_status else None
        return {"entries": entries, "text": text}

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
                entries = run([], on_no_video)
            finally:
                _bsky_extractor.BlueskyIE._extract_post = original
        text = _bluesky_post_text(captured_post[-1]) if captured_post else None
        return {"entries": entries, "text": text}

    return {"entries": run([]), "text": None}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message: Message = update.effective_message
    if not message or not message.text:
        return

    urls = find_supported_urls(message.text)
    if not urls:
        return

    # When replies are disabled for this chat, everything is sent as a plain
    # message (do_quote=False) instead of a reply to the poster's message, so
    # the poster isn't notified. Default (True) keeps the original behavior.
    quote = chat_replies_enabled(message.chat_id)

    for url in urls:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action=ChatAction.UPLOAD_VIDEO
        )
        status = await message.reply_text(f"Downloading…\n{url}", do_quote=quote)

        with tempfile.TemporaryDirectory(prefix="reel-", dir=TEMP_DIR) as tmp_dir:
            try:
                result = await asyncio.to_thread(download_media, url, tmp_dir)
            except Exception as exc:  # noqa: BLE001
                # Failures are logged server-side but not posted to the chat,
                # to avoid cluttering it with error messages.
                log.warning("download failed for %s: %s", url, exc)
                await status.delete()
                continue

            downloaded = result["entries"]
            post_text = result.get("text")

            if not downloaded:
                log.info("nothing downloadable found at %s", url)
                await status.delete()
                continue

            # The post's text (Twitter/Bluesky/Instagram) goes in the caption
            # of the first media item; if too long for a caption it's sent as
            # separate follow-up message(s) so it isn't truncated.
            main_caption, overflow_text = build_caption(post_text, url)
            caption_pending = True

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
                            await message.reply_photo(photo=f, caption=main_caption, do_quote=quote)
                        else:
                            await message.reply_video(
                                video=f,
                                caption=main_caption,
                                width=entry.get("width"),
                                height=entry.get("height"),
                                duration=entry.get("duration"),
                                supports_streaming=True,
                                do_quote=quote,
                            )
                    sent_any = True
                    caption_pending = False
                except Exception as exc:  # noqa: BLE001
                    log.warning("upload failed for %s: %s", path, exc)
                    skipped.append((url, None))
            elif groupable:
                for start in range(0, len(groupable), MEDIA_GROUP_MAX):
                    chunk = groupable[start : start + MEDIA_GROUP_MAX]
                    files = [open(entry["path"], "rb") for _, entry in chunk]
                    # Only the very first item of the whole post carries the
                    # caption (Telegram shows it as the album's caption).
                    captions = []
                    for _ in chunk:
                        captions.append(main_caption if caption_pending else None)
                        caption_pending = False
                    try:
                        media = [
                            (InputMediaPhoto(media=f, caption=captions[i])
                             if kind == "photo" else
                             InputMediaVideo(
                                 media=f,
                                 caption=captions[i],
                                 width=entry.get("width"),
                                 height=entry.get("height"),
                                 duration=entry.get("duration"),
                                 supports_streaming=True,
                             ))
                            for i, ((kind, entry), f) in enumerate(zip(chunk, files))
                        ]
                        await message.reply_media_group(media=media, do_quote=quote)
                        sent_any = True
                    except Exception as exc:  # noqa: BLE001
                        log.warning("album send failed for %s: %s", url, exc)
                        skipped.append((f"{url} (album)", None))
                    finally:
                        for f in files:
                            f.close()

            for label, path in singles:
                caption = main_caption if caption_pending else label
                caption_pending = False
                try:
                    with open(path, "rb") as f:
                        await message.reply_document(document=f, caption=caption, do_quote=quote)
                    sent_any = True
                except Exception as exc:  # noqa: BLE001
                    log.warning("upload failed for %s: %s", path, exc)
                    skipped.append((label, None))

            # If the post text was too long for a caption, send it as its own
            # follow-up message(s) so nothing is lost.
            if sent_any and overflow_text:
                for chunk in _chunk_text(overflow_text):
                    try:
                        await message.reply_text(chunk, do_quote=quote)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("failed to send post text for %s: %s", url, exc)
                        break

            if skipped and sent_any:
                # Partial success: some items went through, some didn't. Note
                # only what couldn't be sent (e.g. a file over the size limit).
                lines = [
                    f"- {c}" + (f" ({s / 1024 / 1024:.1f}MB, over the limit)" if s else " (failed to send)")
                    for c, s in skipped
                ]
                await status.delete()
                await message.reply_text("Couldn't send:\n" + "\n".join(lines), do_quote=quote)
            elif skipped:
                # Nothing sent at all - stay quiet in the chat, just log it.
                log.warning("couldn't send anything for %s: %s", url, skipped)
                await status.delete()
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
        "- Bluesky photos and videos\n\n"
        "By default I reply to the message with the link (which notifies the "
        "sender). Use /replies off if you'd rather I just post the media "
        "without replying. /replies shows the current setting."
    )


async def replies_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    arg = (context.args[0].lower() if context.args else "")

    if arg in ("on", "off"):
        set_chat_replies(message.chat_id, arg == "on")
        if arg == "on":
            await message.reply_text(
                "Replies on — I'll reply to the message with the link (notifying the sender)."
            )
        else:
            await message.reply_text(
                "Replies off — I'll post media without replying to the original message."
            )
        return

    state = "on" if chat_replies_enabled(message.chat_id) else "off"
    await message.reply_text(
        f"Replies are currently {state}.\n"
        "Use /replies on or /replies off to change it."
    )


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("replies", replies_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info(
        "bot starting (cookies=%s, temp_dir=%s, ffprobe=%s)",
        "yes" if COOKIES_FILE else "no",
        TEMP_DIR or tempfile.gettempdir(),
        "yes" if shutil.which("ffprobe") else "no (video dimensions fall back to yt-dlp metadata)",
    )
    app.run_polling()


if __name__ == "__main__":
    main()
