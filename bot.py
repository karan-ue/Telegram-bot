import os
import asyncio
import logging
import tempfile
from urllib.parse import urlparse
from ipaddress import ip_address
import socket

import yt_dlp
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Constants
TELEGRAM_UPLOAD_LIMIT = 700_000_000  # 700 MB
SAFE_DOWNLOAD_LIMIT = 680_000_000    # 680 MB safe limit
MAX_CONCURRENT_DOWNLOADS = 10
MAX_URL_LENGTH = 2048
AUTO_DELETE_DELAY = 2820  # 47 minutes in seconds

# Format selector (ONLY 720p and 480p)
FORMAT_SELECTOR = (
    "best[height<=720][filesize<680000000]/"
    "best[height<=720][filesize_approx<680000000]/"
    "best[height<=480][filesize<680000000]/"
    "best[height<=480][filesize_approx<680000000]/"
    "best"
)

# yt-dlp configuration
YDL_OPTIONS = {
    "format": FORMAT_SELECTOR,
    "outtmpl": "%(title).80s-%(id)s.%(ext)s",
    "noplaylist": True,
    "max_filesize": SAFE_DOWNLOAD_LIMIT,
    "restrictfilenames": True,
    "windowsfilenames": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 30,
    "retries": 5,
    "fragment_retries": 5,
    "overwrites": False,
    "cachedir": False,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
    },
    "skip_unavailable_fragments": True,
}

# Semaphore for concurrent downloads
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

async def validate_url(url: str) -> tuple[bool, str]:
    """Validate URL with comprehensive checks."""
    try:
        if len(url) > MAX_URL_LENGTH:
            return False, "❌ Invalid URL\n\nURL bahut lamba hai (2048 characters se zyada nahi hona chahiye)."

        parsed = urlparse(url)
        if not parsed.scheme or parsed.scheme not in ("http", "https"):
            return False, "❌ Invalid URL\n\nSirf http:// ya https:// URLs allowed hain."

        if not parsed.hostname:
            return False, "❌ Invalid URL\n\nValid hostname nahi mila."

        if parsed.hostname in ("localhost", "localhost.localdomain", "127.0.0.1"):
            return False, "❌ Invalid URL\n\nLocalhost URLs allowed nahi hain."

        try:
            addr_info = await asyncio.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
                timeout=30,
            )
        except socket.gaierror:
            return False, "❌ Invalid URL\n\nDomain resolve nahi ho saka."

        if not addr_info:
            return False, "❌ Invalid URL\n\nDomain ka IP address nahi mila."

        for addr in addr_info:
            ip = addr[4][0]
            try:
                ip_obj = ip_address(ip)
                if (ip_obj.is_private or ip_obj.is_loopback or
                    ip_obj.is_multicast or ip_obj.is_link_local or
                    ip_obj.is_reserved or ip_obj.is_unspecified):
                    return False, "❌ Invalid URL\n\nPrivate ya unsafe network address allowed nahi hai."
            except ValueError:
                continue

        return True, ""

    except Exception as e:
        logger.error(f"URL validation error: {e}")
        return False, "❌ Invalid URL\n\nUnexpected error during validation."

async def download_video(url: str, temp_dir: str) -> tuple[bool, str, str]:
    """Download video using yt-dlp with proper error handling."""
    try:
        ydl_opts = {
            **YDL_OPTIONS,
            "outtmpl": os.path.join(temp_dir, "%(title).80s-%(id)s.%(ext)s"),
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return False, "❌ Video download nahi ho saka.\n\nPossible reasons:\n• Website supported nahi hai ya rate-limited ho gaya\n• Video private, region-locked, ya DRM-protected hai\n• Server block kar chuka hai (VPN try karo)\n• Content age-restricted hai", ""

            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                return False, "❌ Download complete hua, lekin final video file nahi mili.", ""

            file_size = os.path.getsize(filename)
            if file_size > TELEGRAM_UPLOAD_LIMIT:
                os.remove(filename)
                return False, "❌ Downloaded video 700 MB se bada hai.\nIs video ka compatible chhota format available nahi mila.", ""

            return True, "", filename

    except yt_dlp.DownloadError as e:
        logger.error(f"Download error: {e}")
        return False, "❌ Video download nahi ho saka.\n\nPossible reasons:\n• Website supported nahi hai ya rate-limited ho gaya\n• Video private, region-locked, ya DRM-protected hai\n• Server block kar chuka hai (VPN try karo)\n• Content age-restricted hai", ""
    except Exception as e:
        logger.error(f"Unexpected download error: {e}")
        return False, "❌ Unexpected error aa gaya. Thodi der baad try karein.", ""

async def upload_video(update: Update, context: ContextTypes.DEFAULT_TYPE, file_path: str, title: str) -> bool:
    """Upload video to Telegram with proper error handling."""
    try:
        with open(file_path, "rb") as video_file:
            try:
                await context.bot.send_video(
                    chat_id=update.effective_chat.id,
                    video=InputFile(video_file),
                    supports_streaming=True,
                    caption=title,
                )
                return True
            except TelegramError:
                video_file.seek(0)
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=InputFile(video_file),
                    caption=title,
                )
                return True
    except Exception as e:
        logger.error(f"Upload error: {e}")
        return False

