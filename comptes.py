"""Les comptes TikTok suivis par Stats TikTok : profil Chrome et dossier de relevés de chacun.

Chez le propriétaire (rien de réglé) : les comptes fixes, fr = @vocly.app, en = @vocly.en, es = compte espagnol (téléphone du pote).
Version partagée (lanceur.py règle TIKTOK_STATS_COMPTES) : une liste modifiable dans comptes.json,
le premier compte a l'identifiant « fr », les suivants c2, c3... (prévision des vues pour chacun)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home()))
FICHIER = os.environ.get("TIKTOK_STATS_COMPTES")  # chemin de comptes.json (version partagée)
_PROFIL = os.environ.get("TIKTOK_STATS_PROFIL")    # profil Chrome du 1er compte (version partagée)

DEFAUT = [
    {"id": "fr", "nom": "@vocly.app", "court": "FR", "profil": "vocly-tiktok-chrome", "sortie": "stats-out"},
    {"id": "en", "nom": "@vocly.en", "court": "EN", "profil": "vocly-tiktok-chrome-en", "sortie": "stats-out-en"},
    {"id": "es", "nom": "@vocly.es3", "court": "ES", "profil": "vocly-tiktok-chrome-es", "sortie": "stats-out-es"},
]


def modifiable() -> bool:
    return bool(FICHIER)


def liste() -> list[dict]:
    if not FICHIER:
        return [dict(c) for c in DEFAUT]
    try:
        comptes = json.loads(Path(FICHIER).read_text("utf-8"))
        comptes = [c for c in comptes if isinstance(c, dict) and c.get("id")]
    except Exception:
        comptes = []
    if not any(c["id"] == "fr" for c in comptes):
        comptes.insert(0, {"id": "fr", "nom": "Mon compte"})
    return comptes


def existe(cid: str) -> bool:
    return any(c["id"] == cid for c in liste())


def profil(cid: str) -> Path:
    if FICHIER or _PROFIL:
        base = Path(_PROFIL) if _PROFIL else _LOCAL / "tiktok-stats-chrome"
        return base if cid == "fr" else base.with_name(f"{base.name}-{cid}")
    c = next((c for c in DEFAUT if c["id"] == cid), DEFAUT[0])
    return _LOCAL / c["profil"]


def sortie(cid: str) -> Path:
    if not FICHIER:
        c = next((c for c in DEFAUT if c["id"] == cid), DEFAUT[0])
        return HERE / c["sortie"]
    return HERE / ("stats-out" if cid == "fr" else f"stats-out-{cid}")


def _enregistrer(comptes: list[dict]) -> None:
    Path(FICHIER).write_text(json.dumps(comptes, ensure_ascii=False, indent=2) + "\n", "utf-8")


def ajouter(nom: str) -> dict:
    comptes = liste()
    n = 2
    while any(c["id"] == f"c{n}" for c in comptes):
        n += 1
    c = {"id": f"c{n}", "nom": (nom or "").strip()[:40] or f"Compte {n}"}
    comptes.append(c)
    _enregistrer(comptes)
    return c


def renommer(cid: str, nom: str) -> None:
    comptes = liste()
    for c in comptes:
        if c["id"] == cid and nom.strip():
            c["nom"] = nom.strip()[:40]
    _enregistrer(comptes)


def retirer(cid: str) -> None:
    """Retire le compte de la liste (ses relevés restent sur le disque). Le 1er compte ne se retire pas."""
    if cid != "fr":
        _enregistrer([c for c in liste() if c["id"] != cid])


CONNEXION = r'''
import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(sys.argv[1],
        channel=os.environ.get("TIKTOK_STATS_NAVIGATEUR", "chrome") or None, headless=False, viewport=None,
        locale="fr-FR")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.tiktok.com/login")
    try:
        ctx.wait_for_event("close", timeout=0)
    except Exception:
        pass
'''


def connecter(cid: str, attendre: bool = False):
    """Ouvre une fenêtre de navigateur sur le profil du compte pour s'y connecter à TikTok."""
    py = Path(sys.executable)
    if py.name.lower() == "pythonw.exe" and py.with_name("python.exe").exists():
        py = py.with_name("python.exe")
    cmd = [str(py), "-c", CONNEXION, str(profil(cid))]
    if attendre:
        return subprocess.call(cmd)
    return subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
