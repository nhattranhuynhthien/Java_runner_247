import os
import io
import time
import socket
import subprocess
import threading
from flask import Flask, render_template, request, Response, jsonify
from PIL import Image, ImageGrab

os.environ["DISPLAY"] = ":1"
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")
SERVER_START_TIME = time.time()

# Y offset for cropping out the MicroEmulator window menu bar
MENU_BAR_OFFSET = 24
SCREEN_WIDTH = 240
SCREEN_HEIGHT = 360

app = Flask(__name__)
x11_lock = threading.Lock()

ALLOWED_KEYS = {
    'Up', 'Down', 'Left', 'Right', 'Return', 'F1', 'F2', 'BackSpace', 'space', 'Escape', 'Tab'
} | {chr(c) for c in range(ord('a'), ord('z') + 1)} | {chr(c) for c in range(ord('A'), ord('Z') + 1)} | {chr(c) for c in range(ord('0'), ord('9') + 1)} | {'*', '#', '@', '.', '_', '-', '+', '/', ':', '!', '?', '$', '%', '&'}

KEY_MAP = {
    'up': 'Up',
    'down': 'Down',
    'left': 'Left',
    'right': 'Right',
    'ok': 'Return',
    'return': 'Return',
    'enter': 'Return',
    'escape': 'Escape',
    'f1': 'F1',
    'f2': 'F2',
    'backspace': 'BackSpace',
    'star': 'asterisk',
    'hash': 'numbersign',
    ' ': 'space',
}

def check_pin(req):
    pin = req.headers.get("X-PIN")
    if not pin:
        pin = req.args.get("pin")
    if not pin:
        data = req.get_json(silent=True)
        if isinstance(data, dict):
            pin = data.get("pin")
    if not pin:
        pin = req.form.get("pin")
    return str(pin) == str(ADMIN_PIN)

def focus_game_window():
    try:
        p = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "java"], capture_output=True, text=True, timeout=1)
        wids = p.stdout.strip().split()
        if not wids:
            p = subprocess.run(["xdotool", "search", "--onlyvisible", ""], capture_output=True, text=True, timeout=1)
            wids = p.stdout.strip().split()
        if wids:
            subprocess.run(["xdotool", "windowfocus", "--sync", wids[0]], timeout=1)
    except Exception:
        pass

def get_ram_info():
    try:
        mem_total = 0
        mem_available = 0
        if os.path.exists('/proc/meminfo'):
            with open('/proc/meminfo', 'r') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        mem_total = int(line.split()[1]) // 1024
                    elif line.startswith('MemAvailable:'):
                        mem_available = int(line.split()[1]) // 1024
            mem_used = max(0, mem_total - mem_available)
            pct = round((mem_used / mem_total * 100), 1) if mem_total else 0
            return {"used_mb": mem_used, "total_mb": mem_total, "percent": pct}
    except Exception:
        pass
    return {"used_mb": 0, "total_mb": 512, "percent": 0}

