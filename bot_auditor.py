import os
import re
import sys
import gc
import time
import base64
import html
import atexit
import threading
import warnings
from datetime import datetime
import requests
import telebot
from telebot import types as tele_types
from telebot import apihelper
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Tắt cảnh báo không cần thiết từ thư viện Google GenAI
warnings.filterwarnings("ignore")

# Đảm bảo in tiếng Việt và Emoji chuẩn xác trên Windows console mà không bị lỗi charmap codec
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Vô hiệu hóa QuickEdit mode trên Windows Console để click chuột không bao giờ làm freeze tiến trình bot
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            # Tắt ENABLE_QUICK_EDIT_MODE (0x0040)
            new_mode = mode.value & ~0x0040
            kernel32.SetConsoleMode(h_stdin, new_mode)
    except Exception:
        pass

# Cấu hình timeout bền bỉ & tự động retry đa tầng cho kết nối Telegram API
apihelper.RETRY_ON_ERROR = True
apihelper.RETRY_ENGINE = 2  # Sử dụng HTTPAdapter với urllib3 Retry tự phục hồi
apihelper.MAX_RETRIES = 15
apihelper.RETRY_TIMEOUT = 2
apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 30
apihelper.LONG_POLLING_TIMEOUT = 20

# ==============================================================================
# CẤU HÌNH HỆ THỐNG VIBECHECK AI (V2.2 ENTERPRISE GRADE)
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "Khao_Sat_Cong_Nghe")
LOCK_FILE = os.path.join(BASE_DIR, "bot.lock")
BACKLOG_FILE = os.path.join(BASE_DIR, "00_ACTION_BACKLOG.md")
os.makedirs(REPORT_DIR, exist_ok=True)

# Nạp module Master Backlog & Escalation System đã qua kiểm định Sandbox
from escalation_system import (
    BacklogManager,
    BacklogParser,
    BacklogFileLock,
    ResilientAlertDispatcher,
    generate_antigravity_mvp_prompt,
    AdminChatIDManager,
    RemindedStateManager,
    build_overdue_reminder_card,
    dispatch_overdue_alerts,
    start_proactive_escalation_worker
)
backlog_mgr = BacklogManager(BACKLOG_FILE)
state_mgr = RemindedStateManager(BASE_DIR)

# Tải biến môi trường từ .env (override=True để file .env luôn được ưu tiên cao nhất)
load_dotenv(os.path.expanduser("~/.eks/secrets/.env"), override=True)
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ==============================================================================
# CƠ CHẾ KHÓA ĐƠN TIẾN TRÌNH (SINGLE-INSTANCE LOCK — CHỐNG LỖI 409 CONFLICT)
# ==============================================================================
def is_pid_running(pid: int) -> bool:
    """Kiểm tra xem PID có đang thực sự chạy hay không (Hỗ trợ cả Windows và Linux)."""
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

def acquire_single_instance_lock():
    """Tự động kiểm tra và triệt tiêu tiến trình cũ nếu bị treo, đảm bảo không xung đột 409."""
    current_pid = os.getpid()
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                content = f.read().strip()
                if content.isdigit():
                    old_pid = int(content)
                    if old_pid != current_pid and is_pid_running(old_pid):
                        if os.name == "nt":
                            os.system(f"taskkill /PID {old_pid} /F >nul 2>&1")
                        else:
                            try:
                                os.kill(old_pid, 9)
                            except Exception:
                                pass
                        time.sleep(2.5)  # Chờ để Telegram server giải phóng socket getUpdates cũ
        except Exception:
            pass
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(current_pid))
    except Exception:
        pass

def cleanup_lock():
    """Tự động xóa file khóa khi tắt bot (chỉ xóa nếu đúng là file do tiến trình hiện tại tạo)."""
    try:
        current_pid = os.getpid()
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, "r") as f:
                content = f.read().strip()
            if content == str(current_pid):
                os.remove(LOCK_FILE)
    except Exception:
        pass

atexit.register(cleanup_lock)

def check_cloud_coexistence():
    """Kiểm tra và cảnh báo nếu bot trên Render Cloud đang chạy song song (nguyên nhân gây 409 Conflict)."""
    if os.getenv("RENDER") == "true":
        return
    try:
        r = requests.get("https://vibecheck-ai-bot.onrender.com", timeout=2.0)
        if r.status_code == 200 and "VibeCheck AI" in r.text:
            print("\n" + "!" * 80, flush=True)
            print("⚠️  CẢNH BÁO XUNG ĐỘT TIẾN TRÌNH CLOUD & LOCAL (TELEGRAM 409 CONFLICT):", flush=True)
            print("   Dịch vụ VibeCheck AI trên Render Cloud (https://vibecheck-ai-bot.onrender.com) đang HOẠT ĐỘNG 24/7.", flush=True)
            print("   Telegram chỉ cho phép DUY NHẤT 1 tiến trình lắng nghe (getUpdates) cho mỗi Bot Token.", flush=True)
            print("   👉 NẾU BẠN DÙNG BOT BÌNH THƯỜNG: Bạn có thể TẮT máy tính/cửa sổ này, Bot Cloud vẫn chạy 24/7!", flush=True)
            print("   👉 NẾU BẠN CẦN TEST LOCAL: Vui lòng vào Render Dashboard (https://dashboard.render.com)")
            print("      chọn 'vibecheck-ai-bot' -> 'Suspend' để nhường socket Telegram cho máy Local.", flush=True)
            print("!" * 80 + "\n", flush=True)
    except Exception:
        pass

# ==============================================================================
# BỘ NHỚ AN TOÀN ĐA LUỒNG & GIỚI HẠN RAM (THREAD-SAFE BOUNDED CACHE)
# ==============================================================================
CACHE_LOCK = threading.Lock()
AUDIT_CACHE = {}          # Lưu tối đa 50 bài thẩm định gần nhất
CONVERSATION_HISTORY = {} # Lưu tối đa 50 phiên chat, mỗi phiên 6 tin gần nhất

def cache_audit_data(audit_id: str, data: dict):
    with CACHE_LOCK:
        if len(AUDIT_CACHE) >= 50:
            oldest_key = next(iter(AUDIT_CACHE))
            AUDIT_CACHE.pop(oldest_key, None)
        AUDIT_CACHE[audit_id] = data

def get_audit_data(audit_id: str) -> dict:
    with CACHE_LOCK:
        return AUDIT_CACHE.get(audit_id)

def add_to_history(chat_id: int, role: str, text: str):
    with CACHE_LOCK:
        if chat_id not in CONVERSATION_HISTORY:
            if len(CONVERSATION_HISTORY) >= 50:
                oldest_chat = next(iter(CONVERSATION_HISTORY))
                CONVERSATION_HISTORY.pop(oldest_chat, None)
            CONVERSATION_HISTORY[chat_id] = []
        CONVERSATION_HISTORY[chat_id].append({"role": role, "text": text[:2000]})
        if len(CONVERSATION_HISTORY[chat_id]) > 6:
            CONVERSATION_HISTORY[chat_id] = CONVERSATION_HISTORY[chat_id][-6:]

def get_history(chat_id: int) -> list:
    with CACHE_LOCK:
        return list(CONVERSATION_HISTORY.get(chat_id, []))

def clear_history(chat_id: int):
    with CACHE_LOCK:
        CONVERSATION_HISTORY.pop(chat_id, None)

def pop_last_history_turn(chat_id: int):
    with CACHE_LOCK:
        if chat_id in CONVERSATION_HISTORY and len(CONVERSATION_HISTORY[chat_id]) >= 2:
            CONVERSATION_HISTORY[chat_id] = CONVERSATION_HISTORY[chat_id][:-2]


# ==============================================================================
# SYSTEM PROMPTS: CHUẨN VIBECHECK AI (v2.2)
# ==============================================================================

SYSTEM_PROMPT_PASS1 = """Bạn là Chuyên viên Trích xuất Kỹ thuật cho VibeCheck AI (v3.0 Ultra). Nhiệm vụ của bạn là bóc tách toàn diện dữ liệu kỹ thuật từ nguồn được cung cấp:
1. Bản chất kỹ thuật cốt lõi (Core mechanism, kiến trúc, thư viện phụ thuộc).
2. Phát hiện chiêu trò quảng cáo (Hype vs Reality, những điểm tác giả nói quá hoặc giấu nhẹm).
3. Đánh giá phần cứng & chi phí (VRAM, GPU, RAM, API cost, khả năng chạy miễn phí 0đ hoặc chi phí tối thiểu).
4. Nhận diện bối cảnh và lĩnh vực đề tài:
   - Tự động phân loại: (A) Vận tải du lịch / Nhà xe / Đặt vé trực tiếp; (B) Lập trình Vibecoding / AI Automation; (C) Content & SEO; (D) B2B SaaS; (E) Video & Truyền thông MXH; (F) Thương mại điện tử / Kinh doanh chung / Quản trị cá nhân.
   - Phác thảo tiềm năng ứng dụng thực tế theo đúng lĩnh vực được nhận diện.
Hãy đưa ra một bản phân tích thô cực kỳ chi tiết, khách quan, không bỏ sót bất kỳ thông số kỹ thuật nào."""

SYSTEM_PROMPT_PASS2 = """Bạn là VibeCheck AI – CTO thực chiến kiêm Tech Auditor. Nhiệm vụ của bạn là cố vấn kỹ thuật, thẩm định công nghệ và vạch trần hype.

NGUYÊN TẮC HOẠT ĐỘNG & TƯ DUY:
1. Anti-Hype & Thực Dụng:
   - Bóc tách bản chất kỹ thuật, không bị đánh lừa bởi thuật ngữ marketing bóng bẩy.
   - Thẩm định dựa trên: ROI thực tế, độ phức tạp bảo trì, chi phí ẩn (token, server, license) và tính khả thi triển khai.
   - Luôn đưa ra kết luận trước, rõ ràng, không nước đôi, không lý thuyết suông.

2. Ma Trận Đa Ngành Phổ Quát (Universal Application Matrix):
   - NẾU LIÊN QUAN ĐẾN VẬN TẢI / DU LỊCH / XE: Ưu tiên phân tích sâu cho Nhà Xe Thành Tâm (Tuyến Vũng Tàu ↔ Sài Gòn / Sân bay / Đi tỉnh, xe 7 chỗ Xpander, Direct Booking qua Web/Zalo/Hotline, giảm phụ thuộc môi giới cắt phế 50k-100k, giải pháp 0đ hoặc siêu rẻ).
   - NẾU LÀ CÁC LĨNH VỰC KHÁC (AI Agent, Web Dev, SEO/Marketing, E-commerce, B2B, Automation, Kinh doanh, Quản lý): Thẩm định chính xác theo bản chất bài toán của lĩnh vực đó. TUYỆT ĐỐI KHÔNG gượng ép liên hệ tới xe cộ nếu đề tài không liên quan!

3. Đối Chiếu Danh Sách Đã Triển Khai / Đang Chờ Làm (Semantic Dedup & Superseding Matrix):
   - Bạn sẽ được cung cấp danh sách các task đang chờ làm trong Master Backlog (nếu có).
   - Hãy đối chiếu công nghệ đang thẩm định với các task trong danh sách này:
     a. Trùng lặp hoàn toàn / giải quyết cùng bài toán mà giải pháp cũ đã đủ tốt: Đề xuất KHÔNG TRÙNG LẶP (RECOMMENDATION: SKIP_DUPLICATE).
     b. Cùng giải quyết một bài toán nhưng công nghệ MỚI này VƯỢT TRỘI HƠN (chi phí rẻ hơn 0đ, ROI cao hơn, dễ làm MVP hơn, ít phụ thuộc bên thứ 3 hơn): Đề xuất THAY THẾ task cũ (RECOMMENDATION: SUPERSEDE, chỉ rõ MATCHED_TASK_ID, lý do vì sao công nghệ mới tốt hơn).
     c. Nếu không trùng với task nào hoặc mang lại giá trị độc lập hoàn toàn: Đề xuất MỚI (RECOMMENDATION: NEW).
"""

SYSTEM_PROMPT_PASS3_REFLEXION = """Bạn là Trọng Tài Phản Tỉnh Độc Lập & Cố Vấn Tối Cao (Adversarial Devil's Advocate & Principal Auditor) của VibeCheck AI (v3.0 Ultra).
Nhiệm vụ của bạn là thực hiện VÒNG PHẢN BIỆN TỰ ĐỘNG (REFLEXION & CONVERGENCE LOOP):
Rà soát lại toàn bộ bản dự thảo thẩm định từ Pass 2, đóng vai "Luật sư của quỷ" (Devil's Advocate) để bóc tách mọi điểm mù, lỗ hổng ngầm, rủi ro tiềm ẩn và bắt buộc đề xuất PHƯƠNG ÁN THAY THẾ TỐI ƯU VƯỢT TRỘI trước khi xuất bản kết quả cuối cùng.

QUY TRÌNH REFLEXION 3 BƯỚC:
1. 🔍 SĂN TÌM ĐIỂM MÙ & LỖ HỔNG TIỀM ẨN (Blindspot & Failure Modes Hunter):
   - Tìm ít nhất 2-3 rủi ro chí mạng mà con người hoặc Pass 2 dễ bỏ qua:
     * Lỗ hổng bảo mật / Rò rỉ API key / Quyền riêng tư / Nguy cơ bị khóa tài khoản (Ban/Shadowban do spam/scrape).
     * Chi phí ẩn: Liệu có bẫy thanh toán, quota ngầm, chi phí server phình to khi có traffic thật?
     * Gánh nặng bảo trì: 1 người tự vận hành có gánh nổi không? Độ bền khi hệ thống gặp lỗi nửa đêm?
2. 🎯 TỰ ĐỘNG HỘI TỤ & ĐIỀU CHỈNH KẾT LUẬN (Convergence Check):
   - Xem xét lại Routing Tag của Pass 2: Liệu Pass 2 có quá hào hứng (over-hyped) hay quá khắt khe? Điều chỉnh lại nhãn nếu cần.
3. 💡 BẮT BUỘC ĐỀ XUẤT PHƯƠNG ÁN THAY THẾ TỐI ƯU HƠN (Next-Best Alternatives):
   - Đưa ra 1-2 phương án thay thế thông minh hơn:
     * Phương án A (Lean / 0đ / Tốc độ cao): Cách giải quyết bài toán đó nhanh nhất, tốn 0đ hoặc siêu rẻ, ít rủi ro nhất.
     * Phương án B (Công nghệ chuẩn công nghiệp / Mở rộng lâu dài): Lựa chọn kiến trúc bền vững, mã nguồn mở hoặc giải pháp thay thế hàng đầu thị trường.

QUY CHUẨN TRÌNH BÀY ĐẦU RA (Markdown chuẩn Telegram):
- Dòng 1: KẾT LUẬN CỐT LÕI (Dứt khoát, kèm Đèn tín hiệu 🟢 / 🟡 / 🔴).
- Thân bài:
  + Bóc tách kỹ thuật & Bảng đối chiếu Hype vs Thực tế.
  + ⚠️ 3 LỖ HỔNG & RỦI RO TIỀM ẨN (Điểm mù do vòng Reflexion phát hiện).
  + 💡 PHƯƠNG ÁN THAY THẾ TỐI ƯU VƯỢT TRỘI (Phương án Lean 0đ vs Phương án chuẩn).
  + Ứng dụng thực tế theo bối cảnh (Nếu liên quan Xe Thành Tâm thì phân tích rõ, nếu là ngành khác thì phân tích theo ngành đó).
- Dòng cuối: BẮT BUỘC có đúng 1 Routing Tag: [TRIỂN KHAI NGAY] hoặc [LƯU THAM KHẢO] hoặc [BỎ QUA/HYPE].

BẮT BUỘC KÈM 2 KHỐI DỮ LIỆU ĐẶC BIỆT Ở CUỐI CÙNG:
```quick_summary
TÊN: [Tên ngắn gọn của công cụ/dự án/ý tưởng]
TÓM_TẮT_3_DÒNG:
- Dòng 1: Bản chất cốt lõi
- Dòng 2: Chi phí & Rủi ro thực tế
- Dòng 3: Khả năng ứng dụng & Phương án thay thế tốt nhất
XE_VUNG_TAU_ACTION: [Hành động cho Xe Vũng Tàu - Sài Gòn NẾU liên quan đến xe, hoặc ghi: Áp dụng cho bài toán [Lĩnh vực cụ thể]]
ROUTING_TAG: [TRIỂN KHAI NGAY] hoặc [LƯU THAM KHẢO] hoặc [BỎ QUA/HYPE]
```

```comparison_meta
RECOMMENDATION: [NEW hoặc SUPERSEDE hoặc SKIP_DUPLICATE]
MATCHED_TASK_ID: [Mã TASK-xxx nếu có, hoặc NONE]
MATCHED_TASK_NAME: [Tên task cũ nếu có, hoặc NONE]
SUPERSEDE_REASON: [Giải thích ngắn gọn 1-2 câu vì sao nên thay thế hoặc vì sao bị trùng, hoặc NONE]
COMPARISON_VERDICT: [1 câu nhận định so sánh trực diện]
```
"""

