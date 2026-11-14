"""Lanceur Mac de Stats TikTok (version partagée).

Appelé par « Stats TikTok.command » (ou « Connecter mon compte.command » avec --connecter).
1. Met à jour les fichiers depuis le lien de maj-url.txt (si présent), sans toucher aux relevés.
2. Prépare un environnement Python à part avec Playwright (une seule fois).
3. Connexion TikTok (première fois, ou avec --connecter), puis ouvre la page de stats.

Uniquement la bibliothèque standard : doit tourner avec le python3 du Mac (3.9 ou plus).
"""
import base64
import hashlib
import io
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ICI = Path(__file__).resolve().parent
SUPPORT = Path.home() / "Library" / "Application Support" / "TikTok Stats"
VENV = SUPPORT / "venv"
PY = VENV / "bin" / "python"
PROFIL = SUPPORT / "chrome-profil"
CHROME = Path("/Applications/Google Chrome.app")
# Jamais écrasé par une mise à jour : les relevés et les réglages de la personne.
A_GARDER = {"stats-out", "maj-url.txt", ".version"}
SI_ABSENT = {"noms-posts.json"}


def dire(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- mise à jour

def lien_direct(url):
    """Un lien de partage OneDrive (1drv.ms, onedrive.live.com) devient un lien de téléchargement direct."""
    if "1drv.ms" in url or "onedrive.live.com" in url:
        code = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        return f"https://api.onedrive.com/v1.0/shares/u!{code}/root/content"
    if "dropbox.com" in url:
        return url.replace("dl=0", "dl=1") if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    return url


def mettre_a_jour():
    f = ICI / "maj-url.txt"
    url = f.read_text("utf-8").strip() if f.exists() else ""
    if not url:
        return False
    try:
        req = urllib.request.Request(lien_direct(url), headers={"User-Agent": "tiktok-stats-mac"})
        data = urllib.request.urlopen(req, timeout=30).read()
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:  # hors ligne, lien cassé... on continue avec la version actuelle
        dire(f"Mise à jour impossible pour l'instant ({type(e).__name__}) : on garde la version actuelle.")
        return False
    # Version = contenu de version.txt dans le zip (une archive GitHub change d'octets d'un téléchargement à l'autre).
    noms = [i.filename for i in z.infolist() if Path(i.filename).name == "version.txt" and len(Path(i.filename).parts) == 2]
    empreinte = z.read(noms[0]).decode("utf-8").strip() if noms else hashlib.sha256(data).hexdigest()
    vf = ICI / ".version"
    if vf.exists() and vf.read_text("utf-8").strip() == empreinte:
        dire("Déjà à jour.")
        return False
    avant = (ICI / "lanceur.py").read_bytes()
    for info in z.infolist():
        parts = Path(info.filename).parts
        if info.is_dir() or len(parts) < 2:
            continue
        rel = Path(*parts[1:])  # on enlève le dossier « tiktok-stats/ » du zip
        if rel.parts[0] in A_GARDER or ".." in rel.parts:
            continue
        cible = ICI / rel
        if rel.name in SI_ABSENT and cible.exists():
            continue
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(z.read(info))
        if cible.suffix == ".command":
            cible.chmod(0o755)
    vf.write_text(empreinte, "utf-8")
    v = ICI / "version.txt"
    dire(f"Mis à jour : {v.read_text('utf-8').strip() if v.exists() else 'nouvelle version'}.")
    return (ICI / "lanceur.py").read_bytes() != avant


# ---------------------------------------------------------------- installation

def installer():
    SUPPORT.mkdir(parents=True, exist_ok=True)
    if not PY.exists():
        dire("Première fois : installation (2-3 minutes, une seule fois)...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
        subprocess.check_call([str(PY), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        subprocess.check_call([str(PY), "-m", "pip", "install", "-q", "playwright"])
    else:
        r = subprocess.run([str(PY), "-c", "import playwright"], capture_output=True)
        if r.returncode:
            subprocess.check_call([str(PY), "-m", "pip", "install", "-q", "playwright"])
    if not CHROME.exists():
        marque = SUPPORT / ".chromium-ok"
        if not marque.exists():
            dire("Google Chrome n'est pas installé : je télécharge le navigateur de Playwright (une seule fois)...")
            subprocess.check_call([str(PY), "-m", "playwright", "install", "chromium"])
            marque.write_text("ok")


def env():
    e = dict(os.environ)
    e.update({
        "TIKTOK_STATS_PROFIL": str(PROFIL),
        "TIKTOK_STATS_NAVIGATEUR": "chrome" if CHROME.exists() else "",
        "TIKTOK_STATS_CLOUD": "",
        "TIKTOK_STATS_CONNEXION": "double-clique « Connecter mon compte.command »",
        "PYTHONIOENCODING": "utf-8",
    })
    return e


CONNEXION = r'''
import os, sys
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(os.environ["TIKTOK_STATS_PROFIL"],
        channel=os.environ["TIKTOK_STATS_NAVIGATEUR"] or None, headless=False, viewport=None, locale="fr-FR")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://www.tiktok.com/login")
    try:
        ctx.wait_for_event("close", timeout=0)
    except Exception:
        pass
'''


def connecter():
    dire("")
    dire("Une fenêtre de navigateur s'ouvre :")
    dire("  1. connecte-toi à TON compte TikTok ;")
    dire("  2. ouvre une fois https://www.tiktok.com/tiktokstudio ;")
    dire("  3. FERME la fenêtre (Cmd+Q). La suite démarre toute seule.")
    subprocess.call([str(PY), "-c", CONNEXION], env=env())


def connecte():
    return any((PROFIL / "Default" / p).exists() for p in ("Cookies", "Network/Cookies"))


def main():
    args = sys.argv[1:]
    if "--deja-maj" not in args and mettre_a_jour():
        os.execv(sys.executable, [sys.executable, str(ICI / "lanceur.py"), *args, "--deja-maj"])
    installer()
    if "--connecter" in args or not connecte():
        connecter()
        if "--connecter" in args:
            dire("C'est fait. Tu peux maintenant ouvrir « Stats TikTok.command ».")
            return
    dire("")
    dire("La page des stats s'ouvre dans ton navigateur. Clique « Mettre à jour » pour un nouveau relevé.")
    dire("Laisse cette fenêtre ouverte tant que tu regardes la page ; ferme-la pour tout arrêter.")
    subprocess.call([str(PY), str(ICI / "stats_server.py")], cwd=str(ICI), env=env())


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        dire(f"\nL'installation a échoué ({e}). Vérifie ta connexion internet et relance.")
        sys.exit(1)
