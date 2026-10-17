"""Small trusted demo app with no dependencies beyond Python's standard library."""
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = b"""<!doctype html><html><head><title>External application demo</title></head>
<body><h1>Customer Insights demo</h1><p>This separate Docker application was launched by the workbench.</p>
<p>Local demo content only. No customer data is loaded.</p></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8501), Handler).serve_forever()
