import logging
import re
import asyncio
import requests
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

BOT_TOKENS = [
    "8712178357:AAG9P5Y-E6QI9VQIyfklb8W_w7tB0SgKIqE",
    "8666996919:AAG-3Z738hLaMzKXO_LQ0zfLk3X_Cb7P7NU",
    "8687092588:AAEEAcRteSrJ-kf2JxgZzQQsTn_bzLFDEfc",
    "8755742902:AAH_LQD8fB8yCTdOBgeIrA1vcP12ocXO9RU",
]

logging.basicConfig(level=logging.INFO)

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
            headers=headers,
            timeout=10,
            allow_redirects=True
        )
        soup = BeautifulSoup(response.text, "html.parser")

        og_title = ""
        og_title_tag = soup.find("meta", property="og:title")
        if og_title_tag:
            og_title = og_title_tag.get("content", "").strip()

        print(f"[DEBUG] @{username} | og:title={og_title}")

        # Status 1 - Available
        if "auctions for usernames" in og_title.lower():
            return {"text": f"✅ [@{username}](https://fragment.com/username/{username})"}

        # Status 2 - Bisa dibeli
        elif og_title.lower().startswith("buy @"):
            return {"text": f"🟡 [@{username}](https://fragment.com/username/{username})"}

        # Status 3 - Taken
        elif "make an offer" in og_title.lower():
            return {"text": f"🔴 [@{username}](https://fragment.com/username/{username})"}

        else:
            return {"text": f"❓ *@{username}* — Unknown\n└ og:title: `{og_title}`"}

    except Exception as e:
        return {"text": f"⚠️ *@{username}* — Error\n└ {str(e)}"}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    usernames = re.findall(r"@[\w]+", text)

    if not usernames:
        await update.message.reply_text(
            "⚠️ Tidak ada username yang ditemukan.\nContoh: `@claude @gemini @grok`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(f"🔍 Mengecek {len(usernames)} username, mohon tunggu...")

    for username in usernames:
        result = check_fragment(username)
        await update.message.reply_text(result["text"], parse_mode="Markdown")


async def run_bot(token):
    app = ApplicationBuilder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await asyncio.Event().wait()


async def main():
    await asyncio.gather(*[run_bot(token) for token in BOT_TOKENS])


if __name__ == "__main__":
    asyncio.run(main())