import os
import re
import sqlite3
import subprocess
import tempfile
import asyncio
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
import requests
from google import genai

# ==========================================
# KONFIGURASI & VARIABEL LINGKUNGAN
# ==========================================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8821734542:AAE0CqFeawlkXSYlSkfHfGxcfTERdepN1so")
GEMINI_KEY = os.getenv("", "")

# Inisialisasi Google GenAI Client jika API key tersedia
ai_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

DB_FILE = "predictor_history.db"

# ==========================================
# DATABASE SQLITE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            player1 TEXT,
            player2 TEXT,
            game TEXT,
            mode TEXT,
            report TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

init_db()

def save_prediction(user_id, p1, p2, game, mode, report):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO predictions (user_id, player1, player2, game, mode, report, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, p1, p2, game, mode, report, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Gagal menyimpan riwayat: {e}")

# ==========================================
# STATE UNTUK CONVERSATION HANDLER PREDIKTOR
# ==========================================
(
    STATE_GAME,
    STATE_MODE,
    STATE_WINRATE,
    STATE_MATCH_COUNT,
    STATE_EXPERIENCE,
    STATE_RECENT_PERF,
    STATE_STRENGTH_WEAKNESS,
    STATE_VIDEO_P1,
    STATE_VIDEO_P2,
) = range(9)

# ==========================================
# MENU UTAMA & DOWNLOADER LAMA
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🤖 **MULTI-UTILITY & AI PREDICTOR BOT**\n\n"
        "⚡ **Fitur 1: Video Downloader (TikTok & YouTube)**\n"
        "Cukup kirimkan link video TikTok atau YouTube ke obrolan ini, nanti otomatis didownload.\n"
        "*(Udah gratis minta cepet, nunggu lah 😹)*\n\n"
        "🎮 **Fitur 2: AI Game Predictor (BARU)**\n"
        "Prediksi peluang kemenangan pertandingan kompetitif (FF, MLBB, PUBG, dll).\n"
        "Ketik perintah:\n"
        "`/predik <Pemain 1> vs <Pemain 2>`\n"
        "Contoh: `/predik Diky Ryoji vs Naja Agung`\n\n"
        "Ketik `/cancel` kapan saja untuk membatalkan proses prediksi."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

def detect_platform(url: str):
    if "tiktok.com" in url:
        return "tiktok"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    else:
        return None

async def handle_tiktok(update: Update, url: str):
    api_url = f"https://www.tikwm.com/api/?url={url}"
    try:
        resp = requests.get(api_url, timeout=15).json()
        if resp.get("code") == 0:
            video_url = resp["data"]["play"]
            await update.message.reply_video(video=video_url, caption="✅ TikTok tanpa watermark")
        else:
            await update.message.reply_text("❌ Gagal mengambil video TikTok.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error TikTok API: {str(e)}")

async def handle_youtube(update: Update, url: str):
    # DIBERIKAN SEDIKIT MODIFIKASI: Menambahkan user_id ke nama file 
    # agar bot bisa melayani banyak orang bersamaan tanpa tabrakan file.
    user_id = update.effective_user.id
    filename = f"yt_video_{user_id}.mp4"
    
    if os.path.exists(filename):
        try: os.remove(filename)
        except: pass

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]",
        "-o", filename,
        "--no-check-certificate",
        "--merge-output-format", "mp4",
        url
    ]

    try:
        # Jalankan di background (to_thread) agar bot tidak freeze untuk user lain
        await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError:
        await update.message.reply_text("❌ Gagal mengunduh video YouTube. Mungkin link privat/tidak valid.")
        return

    if os.path.exists(filename):
        size_mb = os.path.getsize(filename) / (1024 * 1024)
        if size_mb > 50:
            await update.message.reply_text(f"❌ VIDIO MU KEBESARAN TOLOL ({size_mb:.1f} MB). Maksimal 50 MB.")
        else:
            with open(filename, "rb") as f:
                await update.message.reply_video(video=f, caption="✅ YouTube tanpa watermark")
        try:
            os.remove(filename)
        except OSError:
            pass
    else:
        await update.message.reply_text("❌ Gagal memproses video YouTube.")

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Mencegah bot memproses input saat sedang dalam sesi wawancara /predik
    if context.user_data.get('in_prediction_session'):
        return

    url = update.message.text.strip()
    platform = detect_platform(url)

    if not platform:
        await update.message.reply_text("❌ Link tidak dikenali. Kirim link TikTok/YouTube atau gunakan /predik untuk prediksi game.")
        return

    await update.message.reply_text(f"⏳ Sabar lagi download dari {platform}...")

    try:
        if platform == "tiktok":
            await handle_tiktok(update, url)
        elif platform == "youtube":
            await handle_youtube(update, url)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

