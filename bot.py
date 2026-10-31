from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
import subprocess
import os
import re

TOKEN = "8821734542:AAE0CqFeawlkXSYlSkfHfGxcfTERdepN1so"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BOT PENGHILANG WATERMARK ANJAY\n\n"
        "Gc kirim link nya gw sibuk 😹), "
        "udah gratis minta cepet,nunggu lah"
    )

def detect_platform(url):
    if "tiktok.com" in url:
        return "tiktok"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    else:
        return None

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    platform = detect_platform(url)

    if not platform:
        await update.message.reply_text("❌ Link tidak dikenali. Kirim link TikTok/YouTube ya.")
        return

    await update.message.reply_text(f"⏳ Sabar lagi download dari {platform}...")

    try:
        if platform == "tiktok":
            await handle_tiktok(update, url)
        elif platform == "youtube":
            await handle_youtube(update, url)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

async def handle_tiktok(update: Update, url: str):
    api_url = f"https://www.tikwm.com/api/?url={url}"
    resp = requests.get(api_url).json()
    if resp.get("code") == 0:
        video_url = resp["data"]["play"]
        await update.message.reply_video(video=video_url, caption="✅ TikTok tanpa watermark")
    else:
        await update.message.reply_text("❌ Gagal mengambil video TikTok.")

async def handle_youtube(update: Update, url: str):
    # Download video terbaik tanpa audio (ukuran kecil) ke file sementara
    filename = "yt_video.mp4"
    # Hapus file lama jika ada
    if os.path.exists(filename):
        os.remove(filename)

    # Command yt-dlp: ambil format video terbaik (mp4) tanpa audio, maksimal 720p biar hemat
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]",
        "-o", filename,
        "--no-check-certificate",  # Hindari error SSL
        "--merge-output-format", "mp4",
        url
    ]

    # Jalankan download
    subprocess.run(cmd, check=True, capture_output=True)

    # Cek apakah file ada dan ukurannya di bawah 50 MB (batas Telegram)
    if os.path.exists(filename):
        size_mb = os.path.getsize(filename) / (1024 * 1024)
        if size_mb > 50:
            await update.message.reply_text(f"❌ VIDIO MU KEBESARAN TOLOL({size_mb:.1f} MB). Maksimal 50 MB.")
        else:
            with open(filename, "rb") as f:
                await update.message.reply_video(video=f, caption="✅ YouTube tanpa watermark")
    else:
        await update.message.reply_text("❌ Gagal mengunduh video YouTube. Mungkin link tidak valid.")

    # Bersihkan file
    if os.path.exists(filename):
        os.remove(filename)

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))
    print("Bot berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