async def auto_delete_message(context: ContextTypes.DEFAULT_TYPE, message_id: int, chat_id: int) -> None:
    """Schedule message auto-deletion."""
    await asyncio.sleep(AUTO_DELETE_DELAY)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass

async def process_video_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main function to process video URLs."""
    if not update.message:
        return

    url = update.message.text.strip()
    if not url or url.startswith("/"):
        return

    is_valid, error_msg = await validate_url(url)
    if not is_valid:
        await update.message.reply_text(error_msg)
        return

    async with download_semaphore:
        try:
            check_msg = await update.message.reply_text("🔎 URL check ho raha hai...")
        except TelegramError:
            check_msg = await update.message.reply_text("🔎 URL check ho raha hai...")

        try:
            await check_msg.edit_text("⬇️ Video download ho raha hai...\n720p aur 480p quality mein download ho raha hai.")
        except TelegramError:
            pass

        with tempfile.TemporaryDirectory() as temp_dir:
            success, error_msg, file_path = await download_video(url, temp_dir)
            if not success:
                try:
                    await check_msg.edit_text(error_msg)
                except TelegramError:
                    await update.message.reply_text(error_msg)
                return

            try:
                with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
                    info = ydl.extract_info(url, download=False)
                    title = info.get("title", "Untitled Video")[:850]
                    file_size = os.path.getsize(file_path) / (1024 * 1024)
            except Exception:
                title = "Untitled Video"
                file_size = os.path.getsize(file_path) / (1024 * 1024)

            try:
                await check_msg.edit_text("⬆️ Video Telegram par upload ho raha hai...")
            except TelegramError:
                pass

            upload_success = await upload_video(update, context, file_path, title)
            if not upload_success:
                error_msg = "❌ Video download ho gaya tha, lekin Telegram upload fail.\nThodi der baad dobara try karein."
                try:
                    await check_msg.edit_text(error_msg)
                except TelegramError:
                    await update.message.reply_text(error_msg)
                return

            try:
                await auto_delete_message(context, update.message.message_id, update.effective_chat.id)
            except Exception as e:
                logger.error(f"Failed to schedule auto-delete: {e}")

            caption = f"✅ {title}\n📦 Size: {file_size:.2f} MB\n⏱️ Auto-delete: 47 minutes"
            try:
                await update.message.reply_text(
                    caption,
                    reply_to_message_id=update.message.message_id,
                )
            except TelegramError:
                pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "👋 Namaste! Main ek universal video downloader bot hoon.\n\n"
        "Mujhe kisi bhi website ka public video URL bhejo:\n"
        "✅ YouTube, Instagram, TikTok\n"
        "✅ Faphouse, xVideos, PornHub, Redtube\n"
        "✅ Aur bohot saari sites\n\n"
        "Main sirf 720p aur 480p quality mein download karunga.\n"
        "Video apne aap 47 minutes baad delete ho jayega.\n\n"
        "Example:\nhttps://example.com/video\n\n"
        "⚠️ Private use ke liye hi bhejo!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    await update.message.reply_text(
        "📖 Bot use karne ka tarika:\n\n"
        "1. Video URL copy karo\n"
        "2. URL bot ko send karo\n"
        "3. Bot download karke upload karega\n\n"
        "Features:\n"
        "• Maximum: 700 MB\n"
        "• Quality: 720p aur 480p\n"
        "• Auto-delete: 47 minutes\n"
        "• Concurrent downloads: 10\n"
        "• Sites: YouTube, Instagram, TikTok, adult sites, etc.\n\n"
        "Kuch sites ke liye cookies/auth chahiye (khud set karna padega)"
    )

def main() -> None:
    """Run the bot."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")

if not token:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, process_video_url)
    )
    application.run_polling()

if __name__ == "__main__":
    main()