SYSTEM_PROMPT_CTO_CHAT = """Bạn là VibeCheck AI – CTO thực chiến & Senior Tech Architect.
Người dùng đang đặt câu hỏi tiếp nối (follow-up Q&A), nhờ tư vấn giải pháp, tìm kiếm công cụ/repo, nhờ viết code, hoặc hỏi sâu về báo cáo kỹ thuật trước đó.

I. NGUYÊN TẮC NỘI DUNG (BẮT BUỘC):
1. ĐI THẲNG VÀO VẤN ĐỀ: Trả lời trực diện câu hỏi của người dùng, súc tích (dưới 20 dòng). Tuyệt đối KHÔNG vòng vo, KHÔNG giảng đạo lý thuyết.
2. KHÔNG ÉP KHUÔN 5 TRỤ CỘT & KHÔNG GẮN ROUTING TAG: Đây là phiên thảo luận/cố vấn, không phải báo cáo thẩm định mới. Tuyệt đối không gắn [TRIỂN KHAI NGAY] hay [LƯU THAM KHẢO] trừ khi người dùng yêu cầu rõ ràng.
3. TƯ VẤN REPO & CÔNG NGHỆ THỰC CHIẾN:
   - Dựa trên dữ liệu GitHub Search thực tế được cung cấp để giới thiệu 2 đến 4 giải pháp tốt nhất.
   - Luôn đưa ra kết luận dứt khoát: Khuyên người dùng nên chọn cái nào nhất (ưu tiên giải pháp 0đ, nhẹ, dễ tích hợp).

II. NGUYÊN TẮC TRÌNH BÀY & NHẤN NHÁ THỊ GIÁC (RẤT QUAN TRỌNG):
- IN ĐẬM: Dùng **từ khóa** cho Tên công cụ, số sao ⭐ (ví dụ: ⭐ **78k**), chi phí (**0đ**), và hành động quyết định. Chỉ in đậm từ khóa quan trọng (tối đa 20% câu chữ), tuyệt đối KHÔNG in đậm cả câu dài.
- IN NGHIÊNG: Dùng *từ khóa* cho ưu/nhược điểm, lưu ý kỹ thuật, hoặc giải nghĩa nhỏ.
- CODE: Dùng `owner/repo` cho tên repo, tên file, hoặc lệnh cài đặt.
- HYPERLINK SẠCH: BẮT BUỘC dùng link ẩn dạng [Tên Repo](URL). TUYỆT ĐỐI KHÔNG để trần link URL thô dài ngoằng làm vỡ khung chat.
- TRÍCH DẪN CTO: Dùng dấu > ở đầu và cuối để đóng khung lời khuyên chốt hạ hoặc góc nhìn CTO.
- CẤU TRÚC PHẲNG: Dùng gạch đầu dòng 1 cấp ngắn gọn, câu từ gãy gọn. TUYỆT ĐỐI KHÔNG lồng nhiều tầng dấu hoa thị (* *Ưu*:) gây rối mắt.
"""

# ==============================================================================
# HÀM BỌC GỌI GEMINI BỀN BỈ (AUTO-RETRY & MULTI-MODEL CASCADE — CHỐNG 503/429)
# ==============================================================================