# ==========================================
# FUNGSI BANTUAN AI & PEMROSESAN VIDEO
# ==========================================
def download_tiktok_bytes(url: str):
    """Mendownload MP4 TikTok ke memori sementara untuk dianalisis AI."""
    api_url = f"https://www.tikwm.com/api/?url={url}"
    try:
        resp = requests.get(api_url, timeout=15).json()
        if resp.get("code") == 0:
            direct_video_url = resp["data"]["play"]
            # Gunakan header agar tidak diblokir saat mengambil raw video
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            v_resp = requests.get(direct_video_url, headers=headers, timeout=30)
            if v_resp.status_code == 200:
                return v_resp.content
    except Exception as e:
        print(f"[TikTok fetch error] {e}")
    return None

def analyze_gameplay_vision(video_path: str, player_name: str, game: str) -> str:
    """Mengirim video gameplay ke Google Gemini Vision untuk evaluasi."""
    if not ai_client:
        return "Layanan AI Vision tidak aktif (GEMINI_API_KEY kosong)."

    prompt = (
        f"Anda adalah Analis Esports Profesional. Analisis rekaman gameplay singkat dari pemain '{player_name}' "
        f"pada game '{game}'. Evaluasi aspek berikut secara objektif:\n"
        "1. Akurasi aim / targeting.\n"
        "2. Movement, mobilitas, dan kelincahan gerak.\n"
        "3. Positioning dan kesadaran peta (map awareness).\n"
        "4. Reflex, kecepatan respon, dan pengambilan keputusan.\n"
        "5. Konsistensi permainan saat duel.\n\n"
        "PENTING: Jawab secara faktual berdasarkan apa yang TERLIHAT di video. "
        "JANGAN mengarang hasil. Jika video buram atau durasinya terlalu pendek untuk menilai suatu aspek, "
        "sebutkan dengan jujur bahwa 'Data visual tidak cukup'."
    )

    try:
        # Unggah file ke server Gemini
        video_file = ai_client.files.upload(file=video_path)
        
        # Minta model menganalisis
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[video_file, prompt]
        )
        
        # Bersihkan file dari storage Gemini agar tidak memenuhi kuota
        try: ai_client.files.delete(name=video_file.name)
        except: pass

        return response.text if response.text else "Tidak ada catatan analisis yang dihasilkan."
    except Exception as e:
        return f"Gagal menganalisis gameplay: {str(e)}"

def synthesize_final_report(data: dict) -> str:
    """Menyusun laporan akhir berdasarkan wawancara dan analisis visual."""
    if not ai_client:
        return "⚠️ API Key Gemini AI belum dikonfigurasi."

    prompt = f"""
Bertindaklah sebagai analis AI esport profesional. Buat laporan prediksi pertandingan berdasarkan data berikut.

DATA WAWANCARA:
- Pemain 1: {data['p1']}
- Pemain 2: {data['p2']}
- Game: {data['game']}
- Mode: {data['mode']}
- Win Rate: {data['winrate']}
- Total Match: {data['match_count']}
- Pengalaman: {data['experience']}
- Performa Terbaru: {data['recent_perf']}
- Keunggulan & Kelemahan: {data['strengths_weaknesses']}

HASIL ANALISIS VIDEO GAMEPLAY:
- Gameplay {data['p1']}:
{data['analysis_p1']}

- Gameplay {data['p2']}:
{data['analysis_p2']}

TUGAS:
Buatlah prediksi peluang kemenangan dengan format baku di bawah ini. 
Persentase harus rasional berdasarkan kombinasi data wawancara dan visual. Jangan memberikan 100% pada salah satu.

FORMAT WAJIB YANG HARUS ANDA GUNAKAN (Tulis dalam bahasa Indonesia):

AI GAME PREDICTION REPORT

MATCH: {data['p1'].upper()} VS {data['p2'].upper()}
GAME: {data['game'].upper()}
MODE: {data['mode'].upper()}

WIN PROBABILITY:
{data['p1']}: [X]%
{data['p2']}: [Y]%

ANALYSIS:
- Aim Performance: (Perbandingan aim)
- Movement: (Kelincahan)
- Experience & Stats: (Perbandingan jam terbang & win rate)
- Gameplay Analysis: (Eksekusi taktis dan strategi)

FINAL PREDICTION:
(Kesimpulan singkat pemain mana yang lebih diunggulkan dan alasannya).

*Catatan: Persentase ini merupakan estimasi AI berdasarkan informasi yang diinputkan pengguna dan analisis video parsial, bukan jaminan pasti hasil pertandingan.*
"""
    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"Gagal menghasilkan laporan AI: {str(e)}"

