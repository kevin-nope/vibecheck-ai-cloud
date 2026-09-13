# -*- coding: utf-8 -*-
"""
VIBECHECK DUAL-BOT CLOUD LAUNCHER & WATCHDOG SUPERVISOR
Orchestrates:
1. HTTP Health Check Server (Port 8080 or $PORT) for Render / Cloud health probes
2. Bot 1 (Maker / CTO Thực Chiến - bot_auditor.py)
3. Bot 2 (Checker / Trọng Tài Phản Biện - bot_redteam.py)
"""

import os
import sys
import time
import signal
import threading
import subprocess
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    format="%(asctime)s - [SUPERVISOR] - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("DualBotLauncher")

# Disable internal health server in child bot_auditor to prevent port collisions
os.environ["DISABLE_INTERNAL_HEALTH_SERVER"] = "1"

PORT = int(os.getenv("PORT", "8080"))
SHUTDOWN_REQUESTED = False

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"VibeCheck Dual-Bot System Live (Bot 1: CTO | Bot 2: RedTeam)\n")

    def log_message(self, format, *args):
        pass

def start_health_server(port):
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info(f"✅ HTTP Health Server đang hoạt động tại cổng {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"⚠️ Health Server exception: {e}")

def run_bot_supervisor(script_name, bot_display_name):
    logger.info(f"🚀 Khởi tạo giám sát: {bot_display_name} ({script_name})...")
    while not SHUTDOWN_REQUESTED:
        try:
            cmd = [sys.executable, "-u", script_name]
            p = subprocess.Popen(cmd)
            p.wait()
            if SHUTDOWN_REQUESTED:
                break
            logger.warning(f"⚠️ {bot_display_name} đã dừng (Exit code: {p.returncode}). Tự phục hồi sau 3s...")
            time.sleep(3)
        except Exception as e:
            logger.error(f"❌ Lỗi tiến trình {bot_display_name}: {e}")
            time.sleep(3)

def keep_alive_worker(app_url="https://vibecheck-ai-bot.onrender.com", interval_sec=600):
    """
    CƠ CHẾ TỰ ĐỘNG CHỐNG NGỦ ĐÔNG RENDER FREE (24/7 ZERO-COST KEEP-ALIVE):
    Render Free Web Service tự động tắt (sleep) sau 15 phút không có Inbound HTTP traffic.
    Worker này định kỳ 10 phút gửi 1 request qua Internet đến domain công khai của chính nó,
    kích hoạt edge router của Render và reset bộ đếm 15 phút, duy trì bot sống liên tục 24/7!
    """
    import requests
    time.sleep(30)  # Chờ 30s sau khi server khởi động
    logger.info(f"🛡️ Khởi động Keep-Alive Worker: ping {app_url} mỗi {interval_sec}s...")
    while not SHUTDOWN_REQUESTED:
        try:
            headers = {"User-Agent": "VibeCheck-KeepAlive-Worker/1.0"}
            r = requests.get(app_url, headers=headers, timeout=20)
            logger.info(f"💓 Keep-Alive Ping thành công (Status: {r.status_code}) - Chống ngủ đông Render Free.")
        except Exception as e:
            logger.warning(f"⚠️ Keep-Alive Ping gặp sự cố: {e}")
        time.sleep(interval_sec)

def signal_handler(signum, frame):
    global SHUTDOWN_REQUESTED
    logger.info("Nhận tín hiệu dừng (SIGINT/SIGTERM). Đang tắt hệ thống an toàn...")
    SHUTDOWN_REQUESTED = True
    sys.exit(0)

def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("=" * 60)
    logger.info("🤖 VIBECHECK DUAL-BOT ORCHESTRATOR (ENTERPRISE CLOUD)")
    logger.info("• Bot 1: Maker / CTO Thực Chiến (@thamdinh_ai_bot)")
    logger.info("• Bot 2: Checker / Trọng Tài Phản Biện (@vibecheck_redteam_bot)")
    logger.info("=" * 60)

    # 1. Khởi động HTTP Health Server
    health_thread = threading.Thread(target=start_health_server, args=(PORT,), daemon=True, name="HealthServerThread")
    health_thread.start()

    # 2. Khởi động Bot 1 (Maker / CTO)
    t1 = threading.Thread(
        target=run_bot_supervisor,
        args=("bot_auditor.py", "Bot 1 (Maker/CTO)"),
        daemon=True,
        name="Bot1_Thread"
    )
    t1.start()

    # 3. Khởi động Bot 2 (Checker / RedTeam)
    t2 = threading.Thread(
        target=run_bot_supervisor,
        args=("bot_redteam.py", "Bot 2 (Checker/RedTeam)"),
        daemon=True,
        name="Bot2_Thread"
    )
    t2.start()

    # 4. Khởi động Keep-Alive Worker chống ngủ đông Render Free (10 phút/lần)
    keep_alive_url = os.getenv("RENDER_EXTERNAL_URL", "https://vibecheck-ai-bot.onrender.com")
    ka_thread = threading.Thread(
        target=keep_alive_worker,
        args=(keep_alive_url, 600),
        daemon=True,
        name="KeepAlive_Thread"
    )
    ka_thread.start()

    # Giữ luồng chính sống để hứng signals
    try:
        while not SHUTDOWN_REQUESTED:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Dừng chương trình bởi người dùng.")

if __name__ == "__main__":
    main()
