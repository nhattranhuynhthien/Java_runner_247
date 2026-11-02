# NRO Cloud Controller

MicroEmulator config and RMS saves are stored under `/data/.microemulator`. Mount a persistent volume at `/data` to retain the saved game account, server, and emulator setup across container replacement. On startup, the emulator copies legacy files from `/root/.microemulator` into this location when needed.

Ứng dụng chạy MicroEmulator trong Xvfb, điều khiển bằng dashboard Flask và `xdotool`. Màn hình game là 240×360; Xvfb dùng 240×384 để chứa thêm vùng menu 24 px của MicroEmulator, phần này được cắt khỏi ảnh gửi lên dashboard.

## ROM và dữ liệu

- Đặt game J2ME hợp lệ tại `roms/NRO.jar` trước khi build.
- Có thể tải JAR mới lên từ dashboard. File được lưu tại `/data/roms/NRO.jar` và được thay thế nguyên tử sau khi kiểm tra cấu trúc ZIP/JAR.
- Thư mục `/data` chứa cả ROM tải lên và thông tin đăng nhập đã băm. Gắn persistent volume vào `/data` nếu nền tảng hỗ trợ; nếu không, dữ liệu này có thể mất khi container được thay thế.
- Có thể đổi vị trí ROM bằng biến `ROM_FILE`.

## Đăng nhập

Không còn mật khẩu mặc định. Trước khi chạy, đặt `ADMIN_PASSWORD` thành mật khẩu riêng dài tối thiểu 12 ký tự. Tài khoản mặc định là `admin`; có thể đổi tài khoản/mật khẩu trong dashboard. Sau lần đổi đầu tiên, thông tin băm được lưu trong `/data/security_config.json` và sẽ được ưu tiên hơn biến môi trường.

Ví dụ Docker:

```bash
docker build -t nro-cloud .
docker run -d --name nro -p 8080:8080 \
  -e ADMIN_PASSWORD='your-long-unique-password' \
  -e SESSION_SECRET='your-long-random-session-secret' \
  -v nro-data:/data nro-cloud
```

`SESSION_COOKIE_SECURE=1` bật cờ Secure cho cookie khi chạy sau HTTPS. Không bật nếu truy cập trực tiếp qua HTTP cục bộ.

## Chạy và deploy

- Dashboard: `http://localhost:8080`
- Health check: `GET /health` trả `OK` và không chụp ảnh màn hình.
- Trên Render hoặc Hugging Face Spaces, cấu hình `ADMIN_PASSWORD` và `SESSION_SECRET` trong secrets/environment variables. Với Hugging Face Spaces, đặt `PORT=7860`.
- Nếu nền tảng có persistent disk, mount vào `/data` để giữ ROM và thông tin đăng nhập qua các lần deploy.
- Có thể theo dõi `/health` bằng UptimeRobot. Health check chỉ xác nhận web process phản hồi; nó không đảm bảo game server từ xa đang hoạt động.

## Vận hành

- Supervisor quản lý Xvfb, emulator và Gunicorn.
- Dashboard ngừng polling ảnh và trạng thái khi tab bị ẩn; polling tiếp tục khi tab hiện lại.
- Upload JAR giới hạn tối đa 64 MB và xác thực file có cấu trúc ZIP/JAR.
- Đăng nhập sai bị giới hạn 5 lần trong 5 phút theo địa chỉ kết nối mà Flask nhận được; ứng dụng không tin `X-Forwarded-For` trực tiếp.
