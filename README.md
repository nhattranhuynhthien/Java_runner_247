# 🐉 NRO Cloud 24/7 • Ultra-Light Web Controller

Hệ thống treo game Ngọc Rồng Online (NRO / DragonBoy J2ME Mod) **24/24 trên nền tảng đám mây (Cloud)** với kiến trúc **Siêu Nhẹ (Ultra-Lightweight)**.

---

## ⚡ Điểm Khác Biệt & Tối Ưu Của Kiến Trúc Mới

- 🚀 **Nói không với noVNC / x11vnc / Môi trường Desktop nặng (Fluxbox/XFCE):** Tải trang web trong chớp mắt (< 0.2s), không ngốn CPU và RAM nền.
- 📺 **Màn hình ảo Headless Xvfb chuẩn 240x360:** Khớp 100% kích thước pixel gốc của game J2ME, hình ảnh sắc nét không vỡ viền.
- 📷 **Chụp màn hình On-Demand (Chỉ chụp khi bạn xem):**
  - Chuyển khung hình X11 thành ảnh JPEG nén siêu nhẹ (~4KB).
  - Tự động tạm dừng tải ảnh khi chuyển tab hoặc tắt trình duyệt (`document.hidden`), đưa mức tiêu thụ tài nguyên của server về **gần như 0%**.
- 🎮 **Nạp lệnh trực tiếp qua `xdotool`:**
  - Bấm phím ảo, click chuột hoặc chạm trực tiếp vào màn hình game trên điện thoại đều được ánh xạ tọa độ tức thì vào X11.
  - Hỗ trợ gõ chữ, gửi lệnh chat mod (`td`, `ak`, `k 5`, v.v.) và đăng nhập tài khoản nhanh chóng.
- 🔐 **Bảo mật PIN Admin:** Ngăn chặn người lạ bấm trộm hoặc xem màn hình treo game của bạn.

---

## 📂 Cấu Trúc Dự Án

```text
nro-cloud-controller/
├── Dockerfile                   # Docker build Ubuntu 22.04 + OpenJDK 8 + Supervisor
├── supervisord.conf             # Quản lý 3 tiến trình: Xvfb, MicroEmulator, Flask/Gunicorn
├── requirements.txt             # Flask, Gunicorn, Pillow
├── config2.xml                  # Cấu hình MicroEmulator borderless 240x360
├── scripts/
│   └── start_emu.sh             # Script khởi chạy game với MicroEmulator
├── app/
│   ├── __init__.py
│   ├── server.py                # Backend Flask API (screenshot, input, health)
│   └── templates/
│       └── index.html           # Giao diện web mobile-first hiện đại
├── roms/
│   ├── .gitkeep
│   └── NRO.jar                  # File game / bản mod Ngọc Rồng (.jar)
└── README.md
```

---

## 🛠️ Hướng Dẫn Nạp Game / Đổi Bản Mod

1. **Cách 1: Thêm file trước khi deploy**
   - Đặt file game của bạn vào thư mục `roms/` và đổi tên thành: `NRO.jar`.
2. **Cách 2: Đổi file trực tiếp trên giao diện Web**
   - Bấm nút **"Đổi Mod"** ở góc phải màn hình web.
   - Chọn file `.jar` từ máy tính hoặc điện thoại và bấm **"Nạp & Chạy Ngay"**. Hệ thống sẽ tự động thay thế và khởi động lại game.

---

## 💻 Chạy Kiểm Tra Nội Bộ Bằng Docker

```bash
# 1. Build Docker image
docker build -t nro-cloud .

# 2. Chạy container ở cổng 8080
docker run -d --name nro -p 8080:8080 -e ADMIN_PIN=1234 nro-cloud

# 3. Mở trình duyệt truy cập:
# http://localhost:8080
```

---

## ☁️ Hướng Dẫn Deploy Lên Render.com (Miễn Phí 0 VNĐ)

1. Đẩy mã nguồn lên kho GitHub của bạn:
   ```bash
   git add .
   git commit -m "feat: migrate to nro ultra-light web controller"
   git push origin main
   ```
2. Truy cập [Render.com Dashboard](https://dashboard.render.com):
   - Chọn Web Service của bạn.
   - Nhánh: `main`.
   - Môi trường: `Docker`.
3. Cấu hình biến môi trường (Environment Variables) nếu muốn:
   - `ADMIN_PIN`: Mã PIN bảo mật (mặc định là `1234`).
   - `PORT`: Cổng dịch vụ (Render tự động cấp qua `$PORT`).
4. Bấm **Manual Deploy** -> **Deploy latest commit**.

---

## ⏱️ Giữ Game Treo 24/24 Với UptimeRobot

Render có cơ chế tự động ngủ sau 15 phút nếu không có truy cập. Để server thức 24/24:

1. Đăng ký tài khoản miễn phí tại [UptimeRobot.com](https://uptimerobot.com).
2. Tạo mới một Monitor:
   - **Monitor Type:** `HTTP(s)`
   - **Friendly Name:** `NRO Cloud 24/7`
   - **URL (or IP):** `https://<ten-app-cua-ban>.onrender.com/health`
   - **Monitoring Interval:** `5 minutes`
3. Endpoint `/health` trả về `OK` ngay tức thì trong vài mili-giây mà không đụng vào đồ họa X11, giúp server luôn online ổn định 24/24 hoàn toàn miễn phí.
