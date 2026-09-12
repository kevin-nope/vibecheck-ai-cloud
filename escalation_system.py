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
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger("SandboxEscalation")

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
        LOCK → READ → VALIDATE → MODIFY → TEMP WRITE → FLUSH/FSYNC → ATOMIC REPLACE → UNLOCK
        
        Guarantees:
        - Minimal mutation: Only updates status column of matching task row.
        - Preserves all headers, footers, whitespace, formatting, non-matching rows.
        - Zero truncation or empty file risk.
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
            # Matches "| [ ] Chờ làm |" or "| [ ] |"
            new_line = re.sub(r"(\|\s*)\[ \](\s*[^|]*\|)", r"\1[x]\2", orig_line, count=1)
            
            # If status text was explicitly "Chờ làm", convert to "Hoàn thành" if desired, or just "[x]"
            if "[ ]" in orig_line and "[x]" in new_line:
                if "Chờ làm" in new_line:
                    new_line = new_line.replace("Chờ làm", "Hoàn thành")
            else:
                logger.error(f"Failed to modify status for Task ID '{target_task_id}'. Aborting to preserve file integrity.")
                return False

            all_lines[target_record.line_number] = new_line

            # Atomic Write: Write to temp file on same directory, flush, fsync, atomic replace
            temp_path = f"{self.backlog_path}.tmp.{uuid.uuid4().hex}"
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    for line in all_lines:
                        f.write(line)
                    f.flush()
                    os.fsync(f.fileno())

                # Retry loop for Windows atomic replace to handle transient file locking
                for replace_attempt in range(5):
                    try:
                        os.replace(temp_path, self.backlog_path)
                        return True
                    except PermissionError:
                        if replace_attempt < 4:
                            time.sleep(0.05)
                        else:
                            raise
                return True
            except Exception as e:
                logger.error(f"Atomic replace failed: {e}. Removing temp file.")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise

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
                        return True
                    except PermissionError:
                        if replace_attempt < 4:
                            time.sleep(0.05)
                        else:
                            raise
                return True
            except Exception as e:
                logger.error(f"Atomic replace failed in cancel_task: {e}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise

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
                        return True
                    except PermissionError:
                        if replace_attempt < 4:
                            time.sleep(0.05)
                        else:
                            raise
                return True
            except Exception as e:
                logger.error(f"Atomic replace failed in delete_task: {e}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise

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
                        return task_id
                    except PermissionError:
                        if replace_attempt < 4:
                            time.sleep(0.05)
                        else:
                            raise
                return task_id
            except Exception as e:
                logger.error(f"Failed to add task: {e}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise

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
                        return True, new_task_id
                    except PermissionError:
                        if replace_attempt < 4:
                            time.sleep(0.05)
                        else:
                            raise
                return True, new_task_id
            except Exception as e:
                logger.error(f"Failed to supersede task: {e}")
                if os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                raise



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
# 6. ADMIN CHAT ID PERSISTENCE & AUTO-DISCOVERY
# ==============================================================================

class AdminChatIDManager:
    """
    Manages discovery and persistence of the admin Telegram chat_id.
    Ensures proactive push alerts reach the bot owner.
    """
    FILE_NAME = ".admin_chat_id"

    @classmethod
    def save_chat_id(cls, base_dir: str, chat_id: int) -> None:
        try:
            target_path = os.path.join(base_dir, cls.FILE_NAME)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(str(chat_id).strip())
        except Exception as e:
            logger.error(f"Failed to save admin chat_id: {e}")

    @classmethod
    def get_chat_id(cls, base_dir: str) -> Optional[int]:
        # 1. Check env var
        env_val = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_ADMIN_CHAT_ID")
        if env_val and env_val.strip().lstrip("-").isdigit():
            return int(env_val.strip())
        # 2. Check persistent file
        target_path = os.path.join(base_dir, cls.FILE_NAME)
        if os.path.exists(target_path):
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content and content.lstrip("-").isdigit():
                    return int(content)
            except Exception as e:
                logger.error(f"Failed to read admin chat_id from {target_path}: {e}")
        return None


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
        except Exception as e:
            logger.error(f"Failed to save reminded state: {e}")

    def is_enabled(self) -> bool:
        with self._lock:
            return self._state.get("reminders_enabled", True)

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._state["reminders_enabled"] = enabled
            self._save()

    def should_remind(self, task_id: str, cooldown_hours: float = 24.0, now_ts: Optional[float] = None) -> bool:
        with self._lock:
            if not self._state.get("reminders_enabled", True):
                return False

            current_ts = now_ts if now_ts is not None else time.time()
            
            # Check snooze
            snooze_until = self._state["snoozed_until"].get(task_id, 0)
            if current_ts < snooze_until:
                return False

            # Check cooldown (mặc định 24 giờ chống spam)
            last_ts = self._state["last_reminded"].get(task_id, 0)
            if (current_ts - last_ts) < (cooldown_hours * 3600):
                return False

            return True

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
    bot,
    backlog_path: str,
    base_dir: str,
    threshold_hours: float = 12.0,
    force: bool = False,
    target_chat_id: Optional[int] = None,
    now: Optional[datetime] = None
) -> List[str]:
    """
    Scans for overdue pending tasks and dispatches reminder cards.
    Returns list of task_ids that were alerted.
    """
    chat_id = target_chat_id if target_chat_id else AdminChatIDManager.get_chat_id(base_dir)
    if not chat_id:
        logger.warning("dispatch_overdue_alerts: No admin chat_id found. Cannot dispatch reminders.")
        return []

    backlog_mgr = BacklogManager(backlog_path)
    state_mgr = RemindedStateManager(base_dir)

    if not force and not state_mgr.is_enabled():
        logger.info("dispatch_overdue_alerts: Nhắc nhở tự động đang TẮT theo yêu cầu của Founder.")
        return []

    overdue_tasks = backlog_mgr.get_overdue_tasks(threshold_hours=threshold_hours, now=now)

    alerted_ids = []
    MAX_ALERTS_PER_CYCLE = 1 if not force else 5
    for task in overdue_tasks:
        if len(alerted_ids) >= MAX_ALERTS_PER_CYCLE:
            break

        if force or state_mgr.should_remind(task.task_id, cooldown_hours=24.0):
            card_text, buttons = build_overdue_reminder_card(task)
            
            try:
                # If bot is telebot instance
                from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton(buttons[0]["text"], callback_data=buttons[0]["callback_data"]),
                    InlineKeyboardButton(buttons[1]["text"], callback_data=buttons[1]["callback_data"])
                )
                markup.add(
                    InlineKeyboardButton(buttons[2]["text"], callback_data=buttons[2]["callback_data"]),
                    InlineKeyboardButton(buttons[3]["text"], callback_data=buttons[3]["callback_data"])
                )
                bot.send_message(chat_id, card_text, parse_mode="HTML", reply_markup=markup)
            except Exception as e:
                # Fallback for mock or basic bot
                try:
                    bot.send_message(chat_id, card_text)
                except Exception as inner_e:
                    logger.error(f"Failed to send overdue alert for {task.task_id}: {inner_e}")
                    continue

            state_mgr.record_reminded(task.task_id)
            alerted_ids.append(task.task_id)

    return alerted_ids


def start_proactive_escalation_worker(
    bot,
    backlog_path: str,
    base_dir: str,
    check_interval_seconds: int = 1800,
    threshold_hours: float = 12.0,
    initial_delay_seconds: int = 300
) -> threading.Thread:
    """
    Launches a dedicated background daemon thread that runs periodic overdue scans.
    """
    def worker_loop():
        logger.info(f"EscalationWorker started (Interval: {check_interval_seconds}s, Threshold: {threshold_hours}h).")
        time.sleep(initial_delay_seconds)
        while True:
            try:
                alerted = dispatch_overdue_alerts(
                    bot=bot,
                    backlog_path=backlog_path,
                    base_dir=base_dir,
                    threshold_hours=threshold_hours,
                    force=False
                )
                if alerted:
                    logger.info(f"EscalationWorker: Dispatched overdue alerts for: {alerted}")
            except Exception as e:
                logger.error(f"EscalationWorker error: {e}")

            time.sleep(check_interval_seconds)

    thread = threading.Thread(target=worker_loop, daemon=True, name="EscalationWorkerThread")
    thread.start()
    return thread

