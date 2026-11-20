import os
import io
import math
import time
import socket
import shutil
import base64
import collections
import http.client
import subprocess
import threading
import json
import hmac
import hashlib
import secrets
import struct
import tempfile
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from flask import Flask, render_template, request, Response, jsonify, session, redirect, url_for
from PIL import Image, ImageGrab

os.environ.setdefault("DISPLAY", ":1")
SERVER_START_TIME = time.time()
ROM_FILE = os.path.abspath(os.environ.get("ROM_FILE", "/data/roms/NRO.jar"))

# Y offset for cropping out the MicroEmulator window menu bar
MENU_BAR_OFFSET = 24
SCREEN_WIDTH = 240
SCREEN_HEIGHT = 360

def _resolve_persistent_path(env_key, filename):
    custom = (os.environ.get(env_key) or "").strip()
    if custom:
        return custom
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        return os.path.join("/data", filename)
    return os.path.abspath(filename)

app = Flask(__name__)
app.secret_key = (os.environ.get("SESSION_SECRET") or "").strip() or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=14)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get("SESSION_COOKIE_SECURE", "0").lower() in ("1", "true", "yes")
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

x11_lock = threading.RLock()
restart_lock = threading.Lock()
login_attempts_lock = threading.Lock()
security_config_lock = threading.Lock()
notify_config_lock = threading.Lock()
profiles_lock = threading.RLock()

# Screenshot cache — tránh chụp lại mỗi request khi nhiều tab mở cùng lúc
_screenshot_cache: dict = {}  # keyed by fmt: {"jpeg": {"ts": 0.0, "data": bytes}}
_screenshot_cache_lock = threading.Lock()
_screenshot_capture_lock = threading.Lock()
_screenshot_cache_gen = 0

def invalidate_screenshot_cache():
    global _screenshot_cache_gen
    with _screenshot_cache_lock:
        _screenshot_cache.clear()
        _screenshot_cache_gen += 1

def get_json_dict(req=None):
    target = req if req is not None else request
    data = target.get_json(silent=True)
    return data if isinstance(data, dict) else {}

def _atomic_write_json(filepath, payload, mode=0o600, prefix="cfg-", ensure_ascii=True):
    temp_path = None
    try:
        target_dir = os.path.dirname(filepath) or "."
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, prefix=prefix, suffix=".tmp", delete=False
        ) as f:
            temp_path = f.name
            json.dump(payload, f, ensure_ascii=ensure_ascii, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if os.name == "posix" and mode is not None:
            try:
                os.chmod(temp_path, mode)
            except OSError:
                pass
        os.replace(temp_path, filepath)
        temp_path = None
        return True
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass

def _atomic_write_bytes(filepath, raw_bytes, mode=None, prefix="bin-"):
    temp_path = None
    try:
        target_dir = os.path.dirname(filepath) or "."
        os.makedirs(target_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target_dir, prefix=prefix, suffix=".tmp", delete=False
        ) as f:
            temp_path = f.name
            f.write(raw_bytes)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if os.name == "posix" and mode is not None:
            try:
                os.chmod(temp_path, mode)
            except OSError:
                pass
        os.replace(temp_path, filepath)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass

def safe_run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] Lệnh timed out (được bỏ qua an toàn): {cmd}")
        return None
    except Exception as e:
        print(f"[ERROR] Lỗi thực thi lệnh {cmd}: {e}")
        return None

def _x11_grab_frame():
    """Chỉ chụp vùng màn hình Xvfb trên POSIX (hoặc khi bật FORCE_IMAGEGRAB=1 cho kiểm thử), tránh chụp nhầm màn hình Desktop Windows."""
    if os.name == "posix" or os.environ.get("FORCE_IMAGEGRAB") == "1":
        with x11_lock:
            return ImageGrab.grab(bbox=(0, MENU_BAR_OFFSET, SCREEN_WIDTH, MENU_BAR_OFFSET + SCREEN_HEIGHT))
    return None

def _is_emulator_process_running():
    if os.name != "posix":
        return False
    for pattern in ("microemulator", "start_emu.sh"):
        try:
            p = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=2)
            if p.returncode == 0:
                return True
        except Exception:
            pass
    return False

def is_emulator_running():
    st = get_bridge_status()
    if st and st.get("bridge_online"):
        return True
    return _is_emulator_process_running()

