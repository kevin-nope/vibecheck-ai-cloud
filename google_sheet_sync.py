# -*- coding: utf-8 -*-
"""
VIBECHECK CTO — GOOGLE SHEET SYNC ADAPTER (ENTERPRISE ISOLATED)
Guarantees:
1. Canonical Source of Truth remains 00_ACTION_BACKLOG.md.
2. Google Sheet is a synchronized viewing layer only.
3. 100% Failure Isolation: Google API network timeout, quota (429), or 5xx
   will NEVER crash Bot CTO, Bot Red Team, or Telegram webhook.
4. Idempotent & Deduplicated: Every task is indexed by task_id.
5. Offline / Pending Queue: Failed syncs are stored locally and retried.
6. Schema-Compliant: ID, Ngày lưu, Nguồn, Tiêu đề, Tóm tắt, Kết quả Red Team, Ghi chú.
7. Zero Reminder Impact: Never triggers, spawns, or touches reminder threads.
"""

import os
import sys
import json
import time
import logging
import threading
import concurrent.futures
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("GoogleSheetSync")
logger.setLevel(logging.INFO)

SHEET_COLUMNS = [
    "ID",
    "Ngày lưu",
    "Nguồn",
    "Tiêu đề",
    "Tóm tắt",
    "Kết quả Red Team",
    "Ghi chú"
]

QUEUE_FILE = ".sheet_sync_queue.json"


