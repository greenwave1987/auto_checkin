import os
import asyncio
import base64
from telethon import TelegramClient, events
import qrcode

API_ID = 11027029
API_HASH = "4f06a4742fb65ab1d8051c6fc0f33b09"
BOT_TOKEN = "8525533877:AAGJDqO5TmqtJatwW-tZoDcc8LPtLVVcD8Y"
ADMIN_ID = 1966630851

SESSION_FILE = "user.session"
QR_FILE = "login_qr.png"

bot = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
user = TelegramClient("user", API_ID, API_HASH)

def save_session_to_env():
    with open(SESSION_FILE, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    with open(os.environ["GITHUB_ENV"], "a") as env:
        env.write(f"TG_USER_SESSION={b64}\n")

def make_qr(text):
    img = qrcode.make(text).convert("RGB")
    img.save(QR_FILE)

@bot.on(events.NewMessage(from_users=ADMIN_ID, pattern=r'^/qrlogin$'))
async def qr_login(event):
    await user.connect()
    qr = await user.qr_login()
    make_qr(qr.url)
    await bot.send_file(ADMIN_ID, QR_FILE, caption="请使用 Telegram 扫码登录")
    await qr.wait(timeout=120)
    save_session_to_env()
    await bot.send_message(ADMIN_ID, "✅ 登录成功，Session 已保存到 GitHub 环境变量")
    await user.disconnect()

@bot.on(events.NewMessage(from_users=ADMIN_ID, pattern=r'^/codelogin$'))
async def code_login(event):
    await user.connect()
    async with bot.conversation(ADMIN_ID, timeout=120) as conv:
        await conv.send_message("请输入手机号（如 +8613xxxx）：")
        phone = (await conv.get_response()).text

        await user.send_code_request(phone)
        await conv.send_message("请输入验证码：")
        code = (await conv.get_response()).text

        await user.sign_in(phone, code)

    save_session_to_env()
    await bot.send_message(ADMIN_ID, "✅ 登录成功，Session 已保存到 GitHub 环境变量")
    await user.disconnect()

async def main():
    print("🤖 Bot 已启动")
    await bot.run_until_disconnected()

asyncio.run(main())