def restart_emulator(clean_cache=False):
    with restart_lock:
        if clean_cache:
            cleanup_corrupted_rms_cache()
        if os.name == "posix":
            sup_cmds = [
                ["supervisorctl", "-c", "/etc/supervisor/conf.d/supervisord.conf", "restart", "emulator"],
                ["supervisorctl", "restart", "emulator"],
                ["supervisorctl", "-c", "/etc/supervisor/conf.d/supervisord.conf", "start", "emulator"],
                ["supervisorctl", "start", "emulator"],
            ]
            for cmd in sup_cmds:
                res = safe_run(cmd, timeout=12)
                if res is not None and res.returncode == 0:
                    invalidate_screenshot_cache()
                    return True
            # Fallback if supervisorctl socket is unavailable: restart start_emu.sh directly
            safe_run(["pkill", "-f", "microemulator.jar"], timeout=3)
            for _ in range(10):
                if not _is_emulator_process_running():
                    break
                time.sleep(0.2)
            script_path = "/app/scripts/start_emu.sh"
            if not os.path.exists(script_path):
                script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts", "start_emu.sh"))
            if os.path.exists(script_path) and os.path.exists("/bin/bash"):
                emu_home = os.environ.get("MICROEMULATOR_HOME", "/data/.microemulator")
                user_home = os.path.dirname(emu_home) or "/data"
                subprocess.Popen(
                    ["/bin/bash", script_path],
                    env=dict(
                        os.environ,
                        DISPLAY=os.environ.get("DISPLAY", ":1"),
                        HOME=user_home,
                        MICROEMULATOR_HOME=emu_home,
                    ),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                invalidate_screenshot_cache()
                return True
        raise RuntimeError("Không thể khởi động lại tiến trình giả lập game.")

def run_input_command(cmd, timeout=3):
    """Run a dashboard input command and fail visibly if X11 rejects it."""
    result = safe_run(cmd, timeout=timeout)
    if result is None or result.returncode != 0:
        raise RuntimeError("Không gửi được lệnh tới màn hình game. Hãy thử khởi động lại game.")
    return result

# Persistent administrator configuration
SECURITY_CONFIG_FILE = _resolve_persistent_path("SECURITY_CONFIG_FILE", "security_config.json")

def load_security_config():
    if os.path.exists(SECURITY_CONFIG_FILE):
        try:
            with open(SECURITY_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}

def save_security_config(cfg):
    try:
        return _atomic_write_json(SECURITY_CONFIG_FILE, cfg, mode=0o600, prefix="security-")
    except Exception as e:
        print(f"[SECURITY] Lỗi lưu cấu hình: {e}")
        return False

def hash_secret(secret_str, salt=None):
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", str(secret_str).encode("utf-8"), salt.encode("utf-8"), 100000).hex()
    return hashed, salt

def get_admin_credentials():
    cfg = load_security_config()
    user = str(cfg.get("admin_user", os.environ.get("ADMIN_USER", "admin"))).strip() or "admin"
    return user, cfg

def _current_auth_version():
    """Return a short opaque digest of the active credential state for session invalidation."""
    admin_user, cfg = get_admin_credentials()
    if "hashed_password" in cfg and "salt" in cfg:
        material = f"file:{admin_user.lower()}:{cfg['hashed_password']}:{cfg['salt']}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    configured_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if configured_password:
        material = f"env:{admin_user.lower()}:{configured_password}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return ""

def is_session_authenticated():
    if session.get("authenticated") is not True:
        return False
    cur_ver = _current_auth_version()
    if not cur_ver:
        return False
    sess_ver = str(session.get("auth_ver", ""))
    if not sess_ver:
        return False
    return hmac.compare_digest(sess_ver, cur_ver)

def get_csrf_token():
    tok = session.get("csrf_token")
    if not isinstance(tok, str) or len(tok) < 32:
        tok = secrets.token_hex(32)
        session["csrf_token"] = tok
    return tok

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

    # No valid credentials configured — deny access to prevent unauthorized login
    return False

def update_credentials(new_user, new_pass):
    with security_config_lock:
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
MAX_TRACKED_LOGIN_IPS = 2048
FAILED_LOGIN_ATTEMPTS = {}  # { ip: [timestamps] }

def get_client_ip(req):
    return req.remote_addr or "127.0.0.1"

def _prune_failed_attempts_locked(now):
    expired_ips = []
    for tracked_ip, stamps in FAILED_LOGIN_ATTEMPTS.items():
        recent = [t for t in stamps if now - t < LOGIN_WINDOW_SECONDS]
        if recent:
            FAILED_LOGIN_ATTEMPTS[tracked_ip] = recent
        else:
            expired_ips.append(tracked_ip)
    for tracked_ip in expired_ips:
        FAILED_LOGIN_ATTEMPTS.pop(tracked_ip, None)
    if len(FAILED_LOGIN_ATTEMPTS) > MAX_TRACKED_LOGIN_IPS:
        overflow = len(FAILED_LOGIN_ATTEMPTS) - MAX_TRACKED_LOGIN_IPS
        oldest_ips = sorted(FAILED_LOGIN_ATTEMPTS.keys(), key=lambda k: FAILED_LOGIN_ATTEMPTS[k][-1])[:overflow]
        for old_ip in oldest_ips:
            FAILED_LOGIN_ATTEMPTS.pop(old_ip, None)

def is_ip_rate_limited(ip):
    with login_attempts_lock:
        now = time.time()
        _prune_failed_attempts_locked(now)
        return len(FAILED_LOGIN_ATTEMPTS.get(ip, [])) >= MAX_FAILED_ATTEMPTS

def record_failed_attempt(ip):
    with login_attempts_lock:
        now = time.time()
        _prune_failed_attempts_locked(now)
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

def _normalize_host(host_str, scheme=None):
    h = (host_str or "").strip().lower()
    if not h:
        return ""
    if scheme == "https" and h.endswith(":443"):
        return h[:-4]
    if scheme == "http" and h.endswith(":80"):
        return h[:-3]
    if scheme is None:
        if h.endswith(":443"):
            return h[:-4]
        if h.endswith(":80"):
            return h[:-3]
    return h

@app.before_request
def csrf_protect():
    """Chống tấn công CSRF cross-origin đối với các request thay đổi trạng thái dùng cookie session."""
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    sess_auth = is_session_authenticated()
    if (request.headers.get("X-PIN") or request.headers.get("X-Auth-Token")) and not sess_auth:
        return None

    # Block modern cross-site browser requests via Fetch Metadata header
    sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sec_fetch_site and sec_fetch_site not in ("same-origin", "none"):
        return jsonify({"error": "Cross-origin request blocked (CSRF protection)."}), 403

    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")

    if origin or referer:
        allowed_hosts = set()
        raw_host = (request.host or "").strip().lower()
        if raw_host:
            allowed_hosts.add(raw_host)
            allowed_hosts.add(_normalize_host(raw_host))
        fwd_host = request.headers.get("X-Forwarded-Host", "")
        if fwd_host:
            for h in fwd_host.split(","):
                hs = h.strip().lower()
                if hs:
                    allowed_hosts.add(hs)
                    allowed_hosts.add(_normalize_host(hs))

        if origin:
            if origin.strip().lower() == "null":
                return jsonify({"error": "Cross-origin request blocked (CSRF protection)."}), 403
            parsed_origin = urllib.parse.urlparse(origin)
            origin_host = _normalize_host(parsed_origin.netloc, parsed_origin.scheme.lower())
            if not origin_host or origin_host not in allowed_hosts:
                return jsonify({"error": "Cross-origin request blocked (CSRF protection)."}), 403
        elif referer:
            parsed_ref = urllib.parse.urlparse(referer)
            ref_host = _normalize_host(parsed_ref.netloc, parsed_ref.scheme.lower())
            if not ref_host or ref_host not in allowed_hosts:
                return jsonify({"error": "Cross-origin request blocked (CSRF protection)."}), 403

    # Verify per-session X-CSRF-Token when sent on authenticated mutating requests
    if sess_auth:
        provided_csrf = request.headers.get("X-CSRF-Token")
        if provided_csrf is None and request.mimetype == "application/x-www-form-urlencoded":
            provided_csrf = request.form.get("csrf_token")
        if provided_csrf is not None:
            expected_csrf = str(session.get("csrf_token") or "")
            if not expected_csrf or not hmac.compare_digest(str(provided_csrf), expected_csrf):
                return jsonify({"error": "Invalid CSRF token."}), 403

    return None

@app.after_request
def add_security_headers(response):
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.path.startswith("/api/"):
        invalidate_screenshot_cache()
    if request.path.startswith("/api/") and "Cache-Control" not in response.headers:
        response.headers['Cache-Control'] = 'no-store'
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
    if is_session_authenticated():
        return True
    ip = get_client_ip(req)
    header_pin = req.headers.get("X-PIN") or req.headers.get("X-Auth-Token")
    if header_pin:
        if is_ip_rate_limited(ip):
            return False
        user, _ = get_admin_credentials()
        if verify_credentials(user, header_pin):
            reset_failed_attempts(ip)
            return True
        record_failed_attempt(ip)
        return False

    pin = None
    if req.is_json:
        data = req.get_json(silent=True)
        if isinstance(data, dict):
            pin = data.get("pin") or data.get("password")
    if not pin:
        pin = req.form.get("pin") or req.form.get("password")
    if not pin:
        return False
    if is_ip_rate_limited(ip):
        return False
    user, _ = get_admin_credentials()
    return verify_credentials(user, pin)

def get_game_window():
    for search_cmd in [
        ["xdotool", "search", "--onlyvisible", "--name", "MicroEmulator"],
        ["xdotool", "search", "--onlyvisible", "--class", "org-microemu-app-Main"],
        ["xdotool", "search", "--onlyvisible", "--class", "java"],
    ]:
        try:
            p = subprocess.run(search_cmd, capture_output=True, text=True, timeout=1)
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

def _read_cgroup_ram_info(host_total_mb=0):
    """Read container memory usage/limit from cgroup v2 or v1 so Docker/Render 512MB RAM is reported accurately."""
    def _read_int_file(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if not raw or raw == "max":
                return None
            return int(raw)
        except Exception:
            return None

    def _read_stat_key(path, key):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == key:
                        return int(parts[1])
        except Exception:
            pass
        return 0

    # cgroup v2
    cg2_max = _read_int_file("/sys/fs/cgroup/memory.max")
    cg2_cur = _read_int_file("/sys/fs/cgroup/memory.current")
    if cg2_max is not None and cg2_cur is not None and 0 < cg2_max < (1 << 50):
        total_mb = max(1, cg2_max // (1024 * 1024))
        if not host_total_mb or total_mb <= host_total_mb:
            inactive_file = _read_stat_key("/sys/fs/cgroup/memory.stat", "inactive_file")
            used_bytes = max(0, cg2_cur - inactive_file)
            used_mb = used_bytes // (1024 * 1024)
            pct = round(min(100.0, (used_mb / total_mb * 100.0)), 1)
            return {"used_mb": used_mb, "total_mb": total_mb, "percent": pct}

    # cgroup v1
    cg1_max = _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    cg1_cur = _read_int_file("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if cg1_max is not None and cg1_cur is not None and 0 < cg1_max < (1 << 50):
        total_mb = max(1, cg1_max // (1024 * 1024))
        if not host_total_mb or total_mb <= host_total_mb:
            inactive_file = _read_stat_key("/sys/fs/cgroup/memory/memory.stat", "total_inactive_file")
            used_bytes = max(0, cg1_cur - inactive_file)
            used_mb = used_bytes // (1024 * 1024)
            pct = round(min(100.0, (used_mb / total_mb * 100.0)), 1)
            return {"used_mb": used_mb, "total_mb": total_mb, "percent": pct}

    return None

def get_ram_info():
    try:
        mem_total = 0
        mem_available = 0
        if os.path.exists('/proc/meminfo'):
            with open('/proc/meminfo', 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        mem_total = int(line.split()[1]) // 1024
                    elif line.startswith('MemAvailable:'):
                        mem_available = int(line.split()[1]) // 1024
        cg_info = _read_cgroup_ram_info(host_total_mb=mem_total)
        if cg_info is not None:
            return cg_info
        if mem_total > 0:
            mem_used = max(0, mem_total - mem_available)
            pct = round((mem_used / mem_total * 100), 1)
            return {"used_mb": mem_used, "total_mb": mem_total, "percent": pct}
    except Exception:
        pass
    return {"used_mb": 0, "total_mb": 512, "percent": 0}

_nro_socket_cache = {"ts": 0.0, "online": None}
_nro_socket_cache_lock = threading.Lock()
NRO_SOCKET_CACHE_TTL = 15.0

def check_nro_socket():
    host = (os.environ.get("NRO_SERVER_HOST") or "").strip() or "112.213.94.23"
    raw_port = (os.environ.get("NRO_SERVER_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else 14445
    except ValueError:
        port = 14445
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(1.0)
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass

def _get_cached_nro_socket_status():
    now = time.time()
    with _nro_socket_cache_lock:
        if _nro_socket_cache["online"] is not None and (now - _nro_socket_cache["ts"]) < NRO_SOCKET_CACHE_TTL:
            return _nro_socket_cache["online"]
    online = check_nro_socket()
    with _nro_socket_cache_lock:
        _nro_socket_cache["ts"] = time.time()
        _nro_socket_cache["online"] = online
    return online

@app.route('/health', methods=['GET'])
def health():
    return "OK", 200

@app.route('/health/detailed', methods=['GET'])
def health_detailed():
    """Health check nâng cao: trả trạng thái chi tiết emulator, bridge, Xvfb."""
    bridge_st = get_bridge_status()
    bridge_online = bool(bridge_st and bridge_st.get("bridge_online"))
    emu_running = bridge_online or _is_emulator_process_running()
    display_num = (os.environ.get("DISPLAY", ":1").lstrip(":").split(".")[0] or "1")
    xvfb_ok = os.path.exists(f"/tmp/.X11-unix/X{display_num}")
    uptime_sec = int(time.time() - SERVER_START_TIME)
    return jsonify({
        "web": "ok",
        "emulator": "running" if emu_running else "stopped",
        "bridge": "online" if bridge_online else "offline",
        "xvfb": "ok" if xvfb_ok else "error",
        "uptime_sec": uptime_sec,
        "ram": get_ram_info()
    })

@app.route('/api/ping', methods=['GET'])
def api_ping():
    """Endpoint siêu nhẹ để dashboard kiểm tra kết nối mà không tốn tài nguyên screenshot."""
    if not check_pin(request):
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"pong": True, "ts": time.time()})

@app.route('/', methods=['GET'])
def index():
    token = get_csrf_token()
    if is_session_authenticated():
        return render_template('index.html', csrf_token=token)
    return render_template('login.html', csrf_token=token)

@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    ip = get_client_ip(request)
    if is_ip_rate_limited(ip):
        return jsonify({
            "error": "Quá nhiều lần đăng nhập sai. Vui lòng đợi 5 phút rồi thử lại.",
            "locked": True
        }), 429

    data = get_json_dict()
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

    _, sec_cfg = get_admin_credentials()
    if "hashed_password" not in sec_cfg and not os.environ.get("ADMIN_PASSWORD", "").strip():
        return jsonify({
            "error": "Máy chủ chưa cấu hình ADMIN_PASSWORD. Hãy đặt biến môi trường ADMIN_PASSWORD trước khi đăng nhập."
        }), 503

    if verify_credentials(username, password):
        reset_failed_attempts(ip)
        session["authenticated"] = True
        session["admin_user"] = username
        session["auth_ver"] = _current_auth_version()
        session["csrf_token"] = secrets.token_hex(32)
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

def _change_credentials_impl(old_user, old_pass, new_user, new_pass):
    if not is_session_authenticated() and not check_pin(request):
        return jsonify({"error": "Unauthorized"}), 401

    old_user = str(old_user or "").strip()
    old_pass = str(old_pass or "").strip()
    new_user = str(new_user or "").strip()
    new_pass = str(new_pass or "").strip()

    if not old_user:
        curr_u, _ = get_admin_credentials()
        old_user = curr_u

    if not verify_credentials(old_user, old_pass):
        return jsonify({"error": "Tài khoản hoặc mật khẩu hiện tại không đúng!"}), 400

    if not new_user:
        return jsonify({"error": "Tài khoản mới không được để trống!"}), 400

    if len(new_pass) < 4:
        return jsonify({"error": "Mật khẩu mới phải có ít nhất 4 ký tự!"}), 400

    try:
        update_credentials(new_user, new_pass)
    except OSError:
        return jsonify({"error": "Không thể lưu thông tin đăng nhập. Hãy kiểm tra quyền ghi và volume /data."}), 500
    session["authenticated"] = True
    session["admin_user"] = new_user
    session["auth_ver"] = _current_auth_version()
    csrf_tok = get_csrf_token()
    session.permanent = True
    return jsonify({
        "status": "ok",
        "message": "Đã đổi tài khoản và mật khẩu thành công!",
        "csrf_token": csrf_tok
    })

@app.route('/api/auth/change_credentials', methods=['POST'])
def auth_change_credentials():
    data = get_json_dict()
    return _change_credentials_impl(
        data.get("old_user", ""),
        data.get("old_pass", ""),
        data.get("new_user", ""),
        data.get("new_pass", ""),
    )

# Backward compatibility alias
@app.route('/api/auth/change_pin', methods=['POST'])
def auth_change_pin():
    data = get_json_dict()
    curr_u, _ = get_admin_credentials()
    return _change_credentials_impl(
        curr_u,
        data.get("old_pin", ""),
        curr_u,
        data.get("new_pin", ""),
    )

@app.route('/api/auth/status', methods=['GET'])
def auth_status():
    if not is_session_authenticated():
        return jsonify({"authenticated": False})

    admin_user, cfg = get_admin_credentials()
    is_default = ("hashed_password" not in cfg) and len(os.environ.get("ADMIN_PASSWORD", "").strip()) < 12
    return jsonify({
        "authenticated": True,
        "admin_user": admin_user,
        "is_default": is_default,
        "csrf_token": get_csrf_token()
    })

def capture_game_screenshot_bytes(quality=55, fmt='jpeg'):
    """Capture 240x360 game screenshot bytes (JVM Bridge framebuffer + X11 ImageGrab).
    fmt: 'jpeg' (default) hoặc 'webp' (nhỏ hơn ~30%, browsers hiện đại hỗ trợ).
    """
    def _encode(im, out_fmt, q):
        buf = io.BytesIO()
        if out_fmt == 'webp':
            im.save(buf, format='WEBP', quality=q, method=4)
        else:
            im.save(buf, format='JPEG', quality=q, optimize=True)
        return buf.getvalue()

    x11_rgb = None
    try:
        raw_im = _x11_grab_frame()
        if raw_im is not None:
            try:
                rgb_im = raw_im.convert('RGB')
            finally:
                if raw_im is not None:
                    try:
                        raw_im.close()
                    except Exception:
                        pass
            extrema = rgb_im.getextrema()
            x11_is_black = (extrema == ((0, 0), (0, 0), (0, 0)))
            if not x11_is_black:
                try:
                    return _encode(rgb_im, fmt, quality)
                finally:
                    rgb_im.close()
            # Keep black frame only as last-resort fallback; skip encoding until Bridge check fails
            x11_rgb = rgb_im
    except Exception:
        if x11_rgb is not None:
            try:
                x11_rgb.close()
            except Exception:
                pass
            x11_rgb = None

    # Direct JVM Canvas Framebuffer Screenshot from GameBridgeAgent (used when X11 is black or unavailable)
    try:
        req = urllib.request.Request("http://127.0.0.1:28888/api/game/screenshot", headers={"User-Agent": "NRO-Server"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                data = resp.read()
                if data and len(data) > 200:
                    if fmt == 'webp':
                        try:
                            with Image.open(io.BytesIO(data)) as im_src:
                                im_bridge = im_src.convert('RGB')
                            try:
                                result = _encode(im_bridge, 'webp', quality)
                            finally:
                                im_bridge.close()
                            if x11_rgb is not None:
                                x11_rgb.close()
                            return result
                        except Exception:
                            pass
                    if x11_rgb is not None:
                        x11_rgb.close()
                    return data
    except Exception:
        pass

    if x11_rgb is not None:
        try:
            return _encode(x11_rgb, fmt, quality)
        except Exception:
            pass
        finally:
            try:
                x11_rgb.close()
            except Exception:
                pass

    buf = io.BytesIO()
    blank = Image.new('RGB', (SCREEN_WIDTH, SCREEN_HEIGHT), color=(15, 20, 30))
    try:
        if fmt == 'webp':
            blank.save(buf, format='WEBP', quality=50)
        else:
            blank.save(buf, format='JPEG', quality=50)
    finally:
        blank.close()
    return buf.getvalue()


def get_cached_screenshot(quality=55, max_age=0.2, fmt='jpeg'):
    """Trả screenshot từ cache nếu chưa quá max_age giây; chống thundering herd khi nhiều tab/thread gọi đồng thời."""
    with _screenshot_cache_lock:
        cached = _screenshot_cache.get(fmt)
        if cached and (time.time() - cached["ts"]) < max_age:
            return cached["data"]

    with _screenshot_capture_lock:
        with _screenshot_cache_lock:
            cached = _screenshot_cache.get(fmt)
            if cached and (time.time() - cached["ts"]) < max_age:
                return cached["data"]
            capture_gen = _screenshot_cache_gen

        data = capture_game_screenshot_bytes(quality, fmt=fmt)

        with _screenshot_cache_lock:
            if _screenshot_cache_gen == capture_gen:
                _screenshot_cache[fmt] = {"ts": time.time(), "data": data}
        return data

@app.route('/api/screenshot', methods=['GET'])
def screenshot():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    is_download = request.args.get('download') == '1'
    if is_download:
        img_bytes = capture_game_screenshot_bytes(quality=90, fmt='jpeg')
    else:
        accept = request.headers.get('Accept', '')
        fmt = 'webp' if 'image/webp' in accept else 'jpeg'
        img_bytes = get_cached_screenshot(quality=55, fmt=fmt)

    mime = 'image/webp' if (len(img_bytes) >= 12 and img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP') else 'image/jpeg'
    response = Response(img_bytes, mimetype=mime)
    if is_download:
        response.headers['Content-Disposition'] = 'attachment; filename="NRO_Capture.jpg"'
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

    game_st = get_bridge_status()
    emu_running = bool(game_st and game_st.get("bridge_online")) or _is_emulator_process_running()

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
        "server_socket_online": _get_cached_nro_socket_status(),
        "ram": get_ram_info(),
        "rom": rom_info,
        "saved_account": read_rms_saved_info(),
        "game": game_st
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
        p = None
        try:
            p = subprocess.Popen(
                tool,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=dict(os.environ, DISPLAY=os.environ.get("DISPLAY", ":1")),
            )
            p.communicate(input=value.encode('utf-8'), timeout=2)
            if p.returncode == 0:
                pasted = True
                break
        except subprocess.TimeoutExpired:
            if p is not None:
                try:
                    p.kill()
                    p.communicate(timeout=1)
                except Exception:
                    pass
        except Exception:
            pass

    if pasted:
        time.sleep(0.06)
        cmd_paste = ["xdotool", "key", "ctrl+v"]
        if wid:
            cmd_paste = ["xdotool", "key", "--window", wid, "ctrl+v"]
        run_input_command(cmd_paste, timeout=2)
        return

    # Cách 2: Gõ bằng xdotool (xử lý riêng ký tự @ bằng phím at trong X11/Xvfb)
    if "@" in value:
        parts = value.split("@")
        for i, part in enumerate(parts):
            if part:
                cmd = ["xdotool", "type", "--delay", "25", "--", part]
                if wid:
                    cmd = ["xdotool", "type", "--window", wid, "--delay", "25", "--", part]
                run_input_command(cmd, timeout=5)
                time.sleep(0.05)
            if i < len(parts) - 1:
                # Gõ phím @ chuẩn xác bằng xdotool key at
                cmd_at = ["xdotool", "key", "at"]
                if wid:
                    cmd_at = ["xdotool", "key", "--window", wid, "at"]
                run_input_command(cmd_at, timeout=2)
                time.sleep(0.05)
    else:
        cmd = ["xdotool", "type", "--delay", "20", "--", value]
        if wid:
            cmd = ["xdotool", "type", "--window", wid, "--delay", "20", "--", value]
        run_input_command(cmd, timeout=8)

@app.route('/api/input', methods=['POST'])
def handle_input():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = get_json_dict()
    action = data.get("action")

    try:
        if action == "key":
            raw_input = str(data.get("value", ""))
            raw_val = "space" if raw_input == " " else raw_input.strip()
            key_to_send = KEY_MAP.get(raw_val.lower(), raw_val)

            if not key_to_send or (key_to_send not in ALLOWED_KEYS and key_to_send not in KEY_MAP.values()):
                return jsonify({"error": f"Invalid key: {raw_val}"}), 400

            # 1. Primary: Dispatch key directly into JVM via GameBridgeAgent
            try:
                url = f"http://127.0.0.1:28888/api/game/input_key?key={urllib.parse.quote(raw_val)}"
                res = bridge_call(url, timeout=1.5, retries=1)
                if res and res.get("status") == "ok":
                    return jsonify({"status": "ok", "method": "bridge"})
            except Exception:
                pass

            # 2. Fallback: X11 xdotool
            with x11_lock:
                focus_game_window()
                run_input_command(["xdotool", "key", key_to_send], timeout=2)
            return jsonify({"status": "ok", "method": "x11"})

        elif action == "click":
            try:
                raw_x = float(data.get("x", 0))
                raw_y = float(data.get("y", 0))
                if not (math.isfinite(raw_x) and math.isfinite(raw_y)):
                    return jsonify({"error": "Invalid coordinates"}), 400
                x = max(0, min(SCREEN_WIDTH - 1, int(round(raw_x))))
                y = max(0, min(SCREEN_HEIGHT - 1, int(round(raw_y))))
            except (ValueError, TypeError, OverflowError):
                return jsonify({"error": "Invalid coordinates"}), 400

            # 1. Primary: Dispatch pointer click directly into JVM via GameBridgeAgent (exact 240x360 canvas coordinates)
            try:
                url = f"http://127.0.0.1:28888/api/game/input_click?x={x}&y={y}"
                res = bridge_call(url, timeout=1.5, retries=1)
                if res and res.get("status") == "ok":
                    return jsonify({"status": "ok", "method": "bridge"})
            except Exception:
                pass

            # 2. Fallback: X11 xdotool
            with x11_lock:
                focus_game_window()
                run_input_command(["xdotool", "mousemove", str(x), str(y + MENU_BAR_OFFSET), "click", "1"], timeout=2)
            return jsonify({"status": "ok", "method": "x11"})

        elif action == "text":
            value = str(data.get("value", ""))
            if len(value) > 1024:
                return jsonify({"error": "Text input too long (max 1024 chars)"}), 400
            field_hint = str(data.get("field", ""))
            submit = str(data.get("submit", ""))
            if not submit:
                if data.get("f2") is True:
                    submit = "f2"
                elif data.get("enter") is True:
                    submit = "enter"

            # 1. Primary: Set text directly on active TField (nro.au) inside JVM
            try:
                query = (
                    f"text={urllib.parse.quote(value)}"
                    f"&field={urllib.parse.quote(field_hint)}"
                    f"&submit={urllib.parse.quote(submit)}"
                )
                url = f"http://127.0.0.1:28888/api/game/input_text?{query}"
                res = bridge_call(url, timeout=2.0, retries=1)
                if res and res.get("status") == "ok":
                    return jsonify({"status": "ok", "method": "bridge"})
            except Exception:
                pass

            # 2. Fallback: X11 clipboard / xdotool
            with x11_lock:
                wid = focus_game_window()
                if value:
                    type_text_reliably(value, wid=wid)
                time.sleep(0.08)
                if submit == "f2":
                    run_input_command(["xdotool", "key", "F2"], timeout=2)
                elif submit == "enter":
                    run_input_command(["xdotool", "key", "Return"], timeout=2)
            return jsonify({"status": "ok", "method": "x11"})

        elif action == "clear_text":
            # 1. Primary: Clear active TField directly inside JVM
            try:
                res = bridge_call("http://127.0.0.1:28888/api/game/clear_text", timeout=1.5, retries=1)
                if res and res.get("status") == "ok":
                    return jsonify({"status": "ok", "method": "bridge"})
            except Exception:
                pass

            # 2. Fallback: X11 BackSpace repeat
            with x11_lock:
                focus_game_window()
                run_input_command(["xdotool", "key", "--repeat", "45", "--delay", "8", "BackSpace"], timeout=3)
            return jsonify({"status": "ok", "method": "x11"})

        elif action == "anti_stuck":
            # 0. Nếu tiến trình game chưa bật, tự động khởi động lại ngay
            if not is_emulator_running():
                try:
                    restart_emulator(clean_cache=False)
                    return jsonify({"status": "ok", "message": "Đã tự động khởi động lại tiến trình game!"})
                except Exception:
                    pass

            # 1. Xử lý chống kẹt bên trong JVM (auto-launch Launcher, dừng Xmap đang treo, đóng hộp thoại in-game nro.Q.bo)
            try:
                bridge_call("http://127.0.0.1:28888/api/game/anti_stuck", timeout=1.5, retries=1)
            except Exception:
                pass

            # 2. Hỗ trợ đóng hộp thoại đang mở trên X11 an toàn (không ném lỗi nếu X11 đang bận)
            with x11_lock:
                focus_game_window()
                safe_run(["xdotool", "mousemove", "80", str(274 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "mousemove", "120", str(255 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "mousemove", "120", str(285 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                safe_run(["xdotool", "key", "F1"], timeout=1)
                safe_run(["xdotool", "key", "Escape"], timeout=1)
            return jsonify({"status": "ok"})

        else:
            return jsonify({"error": "Invalid action"}), 400

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Action timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def is_chat_box_open():
    """Kiểm tra xem hộp thoại Chat có đang mở trên màn hình game hay không."""
    try:
        raw_im = _x11_grab_frame()
        if raw_im is not None:
            try:
                im = raw_im.convert('RGB')
            finally:
                raw_im.close()
            try:
                # Nút OK tại (80, 274) có màu cam đặc trưng (r > 190, 100 < g < 185, b < 100)
                r, g, b = im.getpixel((80, 274))[:3]
                if r > 190 and 100 < g < 185 and b < 100:
                    return True
            finally:
                im.close()
    except Exception:
        pass
    return False

class BridgeError(RuntimeError):
    """Structured error raised when GameBridge returns an HTTP error or invalid JSON."""
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code

def _is_retryable_bridge_error(err):
    if isinstance(err, (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, TimeoutError, socket.timeout, http.client.RemoteDisconnected)):
        return True
    if isinstance(err, urllib.error.HTTPError):
        return err.code == 503
    if isinstance(err, urllib.error.URLError):
        return True
    err_str = str(err)
    return ("Connection refused" in err_str or "timed out" in err_str or "RemoteDisconnected" in err_str)

def bridge_call(url, data=b"", timeout=3.5, retries=6):
    """Gửi yêu cầu HTTP tới GameBridgeAgent nội bộ với cơ chế tự động thử lại khi game vừa khởi động."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "NRO-Server"})
    last_err = None
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                res_bytes = resp.read()
                if resp.status == 200:
                    try:
                        parsed = json.loads(res_bytes.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as je:
                        raise BridgeError(f"Phản hồi JSON không hợp lệ từ GameBridge: {je}", status_code=502) from je
                    if not isinstance(parsed, dict):
                        raise BridgeError("Phản hồi từ GameBridge không đúng định dạng JSON object.", status_code=502)
                    return parsed
                raise BridgeError(f"HTTP {resp.status} từ GameBridge", status_code=resp.status)
        except urllib.error.HTTPError as he:
            body_bytes = b""
            try:
                body_bytes = he.read() or b""
            except Exception:
                pass
            finally:
                try:
                    he.close()
                except Exception:
                    pass
            err_msg = f"HTTP {he.code} từ GameBridge"
            if body_bytes:
                try:
                    err_json = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    if isinstance(err_json, dict) and err_json.get("error"):
                        err_msg = str(err_json["error"])
                except Exception:
                    snippet = body_bytes.decode("utf-8", errors="replace").strip()
                    if snippet:
                        err_msg = f"HTTP {he.code}: {snippet[:200]}"
            last_err = BridgeError(err_msg, status_code=he.code)
            if attempt < attempts - 1 and he.code == 503:
                time.sleep(min(0.3 * (2 ** attempt), 3.0))
            else:
                break
        except BridgeError as be:
            last_err = be
            break
        except Exception as e:
            last_err = e
            if attempt < attempts - 1 and _is_retryable_bridge_error(e):
                time.sleep(min(0.3 * (2 ** attempt), 3.0))  # exponential backoff: 0.3s, 0.6s, 1.2s, 2.4s, capped 3.0s
            else:
                break
    raise last_err

def send_via_bridge(cmd):
    """Gửi lệnh trực tiếp vào JVM qua GameBridgeAgent nội bộ nếu có."""
    try:
        url = f"http://127.0.0.1:28888/api/game/cmd?cmd={urllib.parse.quote(cmd)}"
        res = bridge_call(url, timeout=2.0, retries=2)
        return bool(res and res.get("status") == "ok")
    except Exception:
        pass
    return False

def send_mod_actions_via_bridge(cmd):
    """Execute mod commands through the JVM handler only; never type into game chat."""
    url = f"http://127.0.0.1:28888/api/game/mod_action?cmd={urllib.parse.quote(cmd)}"
    result = bridge_call(url, timeout=3.0, retries=3)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise BridgeError(
            (result.get("error") if isinstance(result, dict) else None) or "GameBridge rejected the mod action.",
            status_code=400,
        )
    return result

def configure_punch_via_bridge():
    """Ask the Hùng Hero bridge to add this character's actual punch skill to Auto."""
    result = bridge_call("http://127.0.0.1:28888/api/game/configure_punch", timeout=2.5, retries=2)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise BridgeError(
            (result.get("error") if isinstance(result, dict) else None) or "Hùng Hero không xác nhận được chiêu Đấm.",
            status_code=502,
        )
    return result

def get_bridge_status():
    """Lấy trạng thái game thời gian thực từ JVM Bridge."""
    try:
        req = urllib.request.Request("http://127.0.0.1:28888/api/game/status", headers={"User-Agent": "NRO-Server"})
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                parsed = json.loads(resp.read().decode('utf-8'))
                if isinstance(parsed, dict):
                    return parsed
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
    with x11_lock:
        chat_open = is_chat_box_open()
        if not chat_open:
            # Dragonboy chat trigger: Phím '#' (numbersign) hoặc 'Return'
            cmd_open = ["xdotool", "key", "numbersign"]
            if wid:
                cmd_open = ["xdotool", "key", "--window", wid, "numbersign"]
            safe_run(cmd_open, timeout=2)
            time.sleep(0.35)
            if not is_chat_box_open():
                cmd_open_ret = ["xdotool", "key", "Return"]
                if wid:
                    cmd_open_ret = ["xdotool", "key", "--window", wid, "Return"]
                safe_run(cmd_open_ret, timeout=1)
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

    data = get_json_dict()
    raw_cmd = str(data.get("cmd", "")).strip()
    if not raw_cmd:
        return jsonify({"error": "Command is empty"}), 400

    cmds = [c.strip() for c in raw_cmd.replace(';', ',').split(',') if c.strip()]

    # Custom commands also go through the JVM bridge; never open the chat UI as fallback.
    if send_via_bridge(raw_cmd):
        return jsonify({"status": "ok", "method": "bridge", "sent_cmd": raw_cmd, "executed": cmds})
    return jsonify({"error": "GameBridge chưa sẵn sàng. Hãy khởi động lại game rồi thử lại."}), 503

@app.route('/api/mod_action', methods=['POST'])
def handle_mod_action():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = get_json_dict()
    raw_cmd = str(data.get("cmd", "")).strip().lower()
    if not raw_cmd:
        return jsonify({"error": "Missing mod action."}), 400

    allowed = {"cd", "locdo", "anhat", "gb", "s20", "kvt", "nb", "cflag", "xdh", "annguoi", "anquai", "anitem", "alogin", "sz", "checklag", "ahs", "cs", "xmap", "ts", "tdlt", "ats"}
    cmds = [cmd.strip() for cmd in raw_cmd.replace(";", ",").split(",") if cmd.strip()]
    if not cmds:
        return jsonify({"error": "Missing mod action."}), 400

    def is_valid_skill_cmd(c):
        if not c.startswith("skill"):
            return False
        parts = c[5:].split()
        if len(parts) == 1:
            return parts[0].isdigit() and 1 <= int(parts[0]) <= 10
        if len(parts) == 2:
            return parts[0].isdigit() and parts[1].isdigit() and 1 <= int(parts[0]) <= 10 and 1 <= int(parts[1]) <= 10
        return False

    def is_valid_zone_cmd(c):
        return c.startswith("k") and c[1:].isdigit() and 0 <= int(c[1:]) <= 99

    if any(cmd not in allowed and not is_valid_zone_cmd(cmd) and not is_valid_skill_cmd(cmd) for cmd in cmds):
        return jsonify({"error": "Unsupported mod action."}), 400

    try:
        result = send_mod_actions_via_bridge(",".join(cmds))
        return jsonify({"status": "ok", "method": "game_bridge", "executed": cmds, "details": result})
    except BridgeError as e:
        if 400 <= e.status_code < 500:
            return jsonify({"error": str(e)}), e.status_code
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {e}"}), e.status_code
    except Exception as e:
        err_msg = str(e)
        if "Connection refused" in err_msg:
            err_msg = "GameBridge đang khởi động. Vui lòng bấm lại sau 2 giây..."
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {err_msg}"}), 503

@app.route('/api/auto_farm', methods=['POST'])
def handle_auto_farm():
    """
    Tự động hóa hoàn toàn chức năng Treo Map & Auto Up SKH:
    1. Tự động bay map bằng Xmap (nếu có chọn map cụ thể).
    2. Chờ nhân vật bay tới map rồi đổi khu (zone) mong muốn.
    3. Tự động kích hoạt GỘP CẢ TDLT VÀ TÀN SÁT (không lo hết đậu/hết vé, nhân vật vẫn đánh liên tục):
       Lọc đồ SKH (locdo), Auto nhặt (anhat), Giữ map (gb), Chạy x5 (s20), Giảm đồ họa (xdh), Ẩn người (annguoi).
    """
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = get_json_dict()
    map_val = str(data.get("map", "")).strip()
    zone_val = str(data.get("zone", "")).strip()
    mode = str(data.get("mode", "all") or "all").strip().lower()
    if mode not in ("all", "tdlt", "ts"):
        return jsonify({"error": "Chế độ auto không hợp lệ (chỉ hỗ trợ all, tdlt, ts)."}), 400

    punch_skill = {}
    pre_connect_failed = False
    try:
        punch_skill = configure_punch_via_bridge() or {}
    except Exception as e:
        pre_connect_failed = _is_retryable_bridge_error(e)
        print(f"[AUTO_FARM] Pre-configure punch note (will retry inside AutoFarm worker): {e}")

    try:
        query = f"map={urllib.parse.quote(map_val)}&zone={urllib.parse.quote(zone_val)}&mode={urllib.parse.quote(mode)}"
        url = f"http://127.0.0.1:28888/api/game/auto_farm?{query}"
        res_data = bridge_call(url, timeout=4.0, retries=1 if pre_connect_failed else 3)
        if not isinstance(res_data, dict) or res_data.get("status") != "ok":
            return jsonify({"error": (res_data.get("error") if isinstance(res_data, dict) else None) or "GameBridge từ chối lệnh Auto Farm."}), 502
        skill_num = punch_skill.get('skill', 1)
        traveling = bool(res_data.get('traveling'))
        target_map = res_data.get('map', map_val)
        if mode == "tdlt":
            mode_desc = "Chỉ TDLT (đã tắt Tàn Sát, tự dùng vé TĐLT + Goback + Auto Reconnect)"
        elif mode == "ts":
            mode_desc = "Chỉ Tàn Sát (đã tắt TDLT, đánh liên tục + Goback + Auto Reconnect)"
        else:
            mode_desc = "Combo TDLT + Tàn Sát 24/7 (Goback + TDLT + Tàn Sát + Auto Reconnect)"
        prefix = f"🚀 Đang bay Xmap tới Map {target_map} rồi tự động bật " if traveling else f"Đã gán chiêu đánh (skill{skill_num}) và bật "
        return jsonify({
            "status": "ok",
            "method": "bridge",
            "traveling": traveling,
            "message": f"{prefix}{mode_desc}!",
            "details": res_data
        })
    except BridgeError as e:
        if 400 <= e.status_code < 500:
            return jsonify({"error": str(e)}), e.status_code
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {e}"}), e.status_code
    except Exception as e:
        err_msg = str(e)
        if "Connection refused" in err_msg:
            err_msg = "GameBridge đang khởi động. Vui lòng bấm lại sau 2 giây..."
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {err_msg}"}), 503

@app.route('/api/xmap', methods=['POST'])
def handle_xmap():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403
    data = get_json_dict()
    map_val = str(data.get("map", "")).strip()
    if not map_val.isdigit() or not (0 <= int(map_val) <= 999):
        return jsonify({"error": "Vui lòng chọn một Map cụ thể trong danh sách để bay!"}), 400
    map_id = int(map_val)
    try:
        url = f"http://127.0.0.1:28888/api/game/xmap?map={map_id}"
        res_data = bridge_call(url, timeout=3.0, retries=2)
        if res_data.get("status") == "ok":
            return jsonify({
                "status": "ok",
                "map": map_id,
                "message": f"🚀 Đã kích hoạt Xmap bay tới Map {map_id}!"
            })
        return jsonify({"error": f"Không thể kích hoạt Xmap: {res_data.get('error', 'Lỗi')}"}), 500
    except BridgeError as e:
        if 400 <= e.status_code < 500:
            return jsonify({"error": str(e)}), e.status_code
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {e}"}), e.status_code
    except Exception as e:
        err_msg = str(e)
        if "Connection refused" in err_msg:
            err_msg = "GameBridge đang khởi động. Vui lòng bấm lại sau 2 giây..."
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {err_msg}"}), 503

@app.route('/api/stop_farm', methods=['POST'])
def handle_stop_farm():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403
    try:
        url = "http://127.0.0.1:28888/api/game/stop_farm"
        res_data = bridge_call(url, timeout=3.0, retries=2)
        if not isinstance(res_data, dict) or res_data.get("status") != "ok":
            return jsonify({"error": (res_data.get("error") if isinstance(res_data, dict) else None) or "Không thể dừng Auto Farm."}), 502
        return jsonify({
            "status": "ok",
            "message": "Đã dừng toàn bộ Auto & khôi phục hiển thị đồ họa!",
            "details": res_data
        })
    except BridgeError as e:
        if 400 <= e.status_code < 500:
            return jsonify({"error": str(e)}), e.status_code
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {e}"}), e.status_code
    except Exception as e:
        err_msg = str(e)
        if "Connection refused" in err_msg:
            err_msg = "GameBridge đang khởi động. Vui lòng bấm lại sau 2 giây..."
        return jsonify({"error": f"GameBridge chưa sẵn sàng: {err_msg}"}), 503

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
        raw_im = _x11_grab_frame()
        if raw_im is not None:
            try:
                im = raw_im.convert('RGB')
            finally:
                raw_im.close()
            try:
                # Kiểm tra pixel tại (120, 144)
                # Với layout 5 nút: (120, 144) là tâm nút 'Chơi tiếp' màu cam (r > 180, g > 110)
                # Với layout 4 nút: (120, 144) là hình nền phong cảnh tối phía trên các nút (r < 130)
                r, g, b = im.getpixel((120, 144))[:3]
                if r > 180 and g > 110:
                    return 5
            finally:
                im.close()
    except Exception as e:
        print(f"[LAYOUT] Lỗi kiểm tra bố cục menu: {e}")
    return 4

def dismiss_error_popup_if_any():
    """Tự động phát hiện và xử lý mọi màn hình trung gian: Launcher, Tải dữ liệu, Thông báo, Menu chính."""
    try:
        raw_im = _x11_grab_frame()
        if raw_im is not None:
            try:
                im = raw_im.convert('RGB')
            finally:
                raw_im.close()
            try:
                # 1. Phát hiện màn hình Launcher chờ Start (nền trắng tinh góc trên và giữa)
                p_top = im.getpixel((10, 10))[:3]
                p_mid = im.getpixel((120, 180))[:3]
                if p_top == (255, 255, 255) and p_mid == (255, 255, 255):
                    with x11_lock:
                        safe_run(["xdotool", "mousemove", "160", str(208 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                        safe_run(["xdotool", "key", "F2"], timeout=1)
                        safe_run(["xdotool", "key", "Return"], timeout=1)
                    time.sleep(0.4)
                    return True

                # 2. Phát hiện màn hình 'Vui lòng tải dữ liệu để chơi' (nút cam tại (119, 234), không có nút Chơi tiếp)
                r_dl, g_dl, b_dl = im.getpixel((119, 234))[:3]
                r_ct, g_ct, b_ct = im.getpixel((120, 144))[:3]
                if (r_dl > 180 and 100 < g_dl < 185 and b_dl < 90) and r_ct < 130:
                    with x11_lock:
                        safe_run(["xdotool", "mousemove", "119", str(234 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                        safe_run(["xdotool", "key", "Return"], timeout=1)
                    time.sleep(1.0)
                    return True

                # 3. Phát hiện popup parchment nền giấy vàng (kiểm tra các điểm lề không dính chữ)
                parchment_pts = [(50, 260), (190, 260), (50, 280), (190, 280), (120, 270), (120, 220)]
                is_parchment = any(
                    im.getpixel(pt)[0] > 190 and im.getpixel(pt)[1] > 180 and im.getpixel(pt)[2] > 140
                    for pt in parchment_pts
                )
                if is_parchment:
                    # 3a. Kiểm tra nút [ Có ] tại (80, 320) (trong hộp thoại 'Bạn sẽ mất tài khoản đang chơi...')
                    r_co, g_co, b_co = im.getpixel((80, 320))[:3]
                    if r_co > 180 and 100 < g_co < 185 and b_co < 90:
                        with x11_lock:
                            safe_run(["xdotool", "mousemove", "80", str(320 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                            safe_run(["xdotool", "key", "Return"], timeout=1)
                        time.sleep(0.4)
                        return True

                    # 3b. Kiểm tra nút [ OK ] tại (119, 318) hoặc (119, 320) (trong hộp thoại 'Xin khởi động lại game...' hoặc thông báo lỗi)
                    r_ok, g_ok, b_ok = im.getpixel((119, 318))[:3]
                    if not (r_ok > 180 and 100 < g_ok < 185 and b_ok < 90):
                        r_ok, g_ok, b_ok = im.getpixel((119, 320))[:3]
                    if r_ok > 180 and 100 < g_ok < 185 and b_ok < 90:
                        with x11_lock:
                            safe_run(["xdotool", "mousemove", "119", str(318 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                            safe_run(["xdotool", "key", "Return"], timeout=1)
                            safe_run(["xdotool", "key", "space"], timeout=1)
                        time.sleep(0.4)
                        return True

                    # 3c. Fallback nút OK ở giữa (120, 285)
                    with x11_lock:
                        safe_run(["xdotool", "mousemove", "120", str(285 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                        safe_run(["xdotool", "key", "Return"], timeout=1)
                    time.sleep(0.3)
                    return True

                # 4. Kiểm tra nút Đóng/OK màu cam tại (120, 255)
                r, g, b = im.getpixel((120, 255))[:3]
                if r > 180 and 100 < g < 185 and b < 100:
                    with x11_lock:
                        cmd = ["xdotool", "mousemove", "120", str(255 + MENU_BAR_OFFSET), "click", "1"]
                        safe_run(cmd, timeout=1)
                        safe_run(["xdotool", "key", "Return"], timeout=1)
                    time.sleep(0.3)
                    return True

                # 5. Tự động bấm 'Chơi tiếp' / 'Chơi TK' nếu đang ở Menu chính và không bị popup che
                r_ct, g_ct, b_ct = im.getpixel((120, 144))[:3]
                if r_ct > 180 and 100 < g_ct < 185 and b_ct < 90 and not is_parchment:
                    st = get_bridge_status()
                    if not st or not st.get("in_game"):
                        with x11_lock:
                            safe_run(["xdotool", "mousemove", "120", str(144 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                            safe_run(["xdotool", "key", "Return"], timeout=1)
                        time.sleep(0.5)
                        return True
            finally:
                im.close()
    except Exception as e:
        print(f"[POPUP] Lỗi kiểm tra popup: {e}")
    return False

def emulator_ui_watchdog_worker():
    """
    Lightweight 24/7 background watchdog:
    1. Tự động khởi động lại tiến trình emulator nếu bị dừng hẳn (Game chưa bật).
    2. Tự động kích hoạt MIDlet nếu MicroEmulator đang dừng ở màn hình Launcher hoặc màn hình tải dữ liệu lần đầu.
    (Không tự ý đóng hộp thoại parchment hay tự bấm menu khi người dùng đang thao tác trong game).
    """
    time.sleep(15.0)
    last_restart_ts = 0.0
    down_streak = 0
    while True:
        st = None
        try:
            now = time.time()
            if not is_emulator_running():
                down_streak += 1
                if down_streak >= 2 and (now - last_restart_ts > 30.0):
                    last_restart_ts = now
                    down_streak = 0
                    print("[WATCHDOG] Phát hiện tiến trình giả lập chưa chạy, đang tự động khởi động lại...")
                    try:
                        restart_emulator(clean_cache=False)
                    except Exception as re_err:
                        print(f"[WATCHDOG] Lỗi tự khởi động lại emulator: {re_err}")
                time.sleep(4.0)
                continue
            down_streak = 0

            st = get_bridge_status()
            if st and not st.get("midlet_loaded", True):
                try:
                    bridge_call("http://127.0.0.1:28888/api/game/auto_launch", timeout=1.5, retries=1)
                except Exception:
                    pass

            if not st or not st.get("in_game"):
                if x11_lock.acquire(blocking=False):
                    try:
                        raw_im = _x11_grab_frame()
                        if raw_im is not None:
                            try:
                                im = raw_im.convert('RGB')
                            finally:
                                raw_im.close()
                            try:
                                # Chỉ tự động qua màn hình Launcher trắng hoặc màn hình 'Vui lòng tải dữ liệu để chơi'
                                p_top = im.getpixel((10, 10))[:3]
                                p_mid = im.getpixel((120, 180))[:3]
                                if p_top == (255, 255, 255) and p_mid == (255, 255, 255):
                                    safe_run(["xdotool", "mousemove", "160", str(208 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                                    safe_run(["xdotool", "key", "F2"], timeout=1)
                                    safe_run(["xdotool", "key", "Return"], timeout=1)
                                else:
                                    r_dl, g_dl, b_dl = im.getpixel((119, 234))[:3]
                                    r_ct, g_ct, b_ct = im.getpixel((120, 144))[:3]
                                    if (r_dl > 180 and 100 < g_dl < 185 and b_dl < 90) and r_ct < 130:
                                        safe_run(["xdotool", "mousemove", "119", str(234 + MENU_BAR_OFFSET), "click", "1"], timeout=1)
                                        safe_run(["xdotool", "key", "Return"], timeout=1)
                            finally:
                                im.close()
                    finally:
                        x11_lock.release()
        except Exception:
            pass
        # Adaptive sleep: 2s nếu chưa in_game (theo dõi chặt hơn), 5s nếu đang chơi bình thường
        time.sleep(2.0 if not (st and st.get("in_game")) else 5.0)

def _should_start_background_workers():
    return not os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("DISABLE_BACKGROUND_WORKERS") != "1"

if _should_start_background_workers():
    _ui_watchdog_thread = threading.Thread(target=emulator_ui_watchdog_worker, daemon=True)
    _ui_watchdog_thread.start()

# =========================================================================
# J2ME Record Management System (RMS) - Quản lý Tài khoản & Máy chủ Tự Động
# =========================================================================

def get_all_suite_dirs():
    """
    Trả về danh sách tất cả các thư mục suite RMS của MicroEmulator để đảm bảo ghi đúng 100%.
    Bao gồm suite-Dragonboy250 v3.0, suite-null, và tất cả thư mục suite-* hiện có.
    Khi MICROEMULATOR_HOME được đặt tường minh, chỉ thao tác trong thư mục đó (tránh chạm vào $HOME của máy chủ/host).
    """
    explicit_home = (os.environ.get("MICROEMULATOR_HOME") or "").strip()
    if explicit_home:
        primary_home = explicit_home
        base_dirs = [primary_home]
    else:
        env_home = (os.environ.get("HOME") or "").strip()
        if os.name == "posix":
            primary_home = os.path.join(env_home or "/data", ".microemulator")
            base_dirs = [primary_home, "/root/.microemulator", os.path.expanduser("~/.microemulator")]
        else:
            primary_home = os.path.join(env_home or os.path.expanduser("~"), ".microemulator")
            base_dirs = [primary_home]

    seen_bases = set()
    unique_bases = []
    for base in base_dirs:
        if not base:
            continue
        norm = os.path.normpath(base)
        if norm not in seen_bases:
            seen_bases.add(norm)
            unique_bases.append(norm)

    suite_dirs = []
    for base in unique_bases:
        try:
            if os.path.exists(base):
                for name in sorted(os.listdir(base)):
                    suite_dir = os.path.join(base, name)
                    if name.startswith("suite-") and suite_dir not in suite_dirs:
                        suite_dirs.append(suite_dir)
            for name in ("suite-Dragonboy250 v3.0", "suite-null"):
                suite_dir = os.path.join(base, name)
                if suite_dir not in suite_dirs:
                    suite_dirs.append(suite_dir)
        except Exception:
            pass

    if not suite_dirs:
        suite_dirs.append(os.path.join(primary_home, "suite-Dragonboy250 v3.0"))

    for d in suite_dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    return list(suite_dirs)

def get_rms_suite_dir():
    dirs = get_all_suite_dirs()
    explicit_home = (os.environ.get("MICROEMULATOR_HOME") or "").strip()
    primary_home = explicit_home or os.path.join(os.environ.get("HOME", "/data"), ".microemulator")
    return dirs[0] if dirs else os.path.join(primary_home, "suite-Dragonboy250 v3.0")

def cleanup_corrupted_rms_cache():
    """Xóa toàn bộ cache dữ liệu bị hỏng (CRC corruption) của Dragonboy, giữ nguyên tài khoản & máy chủ đã lưu."""
    suite_dirs = get_all_suite_dirs()
    preserved = {"vjacc.rs", "vjpass.rs", "vjsvselect.rs", "vjcheck.rs"}
    cleared = 0
    for sdir in suite_dirs:
        if not os.path.exists(sdir):
            continue
        try:
            for fname in os.listdir(sdir):
                if fname not in preserved:
                    fpath = os.path.join(sdir, fname)
                    if os.path.isfile(fpath) or os.path.islink(fpath):
                        os.remove(fpath)
                        cleared += 1
                    elif os.path.isdir(fpath):
                        shutil.rmtree(fpath, ignore_errors=True)
                        cleared += 1
        except Exception as e:
            print(f"[REPAIR] Lỗi dọn dẹp cache {sdir}: {e}")
    print(f"[REPAIR] Đã dọn sạch {cleared} file cache hỏng.")
    return cleared

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
        if offset + name_len + 16 > len(data):
            return None, {}
        store_name = data[offset:offset+name_len].decode('utf-8', errors='ignore')
        offset += name_len
        version, last_mod, next_id = struct.unpack_from('>iqi', data, offset)
        offset += 16
        records = {}
        while offset + 8 <= len(data):
            rec_id, rec_len = struct.unpack_from('>ii', data, offset)
            offset += 8
            if rec_len >= 0 and offset + rec_len <= len(data):
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
    - vjsvselect.rs: máy chủ đã chọn (server_num - 1, trong đoạn 0..12)
    Ghi đồng bộ nguyên tử vào tất cả các thư mục suite RMS để đảm bảo emulator luôn nhận diện 100%.
    """
    clean_user = username.strip().lower()
    clean_pass = password.strip()

    bytes_acc = make_rs_bytes("vjacc", {1: clean_user})
    bytes_pass = make_rs_bytes("vjpass", {1: clean_pass}) if clean_pass else None
    bytes_check = make_rs_bytes("vjcheck", {1: 1})
    bytes_sv = None
    if server_num and int(server_num) > 0:
        sv_idx = max(0, min(12, int(server_num) - 1))
        bytes_sv = make_rs_bytes("vjsvselect", {1: sv_idx})

    suite_dirs = get_all_suite_dirs()
    for sdir in suite_dirs:
        try:
            os.makedirs(sdir, exist_ok=True)
            _atomic_write_bytes(os.path.join(sdir, "vjacc.rs"), bytes_acc, prefix="vjacc-")
            if bytes_pass:
                _atomic_write_bytes(os.path.join(sdir, "vjpass.rs"), bytes_pass, prefix="vjpass-")
            _atomic_write_bytes(os.path.join(sdir, "vjcheck.rs"), bytes_check, prefix="vjcheck-")
            if bytes_sv:
                _atomic_write_bytes(os.path.join(sdir, "vjsvselect.rs"), bytes_sv, prefix="vjsv-")
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

_auto_enter_lock = threading.Lock()
_auto_enter_cancel_event = None

def _spawn_auto_enter_worker():
    global _auto_enter_cancel_event
    with _auto_enter_lock:
        if _auto_enter_cancel_event is not None:
            _auto_enter_cancel_event.set()
        cancel_ev = threading.Event()
        _auto_enter_cancel_event = cancel_ev
    t = threading.Thread(target=auto_enter_game_worker, args=(cancel_ev,), daemon=True)
    t.start()
    return t

def auto_enter_game_worker(cancel_event=None):
    """
    Background worker:
    Sau khi nạp RMS và khởi động lại game:
    Theo dõi màn hình và phát hiện ngay khi Menu chính 5 nút (Đã có tài khoản) xuất hiện,
    tự động click nút 1 'Chơi TK' để vào game ngay lập tức!
    """
    print("[AUTO_LOGIN] Bắt đầu theo dõi màn hình để tự động vào game...")
    if cancel_event is not None:
        if cancel_event.wait(1.2):
            return
    else:
        time.sleep(1.2)
    start_t = time.time()
    entered = False

    while time.time() - start_t < 16:
        if cancel_event is not None and cancel_event.is_set():
            return
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
        if cancel_event is not None:
            if cancel_event.wait(0.3):
                return
        else:
            time.sleep(0.3)

    if not entered:
        print("[AUTO_LOGIN] Đã hết thời gian chờ menu (16s).")

@app.route('/api/open_account_dialog', methods=['POST'])
def handle_open_account_dialog():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        # 1. Ưu tiên mở trực tiếp màn hình LoginScr (nro.cJ) trong JVM và focus ô tài khoản
        try:
            res = bridge_call("http://127.0.0.1:28888/api/game/open_account_dialog", timeout=2.0, retries=2)
            if res and res.get("status") == "ok":
                return jsonify({"status": "ok", "method": "bridge", "message": "Đã mở màn hình Đổi tài khoản!"})
        except Exception:
            pass

        # 2. Fallback: Click qua X11
        with x11_lock:
            focus_game_window()
            layout = get_menu_layout()
            y_doi_tk = 202 if layout == 5 else 187
            cmd = ["xdotool", "mousemove", "120", str(y_doi_tk + MENU_BAR_OFFSET), "click", "1"]
            safe_run(cmd, timeout=2)
            time.sleep(0.5)

        return jsonify({"status": "ok", "method": "x11", "message": "Đã mở hộp thoại Đổi tài khoản!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def login_via_bridge(username="", password="", server_num=0):
    """Đăng nhập / đổi tài khoản hoặc đổi máy chủ trực tiếp trong JVM không cần khởi động lại Emulator."""
    try:
        query = (
            f"user={urllib.parse.quote(str(username or ''))}"
            f"&pass={urllib.parse.quote(str(password or ''))}"
            f"&server={int(server_num or 0)}"
        )
        url = f"http://127.0.0.1:28888/api/game/login?{query}"
        req = urllib.request.Request(url, data=b"", headers={"User-Agent": "NRO-Server"})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if resp.status == 200:
                res = json.loads(resp.read().decode("utf-8"))
                return isinstance(res, dict) and res.get("status") == "ok"
    except Exception as e:
        print(f"[DIRECT_LOGIN] Bridge login fallback note: {e}")
    return False

@app.route('/api/select_server', methods=['POST'])
def handle_select_server():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = get_json_dict()
    try:
        server_num = int(data.get("server", 0))
    except (ValueError, TypeError):
        server_num = 0

    if server_num < 1 or server_num > 13:
        return jsonify({"error": "Vui lòng chọn máy chủ hợp lệ (1 - 13)!"}), 400

    try:
        # 1. Ghi trực tiếp máy chủ vào tất cả thư mục RMS (vjsvselect.rs)
        sv_idx = server_num - 1
        sv_bytes = make_rs_bytes("vjsvselect", {1: sv_idx})
        for sdir in get_all_suite_dirs():
            try:
                sv_path = os.path.join(sdir, "vjsvselect.rs")
                _atomic_write_bytes(sv_path, sv_bytes, prefix="vjsv-")
            except Exception:
                pass

        # 2. Ưu tiên đổi máy chủ & kết nối lại trực tiếp trong JVM (0s downtime, không dùng xdotool tránh xung đột)
        if login_via_bridge("", "", server_num):
            return jsonify({
                "status": "ok",
                "method": "direct_jvm",
                "message": f"Đã chuyển trực tiếp sang Máy chủ: Vũ trụ {server_num} (không cần khởi động lại game)!"
            })

        # 3. Fallback: Khởi động lại game nếu JVM Bridge chưa sẵn sàng
        restart_emulator()
        _spawn_auto_enter_worker()

        return jsonify({"status": "ok", "method": "restart", "message": f"Đã chuyển sang Máy chủ: Vũ trụ {server_num} thành công!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/quick_login', methods=['POST'])
def handle_quick_login():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = get_json_dict()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    try:
        server_num = int(data.get("server", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Máy chủ không hợp lệ (chỉ chấp nhận 0 - 13)!"}), 400
    if server_num < 0 or server_num > 13:
        return jsonify({"error": "Máy chủ không hợp lệ (chỉ chấp nhận 0 - 13)!"}), 400
    auto_enter = bool(data.get("auto_enter", True))
    force_restart = bool(data.get("force_restart", False))

    if not username:
        return jsonify({"error": "Vui lòng nhập tài khoản (hoặc SĐT/Email)"}), 400

    try:
        # 1. Ghi trực tiếp thông tin vào RMS trên đĩa để lưu bền vững
        write_rms_account(username, password, server_num)

        # 2. Ưu tiên đăng nhập trực tiếp trong bộ nhớ JVM (không cần restart emulator, tránh xdotool click nhầm nút Hủy)
        if not force_restart and login_via_bridge(username, password, server_num):
            return jsonify({
                "status": "ok",
                "method": "direct_jvm",
                "message": "Đã nạp tài khoản & đăng nhập trực tiếp trong JVM! Đang kết nối vào thế giới..."
            })

        # 3. Fallback: Khởi động lại game để áp dụng tài khoản & máy chủ mới
        restart_emulator()

        if auto_enter:
            _spawn_auto_enter_worker()

        return jsonify({
            "status": "ok",
            "method": "restart",
            "message": "Đã lưu tài khoản & máy chủ vào hệ thống game! Đang tự động kết nối vào thế giới..."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================================================================
# HẠNG MỤC 4: Hệ Thống Thông Báo Telegram / Discord (Báo Đồ SKH, Đầy Rương)
# =========================================================================

NOTIFY_CONFIG_FILE = _resolve_persistent_path("NOTIFY_CONFIG_FILE", "notify_config.json")
PROFILES_CONFIG_FILE = _resolve_persistent_path("PROFILES_CONFIG_FILE", "account_profiles.json")
notify_lock = threading.Lock()
NOTIFY_HISTORY = []  # Lưu tối đa 20 thông báo gần nhất

DEFAULT_NOTIFY_CONFIG = {
    "enabled": True,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "discord_webhook_url": "",
    "notify_skh": True,
    "notify_full_bag": True,
    "notify_disconnect": False
}

ALLOWED_DISCORD_WEBHOOK_HOSTS = {
    "discord.com",
    "discordapp.com",
    "ptb.discord.com",
    "canary.discord.com",
}

def _validate_discord_webhook_url(webhook_url):
    url = str(webhook_url or "").strip()
    if not url:
        return True, ""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_DISCORD_WEBHOOK_HOSTS or not parsed.path.startswith("/api/webhooks/"):
        return False, "Discord Webhook URL không hợp lệ (chỉ chấp nhận https://discord.com/api/webhooks/...)."
    return True, ""

def _validate_telegram_token(bot_token):
    token = str(bot_token or "").strip()
    if not token:
        return True, ""
    if any(ch in token for ch in ("/", "?", "#", "\\", "\r", "\n")) or any(ch.isspace() for ch in token):
        return False, "Telegram Bot Token chứa ký tự không hợp lệ."
    return True, ""

def load_notify_config():
    cfg = dict(DEFAULT_NOTIFY_CONFIG)
    if os.path.exists(NOTIFY_CONFIG_FILE):
        try:
            with open(NOTIFY_CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception as e:
            print(f"[NOTIFY] Lỗi đọc cấu hình: {e}")
    return cfg

def save_notify_config(cfg):
    with notify_config_lock:
        try:
            return _atomic_write_json(NOTIFY_CONFIG_FILE, cfg, mode=0o600, prefix="notify-", ensure_ascii=False)
        except Exception as e:
            print(f"[NOTIFY] Lỗi lưu cấu hình: {e}")
            return False

def _send_telegram(bot_token, chat_id, text, image_bytes=None):
    token = str(bot_token or "").strip()
    cid = str(chat_id or "").strip()
    if not token or not cid:
        return False, "Chưa cấu hình Telegram Bot Token hoặc Chat ID"
    ok_tok, err_tok = _validate_telegram_token(token)
    if not ok_tok:
        return False, err_tok

    try:
        if image_bytes:
            boundary = "----NROCloudBoundary" + secrets.token_hex(8)
            body = bytearray()
            for k, v in [("chat_id", cid), ("caption", text)]:
                body += f"--{boundary}\r\n".encode("utf-8")
                body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode("utf-8")
                body += f"{v}\r\n".encode("utf-8")
            body += f"--{boundary}\r\n".encode("utf-8")
            body += b'Content-Disposition: form-data; name="photo"; filename="nro_skh.jpg"\r\n'
            body += b"Content-Type: image/jpeg\r\n\r\n"
            body += image_bytes + b"\r\n"
            body += f"--{boundary}--\r\n".encode("utf-8")

            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            req = urllib.request.Request(
                url,
                data=bytes(body),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "NRO-Cloud-Bot"
                }
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                return resp.status == 200, "Đã gửi ảnh + tin nhắn Telegram"
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = urllib.parse.urlencode({"chat_id": cid, "text": text}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"User-Agent": "NRO-Cloud-Bot"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                return resp.status == 200, "Đã gửi tin nhắn Telegram"
    except Exception as e:
        return False, f"Lỗi Telegram: {e}"

def _send_discord(webhook_url, text, image_bytes=None):
    url = str(webhook_url or "").strip()
    if not url:
        return False, "Chưa cấu hình Discord Webhook URL"
    ok_url, err_url = _validate_discord_webhook_url(url)
    if not ok_url:
        return False, err_url

    try:
        if image_bytes:
            boundary = "----NRODiscordBoundary" + secrets.token_hex(8)
            payload_json = json.dumps({"content": text, "username": "NRO Cloud 24/7"}).encode("utf-8")
            body = bytearray()
            body += f"--{boundary}\r\n".encode("utf-8")
            body += b'Content-Disposition: form-data; name="payload_json"\r\n'
            body += b"Content-Type: application/json\r\n\r\n"
            body += payload_json + b"\r\n"
            body += f"--{boundary}\r\n".encode("utf-8")
            body += b'Content-Disposition: form-data; name="file"; filename="nro_skh.jpg"\r\n'
            body += b"Content-Type: image/jpeg\r\n\r\n"
            body += image_bytes + b"\r\n"
            body += f"--{boundary}--\r\n".encode("utf-8")

            req = urllib.request.Request(
                url,
                data=bytes(body),
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "NRO-Cloud-Bot"
                }
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                return resp.status in (200, 204), "Đã gửi ảnh + tin nhắn Discord"
        else:
            payload = json.dumps({"content": text, "username": "NRO Cloud 24/7"}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "NRO-Cloud-Bot"}
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                return resp.status in (200, 204), "Đã gửi tin nhắn Discord"
    except Exception as e:
        return False, f"Lỗi Discord: {e}"

def dispatch_notification(text, include_screenshot=True, cfg_override=None):
    cfg = cfg_override or load_notify_config()
    img_bytes = capture_game_screenshot_bytes(quality=65) if include_screenshot else None
    results = []

    if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
        ok, msg = _send_telegram(cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id"), text, img_bytes)
        results.append({"channel": "telegram", "ok": ok, "detail": msg})

    if cfg.get("discord_webhook_url"):
        ok, msg = _send_discord(cfg.get("discord_webhook_url"), text, img_bytes)
        results.append({"channel": "discord", "ok": ok, "detail": msg})

    with notify_lock:
        NOTIFY_HISTORY.insert(0, {
            "time": time.strftime("%H:%M:%S %d/%m"),
            "text": text,
            "results": results
        })
        del NOTIFY_HISTORY[20:]

    return results

def notification_monitor_worker():
    """Luồng chạy ngầm 24/7 theo dõi đồ SKH mới rớt vào hành trang, đầy rương & mất kết nối."""
    last_char = None
    known_skh = set()
    last_full_bag_alert_ts = 0.0
    last_disconnect_alert_ts = 0.0
    offline_since = None

    while True:
        try:
            time.sleep(10.0)
            cfg = load_notify_config()
            if not cfg.get("enabled", True):
                continue

            has_channel = bool(
                (cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"))
                or cfg.get("discord_webhook_url")
            )
            if not has_channel:
                continue

            st = get_bridge_status()
            if not st or not st.get("bridge_online"):
                continue

            in_game = bool(st.get("in_game"))
            char_name = str(st.get("char_name") or "").strip()

            if not in_game or not char_name:
                if st.get("autofarm_active"):
                    if offline_since is None:
                        offline_since = time.time()
                    elif (
                        cfg.get("notify_disconnect")
                        and time.time() - offline_since > 180
                        and time.time() - last_disconnect_alert_ts > 1800
                    ):
                        last_disconnect_alert_ts = time.time()
                        dispatch_notification(
                            "⚠️ [NRO CLOUD 24/7] Nhân vật đang ở ngoài màn hình chờ hơn 3 phút khi đang bật Auto! Hệ thống Watchdog đang thử kết nối lại.",
                            include_screenshot=True,
                            cfg_override=cfg
                        )
                continue

            offline_since = None
            raw_skh = [str(x) for x in (st.get("skh_items") or [])]
            current_skh = set(raw_skh)

            # Nếu mới vào game hoặc vừa đổi nhân vật, đồng bộ danh sách SKH sẵn có để không báo nhầm đồ cũ
            if char_name != last_char:
                last_char = char_name
                known_skh = set(current_skh)
                continue

            # 1. Phát hiện đồ SKH mới nhặt được
            new_items = [item for item in raw_skh if item not in known_skh]
            if new_items:
                known_skh.update(current_skh)
                if cfg.get("notify_skh", True):
                    map_name = st.get("map_name") or f"Map {st.get('map_id', '?')}"
                    zone = st.get("zone", "?")
                    items_str = "\n".join(f"✨ {it}" for it in new_items)
                    msg = (
                        f"🎉 [NRO CLOUD - NHẶT ĐƯỢC ĐỒ SKH MỚI!]\n"
                        f"👤 Nhân vật: {char_name}\n"
                        f"📍 Vị trí: {map_name} (Khu {zone})\n"
                        f"🎁 Vật phẩm SKH mới:\n{items_str}\n"
                        f"🎒 Hành trang trống: {st.get('bag_free', '?')}/{st.get('bag_total', '?')} ô"
                    )
                    dispatch_notification(msg, include_screenshot=True, cfg_override=cfg)
            else:
                known_skh = set(current_skh)

            # 2. Phát hiện đầy hành trang (cooldown 30 phút)
            bag_total = int(st.get("bag_total") or 0)
            bag_free = int(st.get("bag_free") if st.get("bag_free") is not None else 99)
            if (
                cfg.get("notify_full_bag", True)
                and bag_total > 0
                and bag_free == 0
                and time.time() - last_full_bag_alert_ts > 1800
            ):
                last_full_bag_alert_ts = time.time()
                map_name = st.get("map_name") or f"Map {st.get('map_id', '?')}"
                msg = (
                    f"🎒 [NRO CLOUD - ĐẦY HÀNH TRANG!]\n"
                    f"👤 Nhân vật: {char_name}\n"
                    f"📍 Vị trí: {map_name} (Khu {st.get('zone', '?')})\n"
                    f"⚠️ Hành trang đã đầy ({st.get('bag_used')}/{bag_total} ô). Hãy kiểm tra hoặc bật Lọc Đồ (locdo)!"
                )
                dispatch_notification(msg, include_screenshot=True, cfg_override=cfg)

        except Exception as e:
            print(f"[NOTIFY_MONITOR] Error: {e}")

if _should_start_background_workers():
    _notify_thread = threading.Thread(target=notification_monitor_worker, daemon=True)
    _notify_thread.start()

@app.route('/api/notify_config', methods=['GET', 'POST'])
def handle_notify_config():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    if request.method == 'GET':
        cfg = load_notify_config()
        with notify_lock:
            history_snapshot = list(NOTIFY_HISTORY[:10])
        return jsonify({
            "status": "ok",
            "config": cfg,
            "history": history_snapshot
        })

    data = get_json_dict()
    tg_token = str(data.get("telegram_bot_token", "")).strip()
    tg_chat = str(data.get("telegram_chat_id", "")).strip()
    discord_url = str(data.get("discord_webhook_url", "")).strip()

    ok_tok, err_tok = _validate_telegram_token(tg_token)
    if not ok_tok:
        return jsonify({"error": err_tok}), 400
    ok_url, err_url = _validate_discord_webhook_url(discord_url)
    if not ok_url:
        return jsonify({"error": err_url}), 400

    cfg = load_notify_config()
    cfg["enabled"] = bool(data.get("enabled", True))
    cfg["telegram_bot_token"] = tg_token
    cfg["telegram_chat_id"] = tg_chat
    cfg["discord_webhook_url"] = discord_url
    cfg["notify_skh"] = bool(data.get("notify_skh", True))
    cfg["notify_full_bag"] = bool(data.get("notify_full_bag", True))
    cfg["notify_disconnect"] = bool(data.get("notify_disconnect", False))

    if save_notify_config(cfg):
        return jsonify({"status": "ok", "message": "Đã lưu cấu hình thông báo Telegram / Discord!", "config": cfg})
    return jsonify({"error": "Không thể lưu cấu hình thông báo"}), 500

@app.route('/api/notify_test', methods=['POST'])
def handle_notify_test():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = get_json_dict()
    cfg = load_notify_config()
    for k in ("telegram_bot_token", "telegram_chat_id", "discord_webhook_url"):
        if k in data:
            cfg[k] = str(data.get(k) or "").strip()

    ok_tok, err_tok = _validate_telegram_token(cfg.get("telegram_bot_token"))
    if not ok_tok:
        return jsonify({"error": err_tok}), 400
    ok_url, err_url = _validate_discord_webhook_url(cfg.get("discord_webhook_url"))
    if not ok_url:
        return jsonify({"error": err_url}), 400

    if not ((cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")) or cfg.get("discord_webhook_url")):
        return jsonify({"error": "Vui lòng nhập Telegram (Bot Token + Chat ID) hoặc Discord Webhook URL trước khi gửi thử!"}), 400

    st = get_bridge_status() or {}
    if st.get("in_game"):
        try:
            power_val = int(st.get("power") or 0)
        except (ValueError, TypeError):
            power_val = 0
        msg = (
            f"✅ [NRO CLOUD 24/7 - TEST THÔNG BÁO]\n"
            f"👤 Nhân vật: {st.get('char_name')}\n"
            f"📍 Vị trí: {st.get('map_name', 'Unknown')} [{st.get('map_id')}] - Khu {st.get('zone')}\n"
            f"💪 Sức mạnh: {power_val:,}\n"
            f"✨ Số món SKH hiện có: {st.get('skh_count', 0)} món"
        )
    else:
        msg = "✅ [NRO CLOUD 24/7 - TEST THÔNG BÁO]\nKết nối thông báo Telegram / Discord hoạt động tốt! Khi nhân vật nhặt được đồ SKH mới, hệ thống sẽ chụp ảnh màn hình và báo về đây ngay lập tức."

    results = dispatch_notification(msg, include_screenshot=True, cfg_override=cfg)
    any_ok = any(r.get("ok") for r in results)
    if any_ok:
        return jsonify({"status": "ok", "message": "Đã gửi tin nhắn & ảnh chụp màn hình thử nghiệm thành công!", "results": results})
    details = "; ".join(r.get("detail", "") for r in results)
    return jsonify({"error": f"Gửi thông báo thất bại: {details}", "results": results}), 400

# =========================================================================
# HẠNG MỤC 5: Quản Lý Danh Sách Tài Khoản (Multi-Account Profiles Manager)
# =========================================================================

def _get_profile_cipher_key():
    with profiles_lock:
        key_dir = os.path.dirname(PROFILES_CONFIG_FILE) or "."
        key_path = os.path.join(key_dir, ".profile_key")
        if os.path.exists(key_path):
            try:
                with open(key_path, "rb") as f:
                    raw = f.read()
                    if len(raw) >= 32:
                        return raw[:32]
            except Exception:
                pass
        new_key = secrets.token_bytes(32)
        try:
            os.makedirs(key_dir, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            try:
                fd = os.open(key_path, flags, 0o600)
                try:
                    os.write(fd, new_key)
                finally:
                    os.close(fd)
                if os.name == "posix":
                    try:
                        os.chmod(key_path, 0o600)
                    except OSError:
                        pass
                return new_key
            except FileExistsError:
                with open(key_path, "rb") as f:
                    raw = f.read()
                    if len(raw) >= 32:
                        return raw[:32]
                _atomic_write_bytes(key_path, new_key, mode=0o600, prefix="pkey-")
                return new_key
        except Exception:
            return hashlib.sha256(str(app.secret_key).encode("utf-8")).digest()

def _encrypt_profile_secret(plaintext):
    if not plaintext:
        return ""
    s = str(plaintext)
    if s.startswith("enc:v1:"):
        return s
    key = _get_profile_cipher_key()
    pt_bytes = s.encode("utf-8")
    nonce = secrets.token_bytes(16)
    stream = hashlib.pbkdf2_hmac("sha256", key, nonce, 10000, dklen=len(pt_bytes))
    ct_bytes = bytes(a ^ b for a, b in zip(pt_bytes, stream))
    mac = hmac.new(key, nonce + ct_bytes, hashlib.sha256).digest()[:16]
    token = base64.urlsafe_b64encode(nonce + mac + ct_bytes).decode("ascii")
    return f"enc:v1:{token}"

def _decrypt_profile_secret(stored):
    if not stored:
        return ""
    s = str(stored)
    if not s.startswith("enc:v1:"):
        return s
    try:
        raw = base64.urlsafe_b64decode(s[7:].encode("ascii"))
        if len(raw) < 32:
            return ""
        nonce, mac, ct_bytes = raw[:16], raw[16:32], raw[32:]
        key = _get_profile_cipher_key()
        expected_mac = hmac.new(key, nonce + ct_bytes, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expected_mac) or not ct_bytes:
            return ""
        stream = hashlib.pbkdf2_hmac("sha256", key, nonce, 10000, dklen=len(ct_bytes))
        return bytes(a ^ b for a, b in zip(ct_bytes, stream)).decode("utf-8")
    except Exception:
        return ""

def load_account_profiles():
    if os.path.exists(PROFILES_CONFIG_FILE):
        try:
            with open(PROFILES_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    result = []
                    for item in data:
                        if isinstance(item, dict):
                            p = dict(item)
                            if "password" in p:
                                p["password"] = _decrypt_profile_secret(p.get("password", ""))
                            result.append(p)
                    return result
        except Exception as e:
            print(f"[PROFILES] Lỗi đọc danh sách tài khoản: {e}")
    return []

def save_account_profiles(profiles):
    try:
        disk_profiles = []
        for item in profiles:
            if isinstance(item, dict):
                p = dict(item)
                if p.get("password"):
                    p["password"] = _encrypt_profile_secret(p["password"])
                disk_profiles.append(p)
        return _atomic_write_json(PROFILES_CONFIG_FILE, disk_profiles, mode=0o600, prefix="profiles-", ensure_ascii=False)
    except Exception as e:
        print(f"[PROFILES] Lỗi lưu danh sách tài khoản: {e}")
        return False

@app.route('/api/profiles', methods=['GET', 'POST', 'DELETE'])
def handle_profiles():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    if request.method == 'GET':
        return jsonify({"status": "ok", "profiles": load_account_profiles()})

    data = get_json_dict()

    if request.method == 'DELETE':
        prof_id = str(data.get("id") or request.args.get("id") or "").strip()
        if not prof_id:
            return jsonify({"error": "Thiếu ID tài khoản cần xóa"}), 400
        with profiles_lock:
            profiles = load_account_profiles()
            new_profiles = [p for p in profiles if str(p.get("id")) != prof_id]
            if not save_account_profiles(new_profiles):
                return jsonify({"error": "Không thể lưu danh sách tài khoản sau khi xóa"}), 500
        return jsonify({"status": "ok", "message": "Đã xóa tài khoản khỏi danh sách lưu!", "profiles": new_profiles})

    # POST: Thêm mới hoặc cập nhật tài khoản
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    try:
        server = int(data.get("server", 7))
    except (ValueError, TypeError):
        server = 7
    if server < 0 or server > 13:
        server = 7
    label = str(data.get("label", "")).strip()
    fav_map = str(data.get("map", "0")).strip()
    fav_zone = str(data.get("zone", "5")).strip()
    prof_id = str(data.get("id", "")).strip()

    if not username:
        return jsonify({"error": "Vui lòng nhập tài khoản trước khi lưu!"}), 400

    if not label:
        label = f"{username} (VT{server if server > 0 else 7})"

    def _safe_server_int(val):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0

    with profiles_lock:
        profiles = load_account_profiles()
        updated = False
        for p in profiles:
            if (prof_id and str(p.get("id")) == prof_id) or (
                str(p.get("username", "")).lower() == username.lower() and _safe_server_int(p.get("server", 0)) == server
            ):
                p["label"] = label
                p["username"] = username
                if password:
                    p["password"] = password
                p["server"] = server
                p["map"] = fav_map
                p["zone"] = fav_zone
                updated = True
                break

        if not updated:
            profiles.append({
                "id": secrets.token_hex(6),
                "label": label,
                "username": username,
                "password": password,
                "server": server,
                "map": fav_map,
                "zone": fav_zone
            })

        saved_ok = save_account_profiles(profiles)

    if saved_ok:
        return jsonify({
            "status": "ok",
            "message": f"Đã lưu cấu hình tài khoản [{label}]!",
            "profiles": profiles
        })
    return jsonify({"error": "Không thể lưu danh sách tài khoản"}), 500

@app.route('/api/restart', methods=['POST'])
def restart_game():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    try:
        data = get_json_dict()
        clean_cache = bool(data.get("clean_cache", False))
        restart_emulator(clean_cache=clean_cache)
        msg = "Đã dọn cache và khởi động lại game thành công!" if clean_cache else "Đã khởi động lại game thành công!"
        return jsonify({"status": "ok", "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    log_lines = []
    if os.name == "posix":
        for sup_cmd in (
            ["supervisorctl", "-c", "/etc/supervisor/conf.d/supervisord.conf", "status"],
            ["supervisorctl", "status"],
        ):
            try:
                p = subprocess.run(sup_cmd, capture_output=True, text=True, timeout=2)
                if p.stdout and p.stdout.strip():
                    log_lines.append("=== SUPERVISOR STATUS ===\n" + p.stdout.strip() + "\n\n")
                    break
            except Exception:
                pass

    log_candidates = ['/var/log/emulator.log', '/var/log/emulator_err.log', '/var/log/supervisord.log']
    extra_log = os.environ.get("EMULATOR_LOG_FILE", "").strip()
    if extra_log and extra_log not in log_candidates:
        log_candidates.insert(0, extra_log)

    for log_path in log_candidates:
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    tail = list(collections.deque(f, maxlen=35))
                    if tail:
                        log_lines.append(f"=== {log_path} ===\n")
                        log_lines.extend(tail)
                        log_lines.append("\n")
            except Exception:
                pass

    st = get_bridge_status()
    if st and st.get("bridge_online"):
        log_lines.append(f"=== GAME BRIDGE LIVE ===\n{json.dumps(st, ensure_ascii=False, indent=2)}\n")

    if not log_lines:
        log_lines = ["[LOG] Chưa có bản ghi log nào được tạo."]

    return jsonify({"status": "ok", "logs": "".join(log_lines[-120:])})

@app.route('/api/upload_rom', methods=['POST'])
def upload_rom():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    if 'file' not in request.files:
        return jsonify({"error": "Vui lòng chọn file .jar để tải lên!"}), 400

    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.jar'):
        return jsonify({"error": "Chỉ chấp nhận file định dạng .jar!"}), 400

    rom_dir = os.path.dirname(ROM_FILE) or "."
    temp_path = None
    try:
        os.makedirs(rom_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="NRO-", suffix=".jar", dir=rom_dir, delete=False) as temp_file:
            temp_path = temp_file.name
            f.save(temp_file)
        if not zipfile.is_zipfile(temp_path):
            return jsonify({"error": "File tải lên không phải JAR/ZIP hợp lệ."}), 400
        if os.name == "posix":
            try:
                os.chmod(temp_path, 0o644)
            except OSError:
                pass
        os.replace(temp_path, ROM_FILE)
        temp_path = None
    except OSError as e:
        return jsonify({"error": f"Không thể lưu file JAR: {e}"}), 500
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    try:
        restart_emulator()
    except (subprocess.SubprocessError, RuntimeError, OSError):
        return jsonify({"error": "Đã lưu JAR nhưng không thể khởi động lại emulator."}), 500

    return jsonify({"status": "ok", "message": "Đã nạp file .jar thành công và đang khởi động lại game!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
