
import os
import sys
import time
import json
import base64
import asyncio
import requests
import qrcode
from telethon import TelegramClient, events

# =========================
# 基础环境
# =========================

API_ID = 11027029
API_HASH = "4f06a4742fb65ab1d8051c6fc0f33b09"
BOT_TOKEN = "8525533877:AAGJDqO5TmqtJatwW-tZoDcc8LPtLVVcD8Y"
ADMIN_ID = 1966630851
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]

SESSION_FILE = "user.session"
QR_FILE = "qr.png"
SECRET_NAME = "TG_USER_SESSION"
WAIT_SECONDS = 120

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
# 写 GitHub Secret
# =========================

def save_secret(session_b64):
    log("获取 repo 公钥")
    owner, repo = GITHUB_REPO.split("/")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    r = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
        headers=headers,
    )
    r.raise_for_status()
    key_id = r.json()["key_id"]
    key = r.json()["key"]

    from nacl import public, encoding
    sealed_box = public.SealedBox(
        public.PublicKey(key.encode(), encoding.Base64Encoder())
    )

    encrypted = sealed_box.encrypt(session_b64.encode())
    encrypted_b64 = base64.b64encode(encrypted).decode()

    log("写入 GitHub Secret")
    r = requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{SECRET_NAME}",
        headers=headers,
        json={
            "encrypted_value": encrypted_b64,
            "key_id": key_id,
        },
    )
    r.raise_for_status()

# =========================
# 登录逻辑
# =========================

@bot.on(events.NewMessage(from_users=ADMIN_ID, pattern=r'^/qrlogin$'))
async def qr_login(event):
    log("收到 /qrlogin")

    if not user.is_connected():
        log("连接 user client")
        await user.connect()

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

            log("扫码成功，读取 session")
            with open(SESSION_FILE, "rb") as f:
                session_b64 = base64.b64encode(f.read()).decode()

            save_secret(session_b64)

            await bot.send_message(ADMIN_ID, "✅ 登录成功，Session 已保存到 GitHub Secret")
            log("登录完成，准备退出")
            await shutdown()
            return

        except Exception as e:
            if "auth_token_expired" in str(e):
                log("二维码过期，刷新")
                await bot.send_message(ADMIN_ID, "♻️ 二维码已过期，正在刷新")
                continue
            else:
                log(f"登录失败: {e}")
                await bot.send_message(ADMIN_ID, f"❌ 登录失败: {e}")
                await shutdown()
                return

    log("超时未扫码")
    await bot.send_message(ADMIN_ID, "⏱ 2 分钟未扫码，登录已取消")
    await shutdown()

# =========================
# 关闭 bot & 退出
# =========================

async def shutdown():
    log("断开连接")
    if user.is_connected():
        await user.disconnect()
    if bot.is_connected():
        await bot.disconnect()
    log("退出 workflow")
    sys.exit(0)

# =========================
# 主入口
# =========================

async def main():
    log("启动 bot")
    await bot.start(bot_token=BOT_TOKEN)
    log("Bot 已就绪，请发送 /qrlogin")
    await asyncio.sleep(WAIT_SECONDS + 10)
    log("超时退出")
    await shutdown()

asyncio.run(main())
