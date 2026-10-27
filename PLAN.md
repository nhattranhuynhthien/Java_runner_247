# PROJECT SPECIFICATION: NRO Cloud 24/7 Ultra-Light Web Controller

## 1. Project Overview & Architecture
Build a lightweight, Dockerized cloud execution environment to run a J2ME application (`NRO.jar`) 24/7 on a free-tier PaaS (Render, Koyeb, or Hugging Face Spaces with 512MB RAM limit, port 7860/8080).

### Strict Architectural Constraints:
- **NO noVNC / x11vnc / Desktop Environment (Fluxbox/XFCE):** Eliminate heavy remote desktop streaming protocols to ensure <0.2s web load time and near-zero idle CPU usage.
- **Headless Virtual Framebuffer (`Xvfb`):** Run strictly at `240x360x16` resolution matching native J2ME canvas size.
- **On-Demand Web Dashboard (Flask + Gunicorn):** Serve a single-page mobile-first HTML dashboard on port `8080` (configurable via `PORT` env var). Capture and compress the `240x360` X11 framebuffer into lightweight JPEG (~4KB) ONLY when requested by the client browser. When the browser tab is closed, image capture stops completely (0% overhead).
- **Direct Input Injection (`xdotool`):** Translate web button clicks, coordinate taps, and text inputs directly into X11 window events (`xdotool key`, `xdotool mousemove click`, `xdotool type`).

---

## 2. Target Directory Structure
Generate the exact project structure below:

    ├── Dockerfile
    ├── supervisord.conf
    ├── requirements.txt
    ├── config2.xml
    ├── scripts/
    │   └── start_emu.sh
    ├── app/
    │   ├── __init__.py
    │   ├── server.py
    │   └── templates/
    │       └── index.html
    ├── roms/
    │   └── .gitkeep
    └── README.md

---

## 3. Detailed Implementation Specifications

### Task 1: Python Dependencies (`requirements.txt`)
Include:
- `Flask==3.0.3`
- `gunicorn==22.0.0`
- `Pillow==10.3.0`

### Task 2: Pre-configure MicroEmulator Borderless 240x320 (`config2.xml`)
Create `config2.xml` to configure MicroEmulator so it opens at `(0,0)` with size `240x360`:

```xml
<config>
    <files>
        <recent MAXCAPACITY="50"/>
    </files>
    <system-properties>
        <system-property NAME="microedition.platform" VALUE="MicroEmulator-2.0.4"/>
        <system-property NAME="microedition.encoding" VALUE="UTF-8"/>
    </system-properties>
    <windows>
        <main ONSTART="true">
            <x>0</x>
            <y>0</y>
            <width>240</width>
            <height>360</height>
        </main>
    </windows>
    <devices>
        <device DEFAULT="true">
            <name>Resizable device</name>
            <filename>resizable.jar</filename>
            <descriptor>org/microemu/device/resizable/device.xml</descriptor>
            <rectangle>
                <x>0</x>
                <y>0</y>
                <width>240</width>
                <height>360</height>
            </rectangle>
        </device>
    </devices>
</config>
```

### Task 3: Emulator Wrapper Script (`scripts/start_emu.sh`)
Create an executable bash script `scripts/start_emu.sh`:
- Wait 2 seconds for `Xvfb :1` to initialize.
- Check if `/app/roms/NRO.jar` exists.
- If it exists, run:
  `exec java -Xmx220m -Xms128m -Dsun.java2d.opengl=false -jar /app/microemulator.jar --impl org.microemu.device.j2se.J2SEDevice /app/roms/NRO.jar`
- If `/app/roms/NRO.jar` does not exist yet, launch `microemulator.jar` without a ROM argument so the container still boots and the user can test the dashboard.

### Task 4: Backend Control Server (`app/server.py`)
Implement a clean Flask application with thread safety and input sanitization:
- Read `ADMIN_PIN` from `os.environ.get("ADMIN_PIN", "1234")`.
- Set `os.environ["DISPLAY"] = ":1"`.
- **Endpoint `GET /health`:**
  - Return `"OK", 200` immediately. Do NOT touch X11 or take screenshots here (used for UptimeRobot 5-minute keep-alive pings).
- **Endpoint `GET /`:**
  - Render `templates/index.html`.
- **Endpoint `GET /api/screenshot`:**
  - Verify `request.args.get("pin") == ADMIN_PIN`, else return `403`.
  - Use `PIL.ImageGrab.grab(bbox=(0, 0, 240, 360))` to capture X11 display `:1`.
  - Convert to RGB, save to `BytesIO` as `JPEG` with `quality=60`.
  - Return binary response with `mimetype="image/jpeg"` and header `Cache-Control: no-store, no-cache, must-revalidate, max-age=0`.
