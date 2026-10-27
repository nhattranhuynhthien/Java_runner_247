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
    'Up', 'Down', 'Left', 'Right', 'Return', 'F1', 'F2', 'BackSpace', 'space', 'Escape'
} | {chr(c) for c in range(ord('a'), ord('z') + 1)} | {chr(c) for c in range(ord('0'), ord('9') + 1)} | {'*', '#'}

KEY_MAP = {
    'up': 'Up',
    'down': 'Down',
    'left': 'Left',
    'right': 'Right',
    'ok': 'Return',
    'f1': 'F1',
    'f2': 'F2',
    'backspace': 'BackSpace',
    'star': 'asterisk',
    'hash': 'numbersign',
}

def check_pin(req):
    pin = req.headers.get("X-PIN")
    if not pin:
        pin = req.args.get("pin")
    if not pin and req.is_json:
        pin = req.json.get("pin")
    if not pin:
        pin = req.form.get("pin")
    return str(pin) == str(ADMIN_PIN)

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
        # Crop exactly 240x360 game canvas, skipping top menu bar (y=24..384)
        im = ImageGrab.grab(bbox=(0, MENU_BAR_OFFSET, SCREEN_WIDTH, MENU_BAR_OFFSET + SCREEN_HEIGHT))
        if im is None:
            im = Image.new('RGB', (SCREEN_WIDTH, SCREEN_HEIGHT), color=(10, 15, 25))
        else:
            im = im.convert('RGB')

        buf = io.BytesIO()
        # Fast compression: quality=55 yields ~2.5KB image, instantaneous to transfer
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
            if action == "key":
                raw_val = str(data.get("value", ""))
                key_to_send = KEY_MAP.get(raw_val.lower(), raw_val)
                if key_to_send not in ALLOWED_KEYS and key_to_send not in KEY_MAP.values():
                    return jsonify({"error": f"Invalid key: {raw_val}"}), 400
                subprocess.run(["xdotool", "key", key_to_send], timeout=2, check=False)

            elif action == "click":
                try:
                    raw_x = float(data.get("x", 0))
                    raw_y = float(data.get("y", 0))
                    x = max(0, min(SCREEN_WIDTH, int(round(raw_x))))
                    # Add MENU_BAR_OFFSET so click reaches the exact game canvas coordinate
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
                        ["xdotool", "type", "--delay", "30", value],
                        timeout=5,
                        check=False
                    )
                if data.get("enter") is True:
                    subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)

            elif action == "anti_stuck":
                # Click center of OK button on modal dialogs & send Return/space (NO F2 to avoid exit)
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
    cmd = str(data.get("cmd", "")).strip()
    if not cmd:
        return jsonify({"error": "Command is empty"}), 400

    try:
        with x11_lock:
            # 1. Open chat in NRO (Press Return)
            subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)
            time.sleep(0.12)
            # 2. Type command
            subprocess.run(["xdotool", "type", "--delay", "25", cmd], timeout=3, check=False)
            time.sleep(0.12)
            # 3. Submit command (Press Return)
            subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)

        return jsonify({"status": "ok", "sent_cmd": cmd})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/restart', methods=['POST'])
def restart_game():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        subprocess.run(["supervisorctl", "restart", "emulator"], timeout=10, check=False)
        return jsonify({"status": "ok", "message": "Đã gửi lệnh khởi động lại game!"})
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

    # Restart emulator process via supervisor
    subprocess.run(["supervisorctl", "restart", "emulator"], check=False)

    return jsonify({"status": "ok", "message": "Đã nạp file .jar thành công và đang khởi động lại game!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
