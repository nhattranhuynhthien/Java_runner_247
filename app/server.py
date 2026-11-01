import os
import io
import time
import socket
import subprocess
import threading
import json
import hmac
import hashlib
import secrets
import struct
import tempfile
import zipfile
from datetime import timedelta
from flask import Flask, render_template, request, Response, jsonify, session, redirect, url_for
from PIL import Image, ImageGrab

os.environ["DISPLAY"] = ":1"
SERVER_START_TIME = time.time()
ROM_FILE = os.path.abspath(os.environ.get("ROM_FILE", "/data/roms/NRO.jar"))

# Y offset for cropping out the MicroEmulator window menu bar
MENU_BAR_OFFSET = 24
SCREEN_WIDTH = 240
SCREEN_HEIGHT = 360

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=14)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

x11_lock = threading.Lock()
login_attempts_lock = threading.Lock()

def safe_run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] Lệnh timed out (được bỏ qua an toàn): {cmd}")
        return None
    except Exception as e:
        print(f"[ERROR] Lỗi thực thi lệnh {cmd}: {e}")
        return None

def restart_emulator():
    subprocess.run(["supervisorctl", "restart", "emulator"], timeout=15, check=True)

# Persistent administrator configuration
SECURITY_CONFIG_FILE = os.environ.get("SECURITY_CONFIG_FILE", "/data/security_config.json")

