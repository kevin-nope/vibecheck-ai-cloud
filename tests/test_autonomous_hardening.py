"""
Comprehensive Test Suite for Autonomous Production Hardening:
- P0: Strict Auth & No auto-discovery
- P0: Webhook Dedup & Idempotency
- P0: Red Team Fail-Closed Guarantee
- P1: Gemini Semaphore & Deadline
- P1: Media SSRF & Bounded Downloads & Magic Bytes
- P1: Backlog Supersede Unpacking & Pending Guard
- P2: Unified Server & Real Health Checks
"""

import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock
import telebot

# Setup Mock Env before imports
os.environ["ADMIN_CHAT_ID"] = "8546576092"
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
os.environ["TELEGRAM_BOT_2_TOKEN"] = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew22"
os.environ["GEMINI_API_KEY"] = "AIzaSyFakeKeyForTesting123456789"
os.environ["PORT"] = "10000"

import bot_auditor
import bot_redteam
import launcher
from escalation_system import AdminChatIDManager, BacklogManager, DurablePersistenceAdapter

class TestAutonomousHardening(unittest.TestCase):

    def test_01_admin_authorization_and_no_autodiscovery(self):
        """P0: Strict authorization from env, zero auto-discovery."""
        # 1. Non-admin ID must be rejected
        self.assertFalse(AdminChatIDManager.is_authorized(999999999))
        self.assertFalse(AdminChatIDManager.is_authorized("attacker_id"))

        # 2. Configured admin ID must be authorized
        self.assertTrue(AdminChatIDManager.is_authorized(8546576092))
        self.assertTrue(AdminChatIDManager.is_authorized("8546576092"))

        # 3. Verify get_authorized_ids contains exact configured ID
        admin_ids = AdminChatIDManager.get_authorized_ids()
        self.assertIn(8546576092, admin_ids)
        self.assertNotIn(999999999, admin_ids)

    def test_02_idempotency_manager(self):
        """Telegram update / callback deduplication."""
        mgr = bot_auditor.IdempotencyManager(ttl_seconds=5)
        self.assertTrue(mgr.claim("update_1001"))
        self.assertFalse(mgr.claim("update_1001"))  # duplicate should return False
        self.assertTrue(mgr.claim("update_1002"))

    def test_03_ssrf_protection(self):
        """Media Hardening: SSRF defense rejecting private, loopback, link-local IPs."""
        self.assertFalse(bot_auditor.is_safe_public_url("http://127.0.0.1:8080/secret"))
        self.assertFalse(bot_auditor.is_safe_public_url("http://localhost:3000"))
        self.assertFalse(bot_auditor.is_safe_public_url("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(bot_auditor.is_safe_public_url("http://10.0.0.1/internal"))
        self.assertFalse(bot_auditor.is_safe_public_url("http://192.168.1.1/router"))
        self.assertFalse(bot_auditor.is_safe_public_url("http://172.16.0.1/private"))
        self.assertFalse(bot_auditor.is_safe_public_url("ftp://example.com/file"))

        # Safe public domains
        self.assertTrue(bot_auditor.is_safe_public_url("https://www.google.com"))
        self.assertTrue(bot_auditor.is_safe_public_url("https://github.com/vibecheck"))

    def test_04_magic_byte_validation(self):
        """Media Hardening: Validate magic bytes for images and videos."""
        # JPEG: \xFF\xD8\xFF
        self.assertTrue(bot_auditor.validate_magic_bytes(b"\xFF\xD8\xFF\xE0\x00\x10JFIF", "image"))
        # PNG: \x89PNG\r\n\x1a\n
        self.assertTrue(bot_auditor.validate_magic_bytes(b"\x89PNG\r\n\x1a\n\x00\x00", "image"))
        # Fake image
        self.assertFalse(bot_auditor.validate_magic_bytes(b"<script>alert(1)</script>", "image"))

        # MP4: ....ftyp
        self.assertTrue(bot_auditor.validate_magic_bytes(b"\x00\x00\x00\x18ftypmp42", "video"))
        # Fake video
        self.assertFalse(bot_auditor.validate_magic_bytes(b"NOT_A_VIDEO_FILE", "video"))

    def test_05_bounded_stream_download_ssrf_block(self):
        """bounded_stream_download blocks private IPs immediately."""
        data = bot_auditor.bounded_stream_download("http://127.0.0.1:8080/secret", max_bytes=1000)
        self.assertIsNone(data)

    def test_06_redteam_fail_closed_on_error(self):
        """Red Team: Fail-closed logic (errors/timeouts yield UNVERIFIED and is_cancelled=True)."""
        with patch("bot_redteam.call_gemini_redteam") as mock_red:
            # Simulate exception
            mock_red.side_effect = RuntimeError("API Network Crash")
            text, is_cancelled = bot_auditor.autonomous_redteam_review(
                chat_id=8546576092,
                user_request="Thẩm định giải pháp XYZ",
                cto_output="CTO đề xuất"
            )
            self.assertTrue(is_cancelled)
            self.assertIn("UNVERIFIED / BLOCKED", text)

            # Simulate None return (timeout)
            mock_red.side_effect = None
            mock_red.return_value = None
            text2, is_cancelled2 = bot_auditor.autonomous_redteam_review(
                chat_id=8546576092,
                user_request="Thẩm định giải pháp XYZ",
                cto_output="CTO đề xuất"
            )
            self.assertTrue(is_cancelled2)
            self.assertIn("UNVERIFIED / BLOCKED", text2)

    def test_07_redteam_red_flag_cancellation(self):
        """Red Team: Red flag triggers cancellation."""
        with patch("bot_redteam.call_gemini_redteam") as mock_red:
            mock_red.side_effect = None
            mock_red.return_value = "🚦 ĐÈN TÍN HIỆU: 🔴 ĐỎ (Hủy ngay) - Rủi ro sập sàn.\n⚠️ TỬ HUYỆT: Chi phí ẩn vượt vốn."
            text, is_cancelled = bot_auditor.autonomous_redteam_review(
                chat_id=8546576092,
                user_request="Thẩm định giải pháp XYZ",
                cto_output="CTO đề xuất"
            )
            self.assertTrue(is_cancelled)
            self.assertIn("🔴 ĐÃ TỰ HỦY BỎ", text)

    def test_08_redteam_clean_approval(self):
        """Red Team: Verified clean approval."""
        with patch("bot_redteam.call_gemini_redteam") as mock_red:
            mock_red.side_effect = None
            mock_red.return_value = "🚦 ĐÈN TÍN HIỆU: 🟢 XANH (Duyệt) - Khả thi cao.\n💡 PHƯƠNG ÁN: Làm MVP 0đ."
            text, is_cancelled = bot_auditor.autonomous_redteam_review(
                chat_id=8546576092,
                user_request="Thẩm định giải pháp XYZ",
                cto_output="CTO đề xuất"
            )
            self.assertFalse(is_cancelled)
            self.assertIn("🟢 ĐÃ PHÊ DUYỆT", text)

    def test_09_backlog_supersede_unpacking_and_guard(self):
        """Dedupe / Data Integrity: supersede_task returns tuple (success, new_task_id) and pending guard."""
        test_file = "test_backlog_tmp.md"
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("""# MASTER BACKLOG
| Task ID | Ngày | Công Nghệ | Trụ Cột | Việc Cần Làm | Ưu Tiên | Trạng Thái | Link |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| TASK-20260913-0001 | 2026-09-13 10:00 | Old Tool | Test Pillar | Action 1 | 🔴 P1 | [ ] Chờ làm | [Link](a) |
| TASK-20260913-0002 | 2026-09-13 10:00 | Done Tool | Test Pillar | Action 2 | 🔴 P1 | [x] Hoàn thành | [Link](b) |
""")
        mgr = BacklogManager(test_file)
        try:
            # 1. Supersede pending task -> Must succeed
            new_rec = {
                "date_str": "2026-09-13 12:00",
                "tool_name": "Super Tool",
                "pillar": "Test Pillar",
                "action_item": "Better Action",
                "priority": "🔴 P1",
                "status": "[ ] Chờ làm",
                "link": "[NewLink](c)"
            }
            res = mgr.supersede_task("TASK-20260913-0001", new_rec)
            self.assertIsInstance(res, tuple)
            success, new_id = res
            self.assertTrue(success)
            self.assertTrue(new_id.startswith("TASK-"))

            # 2. Attempt to supersede an already completed task -> Must fail
            res_done = mgr.supersede_task("TASK-20260913-0002", new_rec)
            self.assertIsInstance(res_done, tuple)
            s_done, id_done = res_done
            self.assertFalse(s_done)
            self.assertEqual(id_done, "")
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)

    def test_10_handler_unauthorized_rejection(self):
        """Message handlers reject unauthorized callers."""
        mock_bot = MagicMock()
        fake_msg = telebot.types.Message.de_json({
            "message_id": 9999,
            "date": 1700000000,
            "chat": {"id": 123456789, "type": "private"},
            "from": {"id": 123456789, "first_name": "Attacker", "is_bot": False},
            "text": "/start"
        })
        is_allowed = bot_auditor.check_authorization(mock_bot, fake_msg)
        self.assertFalse(is_allowed)
        self.assertTrue(mock_bot.reply_to.called)
        args, _ = mock_bot.reply_to.call_args
        self.assertIn("từ chối", str(args[1]).lower())

        # Test authorized admin
        fake_admin_msg = telebot.types.Message.de_json({
            "message_id": 10000,
            "date": 1700000000,
            "chat": {"id": 8546576092, "type": "private"},
            "from": {"id": 8546576092, "first_name": "Admin", "is_bot": False},
            "text": "/start"
        })
        is_admin_allowed = bot_auditor.check_authorization(mock_bot, fake_admin_msg)
        self.assertTrue(is_admin_allowed)

    def test_11_health_endpoint_response(self):
        """Unified server health endpoint returns 200 when components ready."""
        launcher.HEALTH_STATE["bot1_ready"] = True
        launcher.HEALTH_STATE["bot2_ready"] = True
        launcher.HEALTH_STATE["start_time"] = time.time() - 100

        handler = launcher.UnifiedServerHandler.__new__(launcher.UnifiedServerHandler)
        handler.path = "/healthz"
        
        mock_wfile = MagicMock()
        handler.wfile = mock_wfile
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        handler.send_response.assert_called_with(200)

    def test_12_webhook_secret_fail_fast_on_missing_in_prod(self):
        """Production must fail-fast if WEBHOOK_SECRET_TOKEN is missing."""
        with patch.dict(os.environ, {"RENDER": "true", "WEBHOOK_SECRET_TOKEN": ""}):
            with self.assertRaises(SystemExit) as cm:
                launcher.get_webhook_secret_token()
            self.assertEqual(cm.exception.code, 1)

    def test_13_webhook_secret_uses_independent_env_secret(self):
        """Production uses independent secret from env, zero derivation from bot token."""
        with patch.dict(os.environ, {"WEBHOOK_SECRET_TOKEN": "independent_random_secret_token_123456"}):
            token = launcher.get_webhook_secret_token()
            self.assertEqual(token, "independent_random_secret_token_123456")

    def test_14_persistence_sync_and_restart_simulation(self):
        """Durable Persistence: State is encrypted, synced to remote store, and restored after container wipe."""
        fake_remote_kv = {}

        def fake_post(url, data=None, timeout=None):
            key = url.split("/")[-1]
            fake_remote_kv[key] = data
            m = MagicMock()
            m.status_code = 200
            return m

        def fake_get(url, timeout=None):
            key = url.split("/")[-1]
            m = MagicMock()
            if key in fake_remote_kv:
                m.status_code = 200
                m.content = fake_remote_kv[key]
            else:
                m.status_code = 404
                m.content = b""
            return m

        test_backlog = "test_backlog_durable.md"
        test_state = "test_state_durable.json"

        initial_content = """# MASTER BACKLOG
| Task ID | Ngày | Công Nghệ | Trụ Cột | Việc Cần Làm | Ưu Tiên | Trạng Thái | Link |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| TASK-20260913-0099 | 2026-09-13 10:00 | DurableTool | Core | Test Persistence | 🔴 P1 | [ ] Chờ làm | [Link](a) |
"""
        with open(test_backlog, "w", encoding="utf-8") as f:
            f.write(initial_content)

        try:
            with patch.dict(os.environ, {"PERSISTENCE_STORE_URL": "https://mock.kv/store"}), \
                 patch("requests.post", side_effect=fake_post), \
                 patch("requests.get", side_effect=fake_get):

                # 1. Sync to remote
                mgr = BacklogManager(test_backlog)
                res = mgr.mark_task_completed("TASK-20260913-0099")
                self.assertTrue(res)
                self.assertIn("backlog", fake_remote_kv)

                # 2. Simulate Render Restart (Container wiped, local file deleted)
                os.remove(test_backlog)
                self.assertFalse(os.path.exists(test_backlog))

                # 3. Simulate Container Boot (restore_all called)
                restored = DurablePersistenceAdapter.restore_all(".", test_backlog, test_state)
                self.assertTrue(restored)
                self.assertTrue(os.path.exists(test_backlog))

                with open(test_backlog, "r", encoding="utf-8") as f:
                    restored_content = f.read()

                self.assertIn("TASK-20260913-0099", restored_content)
                self.assertIn("[x] Hoàn thành", restored_content)
        finally:
            if os.path.exists(test_backlog):
                os.remove(test_backlog)
            if os.path.exists(test_state):
                os.remove(test_state)

    def test_15_reminders_permanently_deactivated(self):
        """Founder Directive: Proactive reminders and background workers must be permanently disabled."""
        from escalation_system import dispatch_overdue_alerts, start_proactive_escalation_worker, RemindedStateManager
        # 1. dispatch_overdue_alerts must return empty list even when forced
        alerted = dispatch_overdue_alerts(bot=MagicMock(), backlog_path="00_ACTION_BACKLOG.md", base_dir=".", force=True)
        self.assertEqual(alerted, [])

        # 2. start_proactive_escalation_worker must return None without spawning threads
        worker = start_proactive_escalation_worker(bot=MagicMock(), backlog_path="00_ACTION_BACKLOG.md", base_dir=".")
        self.assertIsNone(worker)

        # 3. RemindedStateManager must permanently report disabled and refuse to remind
        state_mgr = RemindedStateManager(".")
        self.assertFalse(state_mgr.is_enabled())
        self.assertFalse(state_mgr.should_remind("TASK-20260910-001"))

    def test_16_saved_storage_endpoints(self):
        """Centralized Storage: Authenticated access to /saved, /saved.csv, and /saved.json endpoints work correctly."""
        handler = launcher.UnifiedServerHandler.__new__(launcher.UnifiedServerHandler)
        mock_wfile = MagicMock()
        handler.wfile = mock_wfile
        handler.headers = {"Authorization": "Bearer dev_secret_local_only_12345"}
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        # 1. Test /saved (HTML table)
        handler.path = "/saved"
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/html; charset=utf-8")
        written_html = b"".join([call.args[0] for call in mock_wfile.write.call_args_list]).decode("utf-8")
        self.assertIn("Kho Lưu Trữ Công Nghệ & Giải Pháp", written_html)
        self.assertIn("TASK-20260910-001", written_html)

        # 2. Test /saved.csv
        mock_wfile.reset_mock()
        handler.send_response.reset_mock()
        handler.send_header.reset_mock()
        handler.path = "/saved.csv"
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/csv; charset=utf-8")
        written_csv = b"".join([call.args[0] for call in mock_wfile.write.call_args_list]).decode("utf-8")
        self.assertIn("Mã Lưu Trữ", written_csv)
        self.assertIn("TASK-20260910-001", written_csv)

        # 3. Test /saved.json
        mock_wfile.reset_mock()
        handler.send_response.reset_mock()
        handler.send_header.reset_mock()
        handler.path = "/saved.json"
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "application/json; charset=utf-8")
        written_json = b"".join([call.args[0] for call in mock_wfile.write.call_args_list]).decode("utf-8")
        import json
        records = json.loads(written_json)
        self.assertIsInstance(records, list)
        self.assertGreaterEqual(len(records), 5)

    def test_17_data_integrity_and_preservation(self):
        """Data Preservation: All 5 historic tasks are preserved without duplicate IDs or data loss."""
        from escalation_system import BacklogParser
        records, _ = BacklogParser.parse_file("00_ACTION_BACKLOG.md")
        task_ids = [r.task_id for r in records]

        # 1. Verify no duplicates
        self.assertEqual(len(task_ids), len(set(task_ids)))

        # 2. Verify all 5 historical tasks present
        expected_ids = [
            "TASK-20260910-001",
            "TASK-20260910-002",
            "TASK-20260911-003",
            "TASK-20260911-9695",
            "TASK-20260911-1026"
        ]
        for exp_id in expected_ids:
            self.assertIn(exp_id, task_ids)

    def test_18_anonymous_archive_access_denied(self):
        """Privacy: Anonymous access to /saved, /saved.csv, /saved.json, /backlog, /backlog.csv is strictly DENIED (401)."""
        handler = launcher.UnifiedServerHandler.__new__(launcher.UnifiedServerHandler)
        mock_wfile = MagicMock()
        handler.wfile = mock_wfile
        handler.headers = {}
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        endpoints_to_test = [
            "/saved",
            "/saved.csv",
            "/saved.json",
            "/backlog",
            "/backlog.csv",
            "/backlog.json"
        ]
        for endpoint in endpoints_to_test:
            mock_wfile.reset_mock()
            handler.send_response.reset_mock()
            handler.send_header.reset_mock()
            handler.path = endpoint
            handler.do_GET()

            # Must return 401 Unauthorized
            handler.send_response.assert_called_with(401)
            handler.send_header.assert_any_call("WWW-Authenticate", 'Basic realm="VibeCheck Private Archive"')
            written = b"".join([call.args[0] for call in mock_wfile.write.call_args_list]).decode("utf-8")
            self.assertIn("Unauthorized", written)
            self.assertNotIn("TASK-20260910-001", written)

    def test_19_founder_multi_auth_methods(self):
        """Founder Access: Multiple convenient auth methods (Bearer, Basic, X-Auth-Token, ?token=) all grant access (200)."""
        import base64
        secret = "dev_secret_local_only_12345"

        handler = launcher.UnifiedServerHandler.__new__(launcher.UnifiedServerHandler)
        mock_wfile = MagicMock()
        handler.wfile = mock_wfile
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        # 1. Bearer Header
        handler.headers = {"Authorization": f"Bearer {secret}"}
        handler.path = "/saved"
        handler.do_GET()
        handler.send_response.assert_called_with(200)

        # 2. Basic Auth Header
        mock_wfile.reset_mock()
        handler.send_response.reset_mock()
        b64_creds = base64.b64encode(f"founder:{secret}".encode("utf-8")).decode("utf-8")
        handler.headers = {"Authorization": f"Basic {b64_creds}"}
        handler.path = "/saved"
        handler.do_GET()
        handler.send_response.assert_called_with(200)

        # 3. X-Auth-Token Header
        mock_wfile.reset_mock()
        handler.send_response.reset_mock()
        handler.headers = {"X-Auth-Token": secret}
        handler.path = "/saved.csv"
        handler.do_GET()
        handler.send_response.assert_called_with(200)

        # 4. Query param ?token=
        mock_wfile.reset_mock()
        handler.send_response.reset_mock()
        handler.headers = {}
        handler.path = f"/saved?token={secret}"
        handler.do_GET()
        handler.send_response.assert_called_with(200)

        # 5. Query param ?key=
        mock_wfile.reset_mock()
        handler.send_response.reset_mock()
        handler.headers = {}
        handler.path = f"/saved.json?key={secret}"
        handler.do_GET()
        handler.send_response.assert_called_with(200)

if __name__ == "__main__":
    unittest.main()

