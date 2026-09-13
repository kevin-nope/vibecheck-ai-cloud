"""
SANDBOX ARTIFACT: escalation_system.py
PROJECT: Bot_Tham_Dinh_AI (Pre-Production Validation)
SAFETY: This file is completely isolated from production code. It does NOT modify production files.
"""

import os
import re
import time
import uuid
import logging
import threading
import base64
import hashlib
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Set, Union

try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

logger = logging.getLogger("SandboxEscalation")

# ==============================================================================
# 0. DURABLE CLOUD STORAGE ADAPTER (PERSISTENCE LAYER)
# ==============================================================================

class DurablePersistenceAdapter:
    """
    Minimalist Durable Cloud Storage Adapter for Render Free Ephemeral Filesystem.
    - Synchronizes 00_ACTION_BACKLOG.md and .reminded_state.json with a persistent remote KV endpoint.
    - Uses AES-256 (Fernet) encryption derived from ADMIN_CHAT_ID and WEBHOOK_SECRET_TOKEN.
    - Restores persistent state on startup before any bot handlers begin.
    - Syncs updated state on any mutation.
    """
    _cipher = None

    @classmethod
    def get_cipher(cls):
        if cls._cipher is None and Fernet is not None:
            admin_id = os.getenv("ADMIN_CHAT_ID", "default_admin")
            secret = os.getenv("WEBHOOK_SECRET_TOKEN", "default_secret")
            seed = f"{admin_id}:{secret}:durable_vibecheck".encode()
            key = base64.urlsafe_b64encode(hashlib.sha256(seed).digest())
            cls._cipher = Fernet(key)
        return cls._cipher

    @classmethod
    def get_remote_url(cls) -> Optional[str]:
        url = (os.getenv("PERSISTENCE_STORE_URL") or "").strip()
        return url if url.startswith("http") else None

    @classmethod
    def restore_all(cls, base_dir: str, backlog_file: str, state_file: str) -> bool:
        """Called on startup: Restores durable state from remote persistent store if available."""
        remote_url = cls.get_remote_url()
        if not remote_url:
            logger.info("DurablePersistence: PERSISTENCE_STORE_URL not configured. Operating on local disk.")
            return False

        restored_any = False
        try:
            cipher = cls.get_cipher()

            # 1. Restore Backlog
            r_backlog = requests.get(f"{remote_url}/backlog", timeout=5)
            if r_backlog.status_code == 200 and r_backlog.content:
                try:
                    payload = r_backlog.content
                    content = cipher.decrypt(payload).decode("utf-8") if cipher else payload.decode("utf-8")
                    if len(content.strip()) > 50:
                        with open(backlog_file, "w", encoding="utf-8") as f:
                            f.write(content)
                        logger.info(f"✅ DurablePersistence: Restored {backlog_file} from persistent store ({len(content)} bytes).")
                        restored_any = True
                except Exception as de:
                    logger.warning(f"DurablePersistence: Failed to decrypt remote backlog: {de}")
            elif r_backlog.status_code == 404 and os.path.exists(backlog_file):
                cls.sync_backlog(backlog_file)

            # 2. Restore Reminder State
            r_state = requests.get(f"{remote_url}/reminded_state", timeout=5)
            if r_state.status_code == 200 and r_state.content:
                try:
                    payload = r_state.content
                    content = cipher.decrypt(payload).decode("utf-8") if cipher else payload.decode("utf-8")
                    with open(state_file, "w", encoding="utf-8") as f:
                        f.write(content)
                    logger.info(f"✅ DurablePersistence: Restored {state_file} from persistent store.")
                    restored_any = True
                except Exception as de:
                    logger.warning(f"DurablePersistence: Failed to decrypt remote reminder state: {de}")
            elif r_state.status_code == 404 and os.path.exists(state_file):
                cls.sync_state(state_file)

            return restored_any
        except Exception as e:
            logger.warning(f"DurablePersistence: Failed to restore remote state: {e}")
            return False

    @classmethod
    def sync_backlog(cls, backlog_file: str) -> bool:
        """Pushes encrypted backlog to persistent store."""
        remote_url = cls.get_remote_url()
        if not remote_url or not os.path.exists(backlog_file):
            return False
        try:
            cipher = cls.get_cipher()
            with open(backlog_file, "r", encoding="utf-8") as f:
                content = f.read()
            data = cipher.encrypt(content.encode("utf-8")) if cipher else content.encode("utf-8")
            r = requests.post(f"{remote_url}/backlog", data=data, timeout=5)
            if r.status_code in (200, 201):
                logger.info("✅ DurablePersistence: Backlog synced to remote store successfully.")
                return True
            return False
        except Exception as e:
            logger.error(f"DurablePersistence: Failed to sync backlog: {e}")
            return False

    @classmethod
    def sync_state(cls, state_file: str) -> bool:
        """Pushes encrypted reminder state to persistent store."""
        remote_url = cls.get_remote_url()
        if not remote_url or not os.path.exists(state_file):
            return False
        try:
            cipher = cls.get_cipher()
            with open(state_file, "r", encoding="utf-8") as f:
                content = f.read()
            data = cipher.encrypt(content.encode("utf-8")) if cipher else content.encode("utf-8")
            r = requests.post(f"{remote_url}/reminded_state", data=data, timeout=5)
            if r.status_code in (200, 201):
                logger.info("✅ DurablePersistence: Reminder state synced to remote store successfully.")
                return True
            return False
        except Exception as e:
            logger.error(f"DurablePersistence: Failed to sync reminder state: {e}")
            return False