class GoogleSheetSyncAdapter:
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="GSheetSyncWorker")
    _lock = threading.Lock()
    _cached_service = None

    @classmethod
    def get_sheet_id(cls) -> Optional[str]:
        return os.getenv("GOOGLE_SHEET_ID", "").strip() or None

    @classmethod
    def get_sheet_url(cls) -> Optional[str]:
        sheet_id = cls.get_sheet_id()
        if sheet_id:
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        return os.getenv("GOOGLE_SHEET_URL", "").strip() or None

    @classmethod
    def is_configured(cls) -> bool:
        has_sa = bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"))
        has_oauth = bool(os.getenv("GOOGLE_REFRESH_TOKEN") and os.getenv("GOOGLE_CLIENT_ID"))
        has_webhook = bool(os.getenv("GOOGLE_SHEET_WEBHOOK_URL"))
        has_sheet = bool(cls.get_sheet_id() or cls.get_sheet_url())
        return (has_sa or has_oauth or has_webhook) and (has_sheet or has_webhook)

    @classmethod
    def _get_service(cls):
        with cls._lock:
            if cls._cached_service:
                return cls._cached_service
            try:
                from googleapiclient.discovery import build
                from google.oauth2 import service_account, credentials

                sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
                if sa_json:
                    try:
                        import base64
                        if sa_json.startswith("ey"):
                            sa_json = base64.b64decode(sa_json).decode("utf-8")
                        info = json.loads(sa_json)
                        creds = service_account.Credentials.from_service_account_info(
                            info,
                            scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                        )
                        cls._cached_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
                        logger.info("✅ GoogleSheetSync: Connected via Service Account JSON env.")
                        return cls._cached_service
                    except Exception as sae:
                        logger.error(f"Failed to load GOOGLE_SERVICE_ACCOUNT_JSON: {sae}")

                sa_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
                if sa_file and os.path.exists(sa_file):
                    creds = service_account.Credentials.from_service_account_file(
                        sa_file,
                        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
                    )
                    cls._cached_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
                    logger.info("✅ GoogleSheetSync: Connected via Service Account File.")
                    return cls._cached_service

                refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
                client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
                client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
                if refresh_token and client_id and client_secret:
                    creds = credentials.Credentials(
                        token=None,
                        refresh_token=refresh_token,
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=client_id,
                        client_secret=client_secret,
                        scopes=["https://www.googleapis.com/auth/spreadsheets"]
                    )
                    cls._cached_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
                    logger.info("✅ GoogleSheetSync: Connected via OAuth Refresh Token.")
                    return cls._cached_service
            except Exception as e:
                logger.warning(f"GoogleSheetSync: Cannot build Google Sheets client: {e}")
            return None

    @classmethod
    def record_to_row(cls, record: Any) -> List[str]:
        if hasattr(record, "task_id"):
            task_id = record.task_id
            date_str = record.date_str
            tool_name = record.tool_name
            pillar = record.pillar
            action_item = record.action_item
            status = record.status
            report_link = record.report_link
        elif isinstance(record, (list, tuple)) and len(record) >= 7:
            return list(record)
        elif isinstance(record, dict):
            task_id = record.get("task_id", "")
            date_str = record.get("date_str", "") or record.get("date", "")
            tool_name = record.get("tool_name", "") or record.get("name", "")
            pillar = record.get("pillar", "")
            action_item = record.get("action_item", "")
            status = record.get("status", "")
            report_link = record.get("link", "") or record.get("file_link", "") or record.get("report_link", "")
        else:
            return ["", "", "", "", "", "", ""]

        st_lower = status.lower()
        if "đình chỉ" in st_lower or "[-]" in st_lower:
            redteam_result = "🔴 Đã đình chỉ (Cancelled)"
        elif "thay thế" in st_lower or "[~]" in st_lower:
            redteam_result = "↺ Đã thay thế (Superseded)"
        elif "hoàn thành" in st_lower or "[x]" in st_lower:
            redteam_result = "🟢 Hoàn thành / Duy trì nghiêm ngặt"
        else:
            redteam_result = "🟢 Phê duyệt lưu trữ (Approved)"

        clean_notes = f"{status} | {report_link}".strip(" |")
        return [task_id, date_str, tool_name, pillar, action_item, redteam_result, clean_notes]

    @classmethod
    def sync_record_async(cls, record: Any):
        if not cls.is_configured():
            return
        cls._executor.submit(cls.sync_record, record)

    @classmethod
    def sync_record(cls, record: Any) -> bool:
        try:
            row_data = cls.record_to_row(record)
            task_id = row_data[0]
            if not task_id:
                return False

            webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
            if webhook_url:
                return cls._sync_via_webhook(row_data)

            service = cls._get_service()
            sheet_id = cls.get_sheet_id()
            if not service or not sheet_id:
                if cls.is_configured():
                    cls._enqueue_pending(task_id, row_data)
                return False

            return cls._sync_via_api(service, sheet_id, row_data)
        except Exception as e:
            logger.warning(f"GoogleSheetSync: Transient error during sync: {e}")
            try:
                if 'task_id' in locals() and task_id and 'row_data' in locals():
                    cls._enqueue_pending(task_id, row_data)
            except Exception:
                pass
            return False

    @classmethod
    def _sync_via_webhook(cls, row_data: List[str]) -> bool:
        import urllib.request
        webhook_url = os.getenv("GOOGLE_SHEET_WEBHOOK_URL", "").strip()
        if not webhook_url:
            return False

        payload = json.dumps({
            "action": "upsert",
            "task_id": row_data[0],
            "row": row_data,
            "secret": os.getenv("GOOGLE_SHEET_WEBHOOK_SECRET", "")
        }).encode("utf-8")

        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "VibeCheck-CTO-Sync/1.0"
            },
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                body = res.read().decode("utf-8")
                try:
                    resp_json = json.loads(body)
                    app_status = resp_json.get("status")
                    app_code = resp_json.get("code")
                    if app_status == "success" or (app_code == 200 and app_status != "error"):
                        logger.info(f"✅ GoogleSheetSync: Synced {row_data[0]} via Webhook.")
                        return True
                    else:
                        logger.warning(f"GoogleSheetSync: Webhook application-level error (code: {app_code}, status: {app_status}, message: {resp_json.get('message')})")
                        cls._enqueue_pending(row_data[0], row_data)
                        return False
                except Exception:
                    if res.status in (200, 201):
                        logger.info(f"✅ GoogleSheetSync: Synced {row_data[0]} via Webhook (transport status {res.status}).")
                        return True
        except Exception as e:
            logger.warning(f"GoogleSheetSync: Webhook request failed: {e}")
            cls._enqueue_pending(row_data[0], row_data)
            return False
        return False

    @classmethod
    def _sync_via_api(cls, service, sheet_id: str, row_data: List[str]) -> bool:
        task_id = row_data[0]
        sheet_range = "Sheet1!A:G"

        result = service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=sheet_range
        ).execute()

        rows = result.get("values", [])
        if not rows:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Sheet1!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [SHEET_COLUMNS]}
            ).execute()
            rows = [SHEET_COLUMNS]

        target_row_idx = None
        for idx, r in enumerate(rows):
            if r and r[0] == task_id:
                target_row_idx = idx + 1
                break

        if target_row_idx:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=f"Sheet1!A{target_row_idx}:G{target_row_idx}",
                valueInputOption="USER_ENTERED",
                body={"values": [row_data]}
            ).execute()
            logger.info(f"✅ GoogleSheetSync: Updated existing row for {task_id} in Sheet (row {target_row_idx}).")
        else:
            service.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range="Sheet1!A1",
                valueInputOption="USER_ENTERED",
                body={"values": [row_data]}
            ).execute()
            logger.info(f"✅ GoogleSheetSync: Appended new row for {task_id} in Sheet.")

        return True

    @classmethod
    def migrate_backlog(cls, backlog_file: str) -> Tuple[int, int]:
        from escalation_system import BacklogParser
        if not os.path.exists(backlog_file):
            return 0, 0

        records, _ = BacklogParser.parse_file(backlog_file)
        success = 0
        for r in records:
            if cls.sync_record(r):
                success += 1

        logger.info(f"GoogleSheetSync Migration: {success}/{len(records)} records synced.")
        return success, len(records)

    @classmethod
    def _enqueue_pending(cls, task_id: str, row_data: List[str]):
        with cls._lock:
            for attempt in range(5):
                try:
                    queue = {}
                    if os.path.exists(QUEUE_FILE):
                        try:
                            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                                queue = json.load(f)
                        except Exception:
                            queue = {}
                    queue[task_id] = row_data
                    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
                    logger.info(f"GoogleSheetSync: Enqueued {task_id} to offline queue ({len(queue)} pending).")
                    break
                except (PermissionError, OSError):
                    time.sleep(0.05)
                except Exception as e:
                    logger.warning(f"Could not enqueue pending sync: {e}")
                    break

    @classmethod
    def flush_pending_queue(cls) -> int:
        if not os.path.exists(QUEUE_FILE):
            return 0
        try:
            with cls._lock:
                with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                    queue = json.load(f)
            if not queue:
                return 0

            flushed = 0
            remaining = {}
            for task_id, row_data in queue.items():
                if cls.sync_record(row_data):
                    flushed += 1
                else:
                    remaining[task_id] = row_data

            with cls._lock:
                with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                    json.dump(remaining, f, ensure_ascii=False, indent=2)

            return flushed
        except Exception as e:
            logger.warning(f"Error flushing pending sync queue: {e}")
            return 0
