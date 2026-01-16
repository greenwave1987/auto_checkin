import os
import asyncio
import base64
import qrcode
from telethon import TelegramClient, events

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])

bot = TelegramClient("bot", API_ID, API_HASH)
user = TelegramClient("user", API_ID, API_HASH)

def save_session():
    with open("user.session", "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    with open(os.environ["GITHUB_ENV"], "a") as env:
        env.write(f"TG_USER_SESSION={b64}\n")

def make_qr(url):
    qrcode.make(url).convert("RGB").save("qr.png")

@bot.on(events.NewMessage(from_users=ADMIN_ID, pattern=r'^/qrlogin$'))
async def qr_login(event):
    if not user.is_connected():
        await user.connect()

    qr = await user.qr_login()
    make_qr(qr.url)
    await bot.send_file(ADMIN_ID, "qr.png", caption="请扫码登录")
    await qr.wait()
    save_session()
    await bot.send_message(ADMIN_ID, "✅ 登录成功，Session 已保存")

async def main():
    await bot.start(bot_token=BOT_TOKEN)
    print("🤖 Bot 已启动")
    await bot.run_until_disconnected()

asyncio.run(main())
