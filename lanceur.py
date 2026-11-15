"""Lanceur de Stats TikTok (version partagée, Windows et Mac).

Appelé par « Stats TikTok » (.bat sous Windows, .command sur Mac), ou « Connecter mon compte » (--connecter).
Au premier lancement, crée l'app « Stats TikTok » (Mac : dans le dossier et dans Applications ;
Windows : raccourci sur le Bureau), qui relance ce fichier avec --fenetre, sans terminal.
1. Met à jour les fichiers depuis le lien de maj-url.txt (si présent), sans toucher aux relevés.
2. Prépare un environnement Python à part avec Playwright (une seule fois).
3. Connexion TikTok (première fois, ou avec --connecter), puis ouvre la page de stats.

Uniquement la bibliothèque standard : doit tourner avec n'importe quel Python 3.9 ou plus.
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

WINDOWS = os.name == "nt"
ICI = Path(__file__).resolve().parent
if WINDOWS:
    SUPPORT = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TikTok Stats"
    PY = SUPPORT / "venv" / "Scripts" / "python.exe"
    PYW = SUPPORT / "venv" / "Scripts" / "pythonw.exe"
    CHROMES = [Path(os.environ.get(v, "")) / "Google" / "Chrome" / "Application" / "chrome.exe"
               for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(v)]
else:
    SUPPORT = Path.home() / "Library" / "Application Support" / "TikTok Stats"
    PY = SUPPORT / "venv" / "bin" / "python"
    PYW = PY
    CHROMES = [Path("/Applications/Google Chrome.app"), Path.home() / "Applications" / "Google Chrome.app"]
VENV = SUPPORT / "venv"
PROFIL = SUPPORT / "chrome-profil"
# Jamais écrasé par une mise à jour : les relevés et les réglages de la personne.
A_GARDER = {"stats-out", "maj-url.txt", ".version", "comptes.json", "robot-url.txt"}
SI_ABSENT = {"noms-posts.json"}

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def dire(msg=""):
    print(msg, flush=True)


def chrome_installe():
    return any(p.exists() for p in CHROMES)


# ---------------------------------------------------------------- mise à jour

def lien_direct(url):
    """Un lien de partage OneDrive ou Dropbox devient un lien de téléchargement direct."""
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
        req = urllib.request.Request(lien_direct(url), headers={"User-Agent": "tiktok-stats"})
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
        rel = Path(*parts[1:])  # on enlève le dossier racine du zip
        if rel.parts[0] in A_GARDER or rel.parts[0].startswith("stats-out") or ".." in rel.parts:
            continue
        cible = ICI / rel
        if rel.name in SI_ABSENT and cible.exists():
            continue
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_bytes(z.read(info))
        if cible.suffix == ".command" and not WINDOWS:
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
    elif subprocess.run([str(PY), "-c", "import playwright"], capture_output=True).returncode:
        subprocess.check_call([str(PY), "-m", "pip", "install", "-q", "playwright"])
    if subprocess.run([str(PY), "-c", "import webview"], capture_output=True).returncode:
        dire("Installation de la fenêtre de l'app...")
        if subprocess.run([str(PY), "-m", "pip", "install", "-q", "pywebview"]).returncode:
            dire("Fenêtre indisponible : les stats s'ouvriront dans le navigateur.")
    if not chrome_installe():
        marque = SUPPORT / ".chromium-ok"
        if not marque.exists():
            dire("Google Chrome n'est pas installé : je télécharge le navigateur de Playwright (une seule fois)...")
            subprocess.check_call([str(PY), "-m", "playwright", "install", "chromium"])
            marque.write_text("ok")


def robot():
    """Adresse du robot de relevé en ligne de la personne (« Installer le robot »), sinon vide."""
    f = ICI / "robot-url.txt"
    return f.read_text("utf-8").strip() if f.exists() else ""


def env():
    e = dict(os.environ)
    e.update({
        "TIKTOK_STATS_PROFIL": str(PROFIL),
        "TIKTOK_STATS_COMPTES": str(ICI / "comptes.json"),  # liste des comptes (bouton « + » dans la fenêtre)
        "TIKTOK_STATS_NAVIGATEUR": "chrome" if chrome_installe() else "",
        "TIKTOK_STATS_CLOUD": robot(),
        "TIKTOK_STATS_CONNEXION": "lance « Connecter mon compte »",
        "TIKTOK_STATS_SUPPORT": str(SUPPORT),
        "PYTHONIOENCODING": "utf-8",
    })
    return e


CONNEXION = r'''
import os
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
    dire(f"  3. FERME la fenêtre ({'croix en haut à droite' if WINDOWS else 'Cmd+Q'}). La suite démarre toute seule.")
    subprocess.call([str(PY), "-c", CONNEXION], env=env())


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Stats TikTok</string>
<key>CFBundleDisplayName</key><string>Stats TikTok</string>
<key>CFBundleIdentifier</key><string>local.stats-tiktok</string>
<key>CFBundleExecutable</key><string>stats-tiktok</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>10.13</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
"""


