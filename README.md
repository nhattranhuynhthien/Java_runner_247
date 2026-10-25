---
title: Java 24/7 Cloud Runner
emoji: ☕
colorFrom: indigo
colorTo: purple
sdk: gradio
sdk_version: 4.44.0
app_file: app.py
---

# ☕ Nền tảng Chạy và Quản lý Tiến trình Java 24/24 trên Web

Một giải pháp hoàn chỉnh cho phép chạy các chương trình Java liên tục không ngừng nghỉ (24/7) trên nền tảng đám mây miễn phí **Hugging Face Spaces** kết hợp cơ chế đánh thức định kỳ từ **UptimeRobot** với chi phí **0 VNĐ**.

---

## 🌟 Tính năng nổi bật

1. **Trình soạn thảo & Bảng điều khiển Web (Web Dashboard):**
   - Tích hợp **Monaco Editor** (trình soạn thảo lõi của VS Code) hỗ trợ tô màu cú pháp Java, định dạng code và phím tắt `Ctrl + S`.
   - Các nút thao tác nhanh: **Biên dịch & Chạy (Compile & Run)**, **Dừng (Stop)**, **Khởi động lại (Restart)**.
   - Hỗ trợ cả file mã nguồn `.java` và file đóng gói sẵn `.jar`.
   - Hỗ trợ tải lên (Upload) nhiều file Java hoặc thư viện phụ thuộc.

2. **Màn hình Console Thời gian thực (Real-time Streaming):**
   - Truyền tải toàn bộ `System.out.println` và `System.err` lên giao diện tức thì qua **WebSocket (Socket.io)**.
   - **Log Ring Buffer (1.000 dòng):** Lưu 1.000 dòng log gần nhất trên bộ nhớ đệm server. Khi bạn tắt trình duyệt hoặc mở từ thiết bị khác, lịch sử log vẫn hiển thị đầy đủ.
   - **Tương tác trực tiếp (`System.in`):** Khung nhập lệnh console cho phép gửi chuỗi ký tự vào chương trình Java đang chạy (hỗ trợ lịch sử lệnh bằng phím mũi tên Lên/Xuống).

3. **Giám sát & Tự phục hồi (Watchdog & Metrics):**
   - Tự động phát hiện khi tiến trình Java bị văng lỗi (Crash) và tự khởi động lại sau 3 giây.
   - Tự động chạy lại Java ngay khi container Hugging Face khởi động lại (`Auto-Start`).
   - Bảng đo đạc tài nguyên thời gian thực: Mã tiến trình (PID), Thời gian chạy liên tục (Uptime), RAM tiêu thụ bởi JVM và RAM trống của hệ thống.

4. **Bảo mật Quản trị viên (Authentication):**
   - Đặt mật khẩu thông qua biến môi trường bí mật `ADMIN_PASSWORD` trên Hugging Face.
   - Khóa các nút điều khiển và quyền sửa code đối với người lạ.
   - Endpoint kiểm tra sức khỏe `/health` luôn mở công khai để UptimeRobot ping không bị chặn.

---

## 🏗️ Kiến trúc & Nguyên lý 24/24 (0 VNĐ)

```
[Người dùng] <---> [Web Dashboard (Port 7860)] <---> [Tiến trình con Java (OpenJDK 21)]
                           ^
                           | (HTTP GET /health mỗi 5 phút)
                     [UptimeRobot]
```

* **Hugging Face Spaces (Docker Free Tier):** Cung cấp 2 vCPU, 16GB RAM, 50GB ổ cứng.
* **Cổng mở 7860:** Container chạy Node.js Web Controller để phục vụ giao diện và lắng nghe cổng 7860.
* **UptimeRobot:** Gửi HTTP request tới `https://<ten-space>.hf.space/health` mỗi 5 phút để ngăn cơ chế ngủ đông (Sleep sau 48h) của Hugging Face.

---

## 🚀 Hướng dẫn Triển khai Từng Bước

