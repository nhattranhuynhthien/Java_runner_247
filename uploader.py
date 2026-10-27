import os
import sys
import glob
import subprocess
import json
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import cgi
import shutil

PORT = 5000
WORKSPACE_DIR = "/app/workspace"
EXT_KEY_FILE = "/str/FreeJ2MEExternalKeyEvents.txt"

# Mapping human/J2ME key names to xdotool keys and external event codes
KEY_MAPPINGS = {
    'Return': {'xdo': 'Return', 'ext': 'kc'},
    'Enter': {'xdo': 'Return', 'ext': 'kc'},
    'OK': {'xdo': 'Return', 'ext': 'kc'},
    'Space': {'xdo': 'space', 'ext': 'kc'},
    'SoftLeft': {'xdo': 'q', 'ext': 'ls'},
    'SoftRight': {'xdo': 'w', 'ext': 'rs'},
    'ArrowUp': {'xdo': 'Up', 'ext': 'ku'},
    'ArrowDown': {'xdo': 'Down', 'ext': 'kd'},
    'ArrowLeft': {'xdo': 'Left', 'ext': 'kl'},
    'ArrowRight': {'xdo': 'Right', 'ext': 'kr'},
    '0': {'xdo': '0', 'ext': 'k0'},
    '1': {'xdo': '1', 'ext': 'k1'},
    '2': {'xdo': '2', 'ext': 'k2'},
    '3': {'xdo': '3', 'ext': 'k3'},
    '4': {'xdo': '4', 'ext': 'k4'},
    '5': {'xdo': '5', 'ext': 'k5'},
    '6': {'xdo': '6', 'ext': 'k6'},
    '7': {'xdo': '7', 'ext': 'k7'},
    '8': {'xdo': '8', 'ext': 'k8'},
    '9': {'xdo': '9', 'ext': 'k9'},
    '*': {'xdo': 'e', 'ext': 'k*'},
    '#': {'xdo': 'r', 'ext': 'k#'},
}

def inject_key(key_name):
    """Sends key via both xdotool and FreeJ2ME external events for 100% guarantee"""
    info = KEY_MAPPINGS.get(key_name, {'xdo': key_name, 'ext': None})
    xdo_key = info['xdo']
    ext_key = info.get('ext')

    env = os.environ.copy()
    env['DISPLAY'] = ':1'

    # 1. Inject via xdotool
    try:
        subprocess.run(["xdotool", "key", xdo_key], env=env, check=False, timeout=1)
    except Exception:
        pass

    # 2. Inject via FreeJ2ME ExternalKeyEvents file if exists
    if ext_key and os.path.exists(EXT_KEY_FILE):
        try:
            with open(EXT_KEY_FILE, 'w') as f:
                f.write(f"{ext_key}:1\n")
            with open(EXT_KEY_FILE, 'w') as f:
                f.write(f"{ext_key}:0\n")
        except Exception:
            pass

def inject_click(x, y):
    """Moves mouse and clicks at (x, y) via xdotool"""
    env = os.environ.copy()
    env['DISPLAY'] = ':1'
    try:
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], env=env, check=False, timeout=1)
    except Exception:
        pass

class UploadHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == '/api/current':
            jars = glob.glob(os.path.join(WORKSPACE_DIR, '*.jar'))
            current_name = os.path.basename(jars[0]) if jars else "Không có file nào"
            self.send_json({"current": current_name})
            return

        elif parsed.path == '/api/key':
            key = params.get('key', [''])[0]
            if key:
                inject_key(key)
                self.send_json({"success": True, "key": key})
                return
            self.send_json({"error": "No key specified"}, 400)
            return

        elif parsed.path == '/api/dismiss':
            # Dismiss dialog: click OK at center and send Enter
            inject_click(240, 420)
            inject_click(240, 520)
            inject_key('Return')
            inject_key('Space')
            self.send_json({"success": True, "action": "dismiss"})
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/api/key':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                body = json.loads(post_data.decode('utf-8'))
                key = body.get('key', '')
            except Exception:
                key = urllib.parse.parse_qs(parsed.query).get('key', [''])[0]

            if key:
                inject_key(key)
                self.send_json({"success": True, "key": key})
                return
            self.send_json({"error": "No key specified"}, 400)
            return

        elif parsed.path == '/api/click':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                body = json.loads(post_data.decode('utf-8'))
                x = int(body.get('x', 240))
                y = int(body.get('y', 320))
            except Exception:
                x, y = 240, 320

            inject_click(x, y)
            self.send_json({"success": True, "x": x, "y": y})
            return

        elif parsed.path == '/api/dismiss':
            inject_click(240, 420)
            inject_click(240, 520)
            inject_key('Return')
            inject_key('Space')
            self.send_json({"success": True, "action": "dismiss"})
            return

        elif parsed.path == '/api/upload':
            try:
                content_type = self.headers.get('Content-Type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self.send_error(400, "Content-Type must be multipart/form-data")
                    return

                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type}
                )

                if 'file' not in form:
                    self.send_error(400, "No file field in form")
                    return

                file_item = form['file']
                filename = os.path.basename(file_item.filename)
                if not filename.lower().endswith('.jar'):
                    self.send_json({"success": False, "error": "Chỉ chấp nhận file định dạng .jar!"}, 400)
                    return

                for old_jar in glob.glob(os.path.join(WORKSPACE_DIR, '*.jar')):
                    try:
                        os.remove(old_jar)
                    except Exception:
                        pass

                dest_path = os.path.join(WORKSPACE_DIR, filename)
                with open(dest_path, 'wb') as f:
                    shutil.copyfileobj(file_item.file, f)

                # Kill freej2me so watchdog loop starts new jar
                subprocess.run(["pkill", "-9", "-f", "freej2me"], check=False)

                self.send_json({
                    "success": True,
                    "filename": filename,
                    "message": f"Đã tải lên và khởi chạy {filename} thành công!"
                })
                return

            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
                return

        self.send_error(404)

if __name__ == '__main__':
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    try:
        os.makedirs('/str', exist_ok=True)
        open(EXT_KEY_FILE, 'a').close()
    except Exception:
        pass
    server = HTTPServer(('127.0.0.1', PORT), UploadHandler)
    print(f"Uploader server running on 127.0.0.1:{PORT}...")
    server.serve_forever()
