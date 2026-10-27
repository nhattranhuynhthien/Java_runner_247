import os
import io
import subprocess
import threading
from flask import Flask, render_template, request, Response, jsonify
from PIL import Image, ImageGrab

os.environ["DISPLAY"] = ":1"
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")

app = Flask(__name__)
x11_lock = threading.Lock()

ALLOWED_KEYS = {
    'Up', 'Down', 'Left', 'Right', 'Return', 'F1', 'F2', 'BackSpace', 'space'
} | {chr(c) for c in range(ord('a'), ord('z') + 1)} | {chr(c) for c in range(ord('0'), ord('9') + 1)}

def check_pin(req):
    pin = req.args.get("pin")
    if not pin and req.is_json:
        pin = req.json.get("pin")
    if not pin:
        pin = req.form.get("pin")
    return str(pin) == str(ADMIN_PIN)

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
        with x11_lock:
            # Capture the X11 virtual display :1 at 240x360
            im = ImageGrab.grab(bbox=(0, 0, 240, 360))
            if im is None:
                im = Image.new('RGB', (240, 360), color=(10, 15, 25))
            else:
                im = im.convert('RGB')

        buf = io.BytesIO()
        im.save(buf, format='JPEG', quality=60)
        jpeg_bytes = buf.getvalue()

        response = Response(jpeg_bytes, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response

    except Exception as e:
        # Fallback in case Xvfb is momentarily resetting
        buf = io.BytesIO()
        blank = Image.new('RGB', (240, 360), color=(15, 20, 30))
        blank.save(buf, format='JPEG', quality=50)
        response = Response(buf.getvalue(), mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response

@app.route('/api/input', methods=['POST'])
def handle_input():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    data = request.get_json(silent=True) or {}
    action = data.get("action")

    try:
        with x11_lock:
            if action == "key":
                value = str(data.get("value", ""))
                if value not in ALLOWED_KEYS:
                    return jsonify({"error": f"Invalid key: {value}"}), 400
                subprocess.run(["xdotool", "key", value], timeout=2, check=False)

            elif action == "click":
                try:
                    raw_x = float(data.get("x", 0))
                    raw_y = float(data.get("y", 0))
                    x = max(0, min(240, int(round(raw_x))))
                    y = max(0, min(360, int(round(raw_y))))
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
                        ["xdotool", "type", "--delay", "40", value],
                        timeout=5,
                        check=False
                    )
                if data.get("enter") is True:
                    subprocess.run(["xdotool", "key", "Return"], timeout=2, check=False)

            else:
                return jsonify({"error": "Invalid action"}), 400

        return jsonify({"status": "ok"})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Action timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/upload_rom', methods=['POST'])
def upload_rom():
    if not check_pin(request):
        return jsonify({"error": "Unauthorized. Invalid PIN."}), 403

    if 'file' not in request.files:
        return jsonify({"error": "No file field in request"}), 400

    f = request.files['file']
    if not f.filename.lower().endswith('.jar'):
        return jsonify({"error": "Chỉ chấp nhận file định dạng .jar!"}), 400

    os.makedirs("/app/roms", exist_ok=True)
    dest_path = "/app/roms/NRO.jar"
    f.save(dest_path)

    # Restart emulator process via supervisor
    subprocess.run(["supervisorctl", "restart", "emulator"], check=False)

    return jsonify({"status": "ok", "message": "Đã nạp file .jar thành công và khởi động lại game!"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