### Bước 1: Tạo Hugging Face Space
1. Truy cập [huggingface.co/spaces](https://huggingface.co/spaces) và đăng nhập.
2. Bấm vào nút **Create new Space**.
3. Điền các thông số:
   - **Space name:** Đặt tên tùy ý (ví dụ: `my-java-runner`).
   - **Select the Space SDK:** Chọn thẻ **Docker** -> chọn mẫu **Blank**.
   - **Space hardware:** Giữ nguyên **CPU basic (2 vCPU · 16 GB · Free)**.
   - **Space visibility:** Chọn **Public** (Bắt buộc phải Public để UptimeRobot có thể ping vào `/health`).
4. Bấm **Create Space**.

### Bước 2: Cài đặt Mật khẩu Quản trị (Secrets)
1. Trong giao diện Space vừa tạo, chuyển sang tab **Settings**.
2. Cuộn xuống phần **Variables and secrets**.
3. Bấm **New secret**:
   - **Name:** `ADMIN_PASSWORD`
   - **Value:** Nhập mật khẩu bí mật của bạn (ví dụ: `MatKhauCuaToi@2026`).
4. Bấm **Save**.

### Bước 3: Đẩy mã nguồn lên Space
Bạn có thể tải các file lên Space bằng 1 trong 2 cách:

* **Cách 1: Sử dụng Git (Khuyên dùng)**
  ```bash
  git init
  git remote add origin https://huggingface.co/spaces/<username>/<space-name>
  git add .
  git commit -m "Deploy Java 24/7 Runner"
  git push -u origin main -f
  ```
* **Cách 2: Tải trực tiếp trên giao diện Web Hugging Face**
  - Vào tab **Files** -> bấm **Add file** -> **Upload files**.
  - Tải toàn bộ mã nguồn của dự án lên.

Hệ thống Hugging Face sẽ tự động kích hoạt tiến trình `Building`. Sau khoảng 1-2 phút, trạng thái sẽ chuyển sang huy hiệu màu xanh lá **Running**.

### Bước 4: Lấy Direct URL của Space
1. Mở trang Space của bạn.
2. Bấm vào biểu tượng **ba dấu chấm (⋮)** ở góc trên bên phải giao diện Space.
3. Chọn **Embed this Space**.
4. Sao chép đường dẫn **Direct URL** (Có định dạng: `https://<username>-<space-name>.hf.space`).
5. Kiểm tra bằng cách mở trình duyệt vào:
   `https://<username>-<space-name>.hf.space/health`
   Nếu nhận được phản hồi:
   ```json
   {
     "status": "alive",
     "java_process": "running",
     "uptime_seconds": 120
   }
   ```
   thì hệ thống đã hoạt động hoàn hảo!

### Bước 5: Cấu hình UptimeRobot giữ container chạy 24/24
1. Đăng ký tài khoản miễn phí tại [uptimerobot.com](https://uptimerobot.com).
2. Tại bảng điều khiển, bấm **+ Add New Monitor**:
   - **Monitor Type:** Chọn `HTTP(s)`
   - **Friendly Name:** `Java HuggingFace 24/7`
   - **URL (or IP):** Dán Direct URL kèm `/health` (ví dụ: `https://<username>-<space-name>.hf.space/health`)
   - **Monitoring Interval:** Chọn `5 minutes` (Chu kỳ 5 phút)
3. Bấm **Create Monitor**.
4. Từ thời điểm này, UptimeRobot sẽ liên tục gửi tín hiệu "đánh thức" mỗi 5 phút, đảm bảo Space của bạn không bao giờ rơi vào trạng thái ngủ đông.

---

## 💻 Hướng dẫn Chạy Thử nghiệm Cục bộ (Local Development)

### Chạy trực tiếp bằng Node.js & Java:
Yêu cầu máy tính có cài sẵn **Node.js 18+** và **JDK 17 hoặc 21+**.

```bash
# 1. Cài đặt các gói thư viện
npm install

# 2. Khởi chạy server
npm start
```

Mở trình duyệt truy cập: `http://localhost:7860`

### Chạy bằng Docker:
```bash
# 1. Build Docker image
docker build -t java-runner-247 .

# 2. Chạy container
docker run -d -p 7860:7860 -e ADMIN_PASSWORD=admin123 --name java-runner java-runner-247
```

Mở trình duyệt truy cập: `http://localhost:7860`

---

## ⚙️ Cấu hình Nâng cao

Bạn có thể chỉnh sửa cấu hình thông qua nút **Cài đặt** trên giao diện Web hoặc chỉnh file `workspace/config.json`:

```json
{
  "activeFile": "Main.java",
  "jvmArgs": "-Xms256m -Xmx4G",
  "autoRestart": true,
  "autoStart": true
}
```

* **`jvmArgs`:** Tham số bộ nhớ JVM. Vì Hugging Face cấp 16GB RAM, cờ `-Xms256m -Xmx4G` (hoặc `-Xmx8G`) đảm bảo chương trình có đủ dung lượng hoạt động mà không gây tràn bộ nhớ toàn container.
* **`autoRestart`:** Watchdog tự khởi chạy lại tiến trình khi gặp lỗi ngoại lệ.
* **`autoStart`:** Tự khởi chạy lại file Java ngay khi container được bật mà không cần bấm nút.

---

## ⚠️ Lưu ý về Lưu trữ Ổ cứng (Ephemeral Storage)

Ổ cứng trên gói miễn phí của Hugging Face là **bộ nhớ tạm thời (Ephemeral)**. Khi Space được Rebuild hoặc khi Hugging Face điều chuyển hạ tầng, những file mới sinh ra sẽ mất nếu không được sao lưu.

**Khuyến nghị:**
- Đối với dữ liệu quan trọng phát sinh từ code Java, nên kết nối tới cơ sở dữ liệu đám mây miễn phí bên ngoài như:
  - **MongoDB Atlas Free Tier** (512MB)
  - **Supabase PostgreSQL Free Tier** (500MB)
  - **Firebase Realtime Database**
  - Hoặc định kỳ đẩy kết quả lên **Hugging Face Dataset API** / **GitHub Repository**.