def creer_app():
    """L'app « Stats TikTok » qui ouvre directement la fenêtre, sans terminal.
    Créée ici (et non téléchargée) : macOS ne la bloque pas et la lance depuis sa vraie place."""
    lanceur = ICI / "lanceur.py"
    journal = SUPPORT / "stats.log"
    if WINDOWS:
        marque = SUPPORT / ".raccourci"
        if marque.exists() and marque.read_text("utf-8") == str(ICI):
            return
        q = lambda x: str(x).replace("'", "''")
        ps = ("$d=[Environment]::GetFolderPath('Desktop');"
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Stats TikTok.lnk'));"
              f"$s.TargetPath='{q(Path(sys.executable).with_name('pythonw.exe'))}';"
              f"$s.Arguments='\"{q(lanceur)}\" --fenetre';$s.WorkingDirectory='{q(ICI)}';$s.Save()")
        if subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True).returncode == 0:
            marque.write_text(str(ICI), "utf-8")
            dire("Raccourci « Stats TikTok » ajouté sur ton Bureau : la prochaine fois, ouvre-le directement.")
        return
    script = (f'#!/bin/bash\ncd "{ICI}"\n'
              f'exec "{sys.executable}" "{lanceur}" --fenetre >> "{journal}" 2>&1\n')
    for base in (ICI, Path.home() / "Applications"):
        app = base / "Stats TikTok.app"
        exe = app / "Contents" / "MacOS" / "stats-tiktok"
        if exe.exists() and exe.read_text("utf-8") == script:
            continue
        try:
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_text(script, "utf-8")
            exe.chmod(0o755)
            (app / "Contents" / "Info.plist").write_text(PLIST, "utf-8")
            dire(f"App « Stats TikTok » créée dans {base} : la prochaine fois, ouvre-la directement"
                 " (tu peux la glisser dans le Dock).")
        except OSError:
            pass


def ouvrir():
    """La fenêtre des stats (repli : la page dans le navigateur)."""
    py = PYW if "--fenetre" in sys.argv and PYW.exists() else PY
    subprocess.call([str(py), str(ICI / "fenetre.py")], cwd=str(ICI), env=env())


def connecte():
    return any((PROFIL / "Default" / p).exists() for p in ("Cookies", "Network/Cookies"))


def main():
    args = sys.argv[1:]
    if sys.stdout is None:  # pythonw (raccourci Windows) : pas de console, tout va dans le journal
        SUPPORT.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(SUPPORT / "stats.log", "a", encoding="utf-8")
    if "--deja-maj" not in args and mettre_a_jour():
        # Le lanceur lui-même a changé : on relance la nouvelle version.
        sys.exit(subprocess.call([sys.executable, str(ICI / "lanceur.py"), *args, "--deja-maj"]))
    installer()
    if "--fenetre" in args:  # lancé par l'app, sans terminal : pas de questions, on ouvre
        ouvrir()
        return
    creer_app()
    if "--connecter" in args or not connecte():
        connecter()
        if "--connecter" in args:
            dire("C'est fait. Tu peux maintenant ouvrir « Stats TikTok ».")
            return
    dire("")
    dire("La fenêtre des stats s'ouvre. Clique « Mettre à jour » pour un nouveau relevé.")
    ouvrir()


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        dire(f"\nL'installation a échoué ({e}). Vérifie ta connexion internet et relance.")
        sys.exit(1)