def load_security_config():
    if os.path.exists(SECURITY_CONFIG_FILE):
        try:
            with open(SECURITY_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_security_config(cfg):
    temp_path = None
    try:
        config_dir = os.path.dirname(SECURITY_CONFIG_FILE) or "."
        os.makedirs(config_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=config_dir, prefix="security-", suffix=".tmp", delete=False) as f:
            temp_path = f.name
            json.dump(cfg, f, indent=2)
        os.replace(temp_path, SECURITY_CONFIG_FILE)
        temp_path = None
        if os.name == "posix":
            os.chmod(SECURITY_CONFIG_FILE, 0o600)
    except Exception as e:
        print(f"[SECURITY] Lỗi lưu cấu hình: {e}")
        return False
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    return True

def hash_secret(secret_str, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", str(secret_str).encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return hashed, salt

def get_admin_credentials():
    cfg = load_security_config()
    user = cfg.get("admin_user", os.environ.get("ADMIN_USER", "admin")).strip()
    return user, cfg

def verify_credentials(candidate_user, candidate_pass):
    if not candidate_user or not candidate_pass:
        return False
    cand_user = str(candidate_user).strip().lower()
    cand_pass = str(candidate_pass).strip()

    admin_user, cfg = get_admin_credentials()
    if cand_user != admin_user.lower():
        return False

    if "hashed_password" in cfg and "salt" in cfg:
        calc_hash, _ = hash_secret(cand_pass, cfg["salt"])
        return hmac.compare_digest(calc_hash, cfg["hashed_password"])

    # If ADMIN_PASSWORD is set in environment
    configured_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if configured_password:
        return hmac.compare_digest(cand_pass, configured_password)

    # Default fallback: "admin"
    return hmac.compare_digest(cand_pass, "admin")

def update_credentials(new_user, new_pass):
    hashed, salt = hash_secret(str(new_pass).strip())
    cfg = load_security_config()
    cfg["admin_user"] = str(new_user).strip()
    cfg["hashed_password"] = hashed
    cfg["salt"] = salt
    cfg["updated_at"] = time.time()
    if not save_security_config(cfg):
        raise OSError("Could not persist administrator credentials")

# Temporary login rate limit
MAX_FAILED_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
FAILED_LOGIN_ATTEMPTS = {}  # { ip: [timestamps] }

def get_client_ip(req):
    return req.remote_addr or "127.0.0.1"

def record_failed_attempt(ip):
    with login_attempts_lock:
        now = time.time()
        attempts = [t for t in FAILED_LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        FAILED_LOGIN_ATTEMPTS[ip] = attempts
        count = len(attempts)
        if count >= MAX_FAILED_ATTEMPTS:
            return True, 0
        return False, MAX_FAILED_ATTEMPTS - count

def reset_failed_attempts(ip):
    with login_attempts_lock:
        FAILED_LOGIN_ATTEMPTS.pop(ip, None)

@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify({"error": "File tải lên vượt quá giới hạn 64 MB."}), 413

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

ALLOWED_KEYS = {
    'Up', 'Down', 'Left', 'Right', 'Return', 'F1', 'F2', 'BackSpace', 'space', 'Escape', 'Tab',
    'asterisk', 'numbersign', 'KP_Enter', 'KP_Space'
} | {chr(c) for c in range(ord('a'), ord('z') + 1)} | {chr(c) for c in range(ord('A'), ord('Z') + 1)} | {chr(c) for c in range(ord('0'), ord('9') + 1)} | {'*', '#', '@', '.', '_', '-', '+', '/', ':', '!', '?', '$', '%', '&'}

KEY_MAP = {
    'up': 'Up',
    'down': 'Down',
    'left': 'Left',
    'right': 'Right',
    'ok': 'Return',
    'select': 'Return',
    'return': 'Return',
    'enter': 'Return',
    'space': 'space',
    'escape': 'Escape',
    'f1': 'F1',
    'f2': 'F2',
    'backspace': 'BackSpace',
    'star': 'asterisk',
    'hash': 'numbersign',
    ' ': 'space',
}

def check_pin(req):
    if session.get("authenticated") is True:
        return True
    pin = req.headers.get("X-PIN") or req.headers.get("X-Auth-Token")
    if not pin and req.is_json:
        data = req.get_json(silent=True)
        if isinstance(data, dict):
            pin = data.get("pin") or data.get("password")
    if not pin:
        pin = req.form.get("pin") or req.form.get("password")
    if not pin:
        return False
    user, _ = get_admin_credentials()
    return verify_credentials(user, pin)

def get_game_window():
    try:
        p = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "java"], capture_output=True, text=True, timeout=1)
        wids = p.stdout.strip().split()
        if not wids:
            p = subprocess.run(["xdotool", "search", "--onlyvisible", ""], capture_output=True, text=True, timeout=1)
            wids = p.stdout.strip().split()
        if wids:
            wid = wids[-1]
            subprocess.run(["xdotool", "windowfocus", wid], timeout=1, check=False)
            subprocess.run(["xdotool", "windowactivate", wid], timeout=1, check=False)
            return wid
    except Exception:
        pass
    return None

def focus_game_window():
    return get_game_window()

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
    if session.get("authenticated") is True:
        return render_template('index.html')
    return render_template('login.html')

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    ip = get_client_ip(request)
    with login_attempts_lock:
        recent_attempts = [t for t in FAILED_LOGIN_ATTEMPTS.get(ip, []) if time.time() - t < LOGIN_WINDOW_SECONDS]
    if len(recent_attempts) >= MAX_FAILED_ATTEMPTS:
        return jsonify({
            "error": "Quá nhiều lần đăng nhập sai. Vui lòng đợi 5 phút rồi thử lại.",
            "locked": True
        }), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()

    # Also support single pin field for fallback
    if not password and data.get("pin"):
        password = str(data.get("pin")).strip()
    if not username:
        admin_u, _ = get_admin_credentials()
        username = admin_u

    if not password:
        return jsonify({"error": "Vui lòng nhập tài khoản và mật khẩu quản trị!"}), 400

    if verify_credentials(username, password):
        reset_failed_attempts(ip)
        session["authenticated"] = True
        session["admin_user"] = username
        session.permanent = True
        return jsonify({"status": "ok", "message": "Đăng nhập thành công"})
    else:
        is_limited, remaining = record_failed_attempt(ip)
        time.sleep(1.0)
        if is_limited:
            return jsonify({
                "error": "Quá nhiều lần đăng nhập sai. Vui lòng đợi 5 phút rồi thử lại.",
                "locked": True
            }), 429
        return jsonify({
            "error": "Tài khoản hoặc mật khẩu không chính xác!",
            "remaining": remaining
        }), 401

@app.route('/api/auth/logout', methods=['POST', 'GET'])
def auth_logout():
    session.clear()
    if request.method == 'GET':
        return redirect(url_for('index'))
    return jsonify({"status": "ok", "message": "Đã đăng xuất an toàn"})

@app.route('/api/auth/change_credentials', methods=['POST'])
def auth_change_credentials():
    if not session.get("authenticated") and not check_pin(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    old_user = str(data.get("old_user", "")).strip()
    old_pass = str(data.get("old_pass", "")).strip()
    new_user = str(data.get("new_user", "")).strip()
    new_pass = str(data.get("new_pass", "")).strip()

    if not old_user:
        curr_u, _ = get_admin_credentials()
        old_user = curr_u

    if not verify_credentials(old_user, old_pass):
        return jsonify({"error": "Tài khoản hoặc mật khẩu hiện tại không đúng!"}), 400

    if not new_user:
        return jsonify({"error": "Tài khoản mới không được để trống!"}), 400

    if len(new_pass) < 12:
        return jsonify({"error": "Mật khẩu mới phải có ít nhất 12 ký tự!"}), 400

    try:
        update_credentials(new_user, new_pass)
    except OSError:
        return jsonify({"error": "Không thể lưu thông tin đăng nhập. Hãy kiểm tra quyền ghi và volume /data."}), 500
    session["authenticated"] = True
    session["admin_user"] = new_user
    session.permanent = True
    return jsonify({"status": "ok", "message": "Đã đổi tài khoản và mật khẩu thành công!"})

# Backward compatibility alias
@app.route('/api/auth/change_pin', methods=['POST'])
def auth_change_pin():
    data = request.get_json(silent=True) or {}
    old_pin = data.get("old_pin", "")
    new_pin = data.get("new_pin", "")
    curr_u, _ = get_admin_credentials()
    data["old_user"] = curr_u
    data["old_pass"] = old_pin
    data["new_user"] = curr_u
    data["new_pass"] = new_pin
    request._cached_json = (data, data)
    return auth_change_credentials()

@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    is_auth = session.get("authenticated") is True
    if not is_auth:
        return jsonify({"authenticated": False})

    admin_user, cfg = get_admin_credentials()
    is_default = ("hashed_password" not in cfg) and len(os.environ.get("ADMIN_PASSWORD", "").strip()) < 12
    return jsonify({
        "authenticated": True,
        "admin_user": admin_user,
        "is_default": is_default
    })

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
        response.headers['Pragma'] = 'no-cache'
        return response

    except Exception:
        buf = io.BytesIO()
        blank = Image.new('RGB', (SCREEN_WIDTH, SCREEN_HEIGHT), color=(15, 20, 30))
        blank.save(buf, format='JPEG', quality=50)
        response = Response(buf.getvalue(), mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
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

    rom_file = ROM_FILE
    if not os.path.exists(rom_file) and os.path.exists("/app/roms/NRO.jar"):
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
        "rom": rom_info,
        "saved_account": read_rms_saved_info()
    })

def type_text_reliably(value, wid=None):
    """
    Nhập văn bản vào game một cách tin cậy 100%:
    - Ưu tiên dán trực tiếp qua X11 Clipboard (Ctrl+V) để giữ nguyên vẹn ký tự @, số, ký tự đặc biệt.
    - Fallback: tách ký tự @ thành tổ hợp phím shift+2 để tránh lỗi xdotool nuốt mất @ trong Xvfb.
    """
    if not value:
        return

    # Cách 1: Clipboard paste (Ctrl+V) - MicroEmulator hỗ trợ native trong TextBox
    pasted = False
    for tool in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
        try:
            p = subprocess.Popen(tool, stdin=subprocess.PIPE, env=dict(os.environ, DISPLAY=":1"))
            p.communicate(input=value.encode('utf-8'), timeout=2)
            if p.returncode == 0:
                pasted = True
                break
        except Exception:
            pass

    if pasted:
        time.sleep(0.06)
        cmd_paste = ["xdotool", "key", "ctrl+v"]
        if wid:
            cmd_paste = ["xdotool", "key", "--window", wid, "ctrl+v"]
        safe_run(cmd_paste, timeout=2)
        return

    # Cách 2: Gõ bằng xdotool (xử lý riêng ký tự @ bằng phím at trong X11/Xvfb)
    if "@" in value:
        parts = value.split("@")
        for i, part in enumerate(parts):
            if part:
                cmd = ["xdotool", "type", "--delay", "25", "--", part]
                if wid:
                    cmd = ["xdotool", "type", "--window", wid, "--delay", "25", "--", part]
                safe_run(cmd, timeout=5)
                time.sleep(0.05)
            if i < len(parts) - 1:
                # Gõ phím @ chuẩn xác bằng xdotool key at
                cmd_at = ["xdotool", "key", "at"]
                if wid:
                    cmd_at = ["xdotool", "key", "--window", wid, "at"]
                safe_run(cmd_at, timeout=2)
                time.sleep(0.05)
    else:
        cmd = ["xdotool", "type", "--delay", "20", "--", value]
        if wid:
            cmd = ["xdotool", "type", "--window", wid, "--delay", "20", "--", value]
        safe_run(cmd, timeout=8)

@app.route('/api/input', methods=['POST'])
def handle_input():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    action = data.get("action")

    try:
        with x11_lock:
            wid = focus_game_window()

            if action == "key":
                raw_val = str(data.get("value", "")).strip()
                key_to_send = KEY_MAP.get(raw_val.lower(), raw_val)

                if key_to_send not in ALLOWED_KEYS and key_to_send not in KEY_MAP.values():
                    return jsonify({"error": f"Invalid key: {raw_val}"}), 400

                cmd = ["xdotool", "key", key_to_send]
                if wid:
                    cmd = ["xdotool", "key", "--window", wid, key_to_send]
                safe_run(cmd, timeout=2)

                # Khi người dùng bấm OK / Select: Gửi kèm space để hỗ trợ trọn vẹn mọi ngữ cảnh game (cả Enter lẫn Space)
                if raw_val.lower() in ("ok", "select"):
                    cmd_space = ["xdotool", "key", "space"]
                    if wid:
                        cmd_space = ["xdotool", "key", "--window", wid, "space"]
                    safe_run(cmd_space, timeout=2)

            elif action == "click":
                try:
                    raw_x = float(data.get("x", 0))
                    raw_y = float(data.get("y", 0))
                    x = max(0, min(SCREEN_WIDTH - 1, int(round(raw_x))))
                    y = max(0, min(SCREEN_HEIGHT - 1, int(round(raw_y)))) + MENU_BAR_OFFSET
                    cmd = ["xdotool", "mousemove", str(x), str(y), "click", "1"]
                    safe_run(cmd, timeout=2)
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid coordinates"}), 400

            elif action == "text":
                value = str(data.get("value", ""))
                if value:
                    type_text_reliably(value, wid=wid)
                time.sleep(0.08)
                submit = data.get("submit")
                if submit == "f2" or data.get("f2") is True:
                    cmd = ["xdotool", "key", "F2"]
                    if wid:
                        cmd = ["xdotool", "key", "--window", wid, "F2"]
                    safe_run(cmd, timeout=2)
                elif submit == "enter" or data.get("enter") is True:
                    cmd = ["xdotool", "key", "Return"]
                    if wid:
                        cmd = ["xdotool", "key", "--window", wid, "Return"]
                    safe_run(cmd, timeout=2)

            elif action == "clear_text":
                # Clear all characters in the active text box
                cmd = ["xdotool", "key", "--repeat", "45", "--delay", "8", "BackSpace"]
                if wid:
                    cmd = ["xdotool", "key", "--window", wid, "--repeat", "45", "--delay", "8", "BackSpace"]
                safe_run(cmd, timeout=3)

            elif action == "anti_stuck":
                # Đóng mọi hộp thoại đang mở: hộp thoại chat (80, 274), thông báo giữa (120, 245/255), thông báo dưới (120, 285)
                safe_run(["xdotool", "mousemove", "80", str(274 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "mousemove", "120", str(255 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "mousemove", "120", str(245 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "mousemove", "120", str(285 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "key", "Return"], timeout=1)
                safe_run(["xdotool", "key", "space"], timeout=1)
                safe_run(["xdotool", "key", "F1"], timeout=1)
                safe_run(["xdotool", "key", "Escape"], timeout=1)

            else:
                return jsonify({"error": "Invalid action"}), 400

        return jsonify({"status": "ok"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Action timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def is_chat_box_open():
    """Kiểm tra xem hộp thoại Chat có đang mở trên màn hình game hay không."""
    try:
        im = ImageGrab.grab(bbox=(0, MENU_BAR_OFFSET, SCREEN_WIDTH, MENU_BAR_OFFSET + SCREEN_HEIGHT))
        if im is not None:
            im = im.convert('RGB')
            # Nút OK tại (80, 274) có màu cam đặc trưng (r > 190, 100 < g < 185, b < 100)
            r, g, b = im.getpixel((80, 274))[:3]
            if r > 190 and 100 < g < 185 and b < 100:
                return True
    except Exception:
        pass
    return False

def send_via_bridge(cmd):
    """Gửi lệnh trực tiếp vào JVM qua GameBridgeAgent nội bộ nếu có."""
    try:
        import urllib.parse
        import urllib.request
        url = f"http://127.0.0.1:28888/api/game/cmd?cmd={urllib.parse.quote(cmd)}"
        req = urllib.request.Request(url, data=b"", headers={"User-Agent": "NRO-Server"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass
    return False

def get_bridge_status():
    """Lấy trạng thái game thời gian thực từ JVM Bridge."""
    try:
        import urllib.request
        import json
        req = urllib.request.Request("http://127.0.0.1:28888/api/game/status", headers={"User-Agent": "NRO-Server"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode('utf-8'))
    except Exception:
        pass
    return None

def send_chat_command(c, wid=None):
    """
    Gửi một lệnh chat vào game một cách tin cậy 100%:
    1. Ưu tiên qua GameBridgeAgent (tức thì, 0 độ trễ, không lỗi giao diện).
    2. Fallback: dùng xdotool với phím xác nhận F1 (Left Softkey) và độ trễ 1.15s
       để tôn trọng giới hạn cooldown 1000ms trong mã nguồn Dragonboy.
    """
    if send_via_bridge(c):
        return True

    # Fallback xdotool
    chat_open = is_chat_box_open()
    if not chat_open:
        cmd_open = ["xdotool", "key", "Return"]
        if wid:
            cmd_open = ["xdotool", "key", "--window", wid, "Return"]
        safe_run(cmd_open, timeout=2)
        time.sleep(0.3)

    # Xóa sạch ô chat bằng click (X) và BackSpace
    cmd_click_clear = ["xdotool", "mousemove", "211", str(246 + MENU_BAR_OFFSET), "click", "1"]
    safe_run(cmd_click_clear, timeout=1)
    time.sleep(0.08)

    cmd_clear = ["xdotool", "key", "--repeat", "35", "--delay", "6", "BackSpace"]
    if wid:
        cmd_clear = ["xdotool", "key", "--window", wid, "--repeat", "35", "--delay", "6", "BackSpace"]
    safe_run(cmd_clear, timeout=1)
    time.sleep(0.08)

    # Gõ lệnh
    cmd_type = ["xdotool", "type", "--delay", "25", "--", c]
    if wid:
        cmd_type = ["xdotool", "type", "--window", wid, "--delay", "25", "--", c]
    safe_run(cmd_type, timeout=3)
    time.sleep(0.18)

    # Xác nhận lệnh: Trong Dragonboy, nút OK là Left Softkey (F1) hoặc click (80, 274)
    cmd_click_ok = ["xdotool", "mousemove", "80", str(274 + MENU_BAR_OFFSET), "click", "1"]
    safe_run(cmd_click_ok, timeout=2)
    time.sleep(0.1)

    # Gửi Return và F1 (F1 = Left Softkey trong MicroEmulator)
    safe_run(["xdotool", "key", "Return"], timeout=1)
    safe_run(["xdotool", "key", "F1"], timeout=1)

    # Chờ tối thiểu 1.15s vì Dragonboy có chat cooldown 1000ms
    time.sleep(0.5)
    if is_chat_box_open():
        safe_run(cmd_click_ok, timeout=1)
        safe_run(["xdotool", "key", "F1"], timeout=1)
        time.sleep(0.65)
    else:
        time.sleep(0.65)

    return True

@app.route('/api/quick_cmd', methods=['POST'])
def handle_quick_cmd():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    raw_cmd = str(data.get("cmd", "")).strip()
    if not raw_cmd:
        return jsonify({"error": "Command is empty"}), 400

    cmds = [c.strip() for c in raw_cmd.replace(';', ',').split(',') if c.strip()]

    # Thử gửi toàn bộ qua bridge nếu có
    if send_via_bridge(raw_cmd):
        return jsonify({"status": "ok", "method": "bridge", "sent_cmd": raw_cmd, "executed": cmds})

    try:
        with x11_lock:
            wid = focus_game_window()
            for c in cmds:
                send_chat_command(c, wid=wid)

        return jsonify({"status": "ok", "method": "xdotool", "sent_cmd": raw_cmd, "executed": cmds})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/auto_farm', methods=['POST'])
def handle_auto_farm():
    """
    Tự động hóa hoàn toàn chức năng Treo Map & Auto Up SKH:
    1. Tự động bay map bằng Xmap (nếu có chọn map cụ thể).
    2. Chờ nhân vật bay tới map rồi đổi khu (zone) mong muốn.
    3. Tự động kích hoạt TDLT hoặc Tàn sát kèm theo toàn bộ bộ tiện ích:
       Lọc đồ SKH (locdo), Auto nhặt (anhat), Giữ map (gb), Chạy x5 (s20), Giảm đồ họa (xdh), Ẩn người (annguoi).
    """
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    map_val = str(data.get("map", "")).strip()
    zone_val = str(data.get("zone", "")).strip()
    mode = str(data.get("mode", "tdlt")).strip().lower()

    # 1. Ưu tiên gửi qua GameBridgeAgent nội bộ
    try:
        import urllib.parse
        import urllib.request
        query = f"map={urllib.parse.quote(map_val)}&zone={urllib.parse.quote(zone_val)}&mode={urllib.parse.quote(mode)}"
        url = f"http://127.0.0.1:28888/api/game/auto_farm?{query}"
        req = urllib.request.Request(url, data=b"", headers={"User-Agent": "NRO-Server"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                res_data = json.loads(resp.read().decode('utf-8'))
                return jsonify({
                    "status": "ok",
                    "method": "bridge",
                    "message": "Đã kích hoạt Auto Farm thành công qua Game Engine!",
                    "details": res_data
                })
    except Exception as e:
        print(f"[AUTO_FARM] Bridge offline ({e}), running fallback thread...")

    # 2. Fallback: chạy nền bằng chuỗi chat commands
    def fallback_farm_worker():
        try:
            with x11_lock:
                wid = focus_game_window()
                # Nếu người dùng chọn mở menu Xmap
                if map_val == "xmap":
                    send_chat_command("xmap", wid=wid)
                    time.sleep(1.2)

                # Đổi khu nếu có
                if zone_val and zone_val.isdigit():
                    send_chat_command(f"k{zone_val}", wid=wid)
                    time.sleep(1.2)

                # Bật chế độ farm (tdlt hoặc ts)
                farm_cmd = "ts" if mode == "ts" else "tdlt"
                send_chat_command(farm_cmd, wid=wid)
                time.sleep(1.2)

                # Kích hoạt toàn bộ tiện ích mod
                for util in ["locdo", "anhat", "gb", "s20", "xdh", "annguoi"]:
                    send_chat_command(util, wid=wid)
                    time.sleep(1.2)
        except Exception as err:
            print(f"[FALLBACK_FARM] Error: {err}")

    threading.Thread(target=fallback_farm_worker, daemon=True).start()

    return jsonify({
        "status": "ok",
        "method": "fallback",
        "message": "Đã bắt đầu chuỗi tự động đổi khu và kích hoạt Farm!"
    })

@app.route('/api/game_status', methods=['GET'])
def handle_game_status():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized"}), 403
    st = get_bridge_status()
    if st:
        return jsonify({"status": "ok", "game": st})
    return jsonify({"status": "offline", "game": None})

def get_menu_layout():
    """
    Tự động phân tích màn hình game để nhận diện bố cục Menu:
    - 5 nút (Đã có tài khoản): [Chơi tiếp, Chơi mới, Đổi tài khoản, Máy chủ, Cấu hình]
    - 4 nút (Mới khởi động game): [Chơi mới, Đổi tài khoản, Máy chủ, Cấu hình]
    """
    try:
        im = ImageGrab.grab(bbox=(0, MENU_BAR_OFFSET, SCREEN_WIDTH, MENU_BAR_OFFSET + SCREEN_HEIGHT))
        if im is not None:
            im = im.convert('RGB')
            # Kiểm tra pixel tại (120, 144)
            # Với layout 5 nút: (120, 144) là tâm nút 'Chơi tiếp' màu cam (r > 180, g > 110)
            # Với layout 4 nút: (120, 144) là hình nền phong cảnh tối phía trên các nút (r < 130)
            r, g, b = im.getpixel((120, 144))[:3]
            if r > 180 and g > 110:
                return 5
    except Exception as e:
        print(f"[LAYOUT] Lỗi kiểm tra bố cục menu: {e}")
    return 4

def dismiss_error_popup_if_any():
    """Tự động phát hiện và đóng hộp thoại thông báo nếu đang xuất hiện."""
    try:
        im = ImageGrab.grab(bbox=(0, MENU_BAR_OFFSET, SCREEN_WIDTH, MENU_BAR_OFFSET + SCREEN_HEIGHT))
        if im is not None:
            im = im.convert('RGB')
            # Kiểm tra nút Đóng/OK màu cam tại (120, 255)
            r, g, b = im.getpixel((120, 255))[:3]
            if r > 180 and 100 < g < 185 and b < 100:
                cmd = ["xdotool", "mousemove", "120", str(255 + MENU_BAR_OFFSET), "click", "1"]
                safe_run(cmd, timeout=1)
                safe_run(["xdotool", "key", "Return"], timeout=1)
                time.sleep(0.3)
                return True

            # Thân hộp thoại nền giấy vàng tại (120, 220)
            r2, g2, b2 = im.getpixel((120, 220))[:3]
            if r2 > 200 and g2 > 200 and b2 > 170:
                cmd = ["xdotool", "mousemove", "120", str(285 + MENU_BAR_OFFSET), "click", "1"]
                safe_run(cmd, timeout=1)
                safe_run(["xdotool", "key", "Return"], timeout=1)
                time.sleep(0.3)
                return True
    except Exception as e:
        print(f"[POPUP] Lỗi kiểm tra popup: {e}")
    return False

# =========================================================================
# J2ME Record Management System (RMS) - Quản lý Tài khoản & Máy chủ Tự Động
# =========================================================================

def get_all_suite_dirs():
    """
    Trả về danh sách tất cả các thư mục suite RMS của MicroEmulator để đảm bảo ghi đúng 100%.
    Bao gồm suite-Dragonboy250 v3.0, suite-null, và tất cả thư mục suite-* hiện có.
    """
    base_dirs = ["/root/.microemulator", os.path.expanduser("~/.microemulator")]
    home = os.environ.get("HOME")
    if home:
        base_dirs.append(os.path.join(home, ".microemulator"))

    suite_dirs = set()
    for base in base_dirs:
        try:
            if os.path.exists(base):
                for name in os.listdir(base):
                    if name.startswith("suite-"):
                        suite_dirs.add(os.path.join(base, name))
            suite_dirs.add(os.path.join(base, "suite-Dragonboy250 v3.0"))
            suite_dirs.add(os.path.join(base, "suite-null"))
        except Exception:
            pass

    if not suite_dirs:
        suite_dirs.add("/root/.microemulator/suite-Dragonboy250 v3.0")

    for d in suite_dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    return list(suite_dirs)

def get_rms_suite_dir():
    dirs = get_all_suite_dirs()
    return dirs[0] if dirs else "/root/.microemulator/suite-Dragonboy250 v3.0"

def make_rs_bytes(store_name, records):
    """
    Tạo nội dung file .rs nhị phân MicroEmulator 2.0.4 tương thích 100%:
    - 2 bytes unsigned short (big-endian): độ dài tên store
    - tên store UTF-8 bytes
    - 4 bytes int (big-endian): version (1)
    - 8 bytes long (big-endian): lastModified (epoch ms)
    - 4 bytes int (big-endian): nextRecordID
    - Lặp từng record:
        - 4 bytes int: recordId
        - 4 bytes int: length
        - raw data bytes
    """
    name_bytes = store_name.encode('utf-8')
    buf = bytearray()
    buf += struct.pack('>H', len(name_bytes))
    buf += name_bytes
    version = 1
    last_modified = int(time.time() * 1000)
    next_rec_id = max(records.keys(), default=0) + 1
    buf += struct.pack('>i', version)
    buf += struct.pack('>q', last_modified)
    buf += struct.pack('>i', next_rec_id)
    for rec_id, rec_data in records.items():
        if isinstance(rec_data, str):
            rec_data = rec_data.encode('utf-8')
        elif isinstance(rec_data, int):
            rec_data = bytes([rec_data])
        buf += struct.pack('>i', rec_id)
        buf += struct.pack('>i', len(rec_data))
        buf += rec_data
    return bytes(buf)

def read_rs_records(filepath):
    """Đọc cấu trúc file .rs của MicroEmulator trả về (store_name, {id: bytes})."""
    if not os.path.exists(filepath):
        return None, {}
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
        if len(data) < 18:
            return None, {}
        offset = 0
        name_len = struct.unpack_from('>H', data, offset)[0]
        offset += 2
        store_name = data[offset:offset+name_len].decode('utf-8', errors='ignore')
        offset += name_len
        version, last_mod, next_id = struct.unpack_from('>iqi', data, offset)
        offset += 16
        records = {}
        while offset + 8 <= len(data):
            rec_id, rec_len = struct.unpack_from('>ii', data, offset)
            offset += 8
            if offset + rec_len <= len(data):
                records[rec_id] = data[offset:offset+rec_len]
                offset += rec_len
            else:
                break
        return store_name, records
    except Exception as e:
        print(f"[RMS] Lỗi đọc file {filepath}: {e}")
        return None, {}

def write_rms_account(username, password="", server_num=0):
    """
    Ghi trực tiếp tài khoản, mật khẩu và máy chủ vào RecordStore RMS của game NRO:
    - vjacc.rs: tài khoản (username lowercase trim)
    - vjpass.rs: mật khẩu (password)
    - vjcheck.rs: ghi nhớ tài khoản (1)
    - vjsvselect.rs: máy chủ đã chọn (server_num - 1)
    Ghi đồng bộ vào tất cả các thư mục suite RMS để đảm bảo emulator luôn nhận diện 100%.
    """
    clean_user = username.strip().lower()
    clean_pass = password.strip()

    bytes_acc = make_rs_bytes("vjacc", {1: clean_user})
    bytes_pass = make_rs_bytes("vjpass", {1: clean_pass}) if clean_pass else None
    bytes_check = make_rs_bytes("vjcheck", {1: 1})
    bytes_sv = None
    if server_num and int(server_num) > 0:
        sv_idx = max(0, min(13, int(server_num) - 1))
        bytes_sv = make_rs_bytes("vjsvselect", {1: sv_idx})

    suite_dirs = get_all_suite_dirs()
    for sdir in suite_dirs:
        try:
            os.makedirs(sdir, exist_ok=True)
            # 1. Ghi vjacc.rs
            with open(os.path.join(sdir, "vjacc.rs"), "wb") as f:
                f.write(bytes_acc)
            # 2. Ghi vjpass.rs (nếu có pass)
            if bytes_pass:
                with open(os.path.join(sdir, "vjpass.rs"), "wb") as f:
                    f.write(bytes_pass)
            # 3. Ghi vjcheck.rs (luôn là 1)
            with open(os.path.join(sdir, "vjcheck.rs"), "wb") as f:
                f.write(bytes_check)
            # 4. Ghi vjsvselect.rs (nếu có chọn server)
            if bytes_sv:
                with open(os.path.join(sdir, "vjsvselect.rs"), "wb") as f:
                    f.write(bytes_sv)
            print(f"[RMS] Đã ghi cấu hình thành công tại: {sdir}")
        except Exception as e:
            print(f"[RMS] Lỗi ghi cấu hình tại {sdir}: {e}")

    return True

def read_rms_saved_info():
    """Đọc thông tin tài khoản và máy chủ hiện được lưu trong RMS của game."""
    try:
        suite_dirs = get_all_suite_dirs()
        user = ""
        server = 7
        for sdir in suite_dirs:
            acc_path = os.path.join(sdir, "vjacc.rs")
            sv_path = os.path.join(sdir, "vjsvselect.rs")

            if not user and os.path.exists(acc_path):
                _, recs = read_rs_records(acc_path)
                if 1 in recs:
                    user = recs[1].decode('utf-8', errors='ignore')

            if os.path.exists(sv_path):
                _, recs = read_rs_records(sv_path)
                if 1 in recs and len(recs[1]) > 0:
                    server = int(recs[1][0]) + 1

            if user:
                break

        return {"username": user, "server": server, "has_saved": bool(user)}
    except Exception as e:
        print(f"[RMS] Lỗi đọc thông tin đã lưu: {e}")
        return {"username": "", "server": 7, "has_saved": False}

def auto_enter_game_worker():
    """
    Background worker:
    Sau khi nạp RMS và khởi động lại game:
    Theo dõi màn hình và phát hiện ngay khi Menu chính 5 nút (Đã có tài khoản) xuất hiện,
    tự động click nút 1 'Chơi TK' để vào game ngay lập tức!
    """
    print("[AUTO_LOGIN] Bắt đầu theo dõi màn hình để tự động vào game...")
    time.sleep(1.2)
    start_t = time.time()
    entered = False

    while time.time() - start_t < 16:
        try:
            dismiss_error_popup_if_any()
            layout = get_menu_layout()
            if layout == 5:
                elapsed = round(time.time() - start_t, 1)
                print(f"[AUTO_LOGIN] Phát hiện Menu 5 nút sau {elapsed}s. Tự động kích hoạt 'Chơi TK'...")
                with x11_lock:
                    wid = focus_game_window()
                    cmd_click = ["xdotool", "mousemove", "120", str(144 + MENU_BAR_OFFSET), "click", "1"]
                    if wid:
                        cmd_click = ["xdotool", "mousemove", "--window", wid, "120", str(144 + MENU_BAR_OFFSET), "click", "1"]
                    safe_run(cmd_click, timeout=2)
                    time.sleep(0.12)
                    safe_run(["xdotool", "key", "Return"], timeout=1)
                    safe_run(["xdotool", "key", "space"], timeout=1)
                print("[AUTO_LOGIN] Đã kích hoạt 'Chơi TK' thành công!")
                entered = True
                break
        except Exception as e:
            print(f"[AUTO_LOGIN] Lỗi theo dõi: {e}")
        time.sleep(0.3)

    if not entered:
        print("[AUTO_LOGIN] Đã hết thời gian chờ menu (16s).")

@app.route('/api/open_account_dialog', methods=['POST'])
def handle_open_account_dialog():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        with x11_lock:
            focus_game_window()
            dismiss_error_popup_if_any()
            layout = get_menu_layout()
            y_doi_tk = 202 if layout == 5 else 187
            cmd = ["xdotool", "mousemove", "120", str(y_doi_tk + MENU_BAR_OFFSET), "click", "1"]
            safe_run(cmd, timeout=2)
            time.sleep(0.5)

        return jsonify({"status": "ok", "message": "Đã mở hộp thoại Đổi tài khoản!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/select_server', methods=['POST'])
def handle_select_server():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    try:
        server_num = int(data.get("server", 0))
    except (ValueError, TypeError):
        server_num = 0

    if server_num < 1 or server_num > 13:
        return jsonify({"error": "Vui lòng chọn máy chủ hợp lệ (1 - 13)!"}), 400

    try:
        # Ghi trực tiếp máy chủ vào RMS (vjsvselect.rs)
        suite_dir = get_rms_suite_dir()
        sv_idx = server_num - 1
        sv_path = os.path.join(suite_dir, "vjsvselect.rs")
        with open(sv_path, "wb") as f:
            f.write(make_rs_bytes("vjsvselect", {1: sv_idx}))
        print(f"[RMS] Đã lưu máy chủ {server_num} (index {sv_idx}) vào {sv_path}")

        # Khởi động lại game để nạp máy chủ mới
        restart_emulator()

        # Tự động vào game khi Menu chính sẵn sàng
        t = threading.Thread(target=auto_enter_game_worker, daemon=True)
        t.start()

        return jsonify({"status": "ok", "message": f"Đã chuyển sang Máy chủ: Vũ trụ {server_num} thành công!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/quick_login', methods=['POST'])
def handle_quick_login():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    try:
        server_num = int(data.get("server", 0))
    except (ValueError, TypeError):
        server_num = 0
    auto_enter = bool(data.get("auto_enter", True))

    if not username:
        return jsonify({"error": "Vui lòng nhập tài khoản (hoặc SĐT/Email)"}), 400

    try:
        # 1. Ghi trực tiếp thông tin vào RMS của game
        # Hoàn toàn không phụ thuộc tọa độ, không click nhầm, không lag gõ phím!
        write_rms_account(username, password, server_num)

        # 2. Khởi động lại game để áp dụng tài khoản & máy chủ mới ngay lập tức
        restart_emulator()

        # 3. Kích hoạt worker tự động bấm 'Chơi TK' ngay khi màn hình chính 5 nút xuất hiện
        if auto_enter:
            t = threading.Thread(target=auto_enter_game_worker, daemon=True)
            t.start()

        return jsonify({
            "status": "ok",
            "message": "Đã lưu tài khoản & máy chủ vào hệ thống game! Đang tự động kết nối vào thế giới..."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/restart', methods=['POST'])
def restart_game():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        restart_emulator()
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
    if not f.filename or not f.filename.lower().endswith('.jar'):
        return jsonify({"error": "Chỉ chấp nhận file định dạng .jar!"}), 400

    rom_dir = os.path.dirname(ROM_FILE)
    os.makedirs(rom_dir, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="NRO-", suffix=".jar", dir=rom_dir, delete=False) as temp_file:
            temp_path = temp_file.name
            f.save(temp_file)
        if not zipfile.is_zipfile(temp_path):
            os.unlink(temp_path)
            return jsonify({"error": "File tải lên không phải JAR/ZIP hợp lệ."}), 400
        os.replace(temp_path, ROM_FILE)
        temp_path = None
        restart_emulator()
    except subprocess.SubprocessError:
        return jsonify({"error": "Đã lưu JAR nhưng không thể khởi động lại emulator."}), 500
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

    return jsonify({"status": "ok", "message": "Đã nạp file .jar thành công và đang khởi động lại game!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
