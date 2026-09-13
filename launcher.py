# -*- coding: utf-8 -*-
"""
VIBECHECK DUAL-BOT CLOUD LAUNCHER & ORCHESTRATION SERVER (ENTERPRISE GRADE)
Orchestrates:
1. Unified HTTP Server (Port 8080 or $PORT):
   - GET /healthz: Real health probe (200 on healthy components, 503 on failure)
   - GET /readyz: Readiness check
   - GET /: Service liveness landing
   - POST /webhook/bot1: High-throughput, low-latency webhook for Bot 1 (CTO @thamdinh_ai_bot)
   - POST /webhook/bot2: High-throughput, low-latency webhook for Bot 2 (RedTeam @vibecheck_redteam_bot)
2. Background Worker Pool for Fast ACK (<50ms)
3. Cryptographic Webhook Header Secret Validation (X-Telegram-Bot-Api-Secret-Token)
4. Proactive Escalation Daemon Integration
5. Zero-Cost 24/7 Keep-Alive Worker for Render Free Web Service
"""

import os
import sys
import time
import json
import signal
import hashlib
import threading
import logging
import concurrent.futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Dict, Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    format="%(asctime)s - [LAUNCHER] - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("DualBotLauncher")

# Disable internal health server in child modules
os.environ["DISABLE_INTERNAL_HEALTH_SERVER"] = "1"

PORT = int(os.getenv("PORT", "8080"))
SHUTDOWN_REQUESTED = False

# Global state for health monitoring
HEALTH_STATE: Dict[str, Any] = {
    "status": "starting",
    "start_time": time.time(),
    "bot1_ready": False,
    "bot2_ready": False,
    "webhook_mode": False,
    "last_error": None,
    "processed_updates_bot1": 0,
    "processed_updates_bot2": 0
}

# Concurrency Worker Pool (fast ACK < 50ms, processing offloaded to threads)
WORKER_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="WebhookWorker")

bot1_instance = None
bot2_instance = None
webhook_secret_token = ""


def get_webhook_secret_token() -> str:
    """
    Retrieves random independent secret token for Telegram webhook validation.
    Enforces strict startup fail-fast in production if WEBHOOK_SECRET_TOKEN is not configured.
    Zero derivation from bot token.
    """
    env_secret = (os.getenv("WEBHOOK_SECRET_TOKEN") or "").strip()
    is_production = os.getenv("RENDER") == "true" or os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PORT")
    if not env_secret:
        if is_production:
            logger.critical("❌ FATAL: WEBHOOK_SECRET_TOKEN is missing in production environment! Halting startup immediately (fail-fast).")
            sys.exit(1)
        else:
            logger.warning("⚠️ Local development mode: WEBHOOK_SECRET_TOKEN is empty. Using dev secret.")
            return "dev_secret_local_only_12345"
    if len(env_secret) < 16:
        if is_production:
            logger.critical("❌ FATAL: WEBHOOK_SECRET_TOKEN must be at least 16 characters! Halting startup.")
            sys.exit(1)
    return env_secret