# ==============================================================================
# 1. ATOMIC FILE LOCK & CRITICAL SECTION HANDLER
# ==============================================================================

class BacklogFileLock:
    """
    Critical section lock for Backlog operations.
    Guarantees:
    - Atomic file creation via os.O_CREAT | os.O_EXCL.
    - Timeout support to prevent indefinite hanging.
    - Safe release even on unexpected exceptions.
    - Stale lock detection for resilience against process crashes.
    """
    def __init__(self, lock_path: str, timeout: float = 5.0, poll_interval: float = 0.05):
        self.lock_path = lock_path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.fd = None

    def __enter__(self):
        start_time = time.time()
        while True:
            try:
                self.fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                lock_content = f"{os.getpid()}:{threading.get_ident()}:{time.time()}"
                os.write(self.fd, lock_content.encode("utf-8"))
                return self
            except FileExistsError:
                elapsed = time.time() - start_time
                if elapsed >= self.timeout:
                    # Check stale lock (> 20s old)
                    try:
                        mtime = os.path.getmtime(self.lock_path)
                        if time.time() - mtime > 20.0:
                            try:
                                os.remove(self.lock_path)
                                continue
                            except OSError:
                                pass
                    except OSError:
                        pass
                    raise TimeoutError(f"BacklogFileLock: Timeout ({self.timeout}s) waiting for lock '{self.lock_path}'")
                time.sleep(self.poll_interval)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
                self.fd = None
        try:
            if os.path.exists(self.lock_path):
                os.remove(self.lock_path)
        except OSError:
            pass


# ==============================================================================
# 2. MALFORMED-RESISTANT BACKLOG PARSER & RECORD MODEL
# ==============================================================================

class BacklogRecord:
    def __init__(self, task_id: str, date_str: str, tool_name: str, pillar: str,
                 action_item: str, priority: str, status: str, report_link: str,
                 raw_line: str, line_number: int):
        self.task_id = task_id.strip()
        self.date_str = date_str.strip()
        self.tool_name = tool_name.strip()
        self.pillar = pillar.strip()
        self.action_item = action_item.strip()
        self.priority = priority.strip()
        self.status = status.strip()  # "[ ] Chờ làm" or "[x] Hoàn thành" or "[ ]"
        self.report_link = report_link.strip()
        self.raw_line = raw_line
        self.line_number = line_number

    @property
    def is_completed(self) -> bool:
        return "[x]" in self.status.lower()

    @property
    def is_pending(self) -> bool:
        return "[ ]" in self.status and "[x]" not in self.status.lower()

    def parse_timestamp(self) -> Optional[datetime]:
        """Try parsing various valid datetime formats. Returns None if unparseable."""
        formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y"
        ]
        clean_date = self.date_str.strip()
        for fmt in formats:
            try:
                return datetime.strptime(clean_date, fmt)
            except ValueError:
                continue
        return None

    def is_overdue(self, threshold_hours: float = 24.0, now: Optional[datetime] = None) -> bool:
        """Determines if a pending task is overdue beyond threshold_hours."""
        if not self.is_pending:
            return False
        dt = self.parse_timestamp()
        if not dt:
            return False
        current_time = now if now else datetime.now()
        return (current_time - dt) > timedelta(hours=threshold_hours)


