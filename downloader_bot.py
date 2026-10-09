#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram downloader bot v3 (YouTube, TikTok, X, Instagram, Facebook, ... via yt-dlp).

- Small files (<= MAX_DIRECT_MB) are downloaded and sent directly in Telegram.
- Big files: the bot gives a direct download button (like @FBDLBOT) —
  the link opens in the browser, plays, and has a download option.

Setup (local / VPS):
    pip install -r requirements.txt
    # config.env needs: BOT_TOKEN, ALLOWED_USER_IDS
    python downloader_bot.py

Setup (Render.com free hosting):
    - push downloader_bot.py, requirements.txt, render.yaml to a GitHub repo
    - create a free Web Service on Render from that repo
    - set BOT_TOKEN and ALLOWED_USER_IDS as environment variables on Render
    - keep it awake with a free UptimeRobot monitor pinging /health every 5 min
"""

import logging
import os
import random
import re
import shutil
import string
import tempfile
import threading
from pathlib import Path

import yt_dlp
from dotenv import load_dotenv
from flask import Flask
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "config.env")

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_IDS = {x.strip() for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}
MAX_DIRECT_MB = int(os.environ.get("MAX_DIRECT_MB", "50"))  # sent inside Telegram
MAX_TOTAL_MB = 2000  # bigger than this is refused
LOCAL_API = os.environ.get("LOCAL_API_URL", "").strip().rstrip("/")  # optional, raises Telegram limit

URL_RE = re.compile(r"https?://[^\s]+")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("dlbot")


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_IDS or str(user_id) in ALLOWED_IDS


def rand_token(n: int = 8) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    cookies = BASE_DIR / "cookies.txt"
    if cookies.exists():
        opts["cookiefile"] = str(cookies)
    return opts


def probe(url: str, audio_only: bool = False):
    """Get title, approx size and a direct playable URL without downloading."""
    fmt = "bestaudio/best" if audio_only else "best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]/best"
    opts = base_opts()
    opts["format"] = fmt
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("اطلاعاتی از این لینک گرفته نشد.")
    direct = info.get("url")
    if not direct:
        reqs = info.get("requested_formats") or []
        for f in reqs:
            if f.get("acodec") != "none" and f.get("url"):
                direct = f["url"]
                break
        if not direct and reqs and reqs[0].get("url"):
            direct = reqs[0]["url"]
    size = info.get("filesize") or info.get("filesize_approx")
    title = info.get("title") or "video"
    return {"url": direct, "size": size, "title": title[:100]}


def download(url: str, audio_only: bool = False):
    """Download with yt-dlp, return (file_path, title, tmpdir). Raises on failure."""
    tmpdir = Path(tempfile.mkdtemp(prefix="dlbot_"))
    try:
        opts = base_opts()
        opts["outtmpl"] = str(tmpdir / "%(title).80s.%(ext)s")
        if audio_only:
            # no ffmpeg on free hosting: single-file formats only, sent as-is
            opts["format"] = "bestaudio/best"
        else:
            opts["format"] = "best[ext=mp4][acodec!=none][vcodec!=none]/best[acodec!=none]/best"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [p for p in tmpdir.iterdir() if p.is_file()]
        if not files:
            raise RuntimeError("فایلی دانلود نشد.")
        path = max(files, key=lambda p: p.stat().st_size)
        if path.stat().st_size > MAX_TOTAL_MB * 1_000_000:
            raise RuntimeError(f"حجم فایل از {MAX_TOTAL_MB} مگ بیشتره.")
        title = (info.get("title") if isinstance(info, dict) else None) or "video"
        return path, title[:100], tmpdir
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


# ---------------- health server (Render requires an HTTP port) ----------------
health_app = Flask(__name__)


@health_app.route("/")
@health_app.route("/health")
def health():
    return "OK", 200


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port)


# ---------------- handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 📥\n"
        "لینک رو بفرست (یوتیوب، فیسبوک، تیک‌تاک، اینستاگرام، X و...)،\n"
        "بعد انتخاب کن ویدیو می‌خوای یا فقط صداش.\n\n"
        f"📦 فایل‌های تا {MAX_DIRECT_MB} مگ مستقیم همین‌جا ارسال می‌شن.\n"
        "🔗 فایل‌های بزرگ‌تر رو با دکمه دانلود می‌گیری؛ لینک توی مرورگر پخش می‌شه.",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 راهنما\n\n"
        "۱. لینک ویدیو رو بفرست.\n"
        "۲. دکمه «🎬 ویدیو» یا «🎵 فقط صدا» رو بزن.\n"
        "۳. اگه فایل کوچیک باشه همین‌جا می‌گیریش،\n"
        "   اگه بزرگ باشه یه دکمه دانلود می‌گیری.",
    )


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ این ربات خصوصیه.")
        return
    m = URL_RE.search(update.message.text or "")
    if not m:
        return
    url = m.group(0)
    token = rand_token()
    context.bot_data[token] = url
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎬 ویدیو", callback_data=f"v:{token}"),
                InlineKeyboardButton("🎵 فقط صدا", callback_data=f"a:{token}"),
            ]
        ]
    )
    await update.message.reply_text("چی دانلود کنم؟ 👇", reply_markup=kb)


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id):
        await query.edit_message_text("⛔ این ربات خصوصیه.")
        return
    kind, token = query.data.split(":", 1)
    url = context.bot_data.pop(token, None)
    if not url:
        await query.edit_message_text("این لینک منقضی شده، دوباره بفرستش.")
        return
    audio_only = kind == "a"
    await query.edit_message_text("⏳ در حال بررسی لینک...")

    # 1) probe: size + direct url, without downloading
    try:
        meta = probe(url, audio_only=audio_only)
    except Exception as exc:
        log.warning("probe failed for %s: %s", url, exc)
        await query.edit_message_text(
            "❌ نشد. معمولاً یکی از ایناست:\n"
            "• لینک معتبر نیست یا ویدیو حذف/خصوصی شده\n"
            "• سایت ورود خواسته (مخصوصاً اینستاگرام)\n\n"
            "یه لینک دیگه امتحان کن."
        )
        return

    size = meta["size"]
    title = meta["title"]
    limit_bytes = MAX_DIRECT_MB * 1_000_000

    # 2) big file -> direct download button (no download on our server)
    if size and size > limit_bytes and meta["url"]:
        size_mb = size / 1_000_000
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬇ DOWNLOAD VIDEO" if not audio_only else "⬇ DOWNLOAD AUDIO",
                                   url=meta["url"])]]
        )
        await query.edit_message_text(
            f"حجم فایل ({size_mb:.0f} مگ) از سقف ارسال تلگرام بیشتره.\n"
            "روی دکمه زیر بزن تا دانلود بشه: 👇",
            reply_markup=kb,
        )
        return

    # 3) small (or unknown size) -> download and send in Telegram
    await query.edit_message_text("⏳ در حال دانلود... (ممکنه کمی طول بکشه)")
    try:
        path, dl_title, tmpdir = download(url, audio_only=audio_only)
    except Exception as exc:
        log.warning("download failed for %s: %s", url, exc)
        if meta["url"]:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬇ DOWNLOAD", url=meta["url"])]]
            )
            await query.edit_message_text(
                "دانلود مستقیم نشد، ولی می‌تونی از این دکمه بگیریش: 👇",
                reply_markup=kb,
            )
        else:
            await query.edit_message_text("❌ دانلود نشد. یه لینک دیگه امتحان کن.")
        return
    try:
        size_mb = path.stat().st_size / 1_000_000
        if size_mb > MAX_DIRECT_MB and meta["url"]:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬇ DOWNLOAD VIDEO", url=meta["url"])]]
            )
            await query.edit_message_text(
                f"حجم فایل ({size_mb:.0f} مگ) از سقف ارسال تلگرام بیشتره.\n"
                "روی دکمه زیر بزن تا دانلود بشه: 👇",
                reply_markup=kb,
            )
            return
        await query.edit_message_text("📤 در حال ارسال...")
        with open(path, "rb") as f:
            if audio_only:
                await query.message.reply_audio(audio=f, title=dl_title)
            else:
                await query.message.reply_video(
                    video=f, caption=f"{dl_title}\n({size_mb:.0f} مگ)", supports_streaming=True
                )
        await query.message.reply_text("✅ تموم شد!")
    except Exception as exc:
        log.warning("send failed for %s: %s", url, exc)
        await query.message.reply_text("❌ ارسال به تلگرام ناموفق بود.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN پیدا نشد! توکن رو در فایل config.env بذار.")
    threading.Thread(target=run_health_server, daemon=True).start()
    log.info("health server starting")
    builder = ApplicationBuilder().token(TOKEN)
    if LOCAL_API:
        builder = builder.base_url(f"{LOCAL_API}/bot").base_file_url(f"{LOCAL_API}/file/bot")
        log.info("using local Bot API: %s", LOCAL_API)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_choice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    log.info("downloader bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
