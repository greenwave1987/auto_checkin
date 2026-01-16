import os
import sys
import time
import base64
import asyncio
import qrcode
from telethon import TelegramClient, events
from engine.main import SecretUpdater, ConfigReader

# =========================
# 基础配置
# =========================
IDX = 0
QR_FILE = "qr.png"
SECRET_NAME = "TG_USER_SESSION"
WAIT_SECONDS = 120

config = ConfigReader()
secret = SecretUpdater(SECRET_NAME, config_reader=config)

TG_INFO = config.get_value("TG_INFO")
API_ID = TG_INFO[IDX]["api_id"]
API_HASH = TG_INFO[IDX]["api_hash"]

BOT_INFO = config.get_value("BOT_INFO")
BOT_TOKEN = BOT_INFO[IDX]["token"]
ADMIN_ID = BOT_INFO[IDX]["id"]

bot = TelegramClient("bot", API_ID, API_HASH)
user = TelegramClient("user", API_ID, API_HASH)

# =========================
# 日志
# =========================
def log(msg):
    print(f"[LOG] {msg}", flush=True)

# =========================
# QR
# =========================
def make_qr(url):
    qrcode.make(url).convert("RGB").save(QR_FILE)

# =========================
# 关闭并退出
# =========================
async def shutdown():
    log("断开 Telegram 连接")
    await bot.disconnect()
    await user.disconnect()
    log("退出 workflow")
    os._exit(0)   # ✅ CI / GitHub Actions 推荐

# =========================
# 登录逻辑
# =========================
@bot.on(events.NewMessage(from_users=ADMIN_ID, pattern=r'^/qrlogin$'))
async def qr_login(event):
    log("收到 /qrlogin")

    await user.start()

    start = time.time()

    while time.time() - start < WAIT_SECONDS:
        try:
            qr = await user.qr_login()
            make_qr(qr.url)

            await bot.send_file(
                ADMIN_ID,
                QR_FILE,
                caption="📱 请在 30 秒内扫码登录"
            )

            log("等待扫码确认")
            await qr.wait(timeout=40)

            log("扫码成功，保存 session")
            session_path = user.session.filename

            with open(session_path, "rb") as f:
                session_b64 = base64.b64encode(f.read()).decode()

            secret.update(session_b64)

            await bot.send_message(
                ADMIN_ID,
                "✅ 登录成功，Session 已保存到 GitHub Secret"
            )

            await shutdown()
            return

        except Exception as e:
            if "auth_token_expired" in str(e):
                log("二维码过期，重新生成")
                await bot.send_message(ADMIN_ID, "♻️ 二维码已过期，正在刷新")
                continue
            else:
                log(f"登录失败: {e}")
                await bot.send_message(ADMIN_ID, f"❌ 登录失败: {e}")
                await shutdown()
                return

    await bot.send_message(ADMIN_ID, "⏱ 2 分钟未扫码，登录已取消")
    await shutdown()

# =========================
# 主入口
# =========================
async def main():
    log("启动 bot")
    await bot.start(bot_token=BOT_TOKEN)
    log("Bot 已就绪，请发送 /qrlogin")
    await asyncio.sleep(WAIT_SECONDS + 10)
    await shutdown()

asyncio.run(main())
