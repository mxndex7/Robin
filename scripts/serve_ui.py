"""Serve a pasta ui/ sem cache, para testar a interface com o mock.js no navegador.

Uso: .venv/Scripts/python scripts/serve_ui.py [porta]
Depois abra http://127.0.0.1:5180/index.html?mock (veja os parâmetros no topo de ui/js/mock.js).
Só para desenvolvimento: o app usa o servidor do pywebview (com o no-store reaplicado em app.py).
"""

import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5180
    handler = functools.partial(NoCacheHandler, directory=str(UI_DIR))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as server:
        print(f"Interface em http://127.0.0.1:{port}/index.html?mock")
        server.serve_forever()


if __name__ == "__main__":
    main()
