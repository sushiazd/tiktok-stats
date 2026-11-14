"""Stats TikTok dans sa propre fenêtre (Windows et Mac), lancé par lanceur.py avec le Python de l'outil.

Le serveur de stats tourne dans ce même processus : fermer la fenêtre arrête tout.
Si la fenêtre ne peut pas s'ouvrir (pywebview absent ou en panne), la page s'ouvre dans le navigateur.
"""
import os
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path

ICI = Path(__file__).resolve().parent
TITRE = "Stats TikTok"
sys.path.insert(0, str(ICI))
import stats_server  # noqa: E402

FENETRE = None


class Api:
    """Appelé par la page (window.pywebview.api) : F11 ou le menu ⋯ > Plein écran."""

    def plein_ecran(self):
        if FENETRE:
            FENETRE.toggle_fullscreen()


def serveur():
    ThreadingHTTPServer.allow_reuse_address = False
    for port in (8765, 0):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), stats_server.Handler)
            break
        except OSError:
            continue
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main():
    srv, port = serveur()
    url = f"http://127.0.0.1:{port}/"
    try:
        import webview
    except Exception:
        webview = None
    if webview is not None:
        try:
            global FENETRE
            FENETRE = webview.create_window(TITRE, url, width=1240, height=860, min_size=(420, 500),
                                            js_api=Api(), text_select=True)
            dossier = Path(os.environ.get("TIKTOK_STATS_SUPPORT") or ICI) / "fenetre"
            webview.start(private_mode=False, storage_path=str(dossier))
            return
        except Exception as e:
            print(f"Fenêtre impossible ({e}) : ouverture dans le navigateur.", flush=True)
    print(f"Stats TikTok : {url}   (ferme cette fenêtre pour arrêter)", flush=True)
    webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
