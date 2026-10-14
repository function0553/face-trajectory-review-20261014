"""Read-only loopback viewer. No backend proxy, credentials or database access."""
import argparse
import http.server
from pathlib import Path

BASE = Path(__file__).resolve().parent
ALLOWED = {'index.html', 'style.css', 'viewer.mjs', 'trace.mjs', 'sample.json', 'topology-sample.json'}

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE), **kwargs)
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        name = 'index.html' if path == '/' else path.removeprefix('/')
        if name not in ALLOWED:
            self.send_error(404)
            return
        self.path = '/' + name
        super().do_GET()
    def do_HEAD(self):
        self.send_error(405)
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()
    def guess_type(self, path):
        if path.endswith('.mjs'):
            return 'text/javascript; charset=utf-8'
        return super().guess_type(path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8793)
    args = parser.parse_args()
    with http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'Point viewer: http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()
