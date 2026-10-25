import os
import sys
import time
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime

PORT = int(os.getenv("PORT", 7860))
START_TIME = time.time()

class CloudGameHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Health check endpoint for UptimeRobot
        if self.path in ['/health', '/ping']:
            uptime_sec = int(time.time() - START_TIME)
            res_body = (
                f'{{"status":"alive","game":"Ngoc Rong Online","uptime_seconds":{uptime_sec},'
                f'"timestamp":"{datetime.utcnow().isoformat()}"}}'
            ).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-cache, no-store')
            self.send_header('Content-Length', str(len(res_body)))
            self.end_headers()
            self.wfile.write(res_body)
            return

        # Redirect root to custom Ngọc Rồng Web Player UI
        if self.path in ['/', '/index.html']:
            self.path = '/player.html'

        return super().do_GET()

if __name__ == '__main__':
    web_dir = os.path.join(os.path.dirname(__file__), 'public')
    os.chdir(web_dir)
    server = HTTPServer(('0.0.0.0', PORT), CloudGameHandler)
    print(f"Cloud Game Server running on port {PORT}...")
    server.serve_forever()
