"""
Envoi d'un carrousel sur l'iPhone avec Blip, sans clic.

Blip (Windows) accepte en ligne de commande :  blip.exe --peer <appareil> --file <f1> --file <f2> ...
L'application déjà ouverte reçoit la demande et lance le transfert tout de suite.
L'iPhone est celui que Blip propose en premier dans son menu « Envoyer avec Blip » (le dernier utilisé).

On envoie les slides PNG (01 à 12) et POSTER.txt (la légende à coller), pas les illustrations sources.

Envoi auto (activé par défaut, comptes FR et EN) : toutes les 2 min, chaque carrousel placé au planning pour aujourd'hui
ou demain, rendu depuis plus de 2 min et pas encore envoyé, part tout seul sur l'iPhone.
Sans Blip (Mac, version partagée), dispo() renvoie False et rien ne s'affiche.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RACINE = HERE.parent
ETAT = HERE / "blip-envois.json"  # {"auto": bool, "envois": {cle: {"le", "fichiers", "vers", "auto"}}}
_lock = threading.Lock()
STABLE_S = 120  # un dossier encore en cours de rendu par Antigravity ne part pas
AUTO_COMPTES = ("fr", "en")  # postés depuis ton iPhone ; ES est posté par ton ami (bouton manuel seulement)


def _exe() -> Path | None:
    if os.name != "nt":
        return None
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "blip.exe"
    return p if p.exists() else None


def _shell_dir() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "BlipShell"
    return next((d for d in base.glob("BlipStudioInc.BlipApp_*") if d.is_dir()), None) if base.is_dir() else None


def iphone() -> dict | None:
    """Le destinataire que Blip classe en premier (dernier utilisé), sinon le premier appareil connu."""
    d = _shell_dir()
    if not d:
        return None
    for f, cle in (("suggestions.json", "recipients"), ("shell-settings.json", "devices")):
        try:
            items = json.loads((d / f).read_text("utf-8")).get(cle) or []
        except Exception:
            continue
        items = sorted(items, key=lambda x: x.get("rank", 99))
        if items and items[0].get("peerId"):
            return {"peerId": items[0]["peerId"], "nom": items[0].get("name") or "iPhone"}
    return None


def dispo() -> bool:
    return bool(_exe() and iphone())


def etat() -> dict:
    try:
        e = json.loads(ETAT.read_text("utf-8"))
    except Exception:
        e = {}
    return {"auto": e.get("auto", True), "envois": e.get("envois") or {}}


def _ecrire(e: dict):
    tmp = ETAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(e, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(ETAT)


def regler_auto(on: bool):
    with _lock:
        e = etat()
        e["auto"] = bool(on)
        _ecrire(e)


def fichiers(cle: str) -> list[Path]:
    if not re.fullmatch(r"(fr|en|es)/carrousel-\d+", cle or ""):
        return []
    d = (RACINE / cle).resolve()
    if RACINE.resolve() not in d.parents or not d.is_dir():
        return []
    out = sorted(d.glob("*.png"))
    if out and (d / "POSTER.txt").exists():
        out.append(d / "POSTER.txt")
    return out


def envoyer(cle: str, auto: bool = False) -> dict:
    exe, dest = _exe(), iphone()
    if not exe:
        return {"error": "Blip n'est pas installé sur ce PC."}
    if not dest:
        return {"error": "Aucun iPhone connu dans Blip : envoie une fois un fichier à la main avec Blip."}
    fs = fichiers(cle)
    if not fs:
        return {"error": "Carrousel introuvable ou pas encore rendu."}
    cmd = [str(exe), "--peer", dest["peerId"]]
    for f in fs:
        cmd += ["--file", str(f)]
    try:
        p = subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return {"error": f"Blip n'a pas pu être lancé : {e}"}
    try:
        # Blip déjà ouvert : cette commande lui passe la demande et se ferme aussitôt.
        # Blip fermé : elle devient Blip lui-même et reste ouverte, la demande est prise au démarrage.
        if p.wait(15) != 0:
            return {"error": f"Blip a refusé l'envoi (code {p.returncode})."}
    except subprocess.TimeoutExpired:
        pass
    with _lock:
        e = etat()
        e["envois"][cle] = {"le": dt.datetime.now().isoformat(timespec="minutes"), "fichiers": len(fs),
                            "vers": dest["nom"], "auto": auto}
        _ecrire(e)
    return {"ok": True, "fichiers": len(fs), "vers": dest["nom"]}


def a_envoyer(plan: list[dict]) -> list[str]:
    """Carrousels du planning d'aujourd'hui et de demain, rendus, stables et jamais envoyés."""
    e = etat()
    if not e["auto"] or not dispo():
        return []
    auj = dt.date.today()
    jours = {auj.isoformat(), (auj + dt.timedelta(days=1)).isoformat()}
    out = []
    for x in sorted(plan, key=lambda x: (x.get("date", ""), x.get("heure", ""))):
        cle = str(x.get("carrousel", ""))
        if x.get("date") not in jours or cle in e["envois"] or cle in out or cle.split("/")[0] not in AUTO_COMPTES:
            continue
        fs = fichiers(cle)
        if fs and time.time() - max(f.stat().st_mtime for f in fs) > STABLE_S:
            out.append(cle)
    return out


def boucle(lire_plan, toutes_s: int = 120):
    """Thread du serveur : envoi auto. lire_plan() renvoie les lignes du planning (déjà sans les carrousels postés)."""
    def run():
        time.sleep(20)
        while True:
            try:
                for cle in a_envoyer(lire_plan()):
                    envoyer(cle, auto=True)
                    time.sleep(5)
            except Exception:
                pass
            time.sleep(toutes_s)
    if dispo():
        threading.Thread(target=run, daemon=True).start()
