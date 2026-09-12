# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import logging
import threading
from pathlib import Path
import telebot
from telebot import types as tele_types
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(os.path.expanduser("~/.eks/secrets/.env"), override=True)
load_dotenv(".env", override=True)

logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("VibeCheck_RedTeam")

TELEGRAM_BOT_2_TOKEN = os.getenv("TELEGRAM_BOT_2_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN_2") or os.getenv("BOT_2_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "8546576092")

if not TELEGRAM_BOT_2_TOKEN:
    logger.error("THIẾU TELEGRAM_BOT_2_TOKEN! Vui lòng thiết lập trong biến môi trường hoặc .env")

# Thư mục lưu trữ session đệm
SESSIONS_DIR = Path("sessions")
SESSIONS_DIR.mkdir(exist_ok=True)

SYSTEM_PROMPT_REDTEAM = """
BẠN LÀ TRỌNG TÀI CHIẾN LƯỢC & THỦ LĨNH RED TEAM ĐỘC LẬP (VIBECHECK REDTEAM AUDITOR).
BẠN ĐÓNG VAI TRÒ LÀ CỐ VẤN ĐỐI KHÁNG ĐỘC LẬP CỦA FOUNDER (MR. KEVIN).

SỨ MỆNH SỐNG CÒN:
1. BẢO VỆ TỐI ĐA NGUỒN VỐN, THỜI GIAN VÀ SỰ AN TOÀN CỦA FOUNDER.
2. MỔ XẺ, VẠCH LÁ TÌM SÂU, BÓC TÁCH BÁNH VẼ VÀ PHẢN BIỆN TÀN NHẪN.
3. TUYỆT ĐỐI KHÔNG VUỐT VE, KHÔNG NỊNH HÓT, KHÔNG DÙNG TỪ NGỮ CHUNG CHUNG. NÓI THẲNG VÀO TỬ HUYỆT VÀ ĐIỂM NGHẼN THỰC THI!

BỘ NHỚ & BỐI CẢNH (CONTEXT MEMORY):
- Bạn có quyền truy cập vào các tin nhắn trước trong phiên trò chuyện.
- Nếu Founder gửi tài liệu/dự án ở tin nhắn trước, rồi ở tin nhắn sau yêu cầu: "viết tối hậu thư", "hủy dự án nào", "tổng hợp lại": BẠN PHẢI TỰ ĐỘNG DÙNG DỮ LIỆU CỦA CẢ HAI ĐỂ THỰC THI NGAY LẬP TỨC. Tuyệt đối không nói "chưa có văn bản".

CẤU TRÚC PHẢN BIỆN CHUẨN:
🚦 BẢNG ĐÈN TÍN HIỆU:
• 🔴 ĐỎ: RỦI RO CHÍ MẠNG / BÁNH VẼ / CHI PHÍ ẨN PHÌNH TO (Khuyên dừng lại ngay hoặc đập đi xây lại)
• 🟡 VÀNG: CÓ TIỀM NĂNG NHƯNG KẼ HỞ THỰC THI QUÁ LỚN (Cần bịt lỗ hổng trước khi làm)
• 🟢 XANH: KHẢ THI CAO, LOGIC CHẶT CHẼ, AN TOÀN NGUỒN LỰC (Ủng hộ triển khai)

1. 🔍 BÓC TÁCH SỰ THẬT (FACT VS FICTION)
2. ⚠️ 3 LỖ HỔNG CHÍ MẠNG (UNSEEN BLINDSPOTS & RISKS)
3. 💡 PHƯƠNG ÁN B VƯỢT TRỘI (NEXT-BEST ALTERNATIVE)
4. 🎯 HÀNH ĐỘNG DUY NHẤT TRONG 24H TỚI (HOẶC VĂN BẢN YÊU CẦU ĐÌNH CHỈ / TỐI HẬU THƯ NẾU FOUNDER YÊU CẦU)
"""

# ==============================================================================
# SESSION MEMORY MANAGER (SIÊU TIẾT KIỆM TOKEN & BẢO VỆ CONTEXT)
# ==============================================================================
class SessionMemoryManager:
    def __init__(self, max_turns=6, session_ttl=1800):
        self.max_turns = max_turns  # Giữ tối đa 6 tin (3 user + 3 model)
        self.session_ttl = session_ttl  # 30 phút không chat -> tự làm mới
        self.sessions = {}
        self.locks = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, chat_id):
        with self._global_lock:
            if chat_id not in self.locks:
                self.locks[chat_id] = threading.Lock()
            return self.locks[chat_id]

    def _get_session_file(self, chat_id):
        return SESSIONS_DIR / f"{chat_id}.json"

    def get_history(self, chat_id):
        lock = self._get_lock(chat_id)
        with lock:
            if chat_id not in self.sessions:
                s_file = self._get_session_file(chat_id)
                if s_file.exists():
                    try:
                        with open(s_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if time.time() - data.get("last_activity", 0) <= self.session_ttl:
                            self.sessions[chat_id] = data
                        else:
                            s_file.unlink(missing_ok=True)
                            self.sessions[chat_id] = {"messages": [], "last_activity": time.time()}
                    except Exception:
                        self.sessions[chat_id] = {"messages": [], "last_activity": time.time()}
                else:
                    self.sessions[chat_id] = {"messages": [], "last_activity": time.time()}

            sess = self.sessions[chat_id]
            if time.time() - sess.get("last_activity", 0) > self.session_ttl:
                sess["messages"] = []
            sess["last_activity"] = time.time()
            return sess["messages"]

    def add_turn(self, chat_id, user_text, model_text):
        lock = self._get_lock(chat_id)
        with lock:
            if chat_id not in self.sessions:
                self.sessions[chat_id] = {"messages": [], "last_activity": time.time()}
            msgs = self.sessions[chat_id]["messages"]

            # Cắt tỉa nếu tin nhắn quá dài để chống tràn token
            trimmed_user = user_text[:6000] if len(user_text) > 6000 else user_text
            trimmed_model = model_text[:6000] if len(model_text) > 6000 else model_text

            msgs.append({"role": "user", "text": trimmed_user})
            msgs.append({"role": "model", "text": trimmed_model})

            # Cửa sổ trượt: Chỉ giữ tối đa max_turns tin gần nhất
            if len(msgs) > self.max_turns:
                msgs = msgs[-self.max_turns:]
            self.sessions[chat_id]["messages"] = msgs
            self.sessions[chat_id]["last_activity"] = time.time()

            # Lưu vào file để chống mất trí nhớ khi server reload
            try:
                with open(self._get_session_file(chat_id), "w", encoding="utf-8") as f:
                    json.dump(self.sessions[chat_id], f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Không thể lưu session file: {e}")

    def reset(self, chat_id):
        lock = self._get_lock(chat_id)
        with lock:
            self.sessions[chat_id] = {"messages": [], "last_activity": time.time()}
            s_file = self._get_session_file(chat_id)
            if s_file.exists():
                try:
                    s_file.unlink()
                except Exception:
                    pass
            logger.info(f"Đã làm sạch bộ nhớ session cho chat_id: {chat_id}")

memory_mgr = SessionMemoryManager(max_turns=6, session_ttl=1800)

def split_text_chunks(text, max_length=3800):
    if len(text) <= max_length:
        return [text]
    chunks = []
    lines = text.split("\n")
    current_chunk = ""
    for line in lines:
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
        current_chunk += line + "\n"
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    return chunks

def safe_send_markdown(bot, chat_id, text, reply_to_message_id=None):
    chunks = split_text_chunks(text)
    for chunk in chunks:
        try:
            bot.send_message(
                chat_id,
                chunk,
                parse_mode="Markdown",
                reply_to_message_id=reply_to_message_id
            )
        except Exception as e:
            logger.warning(f"Markdown parse warning: {e}. Fallback to plain text.")
            try:
                bot.send_message(
                    chat_id,
                    chunk,
                    parse_mode=None,
                    reply_to_message_id=reply_to_message_id
                )
            except Exception as final_e:
                logger.error(f"Failed to send message chunk: {final_e}")

def call_gemini_redteam(chat_id, current_user_input, save_memory=True):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return (
            "⚠️ **CHƯA THIẾT LẬP GEMINI API KEY**\n\n"
            "Vui lòng thiết lập biến môi trường `GEMINI_API_KEY` từ Google AI Studio (https://aistudio.google.com/app/apikey) để kích hoạt não bộ AI."
        )

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT_REDTEAM,
            temperature=0.3
        )

        # Xây dựng danh sách Contents
        contents = []
        if save_memory:
            history = memory_mgr.get_history(chat_id)
            for item in history:
                role = item.get("role", "user")
                text = item.get("text", "")
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))

        # Thêm tin nhắn hiện tại
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=current_user_input)]))

        cascade_models = [
            "gemini-3.5-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
            "gemini-flash-lite-latest",
            "gemini-flash-latest"
        ]

        last_err = None
        for m in cascade_models:
            try:
                res = client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=config
                )
                if res and res.text:
                    reply_text = res.text.strip()
                    # Chỉ lưu vào bộ nhớ trượt khi được phép
                    if save_memory:
                        memory_mgr.add_turn(chat_id, current_user_input, reply_text)
                    return reply_text
            except Exception as ex:
                last_err = ex
                logger.warning(f"Model {m} fallback: {ex}")
                continue

        return f"⚠️ Lỗi kết nối Google AI Studio: {last_err}"

    except Exception as e:
        logger.error(f"Gemini calling error: {e}")
        return f"❌ Lỗi hệ thống: {e}"