- **Endpoint `POST /api/input`:**
  - Verify `pin` from JSON body or query string.
  - Parse JSON: `{"action": "key" | "click" | "text", "value": "...", "x": 0, "y": 0}`.
  - When `action == "key"`: Validate `value` against an allowed list (`Up`, `Down`, `Left`, `Right`, `Return`, `F1`, `F2`, `BackSpace`, `space`, and single alphanumeric chars `a-z`, `0-9`). Run `subprocess.run(["xdotool", "key", value], timeout=2)`.
  - When `action == "click"`: Clamp `x` to `[0, 240]` and `y` to `[0, 360]`. Run `subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y), "click", "1"], timeout=2)`.
  - When `action == "text"`: Run `subprocess.run(["xdotool", "type", "--delay", "40", str(value)], timeout=5)`. If `enter: true` is passed in JSON, also send `xdotool key Return`.
  - Return `{"status": "ok"}`.

### Task 5: Mobile-First Web Dashboard (`app/templates/index.html`)
Build a responsive dark-themed HTML5 interface with Vanilla JS:
1. **Top Control Bar:**
   - Password input for `PIN` (persisted in `localStorage`).
   - FPS Mode Selector: `Live (1.2s)`, `Slow (3s)`, `Eco / Paused (Manual Only)`.
   - Automatic visibility handler: Pause screenshot polling automatically when `document.hidden === true` so closing/minimizing the browser uses 0% server CPU.
2. **Screen Viewport (`240x360`):**
   - `<img id="screen" width="240" height="360">` showing `/api/screenshot`.
   - Clicking/tapping on the image calculates exact `(x, y)` relative to the `240x360` box, sends `POST /api/input` (`action: "click"`), and triggers a screenshot refresh 150ms later.
3. **NRO Mod Quick Hotkeys (3x2 Grid):**
   - `Tự Đánh (A)` -> key `a`
   - `Tự Nhặt (N)` -> key `n`
   - `Menu Mod (X)` -> key `x`
   - `Auto Hồi Sinh (E)` -> key `e`
   - `Capsule (C)` -> key `c`
   - `Đổi Khu (M)` -> key `m`
4. **J2ME Keypad (3x3 Grid):**
   - `Phím Trái (F1)`, `▲ Lên (Up)`, `Phím Phải (F2)`
   - `◄ Trái (Left)`, `OK / Chọn (Return)`, `Phải ► (Right)`
   - `Giao Dịch (G)`, `▼ Xuống (Down)`, `Xóa (BackSpace)`
5. **Text & Chat Command Box:**
   - Text input field + `Gửi Chữ` button + `Gửi + Enter` button (for typing login credentials or chat commands like `td`, `ak`, `k 5`).

### Task 6: Process Supervision (`supervisord.conf`)
Configure `supervisord` (`nodaemon=true`) with 3 services:
1. `[program:xvfb]`: `/usr/bin/Xvfb :1 -screen 0 240x360x16 -ac -nolisten tcp` (`priority=10`, `autorestart=true`)
2. `[program:emulator]`: `/bin/bash /app/scripts/start_emu.sh` with `environment=DISPLAY=":1"` (`priority=20`, `autorestart=true`)
3. `[program:web]`: `gunicorn -w 1 --threads 4 -b 0.0.0.0:%(ENV_PORT)s app.server:app` with `environment=DISPLAY=":1",PORT="8080"` (`priority=30`, `autorestart=true`)

### Task 7: Production `Dockerfile`
- Base image: `ubuntu:22.04`
- Set `ENV DEBIAN_FRONTEND=noninteractive` and `ENV PORT=8080` and `ENV ADMIN_PIN=1234`.
- Install packages via `apt-get`:
  `openjdk-8-jre xvfb xdotool python3 python3-pip python3-pil curl supervisor libxext6 libxrender1 libxtst6 libxi6 libfreetype6 fontconfig`
- Clean apt cache (`rm -rf /var/lib/apt/lists/*`).
- Download MicroEmulator 2.0.4 jar directly:
  `curl -L https://repo1.maven.org/maven2/org/microemu/microemulator/2.0.4/microemulator-2.0.4.jar -o /app/microemulator.jar`
- Copy `requirements.txt` and run `pip3 install --no-cache-dir -r requirements.txt`.
- Copy `config2.xml` to `/root/.microemulator/config2.xml`.
- Copy the rest of the project into `/app`, `chmod +x /app/scripts/start_emu.sh`.
- `EXPOSE 8080`
- `CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]`

### Task 8: Documentation (`README.md`)
Write a clear Vietnamese guide explaining:
1. How to drop `NRO.jar` into `roms/NRO.jar`.
2. How to build and test locally with Docker (`docker build -t nro-cloud . && docker run -p 8080:8080 nro-cloud`).
3. How to deploy to Render.com or Hugging Face Spaces (change `PORT=7860` if using HF Spaces) and set up UptimeRobot on `/health`.

