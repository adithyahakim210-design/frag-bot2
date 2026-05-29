import logging
import asyncio
import sqlite3
import os
import requests
from bs4 import BeautifulSoup
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes, ConversationHandler
from telethon import TelegramClient
from telethon.sessions import StringSession

BOT_TOKENS = [
    "8986671937:AAEJx99dTwlQufqiQebSx0IFLcDej5reTJM",
]

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")

logging.basicConfig(level=logging.INFO)

DB_PATH = "/data/watchlist.db"

WAIT_PHONE, WAIT_CODE, WAIT_PASSWORD = range(3)

pending_clients = {}


# ===================== DATABASE =====================

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            chat_id INTEGER,
            username TEXT,
            bot_token TEXT,
            PRIMARY KEY (chat_id, username, bot_token)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS autokeep (
            chat_id INTEGER,
            username TEXT,
            PRIMARY KEY (chat_id, username)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            chat_id INTEGER PRIMARY KEY,
            session_string TEXT
        )
    """)

    existing_columns = [row[1] for row in c.execute("PRAGMA table_info(watchlist)")]
    if "bot_token" not in existing_columns:
        c.execute("ALTER TABLE watchlist ADD COLUMN bot_token TEXT NOT NULL DEFAULT ''")

    autokeep_columns = [row[1] for row in c.execute("PRAGMA table_info(autokeep)")]
    if "chat_id" not in autokeep_columns:
        c.execute("DROP TABLE autokeep")
        c.execute("""
            CREATE TABLE autokeep (
                chat_id INTEGER,
                username TEXT,
                PRIMARY KEY (chat_id, username)
            )
        """)

    conn.commit()
    conn.close()

def db_save(chat_id, username, bot_token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO watchlist (chat_id, username, bot_token) VALUES (?, ?, ?)", (chat_id, username, bot_token))
    conn.commit()
    conn.close()

def db_unsave(chat_id, username, bot_token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM watchlist WHERE chat_id = ? AND username = ? AND bot_token = ?", (chat_id, username, bot_token))
    conn.commit()
    conn.close()

def db_list(chat_id, bot_token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username FROM watchlist WHERE chat_id = ? AND bot_token = ?", (chat_id, bot_token))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_all():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, username, bot_token FROM watchlist")
    rows = c.fetchall()
    conn.close()
    return rows

def db_autokeep_add(chat_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO autokeep (chat_id, username) VALUES (?, ?)", (chat_id, username))
    conn.commit()
    conn.close()

def db_autokeep_remove(chat_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM autokeep WHERE chat_id = ? AND username = ?", (chat_id, username))
    conn.commit()
    conn.close()

def db_autokeep_list(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT username FROM autokeep WHERE chat_id = ?", (chat_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def db_autokeep_all():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT chat_id, username FROM autokeep")
    rows = c.fetchall()
    conn.close()
    return rows

def db_autokeep_exists(chat_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM autokeep WHERE chat_id = ? AND username = ?", (chat_id, username))
    result = c.fetchone()
    conn.close()
    return result is not None

def db_save_session(chat_id, session_string):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_sessions (chat_id, session_string) VALUES (?, ?)", (chat_id, session_string))
    conn.commit()
    conn.close()

def db_get_session(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_string FROM user_sessions WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def db_delete_session(chat_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM user_sessions WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


# ===================== FRAGMENT CHECKER =====================

def check_fragment(username: str) -> dict:
    username = username.lstrip("@").lower()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/html+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://fragment.com/"
    }
    try:
        response = requests.get(
            f"https://fragment.com/username/{username}",
            headers=headers, timeout=10, allow_redirects=True
        )
        soup = BeautifulSoup(response.text, "html.parser")
        og_title = ""
        og_title_tag = soup.find("meta", property="og:title")
        if og_title_tag:
            og_title = og_title_tag.get("content", "").strip()

        if "auctions for usernames" in og_title.lower():
            return {"text": f"✅ [@{username}](https://fragment.com/username/{username})", "status": "available"}
        elif og_title.lower().startswith("buy @"):
            return {"text": f"🟡 [@{username}](https://fragment.com/username/{username})", "status": "buy"}
        elif "make an offer" in og_title.lower():
            return {"text": f"🔴 [@{username}](https://fragment.com/username/{username})", "status": "taken"}
        else:
            return {"text": f"❓ *@{username}* — Unknown\n└ og:title: `{og_title}`", "status": "unknown"}
    except Exception as e:
        return {"text": f"⚠️ *@{username}* — Error\n└ {str(e)}", "status": "error"}


# ===================== AUTOKEEP VIA USERBOT =====================

async def do_autokeep(username: str, session_string: str) -> bool:
    try:
        from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest
        from telethon.tl.types import InputChannel

        async with TelegramClient(StringSession(session_string), API_ID, API_HASH) as client:
            result = await client(CreateChannelRequest(
                title=f"@{username}",
                about="",
                megagroup=False
            ))
            channel = result.chats[0]

            await client(UpdateUsernameRequest(
                channel=InputChannel(channel.id, channel.access_hash),
                username=username
            ))

            logging.info(f"Autokeep berhasil: @{username}")
            return True

    except Exception as e:
        logging.error(f"Autokeep gagal untuk @{username}: {e}")
        return False


# ===================== LOGIN CONVERSATION =====================

async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    existing = db_get_session(chat_id)
    if existing:
        await update.message.reply_text("✅ Kamu sudah login. Gunakan /logout dulu jika ingin ganti akun.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📱 Masukkan nomor HP kamu (format internasional, contoh: `+628123456789`):",
        parse_mode="Markdown"
    )
    return WAIT_PHONE

async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    phone = update.message.text.strip()

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        result = await client.send_code_request(phone)
        pending_clients[chat_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": result.phone_code_hash
        }
        await update.message.reply_text(
            "✅ Kode OTP sudah dikirim ke Telegram kamu.\nMasukkan kode OTP:",
            parse_mode="Markdown"
        )
        return WAIT_CODE
    except Exception as e:
        await client.disconnect()
        await update.message.reply_text(f"⚠️ Gagal kirim OTP: {e}")
        return ConversationHandler.END

async def login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    code = update.message.text.strip()
    data = pending_clients.get(chat_id)

    if not data:
        await update.message.reply_text("⚠️ Sesi login tidak ditemukan. Mulai ulang dengan /login.")
        return ConversationHandler.END

    client = data["client"]
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        session_string = client.session.save()
        await client.disconnect()
        db_save_session(chat_id, session_string)
        pending_clients.pop(chat_id, None)
        await update.message.reply_text("✅ Login berhasil! Autokeep sekarang akan menggunakan akunmu.")
        return ConversationHandler.END
    except Exception as e:
        error_str = str(e)
        if "two-steps" in error_str.lower() or "password" in error_str.lower():
            await update.message.reply_text("🔐 Akun kamu punya Two-Step Verification. Masukkan password:")
            return WAIT_PASSWORD
        await client.disconnect()
        pending_clients.pop(chat_id, None)
        await update.message.reply_text(f"⚠️ Login gagal: {e}")
        return ConversationHandler.END

async def login_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    password = update.message.text.strip()
    data = pending_clients.get(chat_id)

    if not data:
        await update.message.reply_text("⚠️ Sesi login tidak ditemukan. Mulai ulang dengan /login.")
        return ConversationHandler.END

    client = data["client"]

    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        await client.disconnect()
        db_save_session(chat_id, session_string)
        pending_clients.pop(chat_id, None)
        await update.message.reply_text("✅ Login berhasil! Autokeep sekarang akan menggunakan akunmu.")
        return ConversationHandler.END
    except Exception as e:
        await client.disconnect()
        pending_clients.pop(chat_id, None)
        await update.message.reply_text(f"⚠️ Password salah atau login gagal: {e}")
        return ConversationHandler.END

async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db_delete_session(chat_id)
    await update.message.reply_text("✅ Logout berhasil. Session kamu telah dihapus.")

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = pending_clients.pop(chat_id, None)
    if data:
        await data["client"].disconnect()
    await update.message.reply_text("❌ Login dibatalkan.")
    return ConversationHandler.END


# ===================== COMMAND HANDLERS =====================

async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: `/save @fikar @gemini`", parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    saved = []
    for arg in context.args:
        username = arg.lstrip("@").lower()
        if username:
            db_save(chat_id, username, bot_token)
            saved.append(username)
    if saved:
        text = "✅ *Berhasil disimpan:*\n" + " ".join([f"@{u}" for u in saved])
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_unsave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Contoh: `/unsave @fikar @gemini`", parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    removed = []
    for arg in context.args:
        username = arg.lstrip("@").lower()
        if username:
            db_unsave(chat_id, username, bot_token)
            removed.append(username)
    if removed:
        text = "🗑 *Berhasil dihapus:*\n" + " ".join([f"@{u}" for u in removed])
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bot_token = context.bot.token
    usernames = db_list(chat_id, bot_token)
    if not usernames:
        await update.message.reply_text("📭 Belum ada username yang dipantau.")
        return
    autokeep_list = db_autokeep_list(chat_id)
    lines = []
    for u in usernames:
        tag = " 🤖" if u in autokeep_list else ""
        lines.append(f"@{u}{tag}")
    text = "👀 *Username yang dipantau:*\n" + " ".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_autokeep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = db_get_session(chat_id)
    if not session:
        await update.message.reply_text(
            "⚠️ Kamu belum login userbot.\nGunakan /login dulu agar autokeep bisa berjalan di akunmu.",
            parse_mode="Markdown"
        )
        return
    if not context.args:
        await update.message.reply_text("Contoh: `/autokeep @fikar @gemini`", parse_mode="Markdown")
        return
    added = []
    for arg in context.args:
        username = arg.lstrip("@").lower()
        if username:
            db_autokeep_add(chat_id, username)
            added.append(username)
    if added:
        text = "🤖 *Autokeep aktif untuk:*\n" + " ".join([f"@{u}" for u in added])
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_unautokeep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Contoh: `/unautokeep @fikar @gemini`", parse_mode="Markdown")
        return
    removed = []
    for arg in context.args:
        username = arg.lstrip("@").lower()
        if username:
            db_autokeep_remove(chat_id, username)
            removed.append(username)
    if removed:
        text = "❌ *Autokeep dinonaktifkan untuk:*\n" + " ".join([f"@{u}" for u in removed])
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_autokeeplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    usernames = db_autokeep_list(chat_id)
    if not usernames:
        await update.message.reply_text("📭 Belum ada username di autokeep.")
        return
    text = "🤖 *Username autokeep:*\n" + " ".join([f"@{u}" for u in usernames])
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Cara Penggunaan Bot*\n\n"
        "*Pantau Username:*\n"
        "`/save @fikar` — simpan username ke watchlist\n"
        "`/unsave @fikar` — hapus dari watchlist\n"
        "`/list` — lihat semua username yang dipantau\n"
        "Bot otomatis cek setiap 15 detik & kirim notif kalau tersedia!\n\n"
        "*Autokeep Username:*\n"
        "`/login` — login userbot untuk autokeep\n"
        "`/logout` — hapus session login\n"
        "`/autokeep @fikar` — aktifkan autokeep untuk username\n"
        "`/unautokeep @fikar` — nonaktifkan autokeep\n"
        "`/autokeeplist` — lihat username yang di-autokeep\n"
        "Bot otomatis assign username ke akunmu begitu tersedia! 🤖"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ===================== BACKGROUND WATCHER =====================

async def watch_loop(bots: list):
    bot_map = {bot.token: bot for bot in bots}

    while True:
        rows = db_all()
        if not rows:
            await asyncio.sleep(15)
            continue

        watchmap = {}
        for chat_id, username, bot_token in rows:
            watchmap.setdefault((chat_id, bot_token), []).append(username)

        autokeep_all = db_autokeep_all()
        autokeep_set = {(row[0], row[1]) for row in autokeep_all}

        for (chat_id, bot_token), usernames in watchmap.items():
            bot = bot_map.get(bot_token)
            if not bot:
                continue
            for username in usernames:
                result = check_fragment(username)
                if result["status"] == "available":
                    if (chat_id, username) in autokeep_set:
                        session = db_get_session(chat_id)
                        if session:
                            success = await do_autokeep(username, session)
                        else:
                            success = False

                        if success:
                            notif = (
                                f"🤖 *Autokeep Berhasil!*\n\n"
                                f"@{username} berhasil di-assign ke akunmu!"
                            )
                            db_autokeep_remove(chat_id, username)
                            db_unsave(chat_id, username, bot_token)
                        else:
                            notif = (
                                f"⚠️ *Autokeep Gagal!*\n\n"
                                f"@{username} tersedia tapi gagal di-assign. Cepat keep manual!\n"
                                f"[Buka Fragment](https://fragment.com/username/{username})"
                            )
                    else:
                        notif = (
                            f"🔔 *Notifikasi Watchlist!*\n\n"
                            f"[@{username}](https://fragment.com/username/{username}) "
                            f"tersedia untuk dikeep!"
                        )
                    try:
                        await bot.send_message(chat_id=chat_id, text=notif, parse_mode="Markdown")
                    except Exception as e:
                        logging.warning(f"Gagal kirim notif ke {chat_id}: {e}")

                elif result["status"] == "buy":
                    notif = (
                        f"🔔 *Notifikasi Watchlist!*\n\n"
                        f"[@{username}](https://fragment.com/username/{username}) "
                        f"tersedia untuk dibeli!"
                    )
                    try:
                        await bot.send_message(chat_id=chat_id, text=notif, parse_mode="Markdown")
                    except Exception as e:
                        logging.warning(f"Gagal kirim notif ke {chat_id}: {e}")

        await asyncio.sleep(15)


# ===================== RUN BOT =====================

async def run_bot(token):
    app = ApplicationBuilder().token(token).build()

    await app.bot.set_my_commands([
        BotCommand("login", "Login userbot untuk autokeep"),
        BotCommand("logout", "Hapus session login"),
        BotCommand("save", "Pantau username"),
        BotCommand("unsave", "Berhenti pantau username"),
        BotCommand("list", "Lihat username yang dipantau"),
        BotCommand("autokeep", "Aktifkan autokeep username"),
        BotCommand("unautokeep", "Nonaktifkan autokeep username"),
        BotCommand("autokeeplist", "Lihat username yang di-autokeep"),
        BotCommand("help", "Cara penggunaan bot"),
    ])

    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", cmd_login)],
        states={
            WAIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            WAIT_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_code)],
            WAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_password)],
        },
        fallbacks=[CommandHandler("cancel", cancel_login)],
    )

    app.add_handler(login_conv)
    app.add_handler(CommandHandler("logout", cmd_logout))
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("unsave", cmd_unsave))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("autokeep", cmd_autokeep))
    app.add_handler(CommandHandler("unautokeep", cmd_unautokeep))
    app.add_handler(CommandHandler("autokeeplist", cmd_autokeeplist))
    app.add_handler(CommandHandler("help", cmd_help))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    return app


async def main():
    init_db()
    apps = await asyncio.gather(*[run_bot(token) for token in BOT_TOKENS])
    bots = [app.bot for app in apps]
    await watch_loop(bots)


if __name__ == "__main__":
    asyncio.run(main())
