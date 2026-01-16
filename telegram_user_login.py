import os
import time
import base64
import asyncio
import qrcode
from telethon import TelegramClient, events, Button
from engine.main import SecretUpdater, ConfigReader

# =========================
# 基础配置
# =========================
IDX = 0
QR_FILE = "qr.png"
SECRET_NAME = "TG_USER_SESSION"
WAIT_SECONDS = 120
MAX_RETRY = 3

retry_count = 0

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
    os._exit(0)

# =========================
# 发送登录菜单（按钮同一行）
# =========================
async def send_login_menu(hint: str | None = None):
    text = "请选择操作："
    if hint:
        text = f"{hint}\n\n{text}"

    await bot.send_message(
        ADMIN_ID,
        text,
        buttons=[
            [
                Button.inline("🔲 扫码登录", data=b"login_qr"),
                Button.inline("❌ 取消", data=b"login_cancel"),
            ]
        ]
    )

# =========================
# 失败处理：重发菜单 or 退出
# =========================
async def resend_menu_or_exit(reason: str):
    global retry_count
    retry_count += 1

    log(f"登录失败：{reason}（{retry_count}/{MAX_RETRY}）")

    if retry_count >= MAX_RETRY:
        await bot.send_message(
            ADMIN_ID,
            f"❌ 登录失败已达 {MAX_RETRY} 次，流程结束。\n原因：{reason}"
        )
        await shutdown()
        return

    await send_login_menu(
        hint=f"⚠️ 登录失败（{retry_count}/{MAX_RETRY}）：{reason}"
    )

# =========================
# 按钮点击处理
# =========================
@bot.on(events.CallbackQuery)
async def on_choice(event):
    if event.sender_id != ADMIN_ID:
        return

    choice = event.data.decode()

    if choice == "login_cancel":
        await event.edit("❌ 已取消登录")
        await shutdown()

    elif choice == "login_qr":
        await event.edit("🔲 已选择扫码登录，正在生成二维码…")
        await start_qr_login()

# =========================
# 扫码登录流程
# =========================
async def start_qr_login():
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
                await resend_menu_or_exit("二维码已过期")
                return
            else:
                await resend_menu_or_exit(str(e))
                return

    await resend_menu_or_exit("扫码超时")

# =========================
# 主入口
# =========================
async def main():
    log("启动 bot")
    await bot.start(bot_token=BOT_TOKEN)

    log("发送登录菜单")
    await send_login_menu()

    await asyncio.sleep(WAIT_SECONDS + 10)
    await shutdown()

asyncio.run(main())