class UnifiedServerHandler(BaseHTTPRequestHandler):
    server_version = "VibeCheckServer/3.0"

    def do_GET(self):
        global HEALTH_STATE
        path = self.path.split("?")[0]

        if path in ("/healthz", "/health"):
            is_healthy = HEALTH_STATE.get("bot1_ready", False) and (HEALTH_STATE.get("bot2_ready", False) or not os.getenv("TELEGRAM_BOT_2_TOKEN"))
            status_code = 200 if is_healthy else 503

            payload = {
                "status": "healthy" if is_healthy else "unhealthy",
                "service": "vibecheck-dual-bot",
                "uptime_seconds": int(time.time() - HEALTH_STATE["start_time"]),
                "bot1_ready": HEALTH_STATE.get("bot1_ready", False),
                "bot2_ready": HEALTH_STATE.get("bot2_ready", False),
                "webhook_mode": HEALTH_STATE.get("webhook_mode", False),
                "processed_bot1": HEALTH_STATE.get("processed_updates_bot1", 0),
                "processed_bot2": HEALTH_STATE.get("processed_updates_bot2", 0)
            }
            if HEALTH_STATE.get("last_error"):
                payload["last_error"] = str(HEALTH_STATE["last_error"])

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return

        elif path in ("/readyz", "/ready"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"status": "ready"}')
            return

        elif path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"VibeCheck Dual-Bot System Live (Bot 1: CTO @thamdinh_ai_bot | Bot 2: RedTeam @vibecheck_redteam_bot)\n")
            return

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not Found\n")

    def do_POST(self):
        global bot1_instance, bot2_instance, webhook_secret_token, HEALTH_STATE
        path = self.path.split("?")[0]

        if path in ("/webhook/bot1", "/webhook/bot2"):
            # 1. Check Secret Token Header
            client_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if webhook_secret_token and client_secret != webhook_secret_token:
                logger.warning(f"⛔ Unauthorized Webhook POST request rejected at {path}: Invalid secret token header.")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Forbidden: Invalid secret token\n")
                return

            # 2. Read Request Body
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 10 * 1024 * 1024:  # 10MB guard
                self.send_response(413)
                self.end_headers()
                return

            post_data = self.rfile.read(content_length).decode("utf-8")

            # 3. Fast ACK (<50ms): Respond HTTP 200 OK immediately to Telegram
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

            # 4. Offload Update Processing to Worker Pool
            try:
                update_json = json.loads(post_data)
                import telebot
                update = telebot.types.Update.de_json(update_json)
                if not update:
                    return

                if path == "/webhook/bot1" and bot1_instance:
                    HEALTH_STATE["processed_updates_bot1"] += 1
                    WORKER_POOL.submit(bot1_instance.process_new_updates, [update])
                elif path == "/webhook/bot2" and bot2_instance:
                    HEALTH_STATE["processed_updates_bot2"] += 1
                    WORKER_POOL.submit(bot2_instance.process_new_updates, [update])
            except Exception as e:
                logger.error(f"Error dispatching webhook update: {e}")
                HEALTH_STATE["last_error"] = str(e)
            return

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress routine health check log spam
        if args and len(args) > 0 and any(p in str(args[0]) for p in ["/healthz", "/readyz", "KeepAlive"]):
            return
        logger.debug("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))


def start_http_server(port: int) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), UnifiedServerHandler)
    logger.info(f"✅ Unified HTTP Server listening on port {port}")
    return server


def keep_alive_worker(app_url: str, interval_sec: int = 600):
    """
    Render Free Web Service Keep-Alive:
    Pings the public /healthz endpoint periodically to prevent 15-minute hibernation.
    """
    import requests
    time.sleep(20)
    health_url = app_url.rstrip("/") + "/healthz"
    logger.info(f"🛡️ Keep-Alive Worker initialized: pinging {health_url} every {interval_sec}s...")
    while not SHUTDOWN_REQUESTED:
        try:
            headers = {"User-Agent": "VibeCheck-KeepAlive-Worker/1.0"}
            r = requests.get(health_url, headers=headers, timeout=15)
            logger.info(f"💓 Keep-Alive Probe Status: {r.status_code}")
        except Exception as e:
            logger.warning(f"⚠️ Keep-Alive Probe warning: {e}")
        time.sleep(interval_sec)


def signal_handler(signum, frame):
    global SHUTDOWN_REQUESTED
    logger.info("🛑 Received termination signal (SIGINT/SIGTERM). Gracefully shutting down...")
    SHUTDOWN_REQUESTED = True
    WORKER_POOL.shutdown(wait=False)
    sys.exit(0)


