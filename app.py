import os
import sys
import time
import json
import threading
import subprocess
import psutil
from datetime import datetime
from collections import deque
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import gradio as gr

# Directories & Configuration
BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR / "workspace"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = WORKSPACE_DIR / "config.json"
LOG_FILE = WORKSPACE_DIR / "app.log"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10MB limit per SRS
MAX_LOG_LINES = 1000

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

default_config = {
    "activeFile": "Main.java",
    "jvmArgs": "-Xms256m -Xmx4G",
    "autoRestart": True,
    "autoStart": True
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**default_config, **data}
        except Exception as e:
            print(f"[CONFIG] Error reading config: {e}")
    return dict(default_config)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[CONFIG] Error saving config: {e}")

app_config = load_config()

# Process Manager State
class JavaProcessManager:
    def __init__(self):
        self.process = None
        self.state = "stopped"  # "stopped", "compiling", "running", "error"
        self.start_time = None
        self.is_manual_stop = False
        self.restart_timer = None
        self.log_ring_buffer = deque(maxlen=MAX_LOG_LINES)
        self.log_counter = 0
        self.cached_mem_mb = 0
        self.cached_cpu = 0.0

    def append_log(self, log_type: str, text: str):
        self.log_counter += 1
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "id": self.log_counter,
            "type": log_type,  # "stdout", "stderr", "system"
            "text": text,
            "timestamp": now_str
        }
        self.log_ring_buffer.append(entry)

        # Rotating file log
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
                backup = WORKSPACE_DIR / "app.log.1"
                if backup.exists():
                    backup.unlink()
                LOG_FILE.rename(backup)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{now_str}] [{log_type.upper()}] {text}\n")
        except Exception:
            pass

    def start(self, target_file: str = None) -> bool:
        if self.process and self.process.poll() is None:
            self.append_log("system", "[CẢNH BÁO] Tiến trình đang chạy. Vui lòng dừng trước khi khởi động mới.")
            return False

        filename = target_file or app_config.get("activeFile", "Main.java")
        file_path = WORKSPACE_DIR / filename
        if not file_path.exists():
            self.append_log("system", f"[LỖI] Không tìm thấy file {filename} trong thư mục workspace.")
            self.state = "error"
            return False

        self.is_manual_stop = False
        ext = file_path.suffix.lower()

        # Step 1: If .java file, compile first with javac
        if ext == ".java":
            self.state = "compiling"
            self.append_log("system", f"[BIÊN DỊCH] Đang biên dịch {filename} với javac (UTF-8)...")
            try:
                res = subprocess.run(
                    ["javac", "-encoding", "UTF-8", filename],
                    cwd=str(WORKSPACE_DIR),
                    capture_output=True,
                    text=True
                )
                if res.stdout.strip():
                    self.append_log("stdout", res.stdout.strip())
                if res.stderr.strip():
                    self.append_log("stderr", res.stderr.strip())

                if res.returncode != 0:
                    self.append_log("stderr", f"[BIÊN DỊCH THẤT BẠI] javac thoát với mã {res.returncode}")
                    self.state = "error"
                    return False
                self.append_log("system", f"[BIÊN DỊCH] Biên dịch thành công {filename}!")
            except Exception as e:
                self.append_log("stderr", f"[LỖI JAVAC] Không thể gọi lệnh javac: {e}. Đảm bảo default-jdk đã được cài đặt.")
                self.state = "error"
                return False

        # Step 2: Prepare Java command
        jvm_args = [arg.strip() for arg in app_config.get("jvmArgs", "-Xms256m -Xmx4G").split() if arg.strip()]
        if ext == ".jar":
            cmd = ["java"] + jvm_args + ["-jar", filename]
        else:
            class_name = file_path.stem
            cmd = ["java"] + jvm_args + [class_name]

        self.append_log("system", f"[KHỞI CHẠY] Lệnh: {' '.join(cmd)}")

        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE_DIR),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )
            self.state = "running"
            self.start_time = time.time()
            self.append_log("system", f"[TRẠNG THÁI] Tiến trình Java khởi động thành công (PID: {self.process.pid}).")
        except Exception as e:
            self.append_log("stderr", f"[LỖI KHỞI ĐỘNG] {e}")
            self.state = "error"
            return False

        # Launch background stream reader threads
        def read_stream(stream, log_type):
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        self.append_log(log_type, line.rstrip("\r\n"))
            except Exception:
                pass
            finally:
                stream.close()

        threading.Thread(target=read_stream, args=(self.process.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=read_stream, args=(self.process.stderr, "stderr"), daemon=True).start()

        # Launch background process watcher thread
        def watch_process():
            ret = self.process.wait()
            duration = int(time.time() - self.start_time) if self.start_time else 0
            self.append_log("system", f"[KẾT THÚC] Tiến trình Java đã thoát với mã: {ret}. Thời gian chạy: {duration}s.")
            self.process = None
            self.state = "stopped"
            self.start_time = None
            self.cached_mem_mb = 0
            self.cached_cpu = 0.0

            # Watchdog Auto-Restart check
            if not self.is_manual_stop and app_config.get("autoRestart", True):
                self.append_log("system", "[WATCHDOG] Phát hiện tiến trình kết thúc. Tự động khởi chạy lại sau 3 giây...")
                time.sleep(3)
                self.start(filename)

        threading.Thread(target=watch_process, daemon=True).start()
        return True

    def stop(self) -> bool:
        if not self.process or self.process.poll() is not None:
            self.state = "stopped"
            return True

        self.is_manual_stop = True
        self.append_log("system", "[DỪNG] Đang dừng tiến trình Java...")

        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
        except Exception:
            pass

        try:
            # Terminate parent and children
            parent = psutil.Process(self.process.pid)
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except Exception:
                    pass
            parent.terminate()
            parent.wait(timeout=3)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass

        self.process = None
        self.state = "stopped"
        self.start_time = None
        self.append_log("system", "[DỪNG] Tiến trình Java đã dừng hoàn toàn.")
        return True

    def send_stdin(self, text: str):
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("Tiến trình Java chưa khởi chạy.")
        self.process.stdin.write(text + "\n")
        self.process.stdin.flush()
        self.append_log("system", f">> [STDIN] {text}")

    def update_metrics(self):
        if self.process and self.process.poll() is None:
            try:
                p = psutil.Process(self.process.pid)
                self.cached_mem_mb = int(p.memory_info().rss / (1024 * 1024))
                self.cached_cpu = round(p.cpu_percent(interval=None), 1)
            except Exception:
                self.cached_mem_mb = 0
                self.cached_cpu = 0.0
        else:
            self.cached_mem_mb = 0
            self.cached_cpu = 0.0

    def get_uptime_str(self):
        if not self.start_time or self.state != "running":
            return "00:00:00"
        elapsed = int(time.time() - self.start_time)
        h = str(elapsed // 3600).zfill(2)
        m = str((elapsed % 3600) // 60).zfill(2)
        s = str(elapsed % 60).zfill(2)
        return f"{h}:{m}:{s}"

manager = JavaProcessManager()

# Background Metrics Updater
def metrics_loop():
    while True:
        manager.update_metrics()
        time.sleep(2)
threading.Thread(target=metrics_loop, daemon=True).start()

# ----------------- FASTAPI APP -----------------
app = FastAPI(title="Java 24/7 Cloud Runner")

# FR-03: Health-check Endpoint for UptimeRobot (Public, No Auth, HTTP 200)
@app.get("/health")
def health_check():
    elapsed = int(time.time() - manager.start_time) if manager.start_time and manager.state == "running" else 0
    return {
        "status": "alive",
        "java_process": manager.state,
        "uptime_seconds": elapsed,
        "memory_mb": manager.cached_mem_mb,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/ping")
def ping():
    return "pong"

# ----------------- GRADIO INTERFACE -----------------
def list_workspace_files():
    files = [f.name for f in WORKSPACE_DIR.iterdir() if f.is_file() and not f.name.startswith(".") and f.name not in ["app.log", "app.log.1", "config.json"]]
    return sorted(files) if files else ["Main.java"]

def get_status_markdown():
    state_colors = {
        "running": "🟢 **ĐANG CHẠY (RUNNING)**",
        "compiling": "🟡 **ĐANG BIÊN DỊCH (COMPILING)...**",
        "stopped": "⚪ **ĐÃ DỪNG (STOPPED)**",
        "error": "🔴 **LỖI THỰC THI (ERROR)**"
    }
    badge = state_colors.get(manager.state, manager.state.upper())
    pid_str = str(manager.process.pid) if manager.process and manager.process.poll() is None else "--"
    uptime_str = manager.get_uptime_str()
    ram_str = f"{manager.cached_mem_mb} MB"
    free_sys_ram = f"{round(psutil.virtual_memory().available / (1024**3), 1)} GB / {round(psutil.virtual_memory().total / (1024**3), 1)} GB"

    return f"""
### 📊 Trạng thái Hệ thống
* **Trạng thái:** {badge}
* **PID:** `{pid_str}` | **Uptime:** `{uptime_str}` | **JVM RAM:** `{ram_str}` | **RAM máy chủ:** `{free_sys_ram}`
* **Keep-Alive URL:** `https://nhattranhuynhthien-random-project.hf.space/health`
"""

def get_console_logs():
    if not manager.log_ring_buffer:
        return "[Chưa có log. Nhấn 'Biên dịch & Chạy' để khởi động]"
    lines = []
    for item in manager.log_ring_buffer:
        lines.append(f"[{item['timestamp']}] [{item['type'].upper()}] {item['text']}")
    return "\n".join(lines[-250:])  # Show latest 250 lines in Gradio textbox

def load_file_content(filename):
    if not filename:
        return "// Không có file được chọn"
    p = WORKSPACE_DIR / filename
    if not p.exists():
        return f"// Không tìm thấy file {filename}"
    if filename.endswith(".jar"):
        return f"// [File nhị phân Java .JAR: {filename} ({round(p.stat().st_size / 1024, 1)} KB)]\n// Nhấn nút 'Biên dịch & Chạy' để khởi chạy file jar này!"
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"// Lỗi đọc file: {e}"

def save_file_content(filename, content, password=""):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return "❌ Sai mật khẩu quản trị (ADMIN_PASSWORD)! Không thể lưu file."
    if not filename or filename.endswith(".jar"):
        return "❌ Không thể lưu file nhị phân hoặc tên file không hợp lệ."
    try:
        p = WORKSPACE_DIR / filename
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        manager.append_log("system", f"[LƯU FILE] Đã lưu file {filename}")
        return f"✅ Đã lưu file {filename} thành công lúc {datetime.now().strftime('%H:%M:%S')}!"
    except Exception as e:
        return f"❌ Lỗi lưu file: {e}"

def run_process_action(filename, password=""):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return "❌ Yêu cầu nhập đúng mật khẩu quản trị (ADMIN_PASSWORD)!"
    target = filename or app_config.get("activeFile", "Main.java")
    app_config["activeFile"] = target
    save_config(app_config)
    success = manager.start(target)
    if success:
        return f"🚀 Đã bắt đầu khởi chạy {target}!"
    return f"❌ Không thể khởi chạy {target}. Xem log console bên dưới."

def stop_process_action(password=""):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return "❌ Yêu cầu nhập đúng mật khẩu quản trị (ADMIN_PASSWORD)!"
    manager.stop()
    return "🛑 Đã gửi lệnh dừng tiến trình."

def restart_process_action(filename, password=""):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return "❌ Yêu cầu nhập đúng mật khẩu quản trị (ADMIN_PASSWORD)!"
    manager.stop()
    time.sleep(1)
    target = filename or app_config.get("activeFile", "Main.java")
    manager.start(target)
    return f"🔄 Đã khởi động lại tiến trình ({target})!"

def send_stdin_action(text, password=""):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return "❌ Yêu cầu nhập đúng mật khẩu quản trị!"
    if not text.strip():
        return ""
    try:
        manager.send_stdin(text.strip())
        return ""
    except Exception as e:
        return f"Lỗi: {e}"

def handle_upload(uploaded_file, password=""):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return gr.update(), "❌ Sai mật khẩu quản trị!"
    if not uploaded_file:
        return gr.update(), "Chưa chọn file nào."
    try:
        src = Path(uploaded_file.name)
        dest = WORKSPACE_DIR / src.name
        with open(dest, "wb") as f:
            f.write(src.read_bytes())
        manager.append_log("system", f"[TẢI LÊN] Đã tải lên file mới: {src.name}")
        new_files = list_workspace_files()
        return gr.update(choices=new_files, value=src.name), f"✅ Tải lên thành công: {src.name}"
    except Exception as e:
        return gr.update(), f"❌ Lỗi tải file: {e}"

# Build Gradio UI
with gr.Blocks(title="Java 24/7 Cloud Runner", theme=gr.themes.Soft(primary_hue="indigo", neutral_hue="slate")) as demo:
    gr.Markdown("# ☕ Java 24/7 Cloud Runner & Process Manager")
    gr.Markdown("Chạy các ứng dụng Java (hoặc file `.jar`) liên tục 24/24 trên nền tảng **Hugging Face Spaces** kết hợp **UptimeRobot** (Chi phí: 0 VNĐ).")

    with gr.Row():
        status_md = gr.Markdown(get_status_markdown)
        with gr.Column():
            auth_box = gr.Textbox(
                label="🔒 Mật khẩu Quản trị (ADMIN_PASSWORD)",
                placeholder="Nhập mật khẩu để mở quyền Run/Stop/Edit (để trống nếu chưa đặt secret)...",
                type="password",
                lines=1
            )
            action_status = gr.Textbox(label="Thông báo thao tác", interactive=False, lines=1)

    with gr.Row():
        btn_run = gr.Button("▶️ Biên dịch & Chạy (Compile & Run)", variant="primary", scale=2)
        btn_stop = gr.Button("⏹️ Dừng (Stop)", variant="stop", scale=1)
        btn_restart = gr.Button("🔄 Khởi động lại (Restart)", variant="secondary", scale=1)

    with gr.Row():
        # Left: Code & File Management
        with gr.Column(scale=6):
            gr.Markdown("### 📝 Trình soạn thảo & Quản lý File")
            with gr.Row():
                file_dropdown = gr.Dropdown(choices=list_workspace_files(), value="Main.java", label="Chọn File trong Workspace", scale=3)
                btn_refresh = gr.Button("🔄 Làm mới", scale=1)
            code_editor = gr.Code(label="Mã nguồn (Java / Config)", language="java", lines=18, value=load_file_content("Main.java"))
            btn_save = gr.Button("💾 Lưu Mã Nguồn", variant="secondary")

            with gr.Accordion("📂 Tải lên File mới (.java hoặc .jar)", open=False):
                file_uploader = gr.File(label="Kéo thả file .java hoặc .jar vào đây", file_types=[".java", ".jar", ".txt", ".json"])
                btn_upload = gr.Button("Tải lên Workspace")

        # Right: Real-time Live Console
        with gr.Column(scale=6):
            gr.Markdown("### 💻 Real-time Live Console (Ring Buffer 1.000 dòng)")
            console_box = gr.Textbox(label="Output Console", lines=21, max_lines=25, value=get_console_logs, interactive=False, autoscroll=True)
            with gr.Row():
                stdin_input = gr.Textbox(label="Gửi lệnh System.in", placeholder="Nhập ping, status, time, help...", scale=4, lines=1)
                btn_send_stdin = gr.Button("Gửi", scale=1)

    # Auto-refresh timer for Console and Status every 2 seconds
    timer = gr.Timer(value=2.0)
    timer.tick(fn=get_status_markdown, outputs=status_md)
    timer.tick(fn=get_console_logs, outputs=console_box)

    # Event Bindings
    file_dropdown.change(fn=load_file_content, inputs=[file_dropdown], outputs=[code_editor])
    btn_refresh.click(fn=lambda: gr.update(choices=list_workspace_files()), outputs=[file_dropdown])
    btn_save.click(fn=save_file_content, inputs=[file_dropdown, code_editor, auth_box], outputs=[action_status])

    btn_run.click(fn=run_process_action, inputs=[file_dropdown, auth_box], outputs=[action_status])
    btn_stop.click(fn=stop_process_action, inputs=[auth_box], outputs=[action_status])
    btn_restart.click(fn=restart_process_action, inputs=[file_dropdown, auth_box], outputs=[action_status])

    btn_send_stdin.click(fn=send_stdin_action, inputs=[stdin_input, auth_box], outputs=[stdin_input])
    stdin_input.submit(fn=send_stdin_action, inputs=[stdin_input, auth_box], outputs=[stdin_input])

    btn_upload.click(fn=handle_upload, inputs=[file_uploader, auth_box], outputs=[file_dropdown, action_status])

# Mount Gradio onto FastAPI at root "/"
app = gr.mount_gradio_app(app, demo, path="/")

# Auto-start Java process on boot
if app_config.get("autoStart", True):
    threading.Thread(target=lambda: (time.sleep(2), manager.start(app_config.get("activeFile", "Main.java"))), daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    print(f"Starting server on port {port}...")
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)

