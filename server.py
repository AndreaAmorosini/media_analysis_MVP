"""Server della dashboard: come `python3 -m http.server`, ma il browser ricontrolla sempre i file.

Senza questa intestazione il browser può tenere in cache una versione vecchia di app.js dopo un
aggiornamento (pulsanti nuovi nella pagina, ma codice vecchio che non li gestisce).

    python3 server.py              # http://localhost:8080/frontend/
    python3 server.py 8090         # altra porta
Va lanciato dalla cartella Dashboard (o con il percorso completo: serve sempre la cartella in cui si trova).
"""
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SenzaCache(SimpleHTTPRequestHandler):
    def end_headers(self):
        # "no-cache": il browser può tenere il file, ma prima di usarlo chiede se è cambiato.
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    porta = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    gestore = functools.partial(SenzaCache, directory=str(Path(__file__).resolve().parent))
    print(f"Dashboard su http://localhost:{porta}/frontend/  (Ctrl+C per fermare)")
    ThreadingHTTPServer(("127.0.0.1", porta), gestore).serve_forever()
