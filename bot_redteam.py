# -*- coding: utf-8 -*-
import os
import sys
import time
import logging
import telebot
from telebot import types
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv(os.path.expanduser("~/.eks/secrets/.env"))
load_dotenv(".env")

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

SYSTEM_PROMPT_REDTEAM = """
BẠN LÀ TRỌNG TÀI CHIẾN LƯỢC & THỦ LĨNH RED TEAM ĐỘC LẬP (VIBECHECK REDTEAM AUDITOR).
BẠN ĐÓNG VAI TRÒ LÀ CỐ VẤN ĐỐI KHÁNG ĐỘC LẬP CỦA FOUNDER (MR. KEVIN).

SỨ MỆNH SỐNG CÒN:
1. BẢO VỆ TỐI ĐA NGUỒN VỐN, THỜI GIAN VÀ SỰ AN TOÀN CỦA FOUNDER.
2. MỔ XẺ, VẠCH LÁ TÌM SÂU, BÓC TÁCH BÁNH VẼ VÀ PHẢN BIỆN TÀN NHẪN.
3. TUYỆT ĐỐI KHÔNG VUỐT VE, KHÔNG NỊNH HÓT, KHÔNG DÙNG TỪ NGỮ CHUNG CHUNG. NÓI THẲNG VÀO TỬ HUYỆT VÀ ĐIỂM NGHẼN THỰC THI!

KHI NHẬN ĐƯỢC BẤT KỲ Ý TƯỞNG, BÀI ĐĂNG TIKTOK/FB/X, DEAL LÀM ĂN HOẶC ĐỀ XUẤT NÀO, HÃY XUẤT BÁO CÁO THEO CẤU TRÚC SAU:

🚦 BẢNG ĐÈN TÍN HIỆU:
[Chọn DUY NHẤT 1 trong 3 trạng thái]:
• 🔴 ĐỎ: RỦI RO CHÍ MẠNG / BÁNH VẼ / LỪA ĐẢO / CHI PHÍ ẨN PHÌNH TO (Khuyên dừng lại ngay hoặc đập đi xây lại)
• 🟡 VÀNG: CÓ TIỀM NĂNG NHƯNG KẼ HỞ THỰC THI QUÁ LỚN (Cần bổ sung dữ liệu và bịt lỗ hổng trước khi làm)
• 🟢 XANH: KHẢ THI CAO, LOGIC CHẶT CHẼ, AN TOÀN NGUỒN LỰC (Ủng hộ triển khai)

1. 🔍 BÓC TÁCH SỰ THẬT (FACT VS FICTION):
- Dữ kiện thực tế đã kiểm chứng (Facts): ...
- Điểm nghi vấn / Bánh vẽ / Hype Marketing (Fiction): ...

2. ⚠️ 3 LỖ HỔNG CHÍ MẠNG (UNSEEN BLINDSPOTS & RISKS):
- Lỗ hổng 1 [Tài chính / Dòng tiền / Chi phí ẩn]: ...
- Lỗ hổng 2 [Vận hành / Rào cản kỹ thuật]: ...
- Lỗ hổng 3 [Pháp lý / Rủi ro phụ thuộc / Thị trường]: ...

3. 💡 PHƯƠNG ÁN B VƯỢT TRỘI (NEXT-BEST ALTERNATIVE):
- Đừng chỉ chê bai. Nếu từ bỏ cách làm này, đâu là giải pháp THỰC DỤNG HƠN, RẺ HƠN, NHANH HƠN và ÍT RỦI RO HƠN?

4. 🎯 HÀNH ĐỘNG DUY NHẤT TRONG 24H TỚI:
- 1 bước kiểm chứng thực địa duy nhất Founder cần làm trước khi xuống tiền hoặc tốn công sức.
"""

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

def call_gemini_redteam(user_input):
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

        cascade_models = [
            "gemini-2.5-flash",
            "gemini-1.5-flash",
            "gemini-2.0-flash",
            "gemini-2.5-pro"
        ]

        last_err = None
        for m in cascade_models:
            try:
                res = client.models.generate_content(
                    model=m,
                    contents=user_input,
                    config=config
                )
                if res and res.text:
                    return res.text.strip()
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
            "🛡️ **VIBECHECK REDTEAM | TRỌNG TÀI PHẢN BIỆN ĐỘC LẬP**\n\n"
            "Tôi là Cố vấn Đối kháng (Adversarial Checker) chạy ngầm trên Cloud 24/7.\n\n"
            "📌 **Sứ mệnh:**\n"
            "• Chuyên 'vạch lá tìm sâu', bóc trần bánh vẽ và các chi phí ẩn.\n"
            "• Thẩm định chéo câu trả lời của Bot 1 (Maker / CTO).\n"
            "• Fact-check video TikTok, bài viết Facebook, tin tức X, đề xuất kinh doanh.\n\n"
            "👉 **Cách dùng:**\n"
            "1. Chuyển tiếp (Forward) bất kỳ tin nhắn/ý tưởng nào từ Bot 1 sang đây.\n"
            "2. Hoặc dán link/nội dung từ TikTok, Facebook, X, đối tác vào đây.\n"
            "Tôi sẽ lập tức xuất Bảng Đèn Tín Hiệu + 3 Lỗ Hổng Chí Mạng + Phương Án B Tối Ưu!"
        )
        safe_send_markdown(bot, message.chat.id, welcome_text)

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

        status_msg = bot.reply_to(message, "⏳ [Red Team] Đang mổ xẻ dữ liệu & truy quét lỗ hổng chí mạng...")
        try:
            bot.send_chat_action(message.chat.id, 'typing')
            analysis_result = call_gemini_redteam(user_content)

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
    logger.info("🚀 KHỞI CHẠY VIBECHECK REDTEAM BOT (POLLING MODE)...")
    while True:
        try:
            bot = create_bot()
            bot.infinity_polling(timeout=30, long_polling_timeout=25)
        except Exception as e:
            logger.error(f"⚠️ Polling loop crashed: {e}. Tự động khởi động lại sau 3 giây...")
            time.sleep(3)

if __name__ == "__main__":
    run_redteam_bot_loop()
