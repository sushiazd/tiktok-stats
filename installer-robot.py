"""Installe TON robot de relevé en ligne (optionnel), sur ton compte Cloudflare gratuit.

Le robot (robot/worker.js) lit toutes les heures les compteurs PUBLICS de tes posts récents (vues, likes, favoris,
partages, commentaires), même quand ton ordinateur est éteint. La prévision des vues à 48 h s'en sert pour avoir
une courbe heure par heure complète. Aucun mot de passe TikTok : il lit les pages publiques, comme un visiteur.

Il faut Node.js (gratuit) et un compte Cloudflare (gratuit). Lancé par « Installer le robot » (.bat / .command).
Relance-le quand tu veux pour changer les comptes suivis ou mettre le robot à jour : il réutilise la même base.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

import lanceur  # mêmes dossiers que l'outil (TikTok Stats dans Application Support / %LOCALAPPDATA%)

ICI = Path(__file__).resolve().parent
TRAVAIL = lanceur.SUPPORT / "robot"
NOM = "tiktok-stats-releve"
URL_FICHIER = ICI / "robot-url.txt"
ENV = {**os.environ, "WRANGLER_SEND_METRICS": "false"}


def dire(msg=""):
    print(msg, flush=True)


def wrangler(*args, capture=True):
    """npx wrangler ... ; capture=False laisse la main à l'écran (connexion dans le navigateur)."""
    cmd = [NPX, "--yes", "wrangler@4", *args]
    if not capture:
        return subprocess.call(cmd, cwd=TRAVAIL, env=ENV), ""
    p = subprocess.run(cmd, cwd=TRAVAIL, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def comptes_proposes():
    """Les noms en @ de la fenêtre (comptes.json), pour proposer une réponse par défaut."""
    try:
        cs = json.loads((ICI / "comptes.json").read_text("utf-8"))
        return [c["nom"].strip().lstrip("@") for c in cs if str(c.get("nom", "")).strip().startswith("@")]
    except Exception:
        return []


def demander_comptes():
    ancien = ""
    toml = TRAVAIL / "wrangler.toml"
    if toml.exists():
        m = re.search(r'COMPTES = "([^"]*)"', toml.read_text("utf-8"))
        ancien = m.group(1) if m else ""
    defaut = ancien or ",".join(comptes_proposes())
    dire("Quels comptes TikTok le robot doit-il suivre ?")
    dire("Écris le nom qui est dans l'adresse du profil (tiktok.com/@NOM), sans @.")
    dire("Plusieurs comptes : sépare-les par des virgules.")
    rep = input(f"Comptes{f' [{defaut}]' if defaut else ''} : ").strip() or defaut
    noms = [n.strip().lstrip("@") for n in rep.split(",") if n.strip()]
    if not noms or not all(re.fullmatch(r"[A-Za-z0-9._]{2,24}", n) for n in noms):
        sys.exit("Nom de compte non reconnu (lettres, chiffres, points et _ seulement). Relance.")
    return noms


def base_d1():
    """L'identifiant de la base D1 du robot (créée la première fois)."""
    code, out = wrangler("d1", "list", "--json")
    if code == 0:
        try:
            debut = out.index("[")
            for b in json.loads(out[debut:out.rindex("]") + 1]):
                if b.get("name") == NOM:
                    return b.get("uuid") or b.get("database_id")
        except ValueError:
            pass
    dire("Création de la base de données du robot...")
    code, out = wrangler("d1", "create", NOM)
    m = re.search(r'database_id"?\s*[=:]\s*"([0-9a-f-]{36})"', out)
    if code or not m:
        dire(out[-1500:])
        sys.exit("Création de la base impossible (message ci-dessus).")
    return m.group(1)


def main():
    dire("=== Robot de relevé en ligne (optionnel) ===")
    dire("")
    if not NPX:
        dire("Il faut Node.js (gratuit) : installe la version « LTS » depuis la page qui s'ouvre, puis relance.")
        webbrowser.open("https://nodejs.org/fr/download")
        sys.exit(1)
    TRAVAIL.mkdir(parents=True, exist_ok=True)
    noms = demander_comptes()

    dire("")
    dire("Vérification de ta connexion à Cloudflare...")
    code, out = wrangler("whoami")
    if code or "not authenticated" in out.lower() or "pas authentifi" in out.lower():
        dire("Une page Cloudflare s'ouvre dans ton navigateur : crée un compte gratuit ou connecte-toi,")
        dire("puis clique « Allow ». Reviens ici ensuite.")
        if wrangler("login", capture=False)[0]:
            sys.exit("Connexion à Cloudflare annulée. Relance quand tu veux.")

    db = base_d1()
    shutil.copyfile(ICI / "robot" / "worker.js", TRAVAIL / "worker.js")
    (TRAVAIL / "wrangler.toml").write_text(
        f'name = "{NOM}"\nmain = "worker.js"\ncompatibility_date = "2025-01-01"\nworkers_dev = true\n\n'
        f'[vars]\nCOMPTES = "{",".join(noms)}"\n\n'
        f'[[d1_databases]]\nbinding = "DB"\ndatabase_name = "{NOM}"\ndatabase_id = "{db}"\n\n'
        f'[triggers]\ncrons = ["0 * * * *"]\n', "utf-8")

    dire("Mise en ligne du robot...")
    code, out = wrangler("deploy")
    m = re.search(r"https://[A-Za-z0-9.-]+\.workers\.dev", out)
    if code or not m:
        dire(out[-2000:])
        if "subdomain" in out.lower():
            dire("")
            dire("Cloudflare veut d'abord que tu choisisses ton adresse workers (une seule fois) :")
            dire("dans la page qui s'ouvre, va dans « Workers & Pages », choisis un nom, puis relance.")
            webbrowser.open("https://dash.cloudflare.com/?to=/:account/workers-and-pages")
        sys.exit("La mise en ligne a échoué (message ci-dessus).")
    url = m.group(0)

    dire("Premier relevé...")
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(url + "/run", headers={"User-Agent": "tiktok-stats"}),
                                              timeout=120).read())
        dire(f"  {r.get('ok', 0)} post(s) relevé(s) sur {r.get('suivis', 0)}."
             + (f" Problèmes : {' | '.join(r['erreurs'])[:300]}" if r.get("erreurs") else ""))
    except Exception as e:  # déjà lancé il y a moins de 5 min, ou réseau : le cron prendra le relais
        dire(f"  (pas tout de suite : {type(e).__name__}, il passera à la prochaine heure)")

    URL_FICHIER.write_text(url + "\n", "utf-8")
    dire("")
    dire("C'est fait. Le robot relève tes posts toutes les heures, même ordinateur éteint.")
    dire(f"Sa page d'état : {url}")
    dire("L'outil Stats TikTok s'en sert tout seul dès sa prochaine ouverture.")


NPX = shutil.which("npx")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAnnulé.")