# ==========================================
# ALUR PERCAKAPAN PREDIKTOR (CONVERSATION)
# ==========================================
async def start_predik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args_text = " ".join(context.args).strip()
    match = re.split(r"(?i)\s+vs\s+", args_text) # Deteksi " vs " (case insensitive)

    if len(match) != 2 or not match[0].strip() or not match[1].strip():
        await update.message.reply_text(
            "⚠️ Format perintah salah!\n\n"
            "Gunakan format: `/predik <Pemain 1> vs <Pemain 2>`\n"
            "Contoh: `/predik Diky Ryoji vs Naja Agung`",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data['in_prediction_session'] = True
    context.user_data["p1"] = match[0].strip()
    context.user_data["p2"] = match[1].strip()

    await update.message.reply_text(
        f"🎯 Memulai sesi prediksi: **{context.user_data['p1']}** VS **{context.user_data['p2']}**.\n\n"
        "Jawab pertanyaan berikut secara berurutan.\n\n"
        "👉 **1/7**: Game apa yang dimainkan? (Misal: Free Fire, Mobile Legends, Valorant)",
        parse_mode="Markdown"
    )
    return STATE_GAME

async def answer_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["game"] = update.message.text.strip()
    await update.message.reply_text("👉 **2/7**: Mode pertandingan apa? (Misal: 1v1 CS, BR, Ranked, Custom Room)", parse_mode="Markdown")
    return STATE_MODE

async def answer_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["mode"] = update.message.text.strip()
    await update.message.reply_text(f"👉 **3/7**: Siapa yang memiliki win rate lebih tinggi antara {context.user_data['p1']} & {context.user_data['p2']}?", parse_mode="Markdown")
    return STATE_WINRATE

async def answer_winrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["winrate"] = update.message.text.strip()
    await update.message.reply_text("👉 **4/7**: Siapa yang memiliki jumlah pertandingan/match lebih banyak?", parse_mode="Markdown")
    return STATE_MATCH_COUNT

async def answer_match_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["match_count"] = update.message.text.strip()
    await update.message.reply_text("👉 **5/7**: Siapa yang memiliki jam terbang dan pengalaman bermain lebih tinggi?", parse_mode="Markdown")
    return STATE_EXPERIENCE

async def answer_experience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["experience"] = update.message.text.strip()
    await update.message.reply_text("👉 **6/7**: Bagaimana performa terbaru (recent matches) dari kedua pemain ini?", parse_mode="Markdown")
    return STATE_RECENT_PERF

async def answer_recent_perf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["recent_perf"] = update.message.text.strip()
    await update.message.reply_text(
        "👉 **7/7**: Apakah ada info tambahan terkait keunggulan/kelemahan spesifik masing-masing pemain?\n"
        "(Contoh: Jago sniping, gampang panik saat sekarat, dsb)", 
        parse_mode="Markdown"
    )
    return STATE_STRENGTH_WEAKNESS

async def answer_strengths(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["strengths_weaknesses"] = update.message.text.strip()
    p1 = context.user_data["p1"]
    
    await update.message.reply_text(
        f"🎬 **Tahap Analisis Visual**\n\n"
        f"Silakan kirimkan **Link video TikTok** ATAU **Upload langsung File Video** gameplay untuk **{p1}**.\n\n"
        "*(Pastikan durasi wajar, jika link TikTok gagal/privat bot akan meminta Anda mengunggah videonya langsung)*",
        parse_mode="Markdown"
    )
    return STATE_VIDEO_P1

async def process_video_input(update: Update, player_name: str, game: str) -> str:
    """Mengolah input video (dari link Tiktok atau file upload) lalu mengirimnya ke AI."""
    temp_dir = tempfile.gettempdir()
    video_path = os.path.join(temp_dir, f"vision_{player_name}_{update.effective_user.id}.mp4")

    # 1. Jika User Mengupload File Video / Dokumen
    if update.message.video or update.message.document:
        doc = update.message.video or update.message.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            return "Ukuran file video melebihi batas 20MB bot API."
        
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(video_path)
    
    # 2. Jika User Mengirim Teks (Link TikTok)
    elif update.message.text:
        text = update.message.text.strip()
        if "tiktok.com" in text:
            v_bytes = await asyncio.to_thread(download_tiktok_bytes, text)
            if not v_bytes:
                return "Link TikTok tidak dapat diunduh (mungkin privat atau link salah). Kirim video secara langsung jika ada."
            
            with open(video_path, "wb") as f:
                f.write(v_bytes)
        elif text.lower() == "skip":
            return "Tidak ada video yang disertakan (Di-skip pengguna)."
        else:
            return "Bukan link TikTok yang valid dan bukan file video."
    else:
        return "Format masukan tidak didukung."

    # Lakukan analisis vision AI secara asinkron agar tidak memblokir antrean
    try:
        analysis_result = await asyncio.to_thread(analyze_gameplay_vision, video_path, player_name, game)
    finally:
        # Bersihkan file temporary lokal
        if os.path.exists(video_path):
            try: os.remove(video_path)
            except: pass

    return analysis_result

async def answer_video_p1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p1 = context.user_data["p1"]
    p2 = context.user_data["p2"]
    game = context.user_data["game"]

    status_msg = await update.message.reply_text(f"⏳ Sedang mendownload & menganalisis gerakan **{p1}** via Vision AI...", parse_mode="Markdown")

    analysis = await process_video_input(update, p1, game)
    context.user_data["analysis_p1"] = analysis

    # Tangani error gagal proses video agar user bisa re-try tanpa keluar state
    if "Gagal" in analysis or "tidak valid" in analysis or "tidak dapat diunduh" in analysis:
        await status_msg.edit_text(f"⚠️ {analysis}\n\nSilakan coba kirim ulang link TikTok lain atau upload file MP4-nya langsung (Atau ketik 'skip').")
        return STATE_VIDEO_P1

    await status_msg.edit_text(f"✅ Gameplay **{p1}** sukses dianalisis.", parse_mode="Markdown")
    await update.message.reply_text(
        f"Sekarang kirimkan **Link video TikTok** ATAU **Upload langsung File Video** gameplay untuk **{p2}**.\n\n"
        "*(Ketik 'skip' jika tidak memiliki videonya)*",
        parse_mode="Markdown"
    )
    return STATE_VIDEO_P2

async def answer_video_p2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p1 = context.user_data["p1"]
    p2 = context.user_data["p2"]
    game = context.user_data["game"]

    status_msg = await update.message.reply_text(f"⏳ Sedang mendownload & menganalisis gerakan **{p2}** via Vision AI...", parse_mode="Markdown")

    analysis = await process_video_input(update, p2, game)
    context.user_data["analysis_p2"] = analysis

    if "Gagal" in analysis or "tidak valid" in analysis or "tidak dapat diunduh" in analysis:
        await status_msg.edit_text(f"⚠️ {analysis}\n\nSilakan coba kirim ulang link TikTok lain atau upload file MP4-nya langsung (Atau ketik 'skip').")
        return STATE_VIDEO_P2

    await status_msg.edit_text("🧠 Analisis selesai. Sedang menyusun Laporan Prediksi Akhir...")

    # Memanggil AI Text Model
    report = await asyncio.to_thread(synthesize_final_report, context.user_data)

    # Simpan ke Database
    save_prediction(
        user_id=update.effective_user.id,
        p1=p1,
        p2=p2,
        game=game,
        mode=context.user_data.get("mode", ""),
        report=report
    )

    await update.message.reply_text(report)
    
    # Hapus flag sesi
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_predik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Sesi prediksi pertandingan telah dibatalkan. Anda kembali ke mode Downloader.")
    return ConversationHandler.END

# ==========================================
# MAIN ROUTING APLIKASI
# ==========================================
def main():
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN belum disetel di environment variables!")
        return

    app = Application.builder().token(TOKEN).build()

    # Conversation Handler untuk Fitur Prediktor
    # Harus didaftarkan lebih dulu sebelum text downloader biasa
    predictor_conv = ConversationHandler(
        entry_points=[CommandHandler("predik", start_predik)],
        states={
            STATE_GAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_game)],
            STATE_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_mode)],
            STATE_WINRATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_winrate)],
            STATE_MATCH_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_match_count)],
            STATE_EXPERIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_experience)],
            STATE_RECENT_PERF: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_recent_perf)],
            STATE_STRENGTH_WEAKNESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, answer_strengths)],
            
            # Mendukung teks (link) dan file video
            STATE_VIDEO_P1: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, answer_video_p1),
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, answer_video_p1),
            ],
            STATE_VIDEO_P2: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, answer_video_p2),
                MessageHandler(filters.VIDEO | filters.Document.VIDEO, answer_video_p2),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_predik)],
        per_user=True,
        per_chat=True,
    )

    # Tambahkan Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(predictor_conv)
    
    # Downloader lama berjalan SEBAGAI FALLBACK jika tidak ada di dalam sesi prediksi
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

    print("✅ Bot berjalan... Tekan Ctrl+C untuk berhenti.")
    app.run_polling()

if __name__ == "__main__":
    main()