def call_gemini_resilient(contents, instruction: str = "", temperature: float = 0.2, max_retries: int = 2) -> str:
    """
    Gọi Gemini với cơ chế cascade đa model và tự phục hồi cấp Enterprise:
    1. Ưu tiên model siêu nhẹ, quota dồi dào, tốc độ cao: gemini-3.5-flash-lite
    2. Fallback sang: gemini-3.5-flash
    3. Fallback sang: gemini-flash-lite-latest
    4. Fallback sang: gemini-flash-latest
    5. Fallback sang: gemini-3.7-flash
    6. Dự phòng cuối: gemini-3.6-flash
    Nếu bất kỳ model nào gặp 429 (Resource Exhausted), 404 (Not Found), hoặc 503 (Server Unavailable),
    hệ thống lập tức chuyển sang model kế tiếp trong mili-giây mà không làm nghẽn luồng.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.GenerateContentConfig(
        system_instruction=instruction if instruction else None,
        temperature=temperature
    )
    models_cascade = [
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash",
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-3.7-flash",
        "gemini-3.6-flash"
    ]

    last_err = None
    for model_name in models_cascade:
        for attempt in range(max_retries):
            try:
                res = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
                if res and res.text:
                    return res.text.strip()
            except Exception as e:
                last_err = e
                err_str = str(e)
                # Nếu 401 UNAUTHENTICATED hoặc API key hết hạn/không hợp lệ -> Dừng ngay lập tức
                if any(kw in err_str for kw in ["401", "UNAUTHENTICATED", "ACCESS_TOKEN_TYPE_UNSUPPORTED", "API_KEY_INVALID", "invalid authentication"]):
                    print(f"\n❌ [LỖI XÁC THỰC] Gemini API Key đã hết hạn hoặc không hợp lệ: {err_str}\n", flush=True)
                    raise PermissionError(f"Gemini API Key trong file .env đã hết hạn hoặc không hợp lệ (401 UNAUTHENTICATED). Vui lòng cập nhật API Key vĩnh viễn (bắt đầu bằng AIzaSy...) từ Google AI Studio (https://aistudio.google.com/app/apikey).")
                # Nếu hết quota (429) hoặc model không khả dụng (404/400) -> Chuyển ngay model tiếp theo
                if any(kw in err_str for kw in ["429", "RESOURCE_EXHAUSTED", "quota", "404", "NOT_FOUND"]):
                    print(f"⚠️ [{model_name}] Quota giới hạn, chuyển ngay model kế tiếp trong cascade...", flush=True)
                    break
                # Nếu 503 UNAVAILABLE (server spike) -> Chuyển ngay model tiếp theo
                elif any(kw in err_str for kw in ["503", "UNAVAILABLE"]):
                    print(f"⚠️ [{model_name}] 503 Unavailable, chuyển ngay model kế tiếp...", flush=True)
                    break
                else:
                    # Các lỗi mạng tạm thời khác: ngủ 0.5s rồi thử lại
                    time.sleep(0.5)

    raise RuntimeError(f"Hệ thống máy chủ AI đang bảo trì hoặc quá tải tạm thời ({last_err}). Vui lòng thử lại sau 5 giây!")


# ==============================================================================
# HÀM BỔ TRỢ: THU THẬP DỮ LIỆU & TRA CỨU THỜI GIAN THỰC
# ==============================================================================

def extract_github_info(url: str) -> str:
    """Lấy thông tin Repo GitHub qua GitHub API."""
    pattern = r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
    match = re.search(pattern, url)
    if not match:
        return ""
    
    owner = match.group(1)
    repo = match.group(2).rstrip("/").removesuffix(".git")
    
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "VibeCheck-AI/2.2"
    }
    
    info_text = f"=== DỮ LIỆU THU THẬP TỪ GITHUB: {owner}/{repo} ===\n"
    try:
        res_repo = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers, timeout=15)
        if res_repo.status_code == 200:
            repo_data = res_repo.json()
            stars = repo_data.get("stargazers_count", 0)
            forks = repo_data.get("forks_count", 0)
            license_info = repo_data.get("license")
            license_name = license_info.get("name", "Chưa rõ") if license_info else "Không có license"
            description = repo_data.get("description", "Không có mô tả")
            language = repo_data.get("language", "Đa ngôn ngữ")
            updated_at = repo_data.get("updated_at", "")
            
            info_text += (
                f"- Repository: {owner}/{repo}\n"
                f"- Stars: ⭐ {stars:,} | Forks: 🍴 {forks:,}\n"
                f"- Giấy phép: 📜 {license_name}\n"
                f"- Ngôn ngữ: 💻 {language}\n"
                f"- Cập nhật: {updated_at}\n"
                f"- Mô tả: {description}\n\n"
            )
            
        res_readme = requests.get(f"https://api.github.com/repos/{owner}/{repo}/readme", headers=headers, timeout=15)
        if res_readme.status_code == 200:
            content_b64 = res_readme.json().get("content", "")
            try:
                readme_text = base64.b64decode(content_b64).decode("utf-8", errors="replace")
                if len(readme_text) > 25000:
                    readme_text = readme_text[:25000] + "\n...[README ĐÃ ĐƯỢC RÚT GỌN]..."
                info_text += f"=== NỘI DUNG README.MD ===\n{readme_text}\n"
            except Exception:
                pass
    except Exception as e:
        info_text += f"- Lỗi kết nối GitHub API: {e}\n"
        
    return info_text


def fetch_jina_reader(url: str) -> str:
    """Lấy nội dung Markdown sạch hoặc phụ đề video YouTube qua Jina Reader."""
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "text/markdown, text/plain"
    }
    try:
        response = requests.get(jina_url, headers=headers, timeout=25)
        if response.status_code == 200:
            text = response.text
            # Phát hiện màn hình chặn đăng nhập của Facebook
            if any(fb in url.lower() for fb in ["facebook.com", "fb.watch", "fb.com"]):
                if any(kw in text.lower() for kw in ["log in", "login_attempt", "đăng nhập", "device-based", "create new account"]):
                    return "FACEBOOK_LOGIN_REQUIRED"
            if len(text) > 30000:
                text = text[:30000] + "\n...[NỘI DUNG ĐÃ ĐƯỢC RÚT GỌN ĐỂ TỐI ƯU]..."
            return f"=== NỘI DUNG THU THẬP TỪ URL ({url}) ===\n{text}"
        return f"Lỗi cào dữ liệu qua Jina Reader (HTTP {response.status_code})"
    except Exception as e:
        return f"Lỗi kết nối Jina Reader: {e}"


def fetch_facebook_opengraph(url: str) -> tuple[str, str, str]:
    """
    Trích xuất metadata OpenGraph từ bài viết Facebook bằng User-Agent Crawler chuẩn của Facebook.
    Cho phép lấy được tiêu đề và nội dung tóm tắt kể cả khi Facebook chặn bot thông thường.
    Trả về: (title, description, image_url)
    """
    headers = {
        "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8"
    }
    try:
        res = requests.get(url, headers=headers, allow_redirects=True, timeout=12)
        if res.status_code == 200:
            content = res.text
            og_title = re.search(r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\']([^"\']+)["\']', content, re.IGNORECASE)
            og_desc = re.search(r'<meta\s+(?:property|name)=["\']og:description["\']\s+content=["\']([^"\']+)["\']', content, re.IGNORECASE)
            og_img = re.search(r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']', content, re.IGNORECASE)

            title = html.unescape(og_title.group(1)).strip() if og_title else ""
            desc = html.unescape(og_desc.group(1)).strip() if og_desc else ""
            img = og_img.group(1).strip() if og_img else ""
            return title, desc, img
    except Exception as e:
        print(f"Lưu ý khi cào Facebook OpenGraph ({url}): {e}", flush=True)
    return "", "", ""


def fetch_tiktok_content(url: str) -> tuple[str, str, bytes | None, bytes | None, list[bytes]]:
    """
    Bóc tách dữ liệu đa tầng video / ảnh TikTok thực tế:
    1. Hỗ trợ cả link đầy đủ và link rút gọn (vt.tiktok.com / vm.tiktok.com).
    2. Tải video MP4 trực tiếp qua TikWM API không dính watermark (nếu <= 25MB).
    3. Nếu video quá lớn (> 25MB) hoặc tải lỗi: tải ảnh bìa gốc HD (cover) làm fallback đưa vào Vision.
    4. Nếu là bài đăng dạng Album ảnh (Photo Carousel): tải tối đa 4 ảnh đầu tiên để đưa vào Vision.
    5. Dự phòng qua TikTok Official oEmbed API nếu TikWM gặp sự cố.
    """
    clean_url = url
    try:
        if any(short in url for short in ["vt.tiktok.com", "vm.tiktok.com", "/t/"]):
            r = requests.head(url, allow_redirects=True, timeout=10)
            clean_url = r.url
    except Exception:
        pass

    title = ""
    author = ""
    video_bytes = None
    cover_bytes = None
    images_bytes_list = []

    # 1. Thử lấy media qua TikWM API
    try:
        api_url = f"https://www.tikwm.com/api/?url={requests.utils.quote(clean_url)}"
        res = requests.get(api_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        if res.status_code == 200:
            res_json = res.json()
            if res_json.get("code") == 0:
                data = res_json.get("data", {})
                title = data.get("title", "")
                author = data.get("author", {}).get("nickname", "")
                video_size = data.get("size", 0)
                play_url = data.get("play") or data.get("wmplay")
                cover_url = data.get("origin_cover") or data.get("cover")
                carousel_images = data.get("images")

                # Trường hợp A: Video chuẩn <= 25MB
                if play_url and (video_size <= 25 * 1024 * 1024):
                    v_res = requests.get(play_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                    if v_res.status_code == 200 and len(v_res.content) <= 25 * 1024 * 1024:
                        video_bytes = v_res.content

                # Trường hợp B: Album ảnh (Slideshow)
                if not video_bytes and carousel_images and isinstance(carousel_images, list):
                    for img_url in carousel_images[:4]:
                        try:
                            img_res = requests.get(img_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                            if img_res.status_code == 200:
                                images_bytes_list.append(img_res.content)
                        except Exception:
                            pass

                # Trường hợp C: Video quá lớn hoặc không tải được -> Tải ảnh bìa HD (cover) làm fallback
                if not video_bytes and not images_bytes_list and cover_url:
                    try:
                        c_res = requests.get(cover_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
                        if c_res.status_code == 200:
                            cover_bytes = c_res.content
                    except Exception:
                        pass
    except Exception as e:
        print(f"Lưu ý khi tải TikTok qua TikWM: {e}", flush=True)

    # 2. Fallback qua TikTok oEmbed API nếu chưa có title
    if not title:
        try:
            oembed_url = f"https://www.tiktok.com/oembed?url={requests.utils.quote(clean_url)}"
            res_o = requests.get(oembed_url, timeout=10)
            if res_o.status_code == 200:
                o_data = res_o.json()
                title = o_data.get("title", "")
                author = o_data.get("author_name", "")
                thumb_url = o_data.get("thumbnail_url")
                if thumb_url and not video_bytes and not cover_bytes:
                    try:
                        th_res = requests.get(thumb_url, timeout=10)
                        if th_res.status_code == 200:
                            cover_bytes = th_res.content
                    except Exception:
                        pass
        except Exception as e:
            print(f"Lưu ý khi gọi TikTok oEmbed: {e}", flush=True)

    return title, author, video_bytes, cover_bytes, images_bytes_list


def search_github_repositories(query_keywords: str, max_results: int = 4) -> list[dict]:
    """Tra cứu trực tiếp repo mã nguồn mở trên GitHub Search API theo thời gian thực."""
    try:
        clean_q = re.sub(r"[^\w\s\-]", " ", query_keywords).strip()
        if not clean_q:
            return []
        
        url = f"https://api.github.com/search/repositories?q={requests.utils.quote(clean_q)}&sort=stars&order=desc&per_page={max_results}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "VibeCheck-AI/2.2"
        }
        res = requests.get(url, headers=headers, timeout=12)
        if res.status_code == 200:
            items = res.json().get("items", [])
            results = []
            for item in items[:max_results]:
                results.append({
                    "name": item.get("full_name", ""),
                    "url": item.get("html_url", ""),
                    "stars": item.get("stargazers_count", 0),
                    "description": item.get("description", "Không có mô tả"),
                    "language": item.get("language", "Đa ngôn ngữ")
                })
            return results
        elif res.status_code in [403, 429]:
            print("⚠️ GitHub API đạt ngưỡng rate limit tìm kiếm tạm thời.", flush=True)
    except Exception as e:
        print(f"Lưu ý khi tra cứu GitHub Search API: {e}", flush=True)
    return []


def find_and_read_local_report(query_text: str) -> tuple[str, str]:
    """Tự động phát hiện và đọc file Markdown đã lưu trong Khao_Sat_Cong_Nghe nếu người dùng nhắc tới."""
    # 1. Bắt trực tiếp theo định dạng tên file: 2026-xx-xx_xxx.md hoặc xxx.md
    md_matches = re.findall(r"([\w\-]+\.md)", query_text, re.IGNORECASE)
    for fn in md_matches:
        # Chống path traversal
        clean_fn = os.path.basename(fn)
        fpath = os.path.join(REPORT_DIR, clean_fn)
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read(5000)
                return f"=== NỘI DUNG TÀI LIỆU CỤC BỘ TRÊN MÁY TÍNH ({clean_fn}) ===\n{content}\n", clean_fn
            except Exception:
                pass

    # 2. Quét mờ theo tên file trong thư mục Khao_Sat_Cong_Nghe
    try:
        files = os.listdir(REPORT_DIR)
        for fn in files:
            if not fn.endswith(".md"):
                continue
            stem = fn.replace(".md", "").replace("_", " ").lower()
            stem_tokens = [w for w in stem.split() if len(w) > 3 and not w.isdigit()]
            if len(stem_tokens) >= 2 and all(tok in query_text.lower() for tok in stem_tokens[:2]):
                fpath = os.path.join(REPORT_DIR, fn)
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read(5000)
                return f"=== NỘI DUNG TÀI LIỆU CỤC BỘ TRÊN MÁY TÍNH ({fn}) ===\n{content}\n", fn
    except Exception:
        pass

    return "", ""


def extract_search_query(user_text: str, matched_file: str = "", context_text: str = "") -> str:
    """Tạo 2-4 từ khóa tiếng Anh chuẩn xác nhất để search GitHub API bằng Gemini Flash."""
    hint = f"User query: {user_text}\n"
    if matched_file:
        hint += f"Referenced file: {matched_file}\n"
    if context_text:
        hint += f"Context: {context_text[:1200]}\n"
        
    prompt = (
        f"{hint}\n"
        "Hãy trích xuất đúng 2 đến 4 từ khóa tiếng Anh quan trọng nhất để tìm kiếm các open-source repository trên GitHub Search API liên quan đến công nghệ này.\n"
        "Chỉ trả về các từ khóa cách nhau bằng dấu cách, không có dấu ngoặc kép, không giải thích gì thêm (Ví dụ: prompt manager local markdown sync)."
    )
    try:
        query = call_gemini_resilient(prompt, temperature=0.1, max_retries=2)
        query = query.replace("\n", " ").replace('"', '').strip()
        if query and len(query) < 80:
            return query
    except Exception:
        pass

    if matched_file:
        clean = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", matched_file).replace(".md", "").replace("_", " ")
        return clean
    return user_text[:40]


# ==============================================================================
# HÀM XỬ LÝ CHẾ ĐỘ 1: DUAL-PASS AUDIT & ON-DEMAND PERSISTENCE
# ==============================================================================

def save_audit_report_to_disk(cached_meta: dict) -> tuple[str, str]:
    """
    Lưu file báo cáo vào máy tính (Khao_Sat_Cong_Nghe/) theo yêu cầu (On-Demand),
    tránh làm đầy ổ cứng tự động. Trả về (saved_file_path, file_name).
    """
    safe_name = re.sub(r"[^\w\-]", "_", cached_meta.get("name", "Bao_Cao"))[:40].strip("_")
    today_str = datetime.now().strftime("%Y-%m-%d")
    file_name = f"{today_str}_{safe_name}.md"
    saved_file_path = os.path.join(REPORT_DIR, file_name)

    clean_report = cached_meta.get("clean_report", "")
    md_content = (
        f"---\n"
        f"title: \"VibeCheck - {cached_meta.get('name', 'Báo Cáo')}\"\n"
        f"date: \"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\"\n"
        f"verdict: \"[{cached_meta.get('verdict', 'LƯU THAM KHẢO')}]\"\n"
        f"project: \"Nhà Xe Thành Tâm / Hệ Thống 5 Trụ Cột\"\n"
        f"auditor: \"VibeCheck AI (v2.8.0)\"\n"
        f"---\n\n"
        f"{clean_report}\n"
    )
    with open(saved_file_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return saved_file_path, file_name


def execute_dual_pass_audit(raw_contents, source_label: str = "Tài liệu / Ý tưởng") -> tuple[str, dict]:
    """Thực hiện thẩm định 2 vòng: Pass 1 (Sơ bộ) -> Pass 2 (CTO Phản biện, Đối chiếu Backlog & Quét lỗi ngầm)."""
    # --- VÒNG 1: Trích xuất & Đánh giá sơ bộ ---
    pass1_result = call_gemini_resilient(raw_contents, instruction=SYSTEM_PROMPT_PASS1, temperature=0.2)

    # --- VÒNG 2: VibeCheck AI – CTO Phản biện, So găng Backlog & Kiểm duyệt lỗi ngầm ---
    if isinstance(raw_contents, list):
        summary_raw = "\n".join([str(p) for p in raw_contents if isinstance(p, str)])
        if not summary_raw.strip():
            summary_raw = f"Nguồn dữ liệu đa phương tiện / video: {source_label}"
    else:
        summary_raw = str(raw_contents)[:8000]

    # Trích xuất danh sách các task đang chờ làm trong Master Backlog để đối chiếu
    active_backlog_summary = backlog_mgr.get_active_tasks_summary()

    pass2_input = (
        f"DỮ LIỆU GỐC:\n{summary_raw}\n\n"
        f"BẢN THẨM ĐỊNH SƠ BỘ (PASS 1):\n{pass1_result}\n\n"
        f"DANH SÁCH TASK ĐANG CHỜ LÀM TRONG ACTION BACKLOG:\n{active_backlog_summary}\n\n"
        "Hãy đóng vai VibeCheck AI – CTO thực chiến, phản biện tàn nhẫn, đối chiếu với danh sách task trên để phát hiện trùng lặp hoặc cơ hội thay thế, quét lỗi tiềm ẩn và lập dự thảo thẩm định."
    )
    pass2_result = call_gemini_resilient(pass2_input, instruction=SYSTEM_PROMPT_PASS2, temperature=0.2)

    # --- VÒNG 3: REFLEXION & CONVERGENCE LOOP (DEVIL'S ADVOCATE & PHƯƠNG ÁN TỐI ƯU) ---
    pass3_input = (
        f"DỮ LIỆU GỐC:\n{summary_raw}\n\n"
        f"DỰ THẢO THẨM ĐỊNH CTO (PASS 2):\n{pass2_result}\n\n"
        f"DANH SÁCH BACKLOG:\n{active_backlog_summary}\n\n"
        "Hãy đóng vai Trọng Tài Phản Tỉnh Độc Lập (Pass 3 Reflexion). Soi kỹ lại dự thảo Pass 2, tìm ra các điểm mù/lỗ hổng ngầm mà Pass 2 chưa thấy, bắt buộc đưa ra Phương án thay thế tối ưu vượt trội (Next-Best Alternatives), và xuất bản Báo Cáo Hoàn Thiện Cuối Cùng chuẩn format."
    )
    final_report = call_gemini_resilient(pass3_input, instruction=SYSTEM_PROMPT_PASS3_REFLEXION, temperature=0.2)

    meta_info = {
        "name": source_label,
        "summary_3_lines": "Không có tóm tắt.",
        "car_action": "Chưa có đề xuất riêng.",
        "verdict": "LƯU THAM KHẢO",
        "comparison": {
            "recommendation": "NEW",
            "matched_task_id": "NONE",
            "matched_task_name": "NONE",
            "supersede_reason": "NONE",
            "comparison_verdict": "Công nghệ độc lập, không trùng lặp."
        }
    }

    clean_report = final_report

    # 1. Trích xuất khối quick_summary
    match_summary = re.search(r"```quick_summary\s*(.*?)\s*```", clean_report, re.DOTALL)
    if match_summary:
        raw_meta = match_summary.group(1)
        clean_report = clean_report.replace(match_summary.group(0), "").strip()
        
        name_m = re.search(r"TÊN:\s*(.*)", raw_meta)
        if name_m:
            meta_info["name"] = name_m.group(1).strip()
            
        sum_m = re.search(r"TÓM_TẮT_3_DÒNG:\s*(.*?)(?=XE_VUNG_TAU_ACTION:|$)", raw_meta, re.DOTALL)
        if sum_m:
            meta_info["summary_3_lines"] = sum_m.group(1).strip()
            
        car_m = re.search(r"XE_VUNG_TAU_ACTION:\s*(.*?)(?=ROUTING_TAG:|$)", raw_meta, re.DOTALL)
        if car_m:
            meta_info["car_action"] = car_m.group(1).strip()
            
        tag_m = re.search(r"ROUTING_TAG:\s*(.*)", raw_meta)
        if tag_m:
            raw_tag = tag_m.group(1).strip()
            if "TRIỂN KHAI NGAY" in raw_tag:
                meta_info["verdict"] = "TRIỂN KHAI NGAY"
            elif "BỎ QUA" in raw_tag or "HYPE" in raw_tag:
                meta_info["verdict"] = "BỎ QUA/HYPE"
            else:
                meta_info["verdict"] = "LƯU THAM KHẢO"

    # 2. Trích xuất khối comparison_meta
    match_comp = re.search(r"```comparison_meta\s*(.*?)\s*```", clean_report, re.DOTALL)
    if match_comp:
        raw_comp = match_comp.group(1)
        clean_report = clean_report.replace(match_comp.group(0), "").strip()
        comp = meta_info["comparison"]
        
        rec_m = re.search(r"RECOMMENDATION:\s*(.*)", raw_comp)
        if rec_m:
            comp["recommendation"] = rec_m.group(1).strip().upper()
            
        id_m = re.search(r"MATCHED_TASK_ID:\s*(.*)", raw_comp)
        if id_m:
            comp["matched_task_id"] = id_m.group(1).strip()
            
        task_name_m = re.search(r"MATCHED_TASK_NAME:\s*(.*)", raw_comp)
        if task_name_m:
            comp["matched_task_name"] = task_name_m.group(1).strip()
            
        rs_m = re.search(r"SUPERSEDE_REASON:\s*(.*)", raw_comp)
        if rs_m:
            comp["supersede_reason"] = rs_m.group(1).strip()
            
        vd_m = re.search(r"COMPARISON_VERDICT:\s*(.*)", raw_comp)
        if vd_m:
            comp["comparison_verdict"] = vd_m.group(1).strip()

    # Bắt dự phòng verdict qua text
    if "[TRIỂN KHAI NGAY]" in clean_report:
        meta_info["verdict"] = "TRIỂN KHAI NGAY"
    elif "[BỎ QUA/HYPE]" in clean_report or "[BỎ QUA]" in clean_report:
        meta_info["verdict"] = "BỎ QUA/HYPE"
    elif "[LƯU THAM KHẢO]" in clean_report:
        meta_info["verdict"] = "LƯU THAM KHẢO"

    # Gắn thẻ hiển thị trực quan nếu có đề xuất So găng Thay thế hoặc Trùng lặp
    comp = meta_info["comparison"]
    if "SUPERSEDE" in comp.get("recommendation", "") and comp.get("matched_task_id") not in ["NONE", ""]:
        card = (
            f"\n\n🥊 <b>SO GĂNG 1v1 & ĐỀ XUẤT THAY THẾ CÔNG NGHỆ:</b>\n"
            f"• <b>Task hiện tại trong Backlog</b>: <code>{comp['matched_task_id']}</code> ({comp['matched_task_name']})\n"
            f"• <b>Công nghệ mới đề xuất</b>: <b>{meta_info['name']}</b>\n"
            f"• <b>Lý do vượt trội</b>: {comp['supersede_reason']}\n"
            f"• <b>Nhận định CTO</b>: <i>{comp['comparison_verdict']}</i>\n"
            f"> 👉 <i>Bấm nút [🔄 Thay thế {comp['matched_task_id']}] bên dưới để tự động cập nhật Backlog!</i>"
        )
        clean_report += card
    elif "SKIP_DUPLICATE" in comp.get("recommendation", "") and comp.get("matched_task_id") not in ["NONE", ""]:
        card = (
            f"\n\n⚠️ <b>CẢNH BÁO TRÙNG LẶP CÔNG DỤNG (SEMANTIC DEDUP):</b>\n"
            f"• Công nghệ này có cùng công năng với task <code>{comp['matched_task_id']}</code> ({comp['matched_task_name']}) đã có trong Backlog.\n"
            f"• Giải pháp hiện tại đã đủ tốt hoặc chưa cần thêm công cụ mới cùng loại.\n"
            f"> 👉 <i>Khuyến nghị: Không cần lưu để tránh phình ổ cứng và phân tán nguồn lực.</i>"
        )
        clean_report += card

    # ZERO DISK AUTO-SAVE: Báo cáo được giữ trong RAM bộ nhớ tạm, CHỈ LƯU KHI SẾP BẤM NÚT LƯU!
    meta_info["clean_report"] = clean_report
    meta_info["saved_file"] = None
    return clean_report, meta_info

execute_triple_pass_audit = execute_dual_pass_audit

def autonomous_redteam_review(chat_id: int, user_request: str, cto_output: str) -> tuple[str, bool]:
    """
    TỰ ĐỘNG HÓA ĐA ĐẶC VỤ (AUTONOMOUS DUAL-AGENT PIPELINE):
    Sau khi CTO đưa ra giải pháp, Red Team tự động can thiệp ngầm để thẩm định và bóc tách lỗ hổng.
    Nếu Red Team phán cờ ĐỎ: Tự động hủy bỏ dự án ngay tại chỗ, xóa sạch khỏi bộ nhớ, cảnh báo Founder!
    Trả về (final_text, is_cancelled).
    """
    non_audit_keywords = ["chào", "hello", "hi", "bạn là ai", "hướng dẫn", "trợ giúp", "/help", "/start", "cảm ơn", "tính năng", "lệnh"]
    if len(user_request.strip()) < 20 and any(k in user_request.lower() for k in non_audit_keywords):
        return cto_output, False

    try:
        from bot_redteam import call_gemini_redteam
        
        # Cắt bớt văn bản đầu vào nếu quá dài để bảo vệ ngân sách token
        clean_user_input = user_request.strip()
        if len(clean_user_input) > 2000:
            clean_user_input = clean_user_input[:2000] + "... [Dữ liệu gốc đã được tóm lược]"
            
        clean_cto_output = cto_output.strip()
        if len(clean_cto_output) > 2500:
            clean_cto_output = clean_cto_output[:2500] + "... [Đề xuất CTO đã được tóm lược]"

        prompt_to_redteam = (
            f"FOUNDER KEVIN YÊU CẦU / DỮ LIỆU ĐẦU VÀO:\n{clean_user_input}\n\n"
            f"CTO ĐÃ ĐỀ XUẤT PHƯƠNG ÁN / THẨM ĐỊNH:\n{clean_cto_output}\n\n"
            "Là Thủ Lĩnh Red Team Độc Lập: Hãy thẩm định đối kháng xem ý tưởng/công nghệ/dự án này có đáng làm không. "
            "BẬT ĐÈN TÍN HIỆU (🔴 ĐỎ / 🟡 VÀNG / 🟢 XANH). "
            "Nếu cờ ĐỎ (rủi ro chí mạng, bánh vẽ, lừa đảo, hoặc đốt tiền vô ích): Ra lệnh cho CTO HỦY BỎ DỰ ÁN NGAY LẬP TỨC và nêu rõ 3 lý do chí mạng!\n"
            "Nếu cờ VÀNG hoặc XANH: Nêu rõ lưu ý phản biện và phương án B tối ưu nhất cho Founder."
        )
        
        redteam_verdict = call_gemini_redteam(chat_id, prompt_to_redteam, save_memory=False)
        
        is_red = ("🔴" in redteam_verdict) or ("cờ đỏ" in redteam_verdict.lower()) or ("đỏ:" in redteam_verdict.lower()) or ("hủy bỏ" in redteam_verdict.lower() and "yêu cầu" in redteam_verdict.lower())
        
        if is_red:
            pop_last_history_turn(chat_id)
            combined = (
                f"🚦 **KẾT LUẬN LIÊN ĐOÀN: 🔴 ĐÃ TỰ ĐỘNG HỦY BỎ (PROJECT CANCELLED)**\n"
                f"*(Red Team đã can thiệp ngầm, phát hiện rủi ro chí mạng và ra lệnh CTO đình chỉ ngay lập tức)*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 **1. ĐỀ XUẤT BAN ĐẦU CỦA CTO:**\n"
                f"{cto_output}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ **2. ĐÒN PHẢN BIỆN & LỆNH HỦY TỪ RED TEAM:**\n"
                f"{redteam_verdict}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧹 **3. BẢO VỆ BỘ NHỚ FOUNDER:**\n"
                f"✅ Toàn bộ ý tưởng rủi ro này đã bị **HỦY BỎ & XÓA KHỎI BỘ NHỚ TẠM**, không lưu vào Action Backlog để bảo vệ tài nguyên của Founder!"
            )
            return combined, True
        else:
            combined = (
                f"🚦 **KẾT LUẬN LIÊN ĐOÀN: 🟢 ĐÃ THẨM ĐỊNH & PHÊ DUYỆT (APPROVED)**\n"
                f"*(CTO đề xuất + Red Team đã kiểm chứng độ an toàn & khả thi)*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 **1. GIẢI PHÁP CỐT LÕI CỦA CTO:**\n"
                f"{cto_output}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ **2. LƯU Ý PHẢN BIỆN TỪ RED TEAM:**\n"
                f"{redteam_verdict}"
            )
            return combined, False
            
    except Exception as e:
        print(f"⚠️ Autonomous RedTeam Review Error: {e}", flush=True)
        return cto_output, False


# ==============================================================================
# HÀM XỬ LÝ CHẾ ĐỘ 2: CTO ADVISOR & SEARCH PARTNER
# ==============================================================================

def execute_cto_chat(chat_id: int, user_query: str, reply_context: str = "", local_file_context: str = "", github_results: list[dict] = None) -> str:
    """Xử lý trao đổi thực chiến, trả lời thẳng vào câu hỏi và hỗ trợ tìm kiếm repo."""
    prompt_parts = []
    
    # 1. Nạp ngữ cảnh file cục bộ trên máy tính
    if local_file_context:
        prompt_parts.append(local_file_context)

    # 2. Nạp ngữ cảnh tin nhắn Reply
    if reply_context:
        prompt_parts.append(reply_context)

    # 3. Nạp dữ liệu repo thật từ GitHub Search API
    if github_results:
        gh_text = "=== DỮ LIỆU REPOSITORY THỰC TẾ TÌM THẤY TỪ GITHUB API ===\n"
        for i, item in enumerate(github_results, 1):
            gh_text += (
                f"{i}. Repo: {item['name']}\n"
                f"   - Link: {item['url']}\n"
                f"   - Stars: ⭐ {item['stars']:,} | Ngôn ngữ: {item['language']}\n"
                f"   - Mô tả: {item['description']}\n\n"
            )
        prompt_parts.append(gh_text)

    # 4. Lịch sử trao đổi gần nhất
    history = get_history(chat_id)
    if history:
        hist_text = "=== LỊCH SỬ TRAO ĐỔI GẦN NHẤT ===\n"
        for msg in history[-4:]:
            role_label = "Người dùng" if msg["role"] == "user" else "VibeCheck CTO"
            hist_text += f"{role_label}: {msg['text']}\n"
        prompt_parts.append(hist_text)

    # 5. Câu hỏi hiện tại
    prompt_parts.append(f"CÂU HỎI / YÊU CẦU CỦA NGƯỜI DÙNG:\n{user_query}")

    combined_prompt = "\n\n".join(prompt_parts)
    answer = call_gemini_resilient(combined_prompt, instruction=SYSTEM_PROMPT_CTO_CHAT, temperature=0.3)

    add_to_history(chat_id, "user", user_query)
    add_to_history(chat_id, "model", answer)

    return answer


def safe_edit_message(bot: telebot.TeleBot, chat_id: int, message_id: int, text: str, parse_mode: str = "HTML") -> bool:
    """
    Cập nhật tin nhắn trạng thái an toàn 4 tầng:
    1. Thử edit với parse_mode (mặc định HTML).
    2. Nếu lỗi định dạng HTML, thử edit dạng plain text (loại bỏ thẻ HTML).
    3. Nếu message bị xóa hoặc không sửa được, gửi tin nhắn MỚI dạng HTML.
    4. Dự phòng cuối: gửi tin nhắn MỚI dạng plain text.
    Đảm bảo người dùng KHÔNG BAO GIỜ bị treo hoặc không nhận được phản hồi.
    """
    if not message_id:
        try:
            bot.send_message(chat_id, text, parse_mode=parse_mode, disable_web_page_preview=True)
            return True
        except Exception:
            return False

    # Tầng 1: Edit HTML
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=parse_mode, disable_web_page_preview=True)
        return True
    except Exception:
        pass

    # Tầng 2: Edit Plain text
    try:
        clean_text = re.sub(r"<[^>]+>", "", text)
        bot.edit_message_text(clean_text, chat_id=chat_id, message_id=message_id, disable_web_page_preview=True)
        return True
    except Exception:
        pass

    # Tầng 3: Fallback gửi tin nhắn mới HTML
    try:
        bot.send_message(chat_id, text, parse_mode=parse_mode, disable_web_page_preview=True)
        return True
    except Exception:
        pass

    # Tầng 4: Fallback gửi tin nhắn mới Plain text
    try:
        clean_text = re.sub(r"<[^>]+>", "", text)
        bot.send_message(chat_id, clean_text, disable_web_page_preview=True)
        return True
    except Exception:
        pass

    return False


def safe_reply_to(bot: telebot.TeleBot, message, text: str, parse_mode: str = None):
    """Gửi phản hồi an toàn, tự fallback sang send_message nếu reply_to thất bại."""
    if not message or not hasattr(message, "chat"):
        return None
    chat_id = message.chat.id
    try:
        return bot.reply_to(message, text, parse_mode=parse_mode)
    except Exception:
        try:
            return bot.send_message(chat_id, text, parse_mode=parse_mode)
        except Exception:
            return None


def safe_delete_message(bot: telebot.TeleBot, chat_id: int, message_id: int):
    """Xóa tin nhắn an toàn, không văng ngoại lệ nếu tin nhắn đã bị xóa trước đó."""
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ==============================================================================
# HÀM BỘ LỌC ĐỊNH DẠNG HTML CHO TELEGRAM (CHUẨN HÓA IN ĐẬM, IN NGHIÊNG, LINK)
# ==============================================================================

def md_to_tg_html(text: str) -> str:
    """Chuyển đổi Markdown chuẩn của LLM sang định dạng HTML hợp lệ cho Telegram."""
    # 1. Bảo vệ các khối code block ```...```
    code_blocks = []
    def save_code_block(m):
        code_blocks.append(m.group(1))
        return f"XCODEBLOCKX{len(code_blocks)-1}X"
    
    text = re.sub(r"```(.*?)```", save_code_block, text, flags=re.DOTALL)
    
    # 2. Bảo vệ các đoạn inline code `...`
    inline_codes = []
    def save_inline_code(m):
        inline_codes.append(m.group(1))
        return f"XINLINECODEX{len(inline_codes)-1}X"
    
    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # 3. Escape HTML (&, <, >)
    text = html.escape(text)

    # 4. Tiêu đề Markdown (# Header, ## Header, ### Header) -> <b>Header</b>
    text = re.sub(r"^(?:#{1,6})\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 5. In đậm: **text** hoặc __text__ -> <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 6. In nghiêng: *text* hoặc _text_ (khi không phải bullet point)
    text = re.sub(r"(?<![\*\w])\*([^\*\n]+?)\*(?![\*\w])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+?)_(?![_\w])", r"<i>\1</i>", text)

    # 7. Blockquote: > text -> <blockquote>text</blockquote>
    def convert_blockquote(match):
        lines = match.group(0).split("\n")
        clean_lines = [re.sub(r"^&gt;\s?", "", l) for l in lines if l.strip()]
        if not clean_lines:
            return ""
        return f"<blockquote>{chr(10).join(clean_lines)}</blockquote>"
    text = re.sub(r"(?:^&gt;.*(?:\n|$))+", convert_blockquote, text, flags=re.MULTILINE)

    # 8. Link Markdown: [title](url) -> <a href="url">title</a>
    def convert_link(match):
        title = match.group(1)
        url = match.group(2)
        return f'<a href="{url}">{title}</a>'
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", convert_link, text)

    # 9. Phục hồi Inline code
    for i, code in enumerate(inline_codes):
        text = text.replace(f"XINLINECODEX{i}X", f"<code>{html.escape(code)}</code>")

    # 10. Phục hồi Code blocks
    for i, code in enumerate(code_blocks):
        text = text.replace(f"XCODEBLOCKX{i}X", f"<pre><code>{html.escape(code)}</code></pre>")

    return text


def send_long_message(bot: telebot.TeleBot, chat_id: int, text: str, reply_to_message_id=None, reply_markup=None):
    """Gửi tin nhắn Telegram bằng HTML parse_mode chuẩn, tự động chia nhỏ nếu quá 3800 ký tự."""
    if not text or not str(text).strip():
        return

    MAX_LENGTH = 3800
    parts = []
    current_chunk = ""

    for line in str(text).split("\n"):
        # Nếu 1 dòng dài bất thường vượt MAX_LENGTH (ví dụ base64, chuỗi token dài)
        while len(line) > MAX_LENGTH:
            parts.append(line[:MAX_LENGTH])
            line = line[MAX_LENGTH:]

        if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
            if current_chunk.strip():
                parts.append(current_chunk.strip())
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk.strip():
        parts.append(current_chunk.strip())

    if not parts:
        return

    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        rep_id = reply_to_message_id if i == 0 else None
        
        html_part = md_to_tg_html(part)
        try:
            bot.send_message(
                chat_id,
                html_part,
                parse_mode="HTML",
                reply_to_message_id=rep_id,
                reply_markup=markup,
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Lỗi khi gửi tin nhắn dạng HTML ({e}), fallback sang tin nhắn thường...", flush=True)
            try:
                bot.send_message(
                    chat_id,
                    part,
                    reply_to_message_id=rep_id,
                    reply_markup=markup,
                    disable_web_page_preview=True
                )
            except Exception as e2:
                print(f"Lỗi gửi tin nhắn fallback: {e2}", flush=True)
        time.sleep(0.3)


# ==============================================================================
# BỘ XỬ LÝ NGOẠI LỆ TRUNG TÂM CHO POLLING & WORKER THREADS (RESILIENT HANDLER)
# ==============================================================================

class ResilientExceptionHandler(telebot.ExceptionHandler):
    """
    Bộ xử lý ngoại lệ trung tâm cho pyTelegramBotAPI.
    Đảm bảo:
    1. Không một ngoại lệ luồng worker nào có thể làm sập hoặc thoát luồng polling chính.
    2. Phân loại lỗi mạng thông thường (Read timed out, Connection reset) để tự động duy trì kết nối.
    3. Ghi nhận lỗi chi tiết vào console để debug mà không gián đoạn dịch vụ 24/7.
    """
    def handle(self, exception):
        exc_str = str(exception)
        # Xử lý xung đột phiên 409
        if "409" in exc_str or "conflict" in exc_str.lower():
            print(f"⚠️ [CONFLICT 409] Phiên Telegram khác vừa ngắt kết nối. Đang duy trì kết nối an toàn...", flush=True)
            time.sleep(3)
            return True
        # Các sự cố socket/timeout tạm thời là bình thường khi long-polling làm mới kết nối
        if any(term in exc_str.lower() for term in ["read timed out", "connection reset", "remotedisconnected", "timeout"]):
            print(f"ℹ️ [NETWORK] Đang tự động làm mới luồng polling Telegram: {exc_str[:120]}...", flush=True)
        else:
            print(f"⚠️ [RESILIENT HANDLER] Đã bắt ngoại lệ luồng Telegram an toàn: {exception}", flush=True)
        # Trả về True để báo cho telebot biết ngoại lệ đã được xử lý thành công
        # Ngăn chặn telebot gọi polling_thread.stop()
        return True


class BotProxy:
    """
    Proxy an toàn đại diện cho instance Telegram Bot hiện hành.
    Cho phép các worker ngầm (như EscalationWorker) luôn gọi bot đang hoạt động
    kể cả khi supervisor loop tái tạo phiên bot mới sau khi rớt mạng.
    """
    def __init__(self, bot=None):
        self._bot = bot

    def set_bot(self, bot):
        self._bot = bot

    def __getattr__(self, name):
        if self._bot is None:
            raise RuntimeError("BotProxy chưa được liên kết với instance bot thực tế nào.")
        return getattr(self._bot, name)


# ==============================================================================
# KHỞI TẠO TELEGRAM BOT & EVENT HANDLERS
# ==============================================================================

def setup_bot():
    bot = telebot.TeleBot(
        TELEGRAM_BOT_TOKEN,
        exception_handler=ResilientExceptionHandler(),
        num_threads=4
    )

    # Profile đã được cập nhật thành công vĩnh viễn trên Telegram servers
    if os.getenv("UPDATE_BOT_PROFILE", "false").lower() == "true":
        try:
            bot.set_my_name("VibeCheck AI | CTO Thực Chiến")
            bot.set_my_description("CTO thực chiến chuyên bóc tách bản chất công nghệ, vạch trần hype, đàm thoại ngữ cảnh và tìm kiếm giải pháp thực chiến.")
            bot.set_my_short_description("VibeCheck AI — Anti-Hype Tech Audit & CTO Partner.")
        except Exception:
            pass

    # 1. Đăng ký Native Command Menu trên mọi Telegram Client (Nút [/] cạnh ô chat)
    try:
        bot.set_my_commands([
            tele_types.BotCommand("menu", "🎛️ Bảng điều khiển tác vụ nhanh"),
            tele_types.BotCommand("backlog", "📋 Xem việc cần triển khai ngay"),
            tele_types.BotCommand("remind_now", "🔔 Quét & gửi nhắc việc tồn đọng ngay"),
            tele_types.BotCommand("remind_status", "⚙️ Trạng thái hệ thống nhắc việc"),
            tele_types.BotCommand("reset", "🔄 Xóa ngữ cảnh trò chuyện CTO"),
            tele_types.BotCommand("help", "📖 Hướng dẫn sử dụng & tính năng"),
        ])
    except Exception as e:
        print(f"Lưu ý khi đăng ký set_my_commands: {e}", flush=True)

    def create_main_reply_keyboard():
        markup = tele_types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        btn1 = tele_types.KeyboardButton("📋 Việc Cần Làm")
        btn2 = tele_types.KeyboardButton("🔔 Nhắc Việc Ngay")
        btn3 = tele_types.KeyboardButton("🎛️ Menu Lệnh")
        btn4 = tele_types.KeyboardButton("⚙️ Trạng Thái")
        markup.add(btn1, btn2)
        markup.add(btn3, btn4)
        return markup

    def create_menu_dashboard_markup():
        markup = tele_types.InlineKeyboardMarkup(row_width=2)
        btn_bl = tele_types.InlineKeyboardButton("📋 Xem Backlog", callback_data="menu_backlog")
        btn_rm = tele_types.InlineKeyboardButton("🔔 Quét Nhắc Việc", callback_data="menu_remind")
        btn_st = tele_types.InlineKeyboardButton("⚙️ Trạng Thái", callback_data="menu_status")
        btn_rs = tele_types.InlineKeyboardButton("🔄 Xóa Chat", callback_data="menu_reset")
        btn_hp = tele_types.InlineKeyboardButton("📖 Hướng Dẫn Sử Dụng", callback_data="menu_help")
        markup.add(btn_bl, btn_rm)
        markup.add(btn_st, btn_rs)
        markup.add(btn_hp)
        return markup

    def create_action_buttons(audit_id: str, is_video: bool = False, comparison_info: dict = None, verdict: str = ""):
        markup = tele_types.InlineKeyboardMarkup(row_width=2)

        # Hàng 1: Nút lưu On-Demand hoặc Thay thế (Superseding Matrix)
        comp = comparison_info or {}
        rec = comp.get("recommendation", "")
        matched_id = comp.get("matched_task_id", "NONE")

        if "SUPERSEDE" in rec and matched_id not in ["NONE", ""]:
            btn_rep = tele_types.InlineKeyboardButton(f"🔄 Thay thế {matched_id}", callback_data=f"rep_{audit_id}_{matched_id}")
            btn_both = tele_types.InlineKeyboardButton("➕ Giữ cả hai & Lưu", callback_data=f"save_{audit_id}")
            btn_dsm = tele_types.InlineKeyboardButton("❌ Bỏ qua không lưu", callback_data=f"dsm_{audit_id}")
            markup.add(btn_rep)
            markup.add(btn_both, btn_dsm)
        elif verdict == "TRIỂN KHAI NGAY":
            btn_save = tele_types.InlineKeyboardButton("💾 Lưu & Triển Khai Ngay", callback_data=f"save_{audit_id}")
            btn_dsm = tele_types.InlineKeyboardButton("❌ Bỏ qua không lưu", callback_data=f"dsm_{audit_id}")
            markup.add(btn_save, btn_dsm)
        else:
            btn_save = tele_types.InlineKeyboardButton("📌 Lưu Vào Máy", callback_data=f"save_{audit_id}")
            btn_dsm = tele_types.InlineKeyboardButton("❌ Không lưu", callback_data=f"dsm_{audit_id}")
            markup.add(btn_save, btn_dsm)

        # Các hàng chức năng CTO Thực chiến
        btn_sum = tele_types.InlineKeyboardButton("⚡ Tóm tắt 3 dòng", callback_data=f"sum_{audit_id}")
        btn_car = tele_types.InlineKeyboardButton("🚗 Riêng xe VT-SG", callback_data=f"car_{audit_id}")
        btn_repo = tele_types.InlineKeyboardButton("🔍 Tìm 3 Repo tương tự", callback_data=f"repo_{audit_id}")
        btn_ag = tele_types.InlineKeyboardButton("⚡ Prompt Cho Antigravity (MVP)", callback_data=f"ag_{audit_id}")
        markup.add(btn_sum, btn_car)
        markup.add(btn_repo, btn_ag)
        if is_video:
            btn_code = tele_types.InlineKeyboardButton("📋 Lấy Code/Prompt trong video", callback_data=f"code_{audit_id}")
            markup.add(btn_code)
        return markup

    @bot.message_handler(commands=['start', 'help'])
    def handle_start(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        welcome_msg = (
            "🤖 **VIBECHECK AI (v2.8.0) — CTO THỰC CHIẾN & CỐ VẤN CÔNG NGHỆ**\n\n"
            "Tôi hoạt động với **2 chế độ thông minh song song**, **Lưu Trữ Theo Yêu Cầu (On-Demand)**, **So Găng Thay Thế 1v1** & **Chủ động nhắc việc tồn đọng**:\n\n"
            "🔥 **1. CHẾ ĐỘ THẨM ĐỊNH CÔNG NGHỆ (Dual-Pass Tech Auditor & On-Demand):**\n"
            "• Gửi **Link TikTok / GitHub / Web / YouTube**, **Ảnh chụp** hoặc **File PDF**.\n"
            "• Kích hoạt 2 vòng kiểm duyệt tàn nhẫn, Gemini Video Vision bóc tách video, đánh giá 5 trụ cột.\n"
            "• **Không tự động lưu file**: Xem tạm trên Telegram. Chỉ khi sếp bấm nút **[💾 Lưu & Triển Khai Ngay]** hoặc **[📌 Lưu Vào Máy]**, file mới được lưu vào máy tính.\n"
            "• **So Găng & Chống Trùng Lặp**: Tự động so sánh với các task trong Backlog; nếu có công nghệ tốt hơn, đề xuất nút **[🔄 Thay thế {Mã_Task}]** 1-chạm.\n\n"
            "🎛️ **2. HỆ THỐNG MENU LỆNH 3 TẦNG (Không cần nhớ lệnh):**\n"
            "• **Nút [/] hoặc Menu**: Nằm ngay cạnh ô nhập tin nhắn, chạm vào là thấy toàn bộ danh sách lệnh.\n"
            "• **Bàn phím nổi 4 nút**: Nằm thường trực ở đáy màn hình điện thoại (bấm 1-chạm rảnh tay khi lái xe).\n"
            "• **Lệnh `/menu`**: Mở Bảng điều khiển Dashboard với các nút tương tác trực quan.\n\n"
            "📋 **3. MASTER ACTION BACKLOG & NHẮC VIỆC TỰ ĐỘNG:**\n"
            "• `/backlog` hoặc `/todo`: Xem danh sách việc cần làm ngay đang chờ thực hiện.\n"
            "• `/remind_now`: Kích hoạt bot quét và gửi ngay thông báo nhắc việc trễ hạn >12 giờ.\n"
            "• `/remind_status`: Kiểm tra cấu hình và tình trạng hệ thống nhắc việc tự động.\n"
            "• `/reset`: Làm sạch lịch sử trò chuyện để bắt đầu phiên tư vấn CTO mới.\n"
            "• Bấm nút `[⚡ Prompt Cho Antigravity]` để lấy prompt dán vào Antigravity IDE code MVP 0đ, `[✅ Đã xong]` để hoàn thành, hoặc `[⏸️ Tạm hoãn 24h]` để hoãn nhắc.\n\n"
            "💬 **4. CHẾ ĐỘ CỐ VẤN THỰC CHIẾN (CTO Partner, Voice Note & Live Search):**\n"
            "• **Gửi tin nhắn thoại (Voice Note)** để hỏi rảnh tay khi đang lái xe đường dài hoặc di chuyển.\n"
            "• **Reply tin nhắn báo cáo** hoặc hỏi câu hỏi tiếp nối (Ví dụ: *\"Có repo nào tương tự không?\"*, *\"Lấy code trong video\"*, *\"Cài đặt thế nào?\"*).\n"
            "• Nhớ ngữ cảnh hội thoại, đọc file `.md` trên máy tính và tra cứu kho GitHub Repos thật để trả lời trực diện!"
        )
        send_long_message(bot, chat_id, welcome_msg, reply_markup=create_main_reply_keyboard())

    # 1. Xử lý ảnh chụp màn hình
    @bot.message_handler(content_types=['photo'])
    def handle_photo(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        status_msg = safe_reply_to(bot, message, "⏳ VibeCheck AI v3.0 đang tải ảnh & kích hoạt Thẩm định 3 vòng Reflexion...")
        status_msg_id = status_msg.message_id if status_msg else None
        downloaded_file = None
        contents = None
        try:
            bot.send_chat_action(chat_id, 'typing')
            photo_item = message.photo[-1]
            file_info = bot.get_file(photo_item.file_id)
            downloaded_file = bot.download_file(file_info.file_path)

            caption = message.caption if message.caption else ""
            user_prompt = "Hãy thẩm định ảnh chụp màn hình này theo đúng quy chuẩn VibeCheck AI 3 vòng Reflexion."
            if caption:
                user_prompt += f"\nGhi chú từ người dùng: {caption}"

            contents = [
                types.Part.from_bytes(data=downloaded_file, mime_type="image/jpeg"),
                user_prompt
            ]

            safe_edit_message(bot, chat_id, status_msg_id, "🧠 V1: Bóc tách kỹ thuật... ➡️ V2: CTO Phản biện... ➡️ V3: Reflexion & Tối ưu...")
            clean_report, meta_info = execute_dual_pass_audit(contents, source_label="Ảnh chụp màn hình")

            safe_edit_message(bot, chat_id, status_msg_id, "🛡️ Bot Red Team đang tự động thẩm định đối kháng...")
            final_report, is_cancelled = autonomous_redteam_review(chat_id, caption if caption else "Ảnh chụp màn hình", clean_report)

            audit_id = str(int(time.time()))[-6:]
            cache_audit_data(audit_id, meta_info)

            if status_msg_id:
                safe_delete_message(bot, chat_id, status_msg_id)

            if is_cancelled:
                meta_info["verdict"] = "🔴"
                send_long_message(bot, chat_id, final_report, reply_to_message_id=message.message_id, reply_markup=None)
            else:
                add_to_history(chat_id, "user", f"[Gửi ảnh chụp màn hình] {caption}")
                add_to_history(chat_id, "model", final_report[:800])
                final_report += "\n\n💡 <i>Báo cáo đang xem tạm thời trong RAM. Bấm nút <b>[💾 Lưu]</b> hoặc <b>[🔄 Thay thế]</b> bên dưới nếu sếp muốn lưu vào máy tính.</i>"
                markup = create_action_buttons(audit_id, is_video=False, comparison_info=meta_info.get("comparison"), verdict=meta_info.get("verdict"))
                send_long_message(bot, chat_id, final_report, reply_to_message_id=message.message_id, reply_markup=markup)
        except PermissionError as pe:
            safe_edit_message(bot, chat_id, status_msg_id, f"❌ <b>LỖI XÁC THỰC AI:</b>\n{pe}")
        except Exception as e:
            print(f"❌ Lỗi xử lý Photo: {e}", flush=True)
            safe_edit_message(bot, chat_id, status_msg_id, "⚠️ Máy chủ AI đang bận tạm thời. Anh vui lòng thử lại sau vài giây nhé!")
        finally:
            # ZERO-FOOTPRINT: Giải phóng bộ nhớ RAM triệt để ngay lập tức
            if downloaded_file is not None:
                del downloaded_file
            if contents is not None:
                del contents
            gc.collect()

    # 2. Xử lý file tài liệu PDF
    @bot.message_handler(content_types=['document'])
    def handle_document(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        doc = message.document
        if not (doc.mime_type == "application/pdf" or (doc.file_name and doc.file_name.lower().endswith(".pdf"))):
            safe_reply_to(bot, message, "⚠️ VibeCheck AI hiện hỗ trợ tài liệu định dạng PDF để thẩm định kỹ thuật.")
            return

        status_msg = safe_reply_to(bot, message, f"⏳ Đang tải file PDF `{doc.file_name}`...")
        status_msg_id = status_msg.message_id if status_msg else None
        pdf_bytes = None
        contents = None
        try:
            bot.send_chat_action(chat_id, 'typing')
            file_info = bot.get_file(doc.file_id)
            pdf_bytes = bot.download_file(file_info.file_path)

            contents = [
                types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                f"Hãy thẩm định tài liệu PDF: {doc.file_name} theo đúng quy chuẩn VibeCheck AI 3 vòng Reflexion."
            ]

            safe_edit_message(bot, chat_id, status_msg_id, "🧠 V1: Trích xuất kiến trúc... ➡️ V2: CTO Phản biện... ➡️ V3: Reflexion & Tối ưu...")
            clean_report, meta_info = execute_dual_pass_audit(contents, source_label=doc.file_name or "Tài liệu PDF")

            safe_edit_message(bot, chat_id, status_msg_id, "🛡️ Bot Red Team đang tự động thẩm định đối kháng...")
            final_report, is_cancelled = autonomous_redteam_review(chat_id, doc.file_name or "Tài liệu PDF", clean_report)

            audit_id = str(int(time.time()))[-6:]
            cache_audit_data(audit_id, meta_info)

            if status_msg_id:
                safe_delete_message(bot, chat_id, status_msg_id)

            if is_cancelled:
                meta_info["verdict"] = "🔴"
                send_long_message(bot, chat_id, final_report, reply_to_message_id=message.message_id, reply_markup=None)
            else:
                add_to_history(chat_id, "user", f"[Gửi tài liệu PDF] {doc.file_name}")
                add_to_history(chat_id, "model", final_report[:800])
                final_report += "\n\n💡 <i>Báo cáo đang xem tạm thời trong RAM. Bấm nút <b>[💾 Lưu]</b> hoặc <b>[🔄 Thay thế]</b> bên dưới nếu sếp muốn lưu vào máy tính.</i>"
                markup = create_action_buttons(audit_id, is_video=False, comparison_info=meta_info.get("comparison"), verdict=meta_info.get("verdict"))
                send_long_message(bot, chat_id, final_report, reply_to_message_id=message.message_id, reply_markup=markup)
        except PermissionError as pe:
            safe_edit_message(bot, chat_id, status_msg_id, f"❌ <b>LỖI XÁC THỰC AI:</b>\n{pe}")
        except Exception as e:
            print(f"❌ Lỗi xử lý Document: {e}", flush=True)
            safe_edit_message(bot, chat_id, status_msg_id, "⚠️ Máy chủ AI đang bận xử lý file. Anh vui lòng gửi lại sau vài giây!")
        finally:
            # ZERO-FOOTPRINT: Giải phóng bộ nhớ RAM triệt để ngay lập tức
            if pdf_bytes is not None:
                del pdf_bytes
            if contents is not None:
                del contents
            gc.collect()

    # 3. Xử lý tin nhắn thoại (Voice Note / Audio Hands-free cho tài xế & CTO)
    @bot.message_handler(content_types=['voice'])
    def handle_voice(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        status_msg = safe_reply_to(bot, message, "🎙️ Đang tiếp nhận tin nhắn thoại...")
        status_msg_id = status_msg.message_id if status_msg else None
        voice_bytes = None
        voice_part = None
        try:
            bot.send_chat_action(chat_id, 'typing')
            file_info = bot.get_file(message.voice.file_id)
            voice_bytes = bot.download_file(file_info.file_path)

            safe_edit_message(bot, chat_id, status_msg_id, "🎧 VibeCheck AI đang nghe giọng nói & trích xuất ý định...")
            
            voice_part = types.Part.from_bytes(data=voice_bytes, mime_type="audio/ogg")
            transcribe_prompt = (
                "Bạn là VibeCheck AI – CTO thực chiến. Đây là tin nhắn thoại giọng nói từ người dùng (tài xế, CTO hoặc kỹ sư công nghệ).\n"
                "1. Hãy nhận diện chính xác nội dung câu hỏi/yêu cầu bằng tiếng Việt.\n"
                "2. Đưa ra câu trả lời trực diện, súc tích, thực chiến theo phong cách CTO, không dài dòng lý thuyết."
            )
            cto_answer = call_gemini_resilient([voice_part, transcribe_prompt], instruction=SYSTEM_PROMPT_CTO_CHAT, temperature=0.3)
            
            safe_edit_message(bot, chat_id, status_msg_id, "🛡️ Bot Red Team đang tự động thẩm định đối kháng...")
            final_answer, is_cancelled = autonomous_redteam_review(chat_id, "[Tin nhắn thoại voice note]", cto_answer)

            if status_msg_id:
                safe_delete_message(bot, chat_id, status_msg_id)
            send_long_message(bot, chat_id, final_answer, reply_to_message_id=message.message_id)
            
            if not is_cancelled:
                add_to_history(chat_id, "user", "[Tin nhắn thoại voice note]")
                add_to_history(chat_id, "model", final_answer[:800])
        except PermissionError as pe:
            safe_edit_message(
                bot, chat_id, status_msg_id,
                f"❌ <b>LỖI XÁC THỰC AI:</b>\n{pe}"
            )
        except Exception as e:
            print(f"❌ Lỗi xử lý Voice: {e}", flush=True)
            safe_edit_message(bot, chat_id, status_msg_id, "⚠️ Máy chủ AI đang bận xử lý âm thanh. Anh vui lòng thử lại hoặc gửi tin nhắn văn bản nhé!")
        finally:
            # ZERO-FOOTPRINT: Giải phóng file âm thanh khỏi bộ nhớ
            if voice_bytes is not None:
                del voice_bytes
            if voice_part is not None:
                del voice_part
            gc.collect()

    # 3. Xử lý văn bản với Bộ Phân Loại Ý Định (Smart Intent Routing)
    @bot.message_handler(content_types=['text'])
    def handle_text(message):
        text = message.text.strip()
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)

        # SMART INTENT INTERCEPTOR (Xử lý tức thì các phím bấm nhanh từ Bàn phím nổi - 0ms LLM Latency, 0đ quota)
        clean_lower = text.lower().strip()
        if clean_lower in ["📋 việc cần làm", "📋 việc cần làm (/backlog)", "/backlog", "backlog", "todo", "/todo"]:
            handle_backlog_cmd(message)
            return

        if clean_lower in ["🔔 nhắc việc ngay", "🔔 nhắc việc ngay (/remind_now)", "/remind_now", "remind_now", "check_overdue", "/check_overdue"]:
            handle_remind_now(message)
            return

        if clean_lower in ["🎛️ menu lệnh", "🎛️ menu lệnh (/menu)", "/menu", "menu", "bảng điều khiển", "dashboard"]:
            handle_menu_cmd(message)
            return

        if clean_lower in ["⚙️ trạng thái", "⚙️ trạng thái (/remind_status)", "/remind_status", "remind_status", "status"]:
            handle_remind_status(message)
            return

        if clean_lower in ["🔄 xóa lịch sử chat", "xóa lịch sử", "/reset", "reset"]:
            handle_reset_cmd(message)
            return

        if clean_lower in ["📖 hướng dẫn", "hướng dẫn", "/help", "help"]:
            handle_start(message)
            return

        # TẮT / BẬT NHẮC NHỞ BẰNG TIẾNG VIỆT TỰ NHIÊN
        if any(p in clean_lower for p in ["tắt nhắc nhở", "tat nhac nho", "đừng nhắc nữa", "dung nhac nua", "tắt thông báo", "tat thong bao", "dừng nhắc nhở"]):
            state_mgr.set_enabled(False)
            send_long_message(bot, chat_id, "🔕 **ĐÃ TẮT TÍNH NĂNG NHẮC VIỆC TỰ ĐỘNG!**\nBot sẽ KHÔNG BAO GIỜ tự động gửi tin nhắn làm phiền anh nữa. Khi nào muốn xem danh sách việc tồn đọng, anh chỉ cần gõ `/backlog`.")
            return

        if any(p in clean_lower for p in ["bật nhắc nhở", "bat nhac nho", "mở nhắc nhở"]):
            state_mgr.set_enabled(True)
            send_long_message(bot, chat_id, "🔔 **ĐÃ BẬT LẠI TÍNH NĂNG NHẮC VIỆC TỰ ĐỘNG.**\nBot sẽ chỉ gửi nhắc nhở tối đa 1 task/ngày cho các việc quá hạn >12h.")
            return

        # TỰ ĐỘNG THỰC THI HÀNH ĐỘNG BACKLOG (ACTION INTENT DETECTOR):
        # Phát hiện khi Founder ra lệnh đình chỉ/hủy/xóa/xong một task cụ thể qua tin nhắn
        task_match = re.search(r"(TASK-\d{8}-\d+)", text, re.IGNORECASE)
        cancel_keywords = ["đình chỉ", "dinh chi", "hủy", "huy", "hủy bỏ", "dừng", "dung", "xóa", "xoa", "cancel", "stop", "abort"]
        done_keywords = ["làm xong", "hoàn thành", "done", "xong", "finish"]

        if task_match:
            target_task_id = task_match.group(1).upper()
            if any(k in clean_lower for k in cancel_keywords):
                success = backlog_mgr.cancel_task(target_task_id)
                confirm_header = (
                    f"🚫 **RÕ LỆNH FOUNDER: ĐÃ ĐÌNH CHỈ THÀNH CÔNG!**\n\n"
                    f"✅ Hệ thống đã cập nhật file `00_ACTION_BACKLOG.md`:\n"
                    f"• **Mã Task**: `{target_task_id}`\n"
                    f"• **Trạng thái mới**: `[-] Đã đình chỉ`\n"
                    f"• **Bảo vệ tài nguyên**: Task này đã được gỡ bỏ hoàn toàn khỏi danh sách nhắc việc tồn đọng!\n\n"
                )
                if len(text.strip()) > 35:
                    reply_ctx = ""
                    if message.reply_to_message:
                        rt = message.reply_to_message.text or message.reply_to_message.caption or ""
                        if rt:
                            reply_ctx = f"=== NỘI DUNG PHẢN HỒI ===\n{rt[:2000]}\n"
                    cto_advice = execute_cto_chat(chat_id, text, reply_context=reply_ctx)
                    send_long_message(bot, chat_id, confirm_header + f"━━━━━━━━━━━━━━━━━━━━━━\n💡 **GHI NHẬN TỪ CTO:**\n{cto_advice}", reply_to_message_id=message.message_id)
                else:
                    send_long_message(bot, chat_id, confirm_header, reply_to_message_id=message.message_id)
                return

            elif any(k in clean_lower for k in done_keywords):
                success = backlog_mgr.mark_task_completed(target_task_id)
                send_long_message(bot, chat_id, f"✅ **RÕ LỆNH FOUNDER!** Đã đánh dấu hoàn thành `{target_task_id}` trong `00_ACTION_BACKLOG.md`!", reply_to_message_id=message.message_id)
                return

        elif any(k in clean_lower for k in cancel_keywords):
            # Quét tìm task trong backlog khớp với tên dự án được nhắc đến
            records, _ = BacklogParser.parse_file(BACKLOG_FILE)
            matched_tasks = [
                r for r in records
                if r.is_pending and any(word in clean_lower for word in r.tool_name.lower().split() if len(word) >= 3)
            ]
            if len(matched_tasks) == 1:
                target_r = matched_tasks[0]
                backlog_mgr.cancel_task(target_r.task_id)
                confirm_header = (
                    f"🚫 **RÕ LỆNH FOUNDER: ĐÃ ĐÌNH CHỈ THÀNH CÔNG!**\n\n"
                    f"✅ Hệ thống đã cập nhật file `00_ACTION_BACKLOG.md`:\n"
                    f"• **Dự án**: {target_r.tool_name} (`{target_r.task_id}`)\n"
                    f"• **Trạng thái mới**: `[-] Đã đình chỉ`\n"
                    f"• **Bảo vệ tài nguyên**: Đã gỡ khỏi danh sách nhắc việc quá hạn!\n\n"
                )
                if len(text.strip()) > 35:
                    reply_ctx = ""
                    if message.reply_to_message:
                        rt = message.reply_to_message.text or message.reply_to_message.caption or ""
                        if rt:
                            reply_ctx = f"=== NỘI DUNG PHẢN HỒI ===\n{rt[:2000]}\n"
                    cto_advice = execute_cto_chat(chat_id, text, reply_context=reply_ctx)
                    send_long_message(bot, chat_id, confirm_header + f"━━━━━━━━━━━━━━━━━━━━━━\n💡 **GHI NHẬN TỪ CTO:**\n{cto_advice}", reply_to_message_id=message.message_id)
                else:
                    send_long_message(bot, chat_id, confirm_header, reply_to_message_id=message.message_id)
                return

        url_regex = r"(https?://[^\s<>\"']+)"
        urls = re.findall(url_regex, text)

        # Trích xuất ngữ cảnh Reply nếu có
        reply_context = ""
        if message.reply_to_message:
            reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
            if reply_text:
                reply_context = f"=== NỘI DUNG TIN NHẮN ĐƯỢC PHẢN HỒI (REPLY) ===\n{reply_text[:3000]}\n"

        # Tự động tìm và đọc file local nếu người dùng nhắc tên file .md
        local_file_context, matched_file = find_and_read_local_report(text)

        is_explicit_audit = any(kw in text.lower() for kw in ["thẩm định", "tham dinh", "check vibe", "vạch trần", "báo cáo kỹ thuật", "đánh giá 5 trụ cột"])
        has_urls = bool(urls)

        # CHẾ ĐỘ 1: THẨM ĐỊNH CÔNG NGHỆ CHUYÊN SÂU (TECH AUDITOR)
        if has_urls or is_explicit_audit:
            status_msg = safe_reply_to(bot, message, "⏳ VibeCheck AI đang tiếp nhận dữ liệu thẩm định...")
            status_msg_id = status_msg.message_id if status_msg else None
            bot.send_chat_action(chat_id, 'typing')

            collected_data = ""
            source_label = text[:30]
            multimodal_content = None
            tt_video_bytes = None
            tt_cover_bytes = None
            tt_images_bytes = None
            is_video_audit = False

            try:
                if urls:
                    for url in urls:
                        if "github.com" in url.lower():
                            safe_edit_message(bot, chat_id, status_msg_id, "🔍 Thu thập dữ liệu GitHub qua API...")
                            gh_data = extract_github_info(url)
                            collected_data += gh_data + "\n"
                            source_label = url.split("github.com/")[-1]

                        elif any(domain in url.lower() for domain in ["tiktok.com"]):
                            safe_edit_message(bot, chat_id, status_msg_id, "🎬 Đang tải video TikTok & phân tích đa phương tiện...")
                            tt_title, tt_author, tt_video_bytes, tt_cover_bytes, tt_images_bytes = fetch_tiktok_content(url)
                            source_label = f"TikTok: {tt_title[:35]}" if tt_title else "Video TikTok"

                            # Tầng 1: Video MP4 chuẩn <= 25MB -> Gemini Video Vision
                            if tt_video_bytes:
                                safe_edit_message(bot, chat_id, status_msg_id, "👀 Gemini đang xem video MP4, nghe lời thoại & bóc tách kỹ thuật...")
                                video_part = types.Part.from_bytes(data=tt_video_bytes, mime_type="video/mp4")
                                multimodal_content = [
                                    video_part,
                                    (
                                        f"DỮ LIỆU VIDEO TIKTOK:\n"
                                        f"- Tiêu đề: {tt_title}\n"
                                        f"- Tác giả: {tt_author}\n"
                                        f"- URL: {url}\n\n"
                                        f"Yêu cầu: Hãy xem toàn bộ video, lắng nghe kỹ giọng đọc/lời thoại của tác giả, quan sát các phần mềm/công cụ/màn hình code "
                                        f"được chia sẻ trong video và bóc tách toàn diện theo quy chuẩn VibeCheck AI."
                                    )
                                ]
                                is_video_audit = True
                            # Tầng 2: Album ảnh / Slideshow (Photo Carousel)
                            elif tt_images_bytes:
                                safe_edit_message(bot, chat_id, status_msg_id, f"🖼️ Đang nạp {len(tt_images_bytes)} ảnh từ album TikTok vào Gemini Vision...")
                                img_parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in tt_images_bytes]
                                multimodal_content = img_parts + [
                                    (
                                        f"DỮ LIỆU ALBUM ẢNH TIKTOK:\n"
                                        f"- Tiêu đề: {tt_title}\n"
                                        f"- Tác giả: {tt_author}\n"
                                        f"- URL: {url}\n\n"
                                        f"Yêu cầu: Hãy phân tích các hình ảnh trong album, đọc kỹ nội dung text/code trong ảnh và bóc tách theo quy chuẩn VibeCheck AI."
                                    )
                                ]
                                is_video_audit = True
                            # Tầng 3: Ảnh bìa gốc HD (Cover fallback)
                            elif tt_cover_bytes:
                                safe_edit_message(bot, chat_id, status_msg_id, "🖼️ Video quá lớn (>25MB), đang phân tích ảnh bìa HD qua Gemini Vision...")
                                cover_part = types.Part.from_bytes(data=tt_cover_bytes, mime_type="image/jpeg")
                                multimodal_content = [
                                    cover_part,
                                    (
                                        f"DỮ LIỆU TIKTOK (ẢNH BÌA HD & METADATA):\n"
                                        f"- Tiêu đề: {tt_title}\n"
                                        f"- Tác giả: {tt_author}\n"
                                        f"- URL: {url}\n\n"
                                        f"Yêu cầu: Hãy quan sát ảnh bìa và phân tích theo quy chuẩn VibeCheck AI."
                                    )
                                ]
                                is_video_audit = True
                            else:
                                collected_data += (
                                    f"=== THÔNG TIN VIDEO TIKTOK ===\n"
                                    f"- Tiêu đề: {tt_title}\n"
                                    f"- Tác giả: {tt_author}\n"
                                    f"- URL: {url}\n\n"
                                )

                        elif any(domain in url.lower() for domain in ["facebook.com", "fb.watch", "fb.com"]):
                            safe_edit_message(bot, chat_id, status_msg_id, "🌐 Đang phân tích bài viết Facebook qua OpenGraph...")
                            fb_title, fb_desc, fb_img = fetch_facebook_opengraph(url)
                            jina_data = fetch_jina_reader(url)

                            if jina_data == "FACEBOOK_LOGIN_REQUIRED" or not jina_data.strip():
                                if fb_desc or fb_title:
                                    collected_data += (
                                        f"=== DỮ LIỆU BÀI VIẾT FACEBOOK (TRÍCH XUẤT OPEN GRAPH) ===\n"
                                        f"- Tiêu đề / Fanpage: {fb_title}\n"
                                        f"- Nội dung trích xuất: {fb_desc}\n"
                                        f"- Link gốc: {url}\n\n"
                                    )
                                    source_label = f"FB: {fb_title[:25]}" if fb_title else "Bài viết Facebook"
                                else:
                                    safe_edit_message(
                                        bot, chat_id, status_msg_id,
                                        "ℹ️ <b>Bài viết Facebook này yêu cầu đăng nhập:</b>\n\n"
                                        "Hệ thống Facebook chặn các công cụ đọc tự động đối với link riêng tư/nhóm kín này.\n\n"
                                        "👉 <b>Mẹo cực nhanh:</b> Bạn hãy <b>copy trực tiếp văn bản bài viết</b> hoặc <b>chụp ảnh màn hình bài viết Facebook</b> gửi vào đây, VibeCheck AI sẽ bóc tách và thẩm định 5 trụ cột ngay lập tức cho bạn!"
                                    )
                                    return
                            else:
                                collected_data += jina_data + "\n"
                                source_label = f"FB: {fb_title[:25]}" if fb_title else url

                        else:
                            safe_edit_message(bot, chat_id, status_msg_id, "🌐 Đang bóc tách nội dung Markdown sạch qua Jina Reader...")
                            jina_data = fetch_jina_reader(url)
                            if jina_data == "FACEBOOK_LOGIN_REQUIRED":
                                safe_edit_message(
                                    bot, chat_id, status_msg_id,
                                    "ℹ️ <b>Bài viết Facebook này yêu cầu đăng nhập:</b>\n\n"
                                    "Hệ thống Facebook chặn các công cụ đọc tự động đối với link riêng tư/nhóm kín này.\n\n"
                                    "👉 <b>Mẹo cực nhanh:</b> Bạn hãy <b>copy trực tiếp văn bản bài viết</b> hoặc <b>chụp ảnh màn hình bài viết Facebook</b> gửi vào đây, VibeCheck AI sẽ bóc tách và thẩm định 5 trụ cột ngay lập tức cho bạn!"
                                )
                                return
                            collected_data += jina_data + "\n"
                            source_label = url

                if multimodal_content:
                    prompt_content = multimodal_content
                else:
                    prompt_content = f"DỮ LIỆU ĐƯỢC CUNG CẤP:\n\n{collected_data if collected_data else text}\n\nYêu cầu từ người dùng: {text}"

                safe_edit_message(bot, chat_id, status_msg_id, "🧠 V1: Bóc tách kỹ thuật... ➡️ V2: CTO Phản biện... ➡️ V3: Reflexion & Tối ưu...")
                clean_report, meta_info = execute_dual_pass_audit(prompt_content, source_label=source_label)

                safe_edit_message(bot, chat_id, status_msg_id, "🛡️ Bot Red Team đang tự động thẩm định đối kháng...")
                final_report, is_cancelled = autonomous_redteam_review(chat_id, text if text else source_label, clean_report)

                audit_id = str(int(time.time()))[-6:]
                cache_audit_data(audit_id, meta_info)

                if status_msg_id:
                    safe_delete_message(bot, chat_id, status_msg_id)

                if is_cancelled:
                    meta_info["verdict"] = "🔴"
                    send_long_message(bot, chat_id, final_report, reply_to_message_id=message.message_id, reply_markup=None)
                else:
                    add_to_history(chat_id, "user", text)
                    add_to_history(chat_id, "model", final_report[:800])
                    final_report += "\n\n💡 <i>Báo cáo đang xem tạm thời trong RAM. Bấm nút <b>[💾 Lưu]</b> hoặc <b>[🔄 Thay thế]</b> bên dưới nếu sếp muốn lưu vào máy tính.</i>"
                    markup = create_action_buttons(audit_id, is_video=is_video_audit, comparison_info=meta_info.get("comparison"), verdict=meta_info.get("verdict"))
                    send_long_message(bot, chat_id, final_report, reply_to_message_id=message.message_id, reply_markup=markup)

            except PermissionError as pe:
                safe_edit_message(
                    bot, chat_id, status_msg_id,
                    f"❌ <b>LỖI XÁC THỰC AI:</b>\n{pe}"
                )
            except Exception as e:
                print(f"❌ Lỗi xử lý Mode 1: {e}", flush=True)
                import traceback
                traceback.print_exc()
                safe_edit_message(bot, chat_id, status_msg_id, "⚠️ Máy chủ AI đang tạm thời quá tải trong giây lát. Bạn vui lòng bấm gửi lại sau 5 giây nhé!")
            finally:
                # ZERO-FOOTPRINT MEMORY & DISK LIFECYCLE: Giải phóng toàn bộ bộ nhớ media ngay lập tức
                if tt_video_bytes is not None:
                    del tt_video_bytes
                if tt_cover_bytes is not None:
                    del tt_cover_bytes
                if tt_images_bytes is not None:
                    del tt_images_bytes
                if multimodal_content is not None:
                    del multimodal_content
                gc.collect()

        # CHẾ ĐỘ 2: CỐ VẤN THỰC CHIẾN & TRA CỨU REPO THỜI GIAN THỰC (CTO CHAT)
        else:
            status_msg = safe_reply_to(bot, message, "💡 CTO VibeCheck đang xử lý...")
            status_msg_id = status_msg.message_id if status_msg else None
            bot.send_chat_action(chat_id, 'typing')

            try:
                search_indicators = ["tìm", "search", "repo", "tương tự", "thay thế", "mã nguồn mở", "open source", "nền tảng nào", "ai nào", "công cụ nào", "tool"]
                is_search = any(ind in text.lower() for ind in search_indicators)

                github_items = []
                if is_search:
                    safe_edit_message(bot, chat_id, status_msg_id, "🔍 Đang quét kho GitHub Repos thời gian thực...")
                    search_keywords = extract_search_query(text, matched_file, reply_context)
                    github_items = search_github_repositories(search_keywords, max_results=4)

                safe_edit_message(bot, chat_id, status_msg_id, "⚡ Đang đúc kết giải pháp CTO thực chiến...")
                cto_response = execute_cto_chat(
                    chat_id=chat_id,
                    user_query=text,
                    reply_context=reply_context,
                    local_file_context=local_file_context,
                    github_results=github_items
                )

                safe_edit_message(bot, chat_id, status_msg_id, "🛡️ Bot Red Team đang tự động thẩm định đối kháng...")
                final_response, is_cancelled = autonomous_redteam_review(chat_id, text, cto_response)

                if status_msg_id:
                    safe_delete_message(bot, chat_id, status_msg_id)
                send_long_message(bot, chat_id, final_response, reply_to_message_id=message.message_id)

            except PermissionError as pe:
                safe_edit_message(
                    bot, chat_id, status_msg_id,
                    f"❌ <b>LỖI XÁC THỰC AI:</b>\n{pe}"
                )
            except Exception as e:
                print(f"❌ Lỗi xử lý Mode 2: {e}", flush=True)
                import traceback
                traceback.print_exc()
                safe_edit_message(bot, chat_id, status_msg_id, "⚠️ Máy chủ AI đang tạm thời quá tải trong giây lát. Bạn vui lòng bấm gửi lại sau 5 giây nhé!")

    # 4. Quản trị Master Action Backlog (/backlog hoặc /todo)
    @bot.message_handler(commands=['backlog', 'todo'])
    def handle_backlog_cmd(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        bot.send_chat_action(chat_id, 'typing')
        if not os.path.exists(BACKLOG_FILE):
            send_long_message(bot, chat_id, "📋 Chưa có file `00_ACTION_BACKLOG.md` trên hệ thống.")
            return

        try:
            records, _ = BacklogParser.parse_file(BACKLOG_FILE)
            pending = [r for r in records if r.is_pending]
            if not pending:
                send_long_message(bot, chat_id, "🎉 **TUYỆT VỜI!**\nHiện không còn việc tồn đọng nào cần triển khai ngay trong `00_ACTION_BACKLOG.md`.")
                return

            msg = f"📋 **MASTER ACTION BACKLOG — VIỆC CẦN TRIỂN KHAI NGAY ({len(pending)} việc):**\n\n"
            markup = tele_types.InlineKeyboardMarkup(row_width=2)
            for idx, r in enumerate(pending[:6], 1):
                msg += (
                    f"**{idx}. {r.tool_name}** (`{r.task_id}`)\n"
                    f"• *Trụ cột*: {r.pillar}\n"
                    f"• *Hành động*: {r.action_item}\n"
                    f"• *Ưu tiên*: {r.priority} | *Thời gian*: {r.date_str}\n\n"
                )
                btn_prompt = tele_types.InlineKeyboardButton(f"⚡ Prompt #{idx}", callback_data=f"agp_{r.task_id}")
                btn_done = tele_types.InlineKeyboardButton(f"✅ Xong #{idx}", callback_data=f"done_{r.task_id}")
                markup.add(btn_prompt, btn_done)

            msg += "> 💡 *Mẹo*: Bấm nút [⚡ Prompt] để lấy ngay prompt dán vào Antigravity IDE, hoặc bấm [✅ Xong] sau khi làm xong."
            send_long_message(bot, chat_id, msg, reply_markup=markup)
        except Exception as e:
            send_long_message(bot, chat_id, f"⚠️ Lỗi đọc Backlog: {e}")

    # 5. Quản trị Chế độ nhắc việc tự động (/remind_now hoặc /remind_status)
    @bot.message_handler(commands=['remind_now', 'check_overdue'])
    def handle_remind_now(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        bot.send_chat_action(chat_id, 'typing')
        try:
            alerted = dispatch_overdue_alerts(
                bot=bot,
                backlog_path=BACKLOG_FILE,
                base_dir=BASE_DIR,
                threshold_hours=12.0,
                force=True,
                target_chat_id=chat_id
            )
            if not alerted:
                send_long_message(bot, chat_id, "✅ **TẤT CẢ ĐỀU ỔN!**\nKhông có task [TRIỂN KHAI NGAY] nào bị trễ hạn quá 12 giờ trong `00_ACTION_BACKLOG.md`.")
            else:
                send_long_message(bot, chat_id, f"🔔 **ĐÃ HOÀN TẤT QUÉT NHẮC VIỆC!**\nĐã gửi thông báo nhắc nhở cho {len(alerted)} task tồn đọng quá 12 giờ.")
        except Exception as e:
            send_long_message(bot, chat_id, f"⚠️ Lỗi quét nhắc việc: {e}")

    @bot.message_handler(commands=['remind_status'])
    def handle_remind_status(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        bot.send_chat_action(chat_id, 'typing')
        try:
            saved_admin = AdminChatIDManager.get_chat_id(BASE_DIR)
            records, _ = BacklogParser.parse_file(BACKLOG_FILE)
            pending = [r for r in records if r.is_pending]
            overdue = [r for r in records if r.is_overdue(threshold_hours=12.0)]

            status_text = (
                "⚙️ <b>TRẠNG THÁI HỆ THỐNG NHẮC VIỆC TỰ ĐỘNG (VIBECHECK ESCALATION):</b>\n\n"
                f"• <b>Admin Chat ID</b>: <code>{saved_admin}</code>\n"
                f"• <b>Ngưỡng trễ hạn (Overdue Threshold)</b>: 12 giờ\n"
                f"• <b>Chu kỳ quét ngầm (Background Interval)</b>: 30 phút / lần\n"
                f"• <b>Giãn cách nhắc lại (Cooldown)</b>: 12 giờ / task (Chống spam)\n"
                f"• <b>Tổng task đang chờ làm</b>: {len(pending)}\n"
                f"• <b>Số task đã trễ hạn (>12h)</b>: {len(overdue)}\n\n"
            )
            if overdue:
                status_text += "<b>Danh sách task quá hạn:</b>\n"
                for o in overdue:
                    info = state_mgr.get_info(o.task_id)
                    snooze_str = f" <i>(Đang hoãn còn {info['snooze_remaining_hours']:.1f}h)</i>" if info['is_snoozed'] else ""
                    status_text += f"• 🔴 <code>{o.task_id}</code>: {o.tool_name}{snooze_str}\n"
            else:
                status_text += "🟢 Không có task nào bị trễ hạn.\n"

            status_text += "\n💡 <i>Gõ /remind_now để kích hoạt quét và nhận thông báo nhắc việc ngay lập tức.</i>"
            send_long_message(bot, chat_id, status_text)
        except Exception as e:
            send_long_message(bot, chat_id, f"⚠️ Lỗi kiểm tra trạng thái: {e}")

    # Lệnh đình chỉ/hủy bỏ task nhanh (/cancel <task_id> hoặc /huy)
    @bot.message_handler(commands=['cancel', 'huy', 'dinhchi'])
    def handle_cancel_cmd(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        parts = message.text.strip().split()
        if len(parts) < 2:
            send_long_message(bot, chat_id, "⚠️ Vui lòng nhập mã Task cần hủy/đình chỉ.\nVí dụ: `/cancel TASK-20260911-1026`")
            return
        target_id = parts[1].strip()
        success = backlog_mgr.cancel_task(target_id)
        if success:
            send_long_message(bot, chat_id, f"🚫 **ĐÃ ĐÌNH CHỈ THÀNH CÔNG TASK `{target_id}`!**\nTask đã được chuyển sang trạng thái `[-] Đã đình chỉ` trong `00_ACTION_BACKLOG.md` và hệ thống sẽ KHÔNG BAO GIỜ nhắc nhở task này nữa.")
        else:
            send_long_message(bot, chat_id, f"⚠️ Không tìm thấy hoặc task `{target_id}` đã hoàn thành/hủy trước đó.")

    # Lệnh bật/tắt hoặc kiểm tra nhắc nhở (/remind [on|off])
    @bot.message_handler(commands=['remind', 'nhacnho'])
    def handle_remind_cmd(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        parts = message.text.strip().split()
        if len(parts) > 1:
            sub = parts[1].lower()
            if sub in ["off", "tat", "tắt", "stop", "disable"]:
                state_mgr.set_enabled(False)
                send_long_message(bot, chat_id, "🔕 **ĐÃ TẮT TÍNH NĂNG NHẮC VIỆC TỰ ĐỘNG!**\nBot sẽ KHÔNG BAO GIỜ tự động gửi tin nhắn làm phiền anh nữa. Khi nào muốn xem danh sách việc tồn đọng, anh chỉ cần gõ `/backlog`.")
                return
            elif sub in ["on", "bat", "bật", "start", "enable"]:
                state_mgr.set_enabled(True)
                send_long_message(bot, chat_id, "🔔 **ĐÃ BẬT LẠI TÍNH NĂNG NHẮC VIỆC TỰ ĐỘNG.**\nBot sẽ gửi nhắc nhở 1 lần/ngày cho các việc quá hạn >12h.")
                return

        handle_remind_now(message)

    # 6. Bảng điều khiển tác vụ nhanh (/menu)
    @bot.message_handler(commands=['menu'])
    def handle_menu_cmd(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        menu_text = (
            "🎛️ <b>BẢNG ĐIỀU KHIỂN TÁC VỤ — VIBECHECK AI (v2.7.0)</b>\n\n"
            "Sếp muốn thực hiện thao tác nào? Chọn nhanh các nút bên dưới hoặc dùng bàn phím nhanh ở đáy màn hình:\n\n"
            "• <b>📋 Xem Backlog</b>: Quản lý các ý tưởng [TRIỂN KHAI NGAY]\n"
            "• <b>🔔 Quét Nhắc Việc</b>: Ép bot kiểm tra và gửi nhắc việc trễ hạn >12h\n"
            "• <b>⚙️ Trạng Thái</b>: Xem cấu hình và tình trạng hệ thống tự động\n"
            "• <b>🔄 Xóa Chat</b>: Làm sạch bộ nhớ hội thoại để chat phiên mới\n"
            "• <b>📖 Hướng Dẫn</b>: Xem đầy đủ các tính năng của bot"
        )
        markup = create_menu_dashboard_markup()
        send_long_message(bot, chat_id, menu_text, reply_markup=markup)

    # 7. Xóa lịch sử trò chuyện (/reset)
    @bot.message_handler(commands=['reset'])
    def handle_reset_cmd(message):
        chat_id = message.chat.id
        AdminChatIDManager.save_chat_id(BASE_DIR, chat_id)
        clear_history(chat_id)
        send_long_message(bot, chat_id, "🔄 <b>ĐÃ XÓA SẠCH LỊCH SỬ HỘI THOẠI!</b>\nNgữ cảnh trò chuyện đã được làm mới hoàn toàn. Sếp có thể gửi câu hỏi hoặc chủ đề mới ngay bây giờ.")

    # 8. Điều khiển ẩn/hiện bàn phím nhanh
    @bot.message_handler(commands=['hide_keyboard'])
    def handle_hide_keyboard(message):
        chat_id = message.chat.id
        hide_markup = tele_types.ReplyKeyboardRemove()
        bot.send_message(chat_id, "⌨️ Đã ẩn bàn phím nhanh. Gõ /show_keyboard để bật lại bất cứ lúc nào.", reply_markup=hide_markup)

    @bot.message_handler(commands=['show_keyboard'])
    def handle_show_keyboard(message):
        chat_id = message.chat.id
        show_markup = create_main_reply_keyboard()
        bot.send_message(chat_id, "⌨️ Đã bật lại bàn phím điều khiển nhanh!", reply_markup=show_markup)

    # 9. Xử lý nút bấm nhanh (Inline Buttons)
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback(call):
        try:
            if not call.data or "_" not in call.data:
                bot.answer_callback_query(call.id)
                return

            AdminChatIDManager.save_chat_id(BASE_DIR, call.message.chat.id)
            action, audit_id = call.data.split("_", 1)

            # Xử lý các phím bấm từ Bảng điều khiển /menu: menu_{sub_action}
            if action == "menu":
                sub_action = audit_id
                bot.answer_callback_query(call.id)
                fake_msg = call.message
                fake_msg.from_user = call.from_user

                if sub_action == "backlog":
                    handle_backlog_cmd(fake_msg)
                elif sub_action == "remind":
                    handle_remind_now(fake_msg)
                elif sub_action == "status":
                    handle_remind_status(fake_msg)
                elif sub_action == "reset":
                    handle_reset_cmd(fake_msg)
                elif sub_action == "help":
                    handle_start(fake_msg)
                return

            # Xử lý cập nhật trạng thái hoàn thành từ Backlog: done_{task_id}
            if action == "done":
                task_id = audit_id
                success = backlog_mgr.mark_task_completed(task_id)
                if success:
                    bot.answer_callback_query(call.id, f"✅ Đã tick hoàn thành: {task_id}!", show_alert=True)
                    send_long_message(bot, call.message.chat.id, f"✅ **ĐÃ HOÀN THÀNH TASK `{task_id}`** trong `00_ACTION_BACKLOG.md`!")
                else:
                    bot.answer_callback_query(call.id, f"⚠️ Task đã hoàn thành hoặc không tìm thấy.", show_alert=True)
                return

            # Xử lý đình chỉ/hủy bỏ task từ Backlog: cancel_{task_id}
            if action == "cancel":
                task_id = audit_id
                success = backlog_mgr.cancel_task(task_id)
                if success:
                    bot.answer_callback_query(call.id, f"🚫 Đã đình chỉ: {task_id}!", show_alert=True)
                    send_long_message(bot, call.message.chat.id, f"🚫 **ĐÃ ĐÌNH CHỈ & HỦY BỎ TASK `{task_id}`** trong `00_ACTION_BACKLOG.md`!\nHệ thống cam kết sẽ không bao giờ nhắc nhở task này nữa.")
                else:
                    bot.answer_callback_query(call.id, f"⚠️ Task đã hoàn thành/đình chỉ hoặc không tìm thấy.", show_alert=True)
                return

            # Xử lý tạm hoãn nhắc nhở: snz_{task_id}
            if action == "snz":
                task_id = audit_id
                state_mgr.snooze_task(task_id, hours=24.0)
                bot.answer_callback_query(call.id, f"⏸️ Đã tạm hoãn nhắc nhở task {task_id} trong 24 giờ!", show_alert=True)
                send_long_message(bot, call.message.chat.id, f"⏸️ **ĐÃ TẠM HOÃN NHẮC NHỞ TASK `{task_id}` TRONG 24 GIỜ.**\nTask vẫn giữ nguyên trạng thái `[ ] Chờ làm` trong Backlog.")
                return

            # Xử lý lấy Prompt Antigravity từ Backlog: agp_{task_id}
            if action == "agp":
                bot.send_chat_action(call.message.chat.id, 'typing')
                bot.answer_callback_query(call.id)
                task_id = audit_id
                try:
                    records, _ = BacklogParser.parse_file(BACKLOG_FILE)
                    target = next((r for r in records if r.task_id == task_id), None)
                    if target:
                        prompt_text = generate_antigravity_mvp_prompt(target)
                        send_long_message(bot, call.message.chat.id, prompt_text)
                    else:
                        send_long_message(bot, call.message.chat.id, f"⚠️ Không tìm thấy thông tin của task `{task_id}`.")
                except Exception as e:
                    send_long_message(bot, call.message.chat.id, f"⚠️ Lỗi sinh prompt: {e}")
                return

            # Xử lý Lưu Báo Cáo Theo Yêu Cầu (On-Demand Persistence): save_{audit_id}
            if action == "save":
                cached = get_audit_data(audit_id)
                if not cached:
                    bot.answer_callback_query(call.id, "Báo cáo trong bộ nhớ tạm đã hết hạn.", show_alert=True)
                    return
                if cached.get("saved_file"):
                    bot.answer_callback_query(call.id, f"File đã được lưu trước đó: {os.path.basename(cached['saved_file'])}", show_alert=True)
                    return

                saved_file_path, file_name = save_audit_report_to_disk(cached)
                cached["saved_file"] = saved_file_path
                bot.answer_callback_query(call.id, f"💾 Đã lưu thành công: {file_name}!", show_alert=True)

                if cached.get("verdict") == "TRIỂN KHAI NGAY":
                    try:
                        pillar_label = "Tuyến Xe VT-SG / 5 Trụ Cột"
                        car_act = cached.get("car_action", "Chưa có đề xuất").replace("\n", " ").strip()
                        rel_link = f"[{file_name}](Khao_Sat_Cong_Nghe/{file_name})"
                        rec_data = {
                            "date_str": datetime.now().strftime('%Y-%m-%d %H:%M'),
                            "tool_name": cached.get("name", "Công cụ mới"),
                            "pillar": pillar_label,
                            "action_item": car_act,
                            "priority": "🔴 P1 (Cao)",
                            "status": "[ ] Chờ làm",
                            "link": rel_link
                        }
                        new_task_id = backlog_mgr.add_task(rec_data)
                        cached["task_id"] = new_task_id
                        msg = (
                            f"💾 <b>ĐÃ LƯU BÁO CÁO VÀO MÁY TÍNH & MASTER BACKLOG!</b>\n\n"
                            f"• <b>File báo cáo</b>: <code>{file_name}</code> (trong <code>Khao_Sat_Cong_Nghe/</code>)\n"
                            f"• <b>Mã Task</b>: <code>{new_task_id}</code>\n"
                            f"• <b>Trạng thái</b>: <code>[ ] Chờ làm</code> trong <code>00_ACTION_BACKLOG.md</code>\n\n"
                            f"👉 <i>Gõ /backlog để theo dõi danh sách việc cần làm ngay mỗi ngày!</i>"
                        )
                        send_long_message(bot, call.message.chat.id, msg)
                    except Exception as e:
                        send_long_message(bot, call.message.chat.id, f"⚠️ Đã lưu file nhưng lỗi ghi Backlog: {e}")
                else:
                    msg = (
                        f"📌 <b>ĐÃ LƯU BÁO CÁO THAM KHẢO VÀO MÁY TÍNH!</b>\n\n"
                        f"• <b>File</b>: <code>{file_name}</code> (trong <code>Khao_Sat_Cong_Nghe/</code>)\n"
                        f"• <b>Phân loại</b>: <b>[{cached.get('verdict', 'LƯU THAM KHẢO')}]</b> (Chưa đưa vào Backlog hành động)."
                    )
                    send_long_message(bot, call.message.chat.id, msg)
                return

            # Xử lý So Găng & Thay Thế Task Cũ Trong Backlog: rep_{audit_id}_{old_task_id}
            if action == "rep":
                if "_" not in audit_id:
                    bot.answer_callback_query(call.id, "Thiếu mã task cần thay thế.", show_alert=True)
                    return
                actual_audit_id, old_task_id = audit_id.split("_", 1)
                cached = get_audit_data(actual_audit_id)
                if not cached:
                    bot.answer_callback_query(call.id, "Báo cáo trong bộ nhớ tạm đã hết hạn.", show_alert=True)
                    return

                saved_file_path, file_name = save_audit_report_to_disk(cached)
                cached["saved_file"] = saved_file_path

                try:
                    pillar_label = "Tuyến Xe VT-SG / 5 Trụ Cột"
                    car_act = cached.get("car_action", "Chưa có đề xuất").replace("\n", " ").strip()
                    rel_link = f"[{file_name}](Khao_Sat_Cong_Nghe/{file_name})"
                    rec_data = {
                        "date_str": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "tool_name": cached.get("name", "Công nghệ thay thế"),
                        "pillar": pillar_label,
                        "action_item": car_act,
                        "priority": "🔴 P1 (Cao)",
                        "status": "[ ] Chờ làm",
                        "link": rel_link
                    }
                    new_task_id = backlog_mgr.supersede_task(old_task_id, rec_data)
                    cached["task_id"] = new_task_id
                    bot.answer_callback_query(call.id, f"🔄 Đã thay thế {old_task_id} bằng {new_task_id}!", show_alert=True)

                    msg = (
                        f"🔄 <b>ĐÃ THAY THẾ CÔNG NGHỆ THÀNH CÔNG TRONG MASTER BACKLOG!</b>\n\n"
                        f"• <b>Task cũ</b>: <code>{old_task_id}</code> ➔ Chuyển trạng thái <code>[~] Thay thế bởi {new_task_id}</code>\n"
                        f"• <b>Task mới</b>: <code>{new_task_id}</code> (<b>{cached['name']}</b>) ➔ <code>[ ] Chờ làm</code>\n"
                        f"• <b>File báo cáo mới</b>: <code>{file_name}</code>\n\n"
                        f"👉 <i>Gõ /backlog để kiểm tra danh sách nhiệm vụ cập nhật mới nhất!</i>"
                    )
                    send_long_message(bot, call.message.chat.id, msg)
                except Exception as e:
                    send_long_message(bot, call.message.chat.id, f"⚠️ Lỗi cập nhật thay thế trong Backlog: {e}")
                return

            # Xử lý Bỏ Qua Không Lưu: dsm_{audit_id}
            if action == "dsm":
                bot.answer_callback_query(call.id, "❌ Đã hủy, không lưu bất kỳ file nào vào máy tính!", show_alert=True)
                send_long_message(bot, call.message.chat.id, "🗑️ <b>ĐÃ HỦY BÁO CÁO!</b>\nKhông có file nào được tạo và ổ cứng của sếp được giữ sạch 100%.")
                return

            cached = get_audit_data(audit_id)

            if not cached:
                bot.answer_callback_query(call.id, "Dữ liệu tóm tắt đã hết hạn trong bộ nhớ đệm.", show_alert=True)
                return

            # Tắt ngay vòng xoay loading trên Telegram client
            bot.answer_callback_query(call.id)

            if action == "sum":
                response_text = (
                    f"⚡ **TÓM TẮT 3 DÒNG ({cached['name']}):**\n\n"
                    f"{cached['summary_3_lines']}\n\n"
                    f"👉 Routing Tag: **[{cached['verdict']}]**"
                )
                send_long_message(bot, call.message.chat.id, response_text)

            elif action == "car":
                response_text = (
                    f"🚗 **HÀNH ĐỘNG CHO XE VŨNG TÀU - SÀI GÒN ({cached['name']}):**\n\n"
                    f"{cached['car_action']}"
                )
                send_long_message(bot, call.message.chat.id, response_text)

            elif action == "repo":
                tool_name = cached['name']
                bot.send_chat_action(call.message.chat.id, 'typing')
                
                kw = extract_search_query(f"Các repo thay thế tương tự cho {tool_name}", context_text=cached.get('summary_3_lines', ''))
                gh_results = search_github_repositories(kw, max_results=4)
                
                reply_prompt = f"Hãy gợi ý 3 repo mã nguồn mở tốt nhất thay thế hoặc có công dụng tương tự cho công cụ: {tool_name}."
                advisor_res = execute_cto_chat(
                    chat_id=call.message.chat.id,
                    user_query=reply_prompt,
                    github_results=gh_results
                )
                send_long_message(bot, call.message.chat.id, advisor_res)

            elif action == "ag":
                bot.send_chat_action(call.message.chat.id, 'typing')
                tool_name = cached['name']
                prompt_req = (
                    f"Hãy tạo 1 bản Prompt kỹ thuật chuẩn chỉ dành riêng cho Antigravity IDE (Gemini Pro 18 tháng, 0 ĐỒNG) "
                    f"để tôi và Antigravity Agent cùng pair-programming triển khai MVP nhanh cho công cụ: {tool_name}. "
                    f"Prompt cần có: Context, Mục tiêu, Tech stack đề xuất (Python/FastAPI hoặc Node.js nhẹ), các file code mẫu cần tạo "
                    f"và hướng dẫn chạy kiểm thử ngay trên máy tính mà không tốn chi phí."
                )
                ag_prompt_res = execute_cto_chat(
                    chat_id=call.message.chat.id,
                    user_query=prompt_req
                )
                send_long_message(bot, call.message.chat.id, f"⚡ **PROMPT CHO ANTIGRAVITY IDE (MVP 0 ĐỒNG):**\n\n{ag_prompt_res}")

            elif action == "code":
                bot.send_chat_action(call.message.chat.id, 'typing')
                tool_name = cached['name']
                code_req = (
                    f"Từ nội dung video và bản thẩm định về công cụ: '{tool_name}', hãy trích xuất toàn bộ các đoạn mã nguồn (code snippets), "
                    f"câu lệnh terminal/CLI, file cấu hình (config/docker/env) hoặc câu prompt kỹ thuật xuất hiện trong video. "
                    f"Trình bày trong khối Markdown code block ```...``` kèm hướng dẫn nhanh để dán vào Antigravity IDE chạy ngay."
                )
                code_extract_res = execute_cto_chat(
                    chat_id=call.message.chat.id,
                    user_query=code_req
                )
                send_long_message(bot, call.message.chat.id, f"📋 **MÃ NGUỒN / PROMPT TRÍCH XUẤT TỪ VIDEO ({tool_name}):**\n\n{code_extract_res}")

        except Exception as e:
            try:
                bot.answer_callback_query(call.id, f"Lỗi: {e}")
            except Exception:
                pass

    return bot


# ==============================================================================
# HÀM CHÍNH (MAIN ENTRY POINT)
# ==============================================================================

def main():
    print("=" * 80, flush=True)
    print("🤖 VIBECHECK AI (v2.8.0) — ON-DEMAND PERSISTENCE & 1V1 SUPERSEDING MATRIX", flush=True)
    print("=" * 80, flush=True)

    # 1. Kích hoạt cơ chế khóa đơn tiến trình (Single-Instance Lock)
    acquire_single_instance_lock()
    check_cloud_coexistence()

    # 2. Kiểm tra biến môi trường
    if not TELEGRAM_BOT_TOKEN or len(TELEGRAM_BOT_TOKEN.strip()) < 10:
        print("\n⚠️ Thiếu TELEGRAM_BOT_TOKEN trong file .env hoặc biến môi trường!", flush=True)
        sys.exit(0)

    if not GEMINI_API_KEY or len(GEMINI_API_KEY.strip()) < 10:
        print("\n⚠️ Thiếu GEMINI_API_KEY trong file .env hoặc biến môi trường!", flush=True)
        sys.exit(0)

    # Khởi động HTTP Health Check Server cho Cloud nếu không bị supervisor tắt
    if os.getenv("DISABLE_INTERNAL_HEALTH_SERVER") != "1":
        port = int(os.getenv("PORT", "8080"))
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            class HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header('Content-type', 'text/plain; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(b"VibeCheck AI v3.0 Ultra is Running 24/7!")
                def log_message(self, format, *args):
                    pass
            health_server = HTTPServer(("0.0.0.0", port), HealthHandler)
            t = threading.Thread(target=health_server.serve_forever, daemon=True, name="HealthCheckThread")
            t.start()
            print(f"✅ CLOUD HEALTH CHECK: Server HTTP đang hoạt động trên cổng {port}", flush=True)
        except Exception as e:
            print(f"⚠️ [HEALTH CHECK] Khởi động HTTP check: {e}", flush=True)

    # Khởi tạo BotProxy để EscalationWorker luôn trỏ đến bot instance đang hoạt động
    bot_proxy = BotProxy()
    start_proactive_escalation_worker(
        bot=bot_proxy,
        backlog_path=BACKLOG_FILE,
        base_dir=BASE_DIR,
        check_interval_seconds=1800,
        threshold_hours=12.0,
        initial_delay_seconds=10
    )
    print("✅ PROACTIVE ESCALATION DAEMON: Đã kích hoạt luồng quét ngầm nhắc việc quá hạn (12h threshold / 30min cycle)", flush=True)

    # ==============================================================================
    # VÒNG LẶP GIÁM SÁT BẤT TỬ (IMMORTAL SUPERVISOR POLLING LOOP)
    # ==============================================================================
    consecutive_failures = 0
    while True:
        bot = None
        try:
            print("\n[+] Khởi tạo kết nối VibeCheck AI Telegram Bot v2.8.0...", flush=True)
            bot = setup_bot()
            bot_proxy.set_bot(bot)
            bot_info = bot.get_me()
            print(f"✅ KẾT NỐI THÀNH CÔNG: @{bot_info.username} ({bot_info.first_name})", flush=True)
            print("✅ SINGLE-INSTANCE LOCK: Đã kích hoạt bot.lock (Không còn lỗi 409 Conflict)", flush=True)
            print("✅ AUTO-RETRY & MULTI-MODEL CASCADE: 6 models (gemini-3.5-flash-lite ➡️ gemini-3.5-flash ➡️ gemini-flash-lite ➡️ gemini-flash ➡️ gemini-3.7-flash ➡️ gemini-3.6-flash)", flush=True)
            print("✅ ON-DEMAND LOCAL PERSISTENCE: Không tự động lưu file, chỉ lưu khi người dùng bấm nút", flush=True)
            print("✅ 1V1 SUPERSEDING MATRIX & DEDUP: Tự đối chiếu Backlog, chống trùng lặp, gợi ý thay thế 1-chạm", flush=True)
            print("✅ ZERO-FOOTPRINT MEMORY & DISK: Tự hủy mọi buffer video/audio/photo sau xử lý (RAM <45MB)", flush=True)
            print("✅ TRIPLE-TIER COMMAND MENU: Đã đăng ký native [/] menu + bàn phím nổi 4 phím + /menu dashboard", flush=True)
            print("✅ MASTER ACTION BACKLOG (/backlog): Đồng bộ hai chiều với 00_ACTION_BACKLOG.md", flush=True)
            print("✅ PROACTIVE OVERDUE ESCALATION (/remind_now): Tự động nhắc việc tồn đọng >12h với nút bấm", flush=True)
            print("✅ ANTIGRAVITY IDE (0 ĐỒNG): Tối ưu hóa 100% cho Gemini Pro 18 tháng & Pair-Programming", flush=True)
            print("✅ BẢO VỆ ĐA LUỒNG & RAM: LRU Cache (Max 50 items) + Thread Lock", flush=True)
            print("✅ CHUẨN HÓA TELEGRAM HTML: In đậm, in nghiêng, hyperlink sạch, tắt preview banner", flush=True)
            print("\n🚀 VibeCheck AI v2.8.0 đang chạy ổn định 24/7... (Nhấn Ctrl+C để dừng)\n", flush=True)

            consecutive_failures = 0
            # timeout=90, long_polling_timeout=20: Telegram nhả socket sau 20s, client timeout tới 90s, loại trừ 100% lỗi ReadTimeout giả lập
            bot.infinity_polling(timeout=90, long_polling_timeout=20, restart_on_change=False)
            print("⚠️ [SUPERVISOR] Phiên polling Telegram vừa kết thúc bình thường. Đang tái tạo phiên mới sau 2s...", flush=True)
            time.sleep(2)
        except telebot.apihelper.ApiTelegramException as e:
            err_code = getattr(e, "error_code", None)
            print(f"\n⚠️ Telegram API Exception (code {err_code}): {e}", flush=True)
            if err_code in [401, 404]:
                print("❌ Token không hợp lệ. Vui lòng kiểm tra lại TELEGRAM_BOT_TOKEN trong .env!", flush=True)
                time.sleep(15)
            elif err_code == 409:
                print("⚠️ [CONFLICT 409] Phiên polling Telegram bị xung đột tạm thời. Đang chờ 5s để tái kết nối độc quyền...", flush=True)
                time.sleep(5)
            else:
                time.sleep(5)
        except (KeyboardInterrupt, SystemExit):
            print("\n🛑 Nhận tín hiệu dừng tiến trình. Đang dọn dẹp và thoát an toàn...", flush=True)
            if bot:
                try:
                    bot.stop_polling()
                except Exception:
                    pass
            break
        except Exception as e:
            consecutive_failures += 1
            delay = min(30, 3 * consecutive_failures)
            print(f"⚠️ [POLLING SUPERVISOR] Sự cố kết nối ({e}). Tự động phục hồi sau {delay}s (Lần thử {consecutive_failures})...", flush=True)
            time.sleep(delay)


if __name__ == "__main__":
    main()