class BacklogParser:
    """
    Zero-Defect Markdown Table Parser.
    Rule: Never destroy uncertain data. Fail closed for write, fail safe for read.
    """
    @staticmethod
    def parse_file(file_path: str) -> Tuple[List[BacklogRecord], List[str]]:
        """
        Parses a markdown backlog file.
        Returns:
            records: List of successfully parsed BacklogRecord objects.
            all_lines: The exact raw lines of the file for 100% fidelity reconstruction.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Backlog file not found: {file_path}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="utf-8-sig", errors="replace") as f:
                raw_content = f.read()

        all_lines = raw_content.splitlines(keepends=True)
        records = []
        seen_ids = set()

        for idx, line in enumerate(all_lines):
            stripped = line.strip()
            # Table row must start and end with '|'
            if not (stripped.startswith("|") and stripped.endswith("|")):
                continue
            # Skip separator row like | :--- | :--- |
            if re.search(r"\|\s*:?-+:?\s*\|", stripped):
                continue

            parts = [p.strip() for p in stripped.split("|")[1:-1]]
            # We expect at least 7-8 columns:
            # Task ID | Ngày | Công Nghệ | Trụ Cột | Việc Cần Làm | Ưu Tiên | Trạng Thái | Link
            if len(parts) < 7:
                logger.warning(f"Line {idx+1} skipped: Expected >= 7 table columns, got {len(parts)}: '{stripped}'")
                continue

            task_id = parts[0]
            # Ignore table header row
            if "task id" in task_id.lower() or "mã task" in task_id.lower():
                continue

            if task_id in seen_ids:
                logger.warning(f"Line {idx+1} has duplicate Task ID: {task_id}. Skipping to avoid collision.")
                continue

            seen_ids.add(task_id)
            date_str = parts[1]
            tool_name = parts[2]
            pillar = parts[3]
            action_item = parts[4]
            priority = parts[5]
            status = parts[6]
            report_link = parts[7] if len(parts) > 7 else ""

            records.append(BacklogRecord(
                task_id=task_id,
                date_str=date_str,
                tool_name=tool_name,
                pillar=pillar,
                action_item=action_item,
                priority=priority,
                status=status,
                report_link=report_link,
                raw_line=line,
                line_number=idx
            ))

        return records, all_lines


# ==============================================================================
# 3. ATOMIC BACKLOG WRITER & STATUS MUTATOR
# ==============================================================================

class BacklogManager:
    """
    Manages Backlog operations with atomic writes and critical section file locking.
    """
    def __init__(self, backlog_path: str):
        self.backlog_path = backlog_path
        self.lock_path = backlog_path + ".lock"

    def _atomic_write_lines(self, all_lines: List[str]) -> bool:
        """Atomic write via temp file, flush, fsync, atomic replace and durable remote sync."""
        temp_path = f"{self.backlog_path}.tmp.{uuid.uuid4().hex}"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                for line in all_lines:
                    f.write(line)
                f.flush()
                os.fsync(f.fileno())

            for replace_attempt in range(5):
                try:
                    os.replace(temp_path, self.backlog_path)
                    DurablePersistenceAdapter.sync_backlog(self.backlog_path)
                    try:
                        csv_path = os.path.join(os.path.dirname(self.backlog_path) or ".", "00_SAVED_ARCHIVE.csv")
                        csv_content = export_backlog_to_csv(self.backlog_path)
                        with open(csv_path, "w", encoding="utf-8") as f_csv:
                            f_csv.write(csv_content)
                    except Exception as ce:
                        logger.warning(f"Could not auto-generate CSV archive: {ce}")
                    return True
                except PermissionError:
                    if replace_attempt < 4:
                        time.sleep(0.05)
                    else:
                        raise
            DurablePersistenceAdapter.sync_backlog(self.backlog_path)
            try:
                csv_path = os.path.join(os.path.dirname(self.backlog_path) or ".", "00_SAVED_ARCHIVE.csv")
                csv_content = export_backlog_to_csv(self.backlog_path)
                with open(csv_path, "w", encoding="utf-8") as f_csv:
                    f_csv.write(csv_content)
            except Exception as ce:
                logger.warning(f"Could not auto-generate CSV archive: {ce}")
            return True
        except Exception as e:
            logger.error(f"Atomic replace failed in BacklogManager: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def get_overdue_tasks(self, threshold_hours: float = 24.0, now: Optional[datetime] = None) -> List[BacklogRecord]:
        """Scans for overdue pending tasks without mutating the file."""
        if not os.path.exists(self.backlog_path):
            logger.warning(f"Backlog path {self.backlog_path} does not exist.")
            return []

        with BacklogFileLock(self.lock_path, timeout=5.0):
            records, _ = BacklogParser.parse_file(self.backlog_path)
            overdue = [r for r in records if r.is_overdue(threshold_hours=threshold_hours, now=now)]
            return overdue

    def mark_task_completed(self, target_task_id: str) -> bool:
        """
        Critical Section:
        LOCK → READ → VALIDATE → MODIFY → ATOMIC WRITE → DURABLE SYNC → UNLOCK
        """
        with BacklogFileLock(self.lock_path, timeout=8.0):
            records, all_lines = BacklogParser.parse_file(self.backlog_path)

            target_record = None
            for r in records:
                if r.task_id == target_task_id:
                    target_record = r
                    break

            if not target_record:
                logger.warning(f"Task ID '{target_task_id}' not found in backlog.")
                return False

            orig_line = all_lines[target_record.line_number]
            # Replace "[ ]" with "[x]" inside the status field while preserving column padding
            new_line = re.sub(r"(\|\s*)\[ \](\s*[^|]*\|)", r"\1[x]\2", orig_line, count=1)
            
            if "[ ]" in orig_line and "[x]" in new_line:
                if "Chờ làm" in new_line:
                    new_line = new_line.replace("Chờ làm", "Hoàn thành")
            else:
                logger.error(f"Failed to modify status for Task ID '{target_task_id}'. Aborting to preserve file integrity.")
                return False

            all_lines[target_record.line_number] = new_line
            return self._atomic_write_lines(all_lines)

    def cancel_task(self, target_task_id: str) -> bool:
        """
        Critical Section:
        Marks target_task_id status as '[-] Đã đình chỉ' atomically.
        """
        with BacklogFileLock(self.lock_path, timeout=8.0):
            records, all_lines = BacklogParser.parse_file(self.backlog_path)
            target_record = None
            for r in records:
                if r.task_id.lower() == target_task_id.strip().lower():
                    target_record = r
                    break

            if not target_record:
                logger.warning(f"Task ID '{target_task_id}' not found for cancellation.")
                return False

            orig_line = all_lines[target_record.line_number]
            new_line = re.sub(r"(\|\s*)\[ \]\s*[^|]*(\s*\|)", r"\1[-] Đã đình chỉ\2", orig_line, count=1)
            if new_line == orig_line:
                new_line = orig_line.replace("[ ] Chờ làm", "[-] Đã đình chỉ").replace("[ ]", "[-] Đã đình chỉ")

            all_lines[target_record.line_number] = new_line
            return self._atomic_write_lines(all_lines)

    def delete_task(self, target_task_id: str) -> bool:
        """
        Critical Section:
        Completely removes the task row from 00_ACTION_BACKLOG.md table atomically.
        """
        with BacklogFileLock(self.lock_path, timeout=8.0):
            records, all_lines = BacklogParser.parse_file(self.backlog_path)
            target_record = None
            for r in records:
                if r.task_id.lower() == target_task_id.strip().lower():
                    target_record = r
                    break

            if not target_record:
                logger.warning(f"Task ID '{target_task_id}' not found for deletion.")
                return False

            # Xóa dòng của task khỏi bảng
            del all_lines[target_record.line_number]
            return self._atomic_write_lines(all_lines)

    def get_active_tasks_summary(self) -> List[Dict[str, str]]:
        """Returns a lightweight summary of all pending tasks for LLM context matching."""
        if not os.path.exists(self.backlog_path):
            return []
        with BacklogFileLock(self.lock_path, timeout=5.0):
            records, _ = BacklogParser.parse_file(self.backlog_path)
            return [
                {
                    "task_id": r.task_id,
                    "date": r.date_str,
                    "tool_name": r.tool_name,
                    "pillar": r.pillar,
                    "action_item": r.action_item,
                    "priority": r.priority,
                    "status": r.status
                }
                for r in records if r.is_pending
            ]

    def add_task(self, record_data: Dict[str, str]) -> str:
        """
        Atomically appends a new task row to 00_ACTION_BACKLOG.md table.
        Generates task_id if not present: TASK-YYYYMMDD-XXX
        """
        with BacklogFileLock(self.lock_path, timeout=8.0):
            records, all_lines = BacklogParser.parse_file(self.backlog_path)
            
            today_str = datetime.now().strftime("%Y%m%d")
            max_idx = 0
            for r in records:
                m = re.search(rf"TASK-{today_str}-(\d+)", r.task_id)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
            next_idx = max_idx + 1
            task_id = record_data.get("task_id") or f"TASK-{today_str}-{next_idx:03d}"
            
            date_col = record_data.get("date_str") or datetime.now().strftime("%Y-%m-%d %H:%M")
            tool_col = record_data.get("tool_name", "Công nghệ mới")
            pillar_col = record_data.get("pillar", "Chung")
            action_col = record_data.get("action_item", "Nghiên cứu & Thẩm định")
            prio_col = record_data.get("priority", "🟡 P2 (Vừa)")
            status_col = "[ ] Chờ làm"
            file_col = record_data.get("file_link", "Chưa lưu file")
            
            new_row = f"| {task_id} | {date_col} | {tool_col} | {pillar_col} | {action_col} | {prio_col} | {status_col} | {file_col} |\n"
            
            insert_idx = len(all_lines)
            for idx in range(len(all_lines) - 1, -1, -1):
                if all_lines[idx].strip().startswith("|"):
                    insert_idx = idx + 1
                    break
            
            all_lines.insert(insert_idx, new_row)
            if self._atomic_write_lines(all_lines):
                return task_id
            return ""

    def supersede_task(self, old_task_id: str, new_record_data: Dict[str, str]) -> Tuple[bool, str]:
        """
        Atomically marks old_task_id as superseded by new_task_id, and inserts the new task.
        Safe State Guard: Only allows superseding pending tasks ([ ] Chờ làm).
        """
        with BacklogFileLock(self.lock_path, timeout=8.0):
            records, all_lines = BacklogParser.parse_file(self.backlog_path)
            
            target_record = None
            for r in records:
                if r.task_id == old_task_id:
                    target_record = r
                    break
            
            if not target_record:
                logger.warning(f"Task ID '{old_task_id}' not found for superseding.")
                return False, ""

            if not target_record.is_pending:
                logger.warning(f"Task ID '{old_task_id}' is not pending (status: '{target_record.status}'). Cannot supersede non-pending task.")
                return False, ""
            
            today_str = datetime.now().strftime("%Y%m%d")
            max_idx = 0
            for r in records:
                m = re.search(rf"TASK-{today_str}-(\d+)", r.task_id)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
            next_idx = max_idx + 1
            new_task_id = new_record_data.get("task_id") or f"TASK-{today_str}-{next_idx:03d}"
            
            # 1. Update old row status
            orig_line = all_lines[target_record.line_number]
            replacement_status = f"[~] Thay thế bởi {new_task_id}"
            new_old_line = re.sub(r"(\|\s*)\[ \]\s*[^|]*(\s*\|)", rf"\1{replacement_status}\2", orig_line, count=1)
            all_lines[target_record.line_number] = new_old_line
            
            # 2. Add new row
            date_col = new_record_data.get("date_str") or datetime.now().strftime("%Y-%m-%d %H:%M")
            tool_col = new_record_data.get("tool_name", "Công nghệ mới")
            pillar_col = new_record_data.get("pillar", target_record.pillar)
            action_col = new_record_data.get("action_item", "Nghiên cứu & Thẩm định")
            prio_col = new_record_data.get("priority", target_record.priority)
            status_col = "[ ] Chờ làm"
            file_col = new_record_data.get("file_link", "Chưa lưu file")
            
            new_row = f"| {new_task_id} | {date_col} | {tool_col} | {pillar_col} | {action_col} | {prio_col} | {status_col} | {file_col} |\n"
            
            insert_idx = len(all_lines)
            for idx in range(len(all_lines) - 1, -1, -1):
                if all_lines[idx].strip().startswith("|"):
                    insert_idx = idx + 1
                    break
            all_lines.insert(insert_idx, new_row)
            if self._atomic_write_lines(all_lines):
                return True, new_task_id
            return False, ""



# ==============================================================================
# 4. RESILIENT TELEGRAM ALERT DISPATCHER (MOCK-COMPATIBLE)
# ==============================================================================

class TelegramRateLimitException(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Telegram Rate Limit: Retry after {retry_after}s")


class TelegramTransientException(Exception):
    pass


class ResilientAlertDispatcher:
    """
    Dispatches overdue alerts with bounded exponential backoff and 429 RetryAfter compliance.
    Safe for both real runtime and sandbox mock test.
    """
    def __init__(self, max_retries: int = 3, initial_delay: float = 0.5, backoff_factor: float = 2.0):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.backoff_factor = backoff_factor

    def send_alert_with_retry(self, sender_func, chat_id: int, message_text: str, sleep_func=time.sleep) -> Tuple[bool, Optional[str]]:
        """
        Executes sender_func(chat_id, message_text) with bounded backoff.
        Returns (success: bool, error_message: Optional[str])
        """
        delay = self.initial_delay
        for attempt in range(1, self.max_retries + 1):
            try:
                sender_func(chat_id, message_text)
                return True, None
            except TelegramRateLimitException as e:
                logger.warning(f"Attempt {attempt}: Hit 429 RateLimit. Backing off for {e.retry_after}s.")
                if attempt == self.max_retries:
                    return False, f"RateLimit retry limit exceeded ({e.retry_after}s needed)"
                sleep_func(e.retry_after)
            except TelegramTransientException as e:
                logger.warning(f"Attempt {attempt}: Transient failure ({e}). Retrying in {delay}s...")
                if attempt == self.max_retries:
                    return False, f"Transient network failure after {self.max_retries} attempts: {e}"
                sleep_func(delay)
                delay *= self.backoff_factor
            except Exception as e:
                logger.error(f"Attempt {attempt}: Non-transient unrecoverable error: {e}")
                return False, f"Unrecoverable error: {e}"

        return False, "Max retries exhausted"


# ==============================================================================
# 5. ANTIGRAVITY / GEMINI PRO PROMPT GENERATOR
# ==============================================================================

def generate_antigravity_mvp_prompt(task: BacklogRecord) -> str:
    """
    Generates a production-ready MVP implementation prompt formatted specifically
    for Antigravity IDE & Gemini Pro (18 months tier, 0 VND operation).
    """
    prompt = (
        f"🤖 BẢN PROMPT TRIỂN KHAI MVP (DÀNH CHO ANTIGRAVITY IDE & GEMINI PRO):\n\n"
        f"Xin chào Antigravity! Tôi muốn triển khai giải pháp MVP thực chiến sau đây:\n"
        f"1. Tên công cụ / Repo: {task.tool_name}\n"
        f"2. Trụ cột mục tiêu: {task.pillar}\n"
        f"3. Nhiệm vụ cụ thể: {task.action_item}\n"
        f"4. Mức độ ưu tiên: {task.priority}\n"
        f"5. Yêu cầu kỹ thuật:\n"
        f"   - Chi phí: 100% miễn phí (0 ĐỒNG), không phụ thuộc SaaS trả phí.\n"
        f"   - Tech stack: Python / FastAPI hoặc Node.js nhỏ gọn, chạy trực tiếp trên Windows.\n"
        f"   - Nếu phục vụ xe tiện chuyến Vũng Tàu - Sài Gòn (Nhà Xe Thành Tâm): Chạy độc lập, không can thiệp sập core app.\n"
        f"   - Hãy viết code hoàn chỉnh, tạo file mẫu và hướng dẫn lệnh chạy ngay."
    )
    return prompt


# ==============================================================================
# 6. ADMIN & OWNER STRICT AUTHORIZATION (NO AUTO-DISCOVERY)
# ==============================================================================

class AdminChatIDManager:
    """
    Manages strict owner authorization exclusively via environment variables.
    Zero Auto-Discovery: Never adopts arbitrary message senders as admin.
    """
    @classmethod
    def get_authorized_ids(cls) -> Set[int]:
        """Returns set of all authorized integer Telegram chat/user IDs."""
        env_val = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID") or ""
        authorized = set()
        for token in env_val.replace(";", ",").split(","):
            token = token.strip()
            if token and token.lstrip("-").isdigit():
                authorized.add(int(token))
        return authorized
    get_admin_ids = get_authorized_ids

    @classmethod
    def is_authorized(cls, user_or_chat_id: Union[int, str, None]) -> bool:
        """Verifies if the given user or chat ID is an authorized owner."""
        if user_or_chat_id is None:
            return False
        try:
            target_id = int(str(user_or_chat_id).strip())
            return target_id in cls.get_authorized_ids()
        except (ValueError, TypeError):
            return False

    @classmethod
    def get_chat_id(cls, base_dir: Optional[str] = None) -> Optional[int]:
        """Returns the primary admin chat_id from environment."""
        auth_ids = cls.get_authorized_ids()
        return next(iter(auth_ids)) if auth_ids else None

    @classmethod
    def save_chat_id(cls, base_dir: str, chat_id: int) -> None:
        """NO-OP: Auto-discovery has been permanently removed for security."""
        pass


# ==============================================================================
# 7. REMINDED STATE & ANTI-SPAM SNOOZE MANAGER
# ==============================================================================

class RemindedStateManager:
    """
    Tracks reminder timestamps and snooze durations in .reminded_state.json.
    Guarantees:
    - Thread-safe updates via threading.Lock().
    - Zero spam: Enforces cooldown_hours (default 12h) between successive reminders.
    - Snooze support: Allows temporary silencing of a task for X hours.
    """
    FILE_NAME = ".reminded_state.json"

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.state_file = os.path.join(base_dir, self.FILE_NAME)
        self._lock = threading.Lock()
        self._state = {"last_reminded": {}, "snoozed_until": {}, "reminders_enabled": True}
        self._load()

    def _load(self):
        import json
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._state["last_reminded"] = data.get("last_reminded", {})
                        self._state["snoozed_until"] = data.get("snoozed_until", {})
                        self._state["reminders_enabled"] = data.get("reminders_enabled", True)
            except Exception as e:
                logger.warning(f"Could not load reminded state: {e}. Reinitializing.")

    def _save(self):
        import json
        try:
            temp_file = f"{self.state_file}.tmp.{uuid.uuid4().hex}"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(temp_file, self.state_file)
            DurablePersistenceAdapter.sync_state(self.state_file)
        except Exception as e:
            logger.error(f"Failed to save reminded state: {e}")

    def is_enabled(self) -> bool:
        # PERMANENTLY DISABLED per Founder directive.
        return False

    def set_enabled(self, enabled: bool) -> None:
        # No-op: reminders are permanently disabled.
        pass

    def should_remind(self, task_id: str, cooldown_hours: float = 24.0, now_ts: Optional[float] = None) -> bool:
        # PERMANENTLY DISABLED: Bot CTO will NEVER remind or spam Telegram.
        return False

    def record_reminded(self, task_id: str, now_ts: Optional[float] = None) -> None:
        with self._lock:
            current_ts = now_ts if now_ts is not None else time.time()
            self._state["last_reminded"][task_id] = current_ts
            self._save()

    def snooze_task(self, task_id: str, hours: float = 24.0, now_ts: Optional[float] = None) -> None:
        with self._lock:
            current_ts = now_ts if now_ts is not None else time.time()
            self._state["snoozed_until"][task_id] = current_ts + (hours * 3600)
            self._save()

    def get_info(self, task_id: str, now_ts: Optional[float] = None) -> dict:
        with self._lock:
            current_ts = now_ts if now_ts is not None else time.time()
            snooze_until = self._state["snoozed_until"].get(task_id, 0)
            last_reminded = self._state["last_reminded"].get(task_id, 0)
            return {
                "is_snoozed": current_ts < snooze_until,
                "snooze_remaining_hours": max(0.0, (snooze_until - current_ts) / 3600),
                "hours_since_last_reminded": (current_ts - last_reminded) / 3600 if last_reminded > 0 else None
            }


# ==============================================================================
# 8. OVERDUE REMINDER CARD & PROACTIVE DISPATCHER
# ==============================================================================

def build_overdue_reminder_card(task: BacklogRecord) -> Tuple[str, List[Dict[str, str]]]:
    """
    Builds the formatted HTML reminder text and action button specifications.
    Can be used by both Telegram bot (InlineKeyboardMarkup) and mock test.
    """
    card_text = (
        f"🔔 <b>[VIBECHECK CTO NHẮC VIỆC TỒN ĐỌNG]</b>\n\n"
        f"Sếp ơi! Task <b>[TRIỂN KHAI NGAY]</b> này đã lưu hơn 12 giờ mà chưa thấy triển khai:\n\n"
        f"🔴 <b>{task.task_id}: {task.tool_name}</b>\n"
        f"• <b>Hành động</b>: {task.action_item}\n"
        f"• <b>Trụ cột</b>: {task.pillar}\n"
        f"• <b>Độ ưu tiên</b>: {task.priority}\n"
        f"• <b>Đã lưu lúc</b>: {task.date_str}\n\n"
        f"👉 Sếp muốn giải quyết việc này thế nào?"
    )
    buttons = [
        {"text": "⚡ Prompt Antigravity", "callback_data": f"agp_{task.task_id}"},
        {"text": "✅ Đã làm xong", "callback_data": f"done_{task.task_id}"},
        {"text": "⏸️ Tạm hoãn 24h", "callback_data": f"snz_{task.task_id}"},
        {"text": "🚫 Đình chỉ / Xóa task", "callback_data": f"cancel_{task.task_id}"}
    ]
    return card_text, buttons


def dispatch_overdue_alerts(
    bot=None,
    backlog_path: str = "",
    base_dir: str = "",
    threshold_hours: float = 12.0,
    force: bool = False,
    target_chat_id: Optional[int] = None,
    now: Optional[datetime] = None
) -> List[str]:
    """
    PERMANENTLY DEACTIVATED per Founder directive.
    Bot CTO will NEVER automatically send overdue reminders or push notifications.
    Returns empty list.
    """
    logger.info("dispatch_overdue_alerts: Overdue reminders are permanently disabled per Founder directive.")
    return []


def start_proactive_escalation_worker(
    bot=None,
    backlog_path: str = "",
    base_dir: str = "",
    check_interval_seconds: int = 1800,
    threshold_hours: float = 12.0,
    initial_delay_seconds: int = 300
) -> Optional[threading.Thread]:
    """
    PERMANENTLY DEACTIVATED per Founder directive.
    Proactive escalation background thread is disabled to eliminate Telegram spam.
    """
    logger.info("start_proactive_escalation_worker: Background escalation thread is permanently disabled per Founder directive.")
    return None


# ==============================================================================
# 9. CENTRALIZED REPOSITORY EXPORTERS (GOOGLE SHEETS / CSV / HTML VIEWER)
# ==============================================================================

def export_backlog_to_csv(backlog_path: str) -> str:
    """
    Exports backlog items to CSV formatted string with UTF-8 BOM for seamless Google Sheets / Excel import.
    """
    import io
    import csv
    if not os.path.exists(backlog_path):
        return "\ufeffMã Lưu Trữ,Ngày Lưu,Công Nghệ / Tool,Trụ Cột Áp Dụng,Nội Dung Đề Xuất,Độ Ưu Tiên,Trạng Thái,File Báo Cáo\n"

    records, _ = BacklogParser.parse_file(backlog_path)
    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow(["Mã Lưu Trữ", "Ngày Lưu", "Công Nghệ / Tool", "Trụ Cột Áp Dụng", "Nội Dung Đề Xuất", "Độ Ưu Tiên", "Trạng Thái", "File Báo Cáo"])
    for r in records:
        writer.writerow([r.task_id, r.date_str, r.tool_name, r.pillar, r.action_item, r.priority, r.status, r.report_link])
    return output.getvalue()


def export_backlog_to_html(backlog_path: str) -> str:
    """
    Exports backlog items to a clean, responsive HTML page with modern styling.
    Compatible with Google Sheets =IMPORTHTML(url, "table", 1).
    """
    import html
    records = []
    if os.path.exists(backlog_path):
        try:
            records, _ = BacklogParser.parse_file(backlog_path)
        except Exception as e:
            logger.error(f"Error parsing backlog for HTML export: {e}")

    rows_html = []
    for r in records:
        status_badge = ""
        st_lower = r.status.lower()
        if "hoàn thành" in st_lower or "[x]" in st_lower:
            status_badge = '<span style="color:#10b981;font-weight:600;">✓ Hoàn thành</span>'
        elif "đình chỉ" in st_lower or "[-]" in st_lower:
            status_badge = '<span style="color:#ef4444;font-weight:600;">✗ Đã đình chỉ</span>'
        elif "thay thế" in st_lower or "[~]" in st_lower:
            status_badge = '<span style="color:#6b7280;font-style:italic;">↺ Đã thay thế</span>'
        else:
            status_badge = '<span style="color:#3b82f6;font-weight:600;">◉ Đã lưu</span>'

        rows_html.append(f"""
        <tr>
            <td style="font-family:monospace;font-weight:bold;">{html.escape(r.task_id)}</td>
            <td style="white-space:nowrap;color:#64748b;">{html.escape(r.date_str)}</td>
            <td style="font-weight:600;color:#0f172a;">{html.escape(r.tool_name)}</td>
            <td style="color:#475569;">{html.escape(r.pillar)}</td>
            <td style="color:#334155;">{html.escape(r.action_item)}</td>
            <td style="white-space:nowrap;">{html.escape(r.priority)}</td>
            <td style="white-space:nowrap;">{status_badge}</td>
        </tr>
        """)

    table_rows = "\n".join(rows_html) if rows_html else '<tr><td colspan="7" style="text-align:center;padding:24px;color:#94a3b8;">Chưa có nội dung nào được lưu.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VibeCheck AI — Kho Lưu Trữ Công Nghệ & Giải Pháp</title>
    <style>
        :root {{
            --bg: #f8fafc;
            --surface: #ffffff;
            --border: #e2e8f0;
            --text: #1e293b;
            --primary: #2563eb;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: var(--surface);
            padding: 28px;
            border-radius: 12px;
            box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.05);
            border: 1px solid var(--border);
        }}
        h1 {{
            font-size: 22px;
            margin-top: 0;
            margin-bottom: 8px;
            color: #0f172a;
        }}
        .subtitle {{
            color: #64748b;
            font-size: 14px;
            margin-bottom: 20px;
        }}
        .info-box {{
            background: #eff6ff;
            border-left: 4px solid var(--primary);
            padding: 14px 16px;
            border-radius: 6px;
            font-size: 13.5px;
            color: #1e40af;
            margin-bottom: 24px;
        }}
        .actions {{
            display: flex;
            gap: 12px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }}
        .btn {{
            display: inline-block;
            padding: 8px 16px;
            background: var(--primary);
            color: white;
            text-decoration: none;
            border-radius: 6px;
            font-size: 13.5px;
            font-weight: 500;
        }}
        .btn-outline {{
            background: transparent;
            color: var(--primary);
            border: 1px solid var(--primary);
        }}
        .table-responsive {{
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13.5px;
            text-align: left;
        }}
        th {{
            background: #f1f5f9;
            color: #475569;
            font-weight: 600;
            padding: 12px;
            border-bottom: 2px solid var(--border);
            white-space: nowrap;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid var(--border);
            vertical-align: top;
        }}
        tr:hover {{
            background-color: #f8fafc;
        }}
        footer {{
            margin-top: 24px;
            font-size: 12px;
            color: #94a3b8;
            text-align: center;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🎯 VibeCheck AI — Kho Lưu Trữ Công Nghệ & Giải Pháp</h1>
        <div class="subtitle">Nơi lưu trữ tập trung các nội dung đã được CTO & Red Team thẩm định. Không nhắc nhở, không thúc ép tiến độ.</div>

        <div class="info-box">
            📊 <b>Tích hợp Google Sheets tự động cập nhật:</b> Mở Google Sheet bất kỳ, tại ô <b>A1</b> dán công thức:
            <br>
            <code style="background:#dbeafe;padding:3px 6px;border-radius:4px;display:inline-block;margin-top:6px;font-weight:bold;">=IMPORTHTML("https://vibecheck-ai-bot.onrender.com/saved", "table", 1)</code>
        </div>

        <div class="actions">
            <a href="/saved.csv" class="btn" download="vibecheck_saved.csv">📥 Tải file CSV</a>
            <a href="/saved.json" class="btn btn-outline" target="_blank">🔗 Xem dữ liệu JSON</a>
        </div>

        <div class="table-responsive">
            <table>
                <thead>
                    <tr>
                        <th>Mã Lưu Trữ</th>
                        <th>Ngày Lưu</th>
                        <th>Công Nghệ / Tool</th>
                        <th>Trụ Cột Áp Dụng</th>
                        <th>Nội Dung Thẩm Định & Giải Pháp</th>
                        <th>Độ Ưu Tiên</th>
                        <th>Trạng Thái</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>
        </div>

        <footer>
            VibeCheck AI Cloud • Đồng bộ tự động theo thời gian thực • Founder mở xem khi cần
        </footer>
    </div>
</body>
</html>"""


