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
from datetime import timedelta
from flask import Flask, render_template, request, Response, jsonify, session, redirect, url_for
from PIL import Image, ImageGrab

os.environ["DISPLAY"] = ":1"
SERVER_START_TIME = time.time()

# Y offset for cropping out the MicroEmulator window menu bar
MENU_BAR_OFFSET = 24
SCREEN_WIDTH = 240
SCREEN_HEIGHT = 360

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=14)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

x11_lock = threading.Lock()

def safe_run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] Lệnh timed out (được bỏ qua an toàn): {cmd}")
        return None
    except Exception as e:
        print(f"[ERROR] Lỗi thực thi lệnh {cmd}: {e}")
        return None

# Persistent Security Configuration & Ban System
SECURITY_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "security_config.json")

def load_security_config():
    if os.path.exists(SECURITY_CONFIG_FILE):
        try:
            with open(SECURITY_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_security_config(cfg):
    try:
        with open(SECURITY_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[SECURITY] Lỗi lưu cấu hình: {e}")

# Automatically clear any banned IPs on deployment/startup so the owner is never locked out
try:
    _cfg = load_security_config()
    if _cfg.get("banned_ips"):
        print(f"[SECURITY] Tự động giải phóng {len(_cfg['banned_ips'])} IP bị khóa khi khởi động lại dịch vụ.")
        _cfg["banned_ips"] = []
        save_security_config(_cfg)
except Exception:
    pass

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
    if cand_user != admin_user.lower() and cand_user != "admin":
        return False

    if "hashed_password" in cfg and "salt" in cfg:
        calc_hash, _ = hash_secret(cand_pass, cfg["salt"])
        return hmac.compare_digest(calc_hash, cfg["hashed_password"])

    # If ADMIN_PASSWORD is explicitly set in environment
    if os.environ.get("ADMIN_PASSWORD"):
        return hmac.compare_digest(cand_pass, os.environ.get("ADMIN_PASSWORD").strip())

    # Initial default: accept BOTH "admin" AND "1234"
    return hmac.compare_digest(cand_pass, "admin") or hmac.compare_digest(cand_pass, "1234")

def update_credentials(new_user, new_pass):
    hashed, salt = hash_secret(str(new_pass).strip())
    cfg = load_security_config()
    cfg["admin_user"] = str(new_user).strip()
    cfg["hashed_password"] = hashed
    cfg["salt"] = salt
    cfg["updated_at"] = time.time()
    save_security_config(cfg)

# IP Blacklist & Anti-Brute-Force: Exactly 3 failed attempts -> Permanent Lockout
MAX_FAILED_ATTEMPTS = 3
FAILED_LOGIN_ATTEMPTS = {}  # { ip: count }

def get_client_ip(req):
    forwarded = req.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return req.remote_addr or "127.0.0.1"

def is_ip_banned(ip):
    cfg = load_security_config()
    banned = cfg.get("banned_ips", [])
    return ip in banned

def ban_ip(ip, reason="Nhập sai thông tin đăng nhập quá 3 lần"):
    cfg = load_security_config()
    banned = set(cfg.get("banned_ips", []))
    banned.add(ip)
    cfg["banned_ips"] = list(banned)
    save_security_config(cfg)
    print(f"[SECURITY] ĐÃ KHÓA VĨNH VIỄN IP: {ip} ({reason})")

def record_failed_attempt(ip):
    count = FAILED_LOGIN_ATTEMPTS.get(ip, 0) + 1
    FAILED_LOGIN_ATTEMPTS[ip] = count
    if count >= MAX_FAILED_ATTEMPTS:
        ban_ip(ip)
        return True, 0
    return False, MAX_FAILED_ATTEMPTS - count

def reset_failed_attempts(ip):
    FAILED_LOGIN_ATTEMPTS.pop(ip, None)

@app.before_request
def security_gate():
    # Always allow health check
    if request.path == '/health':
        return None

    ip = get_client_ip(request)
    if is_ip_banned(ip):
        if request.path.startswith('/api/'):
            return jsonify({
                "error": "Truy cập bị từ chối: Địa chỉ IP của bạn đã bị khóa vĩnh viễn do nhập sai quá 3 lần.",
                "banned": True
            }), 403
        return render_template('blocked.html', ip=ip), 403

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

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
    if session.get("authenticated") is True:
        return True
    pin = req.headers.get("X-PIN") or req.headers.get("X-Auth-Token")
    if not pin:
        pin = req.args.get("pin")
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
    if is_ip_banned(ip):
        return jsonify({
            "error": "Truy cập bị từ chối: Địa chỉ IP đã bị khóa vĩnh viễn do nhập sai quá 3 lần.",
            "banned": True
        }), 403

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
        is_banned, remaining = record_failed_attempt(ip)
        time.sleep(1.0)
        if is_banned:
            return jsonify({
                "error": "Bạn đã nhập sai thông tin 3 lần. Địa chỉ IP của bạn đã bị KHÓA TRUY CẬP VĨNH VIỄN!",
                "banned": True
            }), 403
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

    if len(new_pass) < 4:
        return jsonify({"error": "Mật khẩu mới phải có ít nhất 4 ký tự!"}), 400

    update_credentials(new_user, new_pass)
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
    is_default = ("hashed_password" not in cfg) and (os.environ.get("ADMIN_PASSWORD") is None)
    banned_count = len(cfg.get("banned_ips", []))
    return jsonify({
        "authenticated": True,
        "admin_user": admin_user,
        "is_default": is_default,
        "banned_count": banned_count
    })

@app.route('/api/auth/unban', methods=['POST'])
def auth_unban():
    if not session.get("authenticated") and not check_pin(request):
        return jsonify({"error": "Unauthorized"}), 401
    cfg = load_security_config()
    cfg["banned_ips"] = []
    save_security_config(cfg)
    return jsonify({"status": "ok", "message": "Đã gỡ lệnh cấm toàn bộ IP!"})

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
            wid = focus_game_window()

            if action == "key":
                raw_val = str(data.get("value", ""))
                key_to_send = KEY_MAP.get(raw_val.lower(), raw_val)
                # If single character, type it directly with xdotool type to preserve casing & symbols
                if len(key_to_send) == 1:
                    cmd = ["xdotool", "type", "--delay", "20", "--", key_to_send]
                    if wid:
                        cmd = ["xdotool", "type", "--window", wid, "--delay", "20", "--", key_to_send]
                    subprocess.run(cmd, timeout=2, check=False)
                else:
                    if key_to_send not in ALLOWED_KEYS and key_to_send not in KEY_MAP.values():
                        return jsonify({"error": f"Invalid key: {raw_val}"}), 400
                    cmd = ["xdotool", "key", key_to_send]
                    if wid:
                        cmd = ["xdotool", "key", "--window", wid, key_to_send]
                    subprocess.run(cmd, timeout=2, check=False)

            elif action == "click":
                try:
                    raw_x = float(data.get("x", 0))
                    raw_y = float(data.get("y", 0))
                    x = max(0, min(SCREEN_WIDTH, int(round(raw_x))))
                    y = max(0, min(SCREEN_HEIGHT, int(round(raw_y)))) + MENU_BAR_OFFSET
                    cmd = ["xdotool", "mousemove", str(x), str(y), "click", "1"]
                    safe_run(cmd, timeout=2)
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid coordinates"}), 400

            elif action == "text":
                value = str(data.get("value", ""))
                if value:
                    cmd = ["xdotool", "type", "--delay", "20", "--", value]
                    if wid:
                        cmd = ["xdotool", "type", "--window", wid, "--delay", "20", "--", value]
                    safe_run(cmd, timeout=8)
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
                ok_y = 245 + MENU_BAR_OFFSET
                safe_run(["xdotool", "mousemove", "120", str(ok_y), "click", "1"], timeout=2)
                safe_run(["xdotool", "key", "Return"], timeout=2)
                safe_run(["xdotool", "key", "space"], timeout=2)

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
            wid = focus_game_window()
            for c in cmds:
                # 1. Open chat in NRO (Press Return)
                cmd = ["xdotool", "key", "Return"]
                if wid:
                    cmd = ["xdotool", "key", "--window", wid, "Return"]
                subprocess.run(cmd, timeout=2, check=False)
                time.sleep(0.12)
                # 2. Type command
                cmd = ["xdotool", "type", "--delay", "25", "--", c]
                if wid:
                    cmd = ["xdotool", "type", "--window", wid, "--delay", "25", "--", c]
                subprocess.run(cmd, timeout=3, check=False)
                time.sleep(0.12)
                # 3. Submit command (Press Return)
                cmd = ["xdotool", "key", "Return"]
                if wid:
                    cmd = ["xdotool", "key", "--window", wid, "Return"]
                subprocess.run(cmd, timeout=2, check=False)
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
        wid = get_game_window()

        def do_key(k):
            cmd = ["xdotool", "key"]
            if wid:
                cmd += ["--window", wid]
            cmd.append(k)
            safe_run(cmd, timeout=2)

        def do_type(t):
            cmd = ["xdotool", "type"]
            if wid:
                cmd += ["--window", wid]
            cmd += ["--delay", "20", "--", t]
            safe_run(cmd, timeout=8)

        def do_click(x, y):
            cmd = ["xdotool", "mousemove", str(x), str(y), "click", "1"]
            safe_run(cmd, timeout=2)

        def do_clear():
            cmd = ["xdotool", "key"]
            if wid:
                cmd += ["--window", wid]
            cmd += ["--repeat", "45", "--delay", "8", "BackSpace"]
            safe_run(cmd, timeout=3)

        # 1. Từ menu chính nếu được chọn: Mở hộp thoại Đổi tài khoản
        if from_menu:
            # Click trực tiếp nút 'Đổi tài khoản' tại (120, 126 + MENU_BAR_OFFSET)
            btn_y = 126 + MENU_BAR_OFFSET
            do_click(120, btn_y)
            time.sleep(0.8)

        # 2. Xóa ô tài khoản và gõ tài khoản
        do_clear()
        time.sleep(0.1)
        do_type(username)
        time.sleep(0.15)

        # Xác nhận tài khoản: Gửi F2 (softkey OK) và Return
        do_key("F2")
        time.sleep(0.1)
        do_key("Return")
        # Chờ hộp thoại 'Mật khẩu' mở ra
        time.sleep(0.8)

        if password:
            # 3. Xóa ô mật khẩu và gõ mật khẩu
            do_clear()
            time.sleep(0.1)
            do_type(password)
            time.sleep(0.15)

            # Xác nhận mật khẩu: Gửi F2 và Return
            do_key("F2")
            time.sleep(0.1)
            do_key("Return")
            # Chờ màn hình trở lại menu đăng nhập
            time.sleep(0.8)

        if auto_enter:
            # 4. Bấm Đăng nhập / Chơi tiếp vào game
            play_y = 88 + MENU_BAR_OFFSET
            do_click(120, play_y)
            time.sleep(0.1)
            do_key("Return")

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