def main():
    global bot1_instance, bot2_instance, webhook_secret_token, HEALTH_STATE

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("=" * 65)
    logger.info("🚀 VIBECHECK DUAL-BOT ENTERPRISE ORCHESTRATOR")
    logger.info("• Bot 1 (CTO / Tech Auditor): @thamdinh_ai_bot")
    logger.info("• Bot 2 (Red Team Referee): @vibecheck_redteam_bot")
    logger.info("=" * 65)

    webhook_secret_token = get_webhook_secret_token()

    # 0. Restore Durable Persistence (Backlog & Reminder State) before starting bots
    try:
        from escalation_system import DurablePersistenceAdapter
        import bot_auditor
        DurablePersistenceAdapter.restore_all(
            base_dir=bot_auditor.BASE_DIR,
            backlog_file=bot_auditor.BACKLOG_FILE,
            state_file=os.path.join(bot_auditor.BASE_DIR, ".reminded_state.json")
        )
    except Exception as e:
        logger.warning(f"DurablePersistence startup restore warning: {e}")

    # 1. Initialize Bot 1 (Maker / CTO)
    try:
        import bot_auditor
        bot1_instance = bot_auditor.setup_bot()
        HEALTH_STATE["bot1_ready"] = True
        logger.info("✅ Bot 1 (Maker / CTO) setup completed.")

        # Start Proactive Escalation Worker
        from escalation_system import start_proactive_escalation_worker
        start_proactive_escalation_worker(
            bot=bot1_instance,
            backlog_path=bot_auditor.BACKLOG_FILE,
            base_dir=bot_auditor.BASE_DIR,
            check_interval_seconds=1800,
            threshold_hours=12.0,
            initial_delay_seconds=10
        )
        logger.info("✅ Proactive Escalation Worker started for Bot 1.")
    except Exception as e:
        logger.error(f"❌ Failed to setup Bot 1: {e}")
        HEALTH_STATE["last_error"] = f"Bot 1 Setup Error: {e}"

    # 2. Initialize Bot 2 (Checker / Red Team)
    bot2_token = os.getenv("TELEGRAM_BOT_2_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN_2")
    if bot2_token and len(bot2_token.strip()) > 10:
        try:
            import bot_redteam
            bot2_instance = bot_redteam.create_bot()
            HEALTH_STATE["bot2_ready"] = True
            logger.info("✅ Bot 2 (Checker / RedTeam) setup completed.")
        except Exception as e:
            logger.error(f"❌ Failed to setup Bot 2: {e}")
            HEALTH_STATE["last_error"] = f"Bot 2 Setup Error: {e}"
    else:
        logger.warning("⚠️ TELEGRAM_BOT_2_TOKEN not configured. Bot 2 is inactive.")

    # 3. Start Unified HTTP Server
    server = start_http_server(PORT)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="HTTPServerThread")
    server_thread.start()

    # 4. Determine Execution Mode (Webhook vs Polling)
    is_cloud = os.getenv("RENDER") == "true" or bool(os.getenv("RENDER_EXTERNAL_URL"))
    force_polling = os.getenv("USE_POLLING") == "1"
    public_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or "https://vibecheck-ai-bot.onrender.com"

    if is_cloud and not force_polling:
        HEALTH_STATE["webhook_mode"] = True
        logger.info(f"🌐 Activating TELEGRAM WEBHOOK MODE on {public_url}...")

        # Configure Webhooks for Telegram Bots
        try:
            if bot1_instance:
                w1_url = f"{public_url}/webhook/bot1"
                bot1_instance.set_webhook(
                    url=w1_url,
                    secret_token=webhook_secret_token,
                    drop_pending_updates=False
                )
                logger.info(f"✅ Bot 1 Webhook configured: {w1_url}")

            if bot2_instance:
                w2_url = f"{public_url}/webhook/bot2"
                bot2_instance.set_webhook(
                    url=w2_url,
                    secret_token=webhook_secret_token,
                    drop_pending_updates=False
                )
                logger.info(f"✅ Bot 2 Webhook configured: {w2_url}")
        except Exception as e:
            logger.error(f"❌ Webhook configuration error: {e}")
            HEALTH_STATE["last_error"] = f"Webhook Error: {e}"

        # Start Keep-Alive Worker
        ka_thread = threading.Thread(
            target=keep_alive_worker,
            args=(public_url, 600),
            daemon=True,
            name="KeepAlive_Thread"
        )
        ka_thread.start()

    else:
        # Fallback to Polling Mode (local development / debugging)
        HEALTH_STATE["webhook_mode"] = False
        logger.info("🔄 Running in LOCAL POLLING MODE (Deleting Webhooks)...")
        if bot1_instance:
            try:
                bot1_instance.delete_webhook()
            except Exception:
                pass
            t1 = threading.Thread(
                target=lambda: bot1_instance.infinity_polling(timeout=90, long_polling_timeout=20),
                daemon=True,
                name="Bot1_Polling"
            )
            t1.start()

        if bot2_instance:
            try:
                bot2_instance.delete_webhook()
            except Exception:
                pass
            t2 = threading.Thread(
                target=lambda: bot2_instance.infinity_polling(timeout=30, long_polling_timeout=20),
                daemon=True,
                name="Bot2_Polling"
            )
            t2.start()

    HEALTH_STATE["status"] = "running"
    logger.info(f"🌟 VibeCheck Dual-Bot System is fully OPERATIONAL on port {PORT}!")

    # Keep Main Thread Alive
    try:
        while not SHUTDOWN_REQUESTED:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down launcher.")
        server.shutdown()


if __name__ == "__main__":
    main()