def check_nro_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(('112.213.94.23', 14445))
        s.close()
        return True
    except Exception:
        return False

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/api/screenshot', methods=['GET'])
def screenshot():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        im = ImageGrab.grab(bbox=(0, MENU_BAR_OFFSET, SCREEN_WIDTH, MENU_BAR_OFFSET + SCREEN_HEIGHT))
        if im is None:
            im = Image.new('RGB', (SCREEN_WIDTH, SCREEN_HEIGHT), color=(10, 15, 25))
        else:
            im = im.convert('RGB')

        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=55, optimize=True)
        jpeg_bytes = buf.getvalue()

        response = Response(jpeg_bytes, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response

    except Exception:
        buf = io.BytesIO()
        blank = Image.new('RGB', (SCREEN_WIDTH, SCREEN_HEIGHT), color=(15, 20, 30))
        blank.save(buf, format='JPEG', quality=50)
        response = Response(buf.getvalue(), mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response

@app.route('/api/status', methods=['GET'])
def server_status():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    uptime_sec = int(time.time() - SERVER_START_TIME)
    days = uptime_sec // 86400
    hours = (uptime_sec % 86400) // 3600
    minutes = (uptime_sec % 3600) // 60
    if days > 0:
        uptime_human = f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        uptime_human = f"{hours}h {minutes}m"
    else:
        uptime_human = f"{minutes}m {uptime_sec % 60}s"

    emu_running = False
    try:
        p = subprocess.run(["pgrep", "-f", "microemulator"], capture_output=True, text=True)
        emu_running = (p.returncode == 0)
    except Exception:
        pass

    rom_file = "/app/roms/NRO.jar"
    rom_info = {"name": "Không có", "size_kb": 0}
    if os.path.exists(rom_file):
        rom_info = {
            "name": os.path.basename(rom_file),
            "size_kb": round(os.path.getsize(rom_file) / 1024, 1)
        }

    return jsonify({
        "status": "online",
        "uptime": uptime_human,
        "uptime_sec": uptime_sec,
        "emulator_running": emu_running,
        "server_socket_online": check_nro_socket(),
        "ram": get_ram_info(),
        "rom": rom_info
    })

@app.route('/api/input', methods=['POST'])
def handle_input():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    action = data.get("action")

    try:
        with x11_lock:
            focus_game_window()

            if action == "key":
                raw_val = str(data.get("value", ""))
                key_to_send = KEY_MAP.get(raw_val.lower(), raw_val)
                # If single character, type it directly with xdotool type to preserve casing & symbols
                if len(key_to_send) == 1:
                    subprocess.run(["xdotool", "type", "--delay", "20", "--", key_to_send], timeout=2, check=False)
                else:
                    if key_to_send not in ALLOWED_KEYS and key_to_send not in KEY_MAP.values():
                        return jsonify({"error": f"Invalid key: {raw_val}"}), 400
                    subprocess.run(["xdotool", "key", key_to_send], timeout=2, check=False)

            elif action == "click":
                try:
                    raw_x = float(data.get("x", 0))
                    raw_y = float(data.get("y", 0))
                    x = max(0, min(SCREEN_WIDTH, int(round(raw_x))))
                    y = max(0, min(SCREEN_HEIGHT, int(round(raw_y)))) + MENU_BAR_OFFSET
                    subprocess.run(
                        ["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"],
                        timeout=2,
                        check=False
                    )
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid coordinates"}), 400

            elif action == "text":
                value = str(data.get("value", ""))
                if value:
                    subprocess.run(
                        ["xdotool", "type", "--delay", "25", "--", value],
                        timeout=8,
                        check=False
                    )
                time.sleep(0.08)
                submit = data.get("submit")
                if submit == "f2" or data.get("f2") is True:
                    subprocess.run(["xdotool", "key", "F2"], timeout=2, check=False)
                elif submit == "enter" or data.get("enter") is True:
                    subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)

            elif action == "clear_text":
                # Clear all characters in the active text box
                subprocess.run(
                    ["xdotool", "key", "--repeat", "35", "--delay", "10", "BackSpace"],
                    timeout=3,
                    check=False
                )

            elif action == "anti_stuck":
                ok_y = 245 + MENU_BAR_OFFSET
                subprocess.run(["xdotool", "mousemove", "--sync", "120", str(ok_y), "click", "1"], timeout=2, check=False)
                subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
                subprocess.run(["xdotool", "key", "space"], timeout=2, check=False)

            else:
                return jsonify({"error": "Invalid action"}), 400

        return jsonify({"status": "ok"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Action timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/quick_cmd', methods=['POST'])
def handle_quick_cmd():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    raw_cmd = str(data.get("cmd", "")).strip()
    if not raw_cmd:
        return jsonify({"error": "Command is empty"}), 400

    cmds = [c.strip() for c in raw_cmd.replace(';', ',').split(',') if c.strip()]

    try:
        with x11_lock:
            focus_game_window()
            for c in cmds:
                # 1. Open chat in NRO (Press Return)
                subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
                time.sleep(0.12)
                # 2. Type command
                subprocess.run(["xdotool", "type", "--delay", "25", "--", c], timeout=3, check=False)
                time.sleep(0.12)
                # 3. Submit command (Press Return)
                subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
                time.sleep(0.16)

        return jsonify({"status": "ok", "sent_cmd": raw_cmd, "executed": cmds})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/quick_login', methods=['POST'])
def handle_quick_login():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    from_menu = bool(data.get("from_menu", True))
    auto_enter = bool(data.get("auto_enter", True))

    if not username:
        return jsonify({"error": "Vui lòng nhập tài khoản (hoặc SĐT/Email)"}), 400

    try:
        with x11_lock:
            focus_game_window()

            if from_menu:
                # 1. Từ menu chính: Bấm phím Xuống chọn 'Đổi tài khoản' và bấm Return mở hộp thoại
                subprocess.run(["xdotool", "key", "Down"], timeout=2, check=False)
                time.sleep(0.14)
                subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
                time.sleep(0.55)

            # 2. Xóa sạch hộp tài khoản cũ và gõ tài khoản mới
            subprocess.run(["xdotool", "key", "--repeat", "40", "--delay", "10", "BackSpace"], timeout=3, check=False)
            time.sleep(0.08)
            subprocess.run(["xdotool", "type", "--delay", "25", "--", username], timeout=6, check=False)
            time.sleep(0.12)
            # Xác nhận tài khoản (gửi cả F2 và Return để chắc chắn hộp thoại ăn phím)
            subprocess.run(["xdotool", "key", "F2"], timeout=2, check=False)
            time.sleep(0.1)
            subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
            time.sleep(0.55)

            if password:
                # 3. Xóa sạch hộp mật khẩu cũ và gõ mật khẩu mới
                subprocess.run(["xdotool", "key", "--repeat", "40", "--delay", "10", "BackSpace"], timeout=3, check=False)
                time.sleep(0.08)
                subprocess.run(["xdotool", "type", "--delay", "25", "--", password], timeout=6, check=False)
                time.sleep(0.12)
                # Xác nhận mật khẩu
                subprocess.run(["xdotool", "key", "F2"], timeout=2, check=False)
                time.sleep(0.1)
                subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
                time.sleep(0.55)

            if auto_enter:
                # 4. Bấm OK vào game
                subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)

        return jsonify({"status": "ok", "message": "Đã thực hiện chuỗi đăng nhập tự động thành công!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/restart', methods=['POST'])
def restart_game():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        subprocess.run(["pkill", "-9", "-f", "microemulator"], timeout=5, check=False)
        subprocess.run(["supervisorctl", "restart", "emulator"], timeout=10, check=False)
        return jsonify({"status": "ok", "message": "Đã khởi động lại game thành công!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    log_lines = []
    for log_path in ['/var/log/emulator.log', '/var/log/emulator_err.log']:
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    log_lines.extend(lines[-30:])
            except Exception:
                pass

    if not log_lines:
        log_lines = ["[LOG] Chưa có bản ghi log nào được tạo."]

    return jsonify({"status": "ok", "logs": "".join(log_lines[-50:])})

@app.route('/api/upload_rom', methods=['POST'])
def upload_rom():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    if 'file' not in request.files:
        return jsonify({"error": "Vui lòng chọn file .jar để tải lên!"}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.jar'):
        return jsonify({"error": "Chỉ chấp nhận file định dạng .jar!"}), 400

    os.makedirs("/app/roms", exist_ok=True)
    dest_path = "/app/roms/NRO.jar"
    f.save(dest_path)

    subprocess.run(["pkill", "-9", "-f", "microemulator"], check=False)
    subprocess.run(["supervisorctl", "restart", "emulator"], check=False)

    return jsonify({"status": "ok", "message": "Đã nạp file .jar thành công và đang khởi động lại game!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