def create_bot():
    token = TELEGRAM_BOT_2_TOKEN
    if not token:
        raise ValueError("TELEGRAM_BOT_2_TOKEN is missing!")
    bot = telebot.TeleBot(token)

    @bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        chat_id = str(message.chat.id)
        if ADMIN_CHAT_ID and chat_id != str(ADMIN_CHAT_ID):
            bot.reply_to(message, "⛔ Quyền truy cập bị từ chối. Đây là Trọng tài Phản biện Chiến lược cá nhân của Mr. Kevin.")
            return

        welcome_text = (
            "🛡️ **VIBECHECK REDTEAM | TRỌNG TÀI PHẢN BIỆN ĐỘC LẬP (v3.1 STATEFUL)**\n\n"
            "Tôi là Cố vấn Đối kháng chạy ngầm trên Cloud 24/7 với **Bộ nhớ Ngữ cảnh Siêu Tiết Kiệm Token**.\n\n"
            "📌 **Sứ mệnh:**\n"
            "• Chuyên 'vạch lá tìm sâu', bóc trần bánh vẽ và các chi phí ẩn.\n"
            "• Thẩm định chéo câu trả lời của Bot 1 (Maker / CTO).\n"
            "• Ghi nhớ mạch hội thoại liên tục, không bao giờ bắt Founder lặp lại dữ liệu!\n\n"
            "👉 **Các lệnh điều khiển:**\n"
            "• `/new` hoặc `/reset`: Xóa sạch ngữ cảnh cũ để bắt đầu thẩm định một vụ việc hoàn toàn mới.\n"
            "• `/verdict`: Yêu cầu xuất ngay Bản Tối Hậu Thư / Quyết định đình chỉ dự án dựa trên những gì vừa bàn."
        )
        safe_send_markdown(bot, message.chat.id, welcome_text)

    @bot.message_handler(commands=['new', 'reset', 'clear'])
    def handle_reset_session(message):
        chat_id = str(message.chat.id)
        if ADMIN_CHAT_ID and chat_id != str(ADMIN_CHAT_ID):
            return
        memory_mgr.reset(message.chat.id)
        bot.reply_to(message, "🧹 **BỘ NHỚ ĐÃ ĐƯỢC LÀM SẠCH 100%!**\n\nTôi đã đóng hồ sơ cũ và mở một trang giấy trắng. Token tiêu thụ đã về 0. Bạn hãy gửi vụ việc mới vào đây!")

    @bot.message_handler(commands=['verdict', 'toihauthu'])
    def handle_verdict_command(message):
        chat_id = str(message.chat.id)
        if ADMIN_CHAT_ID and chat_id != str(ADMIN_CHAT_ID):
            return
        prompt_command = "Dựa trên toàn bộ dữ liệu và các dự án chúng ta vừa trao đổi, hãy xuất ngay một 'VĂN BẢN TỐI HẬU THƯ / QUYẾT ĐỊNH ĐÌNH CHỈ' (Termination Notice) thật sắc bén, quyết đoán, nêu rõ lý do hủy dự án nào và yêu cầu CTO dừng ngay lập tức."
        status_msg = bot.reply_to(message, "⏳ [Red Team] Đang rà soát toàn bộ lịch sử vụ việc & soạn thảo Tối Hậu Thư...")
        try:
            bot.send_chat_action(message.chat.id, 'typing')
            analysis_result = call_gemini_redteam(message.chat.id, prompt_command)
            try:
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception:
                pass
            safe_send_markdown(bot, message.chat.id, analysis_result, reply_to_message_id=message.message_id)
        except Exception as e:
            logger.error(f"Verdict error: {e}")
            bot.send_message(message.chat.id, f"⚠️ Lỗi soạn thảo phán quyết: {e}")

    @bot.message_handler(func=lambda msg: True, content_types=['text', 'photo'])
    def handle_audit_request(message):
        chat_id = str(message.chat.id)
        if ADMIN_CHAT_ID and chat_id != str(ADMIN_CHAT_ID):
            bot.reply_to(message, "⛔ Quyền truy cập bị từ chối.")
            return

        user_content = message.text or message.caption or ""
        if not user_content.strip():
            bot.reply_to(message, "⚠️ Vui lòng gửi nội dung văn bản hoặc link bài viết để tôi tiến hành thẩm định phản biện.")
            return

        status_msg = bot.reply_to(message, "⏳ [Red Team] Đang kết nối bộ nhớ ngữ cảnh & mổ xẻ dữ liệu...")
        try:
            bot.send_chat_action(message.chat.id, 'typing')
            analysis_result = call_gemini_redteam(message.chat.id, user_content)

            try:
                bot.delete_message(message.chat.id, status_msg.message_id)
            except Exception:
                pass

            safe_send_markdown(bot, message.chat.id, analysis_result, reply_to_message_id=message.message_id)

        except Exception as e:
            logger.error(f"Audit processing error: {e}")
            try:
                bot.edit_message_text(
                    f"⚠️ Quá trình thẩm định gặp sự cố: {e}",
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id
                )
            except Exception:
                bot.send_message(message.chat.id, f"⚠️ Quá trình thẩm định gặp sự cố: {e}")

    return bot

def run_redteam_bot_loop():
    logger.info("🚀 KHỞI CHẠY VIBECHECK REDTEAM BOT STATEFUL (POLLING MODE)...")
    while True:
        try:
            bot = create_bot()
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"⚠️ Polling loop crashed: {e}. Tự động khởi động lại sau 3 giây...")
            time.sleep(3)

if __name__ == "__main__":
    run_redteam_bot_loop()
