# telegram-reel-bot

Telegram bot that watches for supported links in a chat, downloads the
media, and replies in the same chat with it attached:

- Instagram reels, photo posts, and carousels ("sets" — sent as multiple
  replies, one per item, each with its own filesize check)
- Facebook reels/videos
- YouTube videos and Shorts

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram
   (`/newbot`), copy the token it gives you.
2. `python3 -m venv venv && source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `cp config.yaml.example config.yaml` and fill in `bot_token` (and
   optionally `temp_dir` / `cookies_file`).
5. `python bot.py`

Leave it running (e.g. as a systemd service, or in `tmux`/`screen`) — it
uses long polling, so no public URL or port forwarding is needed.

By default it looks for `config.yaml` next to `bot.py`. To point it at a
different path (e.g. `/etc/telegram-reel-bot/config.yaml`), set the
`CONFIG_PATH` environment variable.

### Example systemd unit

See [telegram-reel-bot.service.example](telegram-reel-bot.service.example).
Copy it to `/etc/systemd/system/telegram-reel-bot.service`, adjust the
paths/user, then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-reel-bot
```

## Instagram login (optional but recommended)

Instagram frequently rate-limits or blocks anonymous download requests.
If reels fail to download, export cookies from a logged-in browser session
(e.g. with the "Get cookies.txt" browser extension) into a `cookies.txt`
file, and set `cookies_file` in `config.yaml` to its path.

Treat `cookies.txt` like a password — it grants access to your account.
Don't commit it or share it.

## Limits

- Telegram's Bot API caps file uploads from bots at 50MB (10MB for
  `sendPhoto`, though oversized images still go through as documents).
  Larger videos will download successfully but the bot will report that it
  can't attach them. To lift this, you'd need to run your own [local Bot
  API server](https://github.com/tdlib/telegram-bot-api) (up to 2000MB) and
  point `python-telegram-bot` at it — not set up here.
- Works in group chats too — the bot only replies to messages containing a
  supported link, and ignores everything else.
- YouTube regularly changes how it blocks anonymous/automated downloads,
  which breaks extraction until yt-dlp ships a fix. Run
  `pip install -U yt-dlp` periodically (e.g. a weekly cron) to stay current.
