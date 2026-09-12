# 🤖 VibeCheck AI (v2.2 Enterprise Grade) — CTO Thực Chiến & Anti-Hype

**VibeCheck AI** hoạt động như một **CTO thực chiến kiêm Tech Auditor**, chuyên "check vibe" công nghệ, bóc tách bản chất, vạch trần chiêu trò quảng cáo (hype) với cơ chế **Tự phản biện & Quét lỗi ngầm 2 vòng (Dual-Pass Self-Correction)**, kết hợp **Đàm thoại ngữ cảnh linh hoạt & Tra cứu GitHub thời gian thực**.

Mọi đánh giá đều khóa chặt vào **5 trụ cột kinh doanh thực tế**:
1. 🚗 **Dịch vụ Xe Du Lịch & Web Booking** (Tuyến Vũng Tàu ↔ Sài Gòn / Sân bay Tân Sơn Nhất & Đi tỉnh - Nhà Xe Thành Tâm)
2. ⚡ **Vibecoding** (MVP tốc độ cao, Cursor, Python/Node, Automation)
3. ✍️ **Content & SEO thực chiến** (Website authority, chuyển đổi)
4. 💼 **Đóng gói B2B** (Giải pháp tự động hóa cho doanh nghiệp)
5. 🎬 **Video thực tế chuyển đổi cao** (Short-form / TikTok / Reels)

---

## 🚀 Tính Năng Đột Phá (v2.2)
- **Kiến trúc 2 Chế Độ (Dual-Mode Intent Routing)**:
  - *Chế độ 1 - Tech Auditor*: Thẩm định chuyên sâu 5 trụ cột khi gửi Link GitHub/Web/YouTube, Ảnh chụp màn hình, File PDF.
  - *Chế độ 2 - CTO Advisor & Live Search*: Trả lời trực diện dưới 20 dòng khi Reply tin nhắn hoặc hỏi đáp tiếp nối, tra cứu repo GitHub thật theo thời gian thực.
- **Local File Context Reader**: Tự động phát hiện tên file `.md` trong thư mục `Khao_Sat_Cong_Nghe/` để đọc nạp vào trí nhớ AI.
- **Model Cascade & Auto-Retry Resilient**: Tự động thử lại ngầm khi Google AI quá tải (503/429), tự động fallback sang `gemini-3.5-flash-lite`.
- **Telegram HTML Engine**: Hiển thị in đậm, in nghiêng, trích dẫn, link bấm sạch sẽ, triệt tiêu 100% banner xem trước trang web khổng lồ.
- **Single-Instance Lock (`bot.lock`)**: Chống lỗi 409 Conflict khi khởi động song song.
- **Hệ thống 4 Nút bấm tương tác nhanh**:
  - `⚡ Tóm tắt 3 dòng`
  - `🚗 Riêng xe VT-SG`
  - `🔍 Tìm 3 Repo tương tự` (1 chạm tra cứu GitHub)
  - `💻 Prompt Cursor (MVP)` (1 chạm sinh prompt làm MVP trong 15 phút)

---

## 🛠️ Cài Đặt & Khởi Động
```bash
# 1. Cài đặt thư viện
pip install -r requirements.txt

# 2. Cấu hình biến môi trường (nếu chưa có file .env)
# Điền TELEGRAM_BOT_TOKEN và GEMINI_API_KEY vào file .env

# 3. Khởi động VibeCheck AI
# Cách 1: Nhấp đúp chuột vào file run_bot.bat
# Cách 2: Chạy dòng lệnh
python -u bot_auditor.py
```
