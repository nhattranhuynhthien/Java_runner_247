import os
import sys
import glob
import subprocess
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
import cgi

PORT = 5000
WORKSPACE_DIR = "/app/workspace"

class UploadHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/current':
            jars = glob.glob(os.path.join(WORKSPACE_DIR, '*.jar'))
            current_name = os.path.basename(jars[0]) if jars else "Không có file nào"
            res = json.dumps({"current": current_name}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(res)))
            self.end_headers()
            self.wfile.write(res)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == '/api/upload':
            try:
                content_type = self.headers.get('Content-Type')
                if not content_type or 'multipart/form-data' not in content_type:
                    self.send_error(400, "Content-Type must be multipart/form-data")
                    return

                # Parse multipart form data
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
                    res = json.dumps({"success": False, "error": "Chỉ chấp nhận file định dạng .jar!"}).encode('utf-8')
                    self.send_response(400)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(res)
                    return

                # Remove old jar files
                for old_jar in glob.glob(os.path.join(WORKSPACE_DIR, '*.jar')):
                    try:
                        os.remove(old_jar)
                    except Exception:
                        pass

                # Save new jar file
                dest_path = os.path.join(WORKSPACE_DIR, filename)
                with open(dest_path, 'wb') as f:
                    shutil.copyfileobj(file_item.file, f)

                # Kill old freej2me process so watchdog automatically launches the new jar!
                subprocess.run(["pkill", "-9", "-f", "freej2me"], check=False)

                res = json.dumps({
                    "success": True,
                    "filename": filename,
                    "message": f"Đã tải lên và khởi chạy {filename} thành công!"
                }).encode('utf-8')

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(res)))
                self.end_headers()
                self.wfile.write(res)
                return

            except Exception as e:
                res = json.dumps({"success": False, "error": str(e)}).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(res)
                return

        self.send_error(404)

if __name__ == '__main__':
    import shutil
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    server = HTTPServer(('127.0.0.1', PORT), UploadHandler)
    print(f"Uploader server running on 127.0.0.1:{PORT}...")
    server.serve_forever()

