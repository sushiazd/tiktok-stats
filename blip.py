"""
Envoi d'un carrousel sur l'iPhone avec Blip, sans clic.

Blip (Windows) accepte en ligne de commande :  blip.exe --peer <appareil> --file <f1> --file <f2> ...
L'application déjà ouverte reçoit la demande et lance le transfert tout de suite.
Chaque compte a son appareil : celui dont le nom dans Blip finit par « - FR », « - EN », « - ES » ou « - IT »
(par exemple « iPhone - FR », « Tablette - EN »). Sans ça : blip-envois.json >
"appareils", puis la table APPAREILS, puis celui que Blip propose en premier.

On envoie le dossier entier du carrousel (slides, POSTER.txt, POST.json, illustrations), comme à la main.

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
AUTO_COMPTES = ("fr", "en", "es", "it")  # chaque compte va sur l'appareil Blip nommé « … - FR / - EN / - ES / - IT »


def _exe() -> Path | None:
    if os.name != "nt":
        return None
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "blip.exe"
    return p if p.exists() else None


def _shell_dir() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "BlipShell"
    return next((d for d in base.glob("BlipStudioInc.BlipApp_*") if d.is_dir()), None) if base.is_dir() else None


# Repli si aucun appareil de Blip ne porte le suffixe du compte (noms du 24/11 vers 20 h).
APPAREILS: dict = {}


def _appareils_blip() -> list[dict]:
    """Les appareils connus de Blip (nom + peerId)."""
    d = _shell_dir()
    out: list[dict] = []
    if not d:
        return out
    for f, cle in (("shell-settings.json", "devices"), ("suggestions.json", "recipients")):
        try:
            items = json.loads((d / f).read_text("utf-8")).get(cle) or []
        except Exception:
            continue
        for x in items:
            if x.get("peerId") and not any(y["peerId"] == x["peerId"] for y in out):
                out.append({"peerId": x["peerId"], "nom": x.get("name") or ""})
    return out


def appareil(acc: str) -> dict | None:
    """L'appareil où l'on poste ce compte : celui dont le nom Blip finit par « - FR », « - EN », « - ES », « - IT »
    (renommer l'appareil dans Blip suffit) ; sinon blip-envois.json > "appareils", sinon APPAREILS."""
    suf = f"- {acc.upper()}"
    for x in _appareils_blip():
        if x["nom"].strip().upper().endswith(suf):
            return x
    a = {**APPAREILS, **(etat().get("appareils") or {})}.get(acc)
    return a if a and a.get("peerId") else iphone()


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
    return {"auto": e.get("auto", True), "envois": e.get("envois") or {}, **({"appareils": e["appareils"]} if e.get("appareils") else {})}


def _ecrire(e: dict):
    tmp = ETAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(e, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(ETAT)


def regler_auto(on: bool):
    with _lock:
        e = etat()
        e["auto"] = bool(on)
        _ecrire(e)


def dossier(cle: str) -> Path | None:
    """Le dossier du carrousel, seulement s'il est rendu (au moins une slide PNG)."""
    if not re.fullmatch(r"(fr|en|es|it)/carrousel-\d+", cle or ""):
        return None
    d = (RACINE / cle).resolve()
    if RACINE.resolve() not in d.parents or not d.is_dir() or not any(d.glob("*.png")):
        return None
    return d


def fichiers(cle: str) -> list[Path]:
    """Tous les fichiers du dossier (sert à compter et à voir si Antigravity a fini d'écrire)."""
    d = dossier(cle)
    return [f for f in d.rglob("*") if f.is_file()] if d else []


COPIES = HERE / "envois-blip"  # copies gardées (Blip lit les fichiers pendant tout le transfert) ; pas dans LOCALAPPDATA, redirigé quand Claude lance l'envoi


JOURS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
MOIS = ["janv", "févr", "mars", "avr", "mai", "juin", "juil", "août", "sept", "oct", "nov", "déc"]


def _quand(cle: str) -> str:
    """Date et heure prévues au planning (le prochain créneau, sinon le dernier) : « mar 24 nov 19h30 »."""
    try:
        plan = json.loads((HERE / "planning.json").read_text("utf-8")).get("plan") or []
    except Exception:
        return ""
    autre = cle.replace("/carrousel-", "/prompt-")
    xs = sorted((x for x in plan if x.get("carrousel") in (cle, autre) and x.get("date") and x.get("heure")),
                key=lambda x: (x["date"], x["heure"]))
    if not xs:
        return ""
    auj = dt.date.today().isoformat()
    x = next((x for x in xs if x["date"] >= auj), xs[-1])
    try:
        d = dt.date.fromisoformat(x["date"])
    except ValueError:
        return ""
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {x['heure'].replace(':', 'h')}"


def nom_envoi(cle: str) -> str:
    """Nom du dossier reçu sur le téléphone : « FR 78 - mar 24 nov 19h30 », « ES 04 » s'il n'est pas au planning."""
    acc, n = cle.split("/")[0], cle.rsplit("-", 1)[-1]
    num = n.zfill(2) if acc in ("es", "it") else str(int(n))
    q = _quand(cle)
    return f"{acc.upper()} {num}" + (f" - {q}" if q else "")


def _copie_nommee(cle: str, d: Path) -> Path:
    import shutil
    cible = COPIES / nom_envoi(cle)
    if cible.exists():
        shutil.rmtree(cible)
    shutil.copytree(d, cible)
    return cible


def envoyer(cle: str, auto: bool = False) -> dict:
    exe, dest = _exe(), appareil(cle.split("/")[0])
    if not exe:
        return {"error": "Blip n'est pas installé sur ce PC."}
    if not dest:
        return {"error": "Aucun iPhone connu dans Blip : envoie une fois un fichier à la main avec Blip."}
    d, fs = dossier(cle), fichiers(cle)
    if not d:
        return {"error": "Carrousel introuvable ou pas encore rendu."}
    try:
        d = _copie_nommee(cle, d)  # « IT 01 - inverno » plutôt que « carrousel-01 » sur le téléphone
    except Exception:
        pass
    cmd = [str(exe), "--peer", dest["peerId"], "--file", str(d)]
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
