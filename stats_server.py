"""
Page de stats TikTok en local, avec un bouton « Mettre à jour » qui relance scrap_stats.py.

    python stats_server.py          # ouvre http://localhost:8765 dans le navigateur
    python stats_server.py --port 9000 --no-open

Ne répond qu'à ce PC (127.0.0.1). La page lit le dernier relevé de stats-out/<date_heure>/tout.json.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "stats-page"
sys.path.insert(0, str(HERE))
import comptes  # noqa: E402  (liste des comptes, profil et dossier de relevés de chacun)

OUT = comptes.sortie("fr")  # compte par défaut (predict.py ne lit que celui-ci)


def out_of(acc: str) -> Path:
    return comptes.sortie(acc if comptes.existe(acc) else "fr")

job = {"running": False, "lines": [], "code": None, "started": None, "ended": None, "args": []}
lock = threading.Lock()


def runs(acc: str = "fr") -> list[dict]:
    OUT = out_of(acc)
    items = [p for p in OUT.iterdir() if p.is_dir() and (p / "tout.json").exists()] if OUT.exists() else []
    items.sort(key=lambda p: p.name, reverse=True)
    return [{"name": p.name} for p in items]


def complete_auto(d: dict, acc: str = "fr"):
    OUT = out_of(acc)
    """Un relevé automatique ne contient que les 4 derniers posts, sans le compte : on complète avec
    les autres posts et le compte tels que vus dans leur relevé le plus récent."""
    have = {p["id"] for p in d.get("summary", [])}
    need_compte = not (d.get("compte") or {}).get("days")
    for r in runs(acc):
        if r["name"] == d["run"]:
            continue
        try:
            o = json.loads((OUT / r["name"] / "tout.json").read_text("utf-8"))
        except Exception:
            continue
        add = [p for p in o.get("summary", []) if p["id"] not in have]
        if add:
            ids = {p["id"] for p in add}
            d["summary"] += add
            for k in ("hours", "slides", "audience"):
                d[k] = d.get(k, []) + [x for x in o.get(k, []) if x.get("id") in ids]
            have |= ids
        if need_compte and (o.get("compte") or {}).get("days"):
            d["compte"] = o["compte"]
            need_compte = False
    d["summary"].sort(key=lambda p: p["poste_le"], reverse=True)


def start_refresh(args: list[str], acc: str = "fr") -> bool:
    with lock:
        if job["running"]:
            return False
        job.update(running=True, lines=[], code=None, started=time.time(), ended=None, args=args, account=acc)

    def work():
        # Dans la fenêtre (pythonw.exe), le relevé passe par python.exe pour que sa sortie soit lue.
        py = Path(sys.executable)
        py = py.with_name("python.exe") if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists() else py
        cmd = [str(py), "-u", str(HERE / "scrap_stats.py"), "--account", acc, *args]
        try:
            p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in p.stdout:
                with lock:
                    job["lines"].append(line.rstrip())
                    job["lines"] = job["lines"][-200:]
            code = p.wait()
        except Exception as e:  # python introuvable, etc.
            with lock:
                job["lines"].append(f"Erreur : {e}")
            code = -1
        with lock:
            job.update(running=False, code=code, ended=time.time())

    threading.Thread(target=work, daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def same_origin(self) -> bool:
        # Bloque les requêtes envoyées par un autre site ouvert dans le navigateur.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if not host.split(":")[0] in ("localhost", "127.0.0.1"):
            return False
        return origin is None or origin == f"http://{host}"

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        acc = params.get("account", "fr")
        acc = acc if comptes.existe(acc) else "fr"
        if path == "/api/comptes":
            return self.json({"comptes": [{k: c.get(k) for k in ("id", "nom", "court")} for c in comptes.liste()],
                              "ajout": comptes.modifiable()})
        if path == "/api/runs":
            return self.json(runs(acc))
        if path == "/api/data":
            name = params.get("run") or (runs(acc)[0]["name"] if runs(acc) else "")
            f = out_of(acc) / Path(name).name / "tout.json"  # .name : pas de ../
            if not name or not f.exists():
                return self.json({"error": "Aucun relevé : clique sur « Mettre à jour »."}, 404)
            d = json.loads(f.read_text("utf-8"))
            d["run"] = f.parent.name
            if f.parent.name.endswith("-auto"):
                complete_auto(d, acc)
            import predict
            return self.json(predict.add_real_age(d))
        if path in ("/api/history", "/api/prediction") and acc != "fr":
            return self.json({"error": "Les prédictions ne sont disponibles que pour le compte FR pour l'instant."}, 404)
        if path == "/api/history":  # chaque post, tel que vu dans son relevé le plus récent (sert aux prédictions)
            posts: dict = {}
            for r in reversed(runs()):  # du plus ancien au plus récent : le plus récent écrase
                try:
                    d = json.loads((OUT / r["name"] / "tout.json").read_text("utf-8"))
                except Exception:
                    continue
                for p in d.get("summary", []):
                    posts[p["id"]] = {"summary": p, "run": r["name"],
                                      "hours": [h for h in d.get("hours", []) if h["id"] == p["id"]]}
            return self.json({"posts": posts})
        if path == "/api/prediction":  # calcul dans predict.py (tous les relevés, test sur les anciens posts)
            try:
                import predict
                return self.json(predict.prediction(params.get("id")))
            except Exception as e:
                return self.json({"error": f"Prédiction indisponible : {e}"}, 500)
        if path == "/api/refresh":
            with lock:
                return self.json({k: job.get(k) for k in ("running", "lines", "code", "started", "ended", "account")})
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = (PAGE / rel).resolve()
        if PAGE not in f.parents and f != PAGE / "index.html" or not f.is_file():
            return self.send(404, b"Introuvable", "text/plain; charset=utf-8")
        ctype = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(
            f.suffix, "application/octet-stream")
        self.send(200, f.read_bytes(), ctype + "; charset=utf-8")

    def do_POST(self):
        if self.path not in ("/api/refresh", "/api/comptes") or not self.same_origin():
            return self.json({"error": "refusé"}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            body = {}
        if self.path == "/api/comptes":
            return self.comptes_post(body)
        n = body.get("n", 6)
        args = ["--all"] if n == "all" else ["--last", str(max(1, min(int(n), 200)))]
        if body.get("compte") is False:
            args.append("--no-compte")
        acc = body.get("account", "fr")
        if not start_refresh(args, acc if comptes.existe(acc) else "fr"):
            return self.json({"error": "Un relevé est déjà en cours."}, 409)
        self.json({"ok": True})

    def comptes_post(self, body: dict):
        """Ajouter, reconnecter, renommer ou retirer un compte (version partagée seulement)."""
        action, cid = body.get("action"), body.get("id", "")
        if action == "connecter" and comptes.existe(cid):
            comptes.connecter(cid)
            return self.json({"ok": True})
        if not comptes.modifiable():
            return self.json({"error": "La liste des comptes n'est pas modifiable ici."}, 403)
        if action == "ajouter":
            c = comptes.ajouter(str(body.get("nom", "")))
            comptes.connecter(c["id"])
            return self.json({"ok": True, "compte": c})
        if action == "renommer" and comptes.existe(cid):
            comptes.renommer(cid, str(body.get("nom", "")))
            return self.json({"ok": True})
        if action == "retirer" and comptes.existe(cid) and cid != "fr":
            comptes.retirer(cid)
            return self.json({"ok": True})
        return self.json({"error": "Action inconnue."}, 400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    url = f"http://localhost:{a.port}"
    ThreadingHTTPServer.allow_reuse_address = False  # sous Windows, sinon 2 serveurs se partagent le port en silence
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        print(f"La page tourne déjà sur {url} (une autre fenêtre). Ferme-la pour relancer la nouvelle version.")
        if not a.no_open:
            webbrowser.open(url)
        return
    print(f"Stats TikTok : {url}   (Ctrl+C pour arrêter)", flush=True)
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
