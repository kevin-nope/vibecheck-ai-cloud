# Sử dụng Python 3.12 Slim siêu nhẹ và tối ưu bảo mật
FROM python:3.12-slim

# Thiết lập biến môi trường chuẩn hóa tiếng Việt và logs thời gian thực
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PYTHONUTF8=1

# Thiết lập thư mục làm việc trong container
WORKDIR /app

# Cài đặt curl và các công cụ cơ bản
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Sao chép và cài đặt các thư viện phụ thuộc
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép toàn bộ mã nguồn vào container
COPY . .

# Xóa file lock nếu lỡ bị copy vào để tránh lỗi treo bot
RUN rm -f bot.lock

# Lệnh khởi động bot ở chế độ không đệm (unbuffered)
CMD ["python", "-u", "bot_auditor.py"]
