"""Vue d'ensemble de Stats TikTok : vues par jour des comptes, installs PostHog, planning des carrousels.

- Vues par jour : série « vues » du compte dans chaque relevé (TikTok la donne avec un jour de retard :
  j-k = date du relevé - k - 1, vérifié sur les relevés du 17 au 23 novembre). Les jours que TikTok
  n'a pas encore donnés sont estimés avec les vues heure par heure des posts récents (marqués « estimé »).
- Installs : installs-posthog.json, rempli depuis PostHog (pas de clé PostHog dans l'outil).
- Planning : planning.json (créneaux de chaque compte + carrousels placés). Les carrousels viennent des
  dossiers fr/, en/, es/ de TiktokVocly ; un carrousel est « posté » quand noms-posts.json a son numéro.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path

import comptes

HERE = Path(__file__).resolve().parent
RACINE = HERE.parent  # TiktokVocly (absent dans la version partagée : pas de carrousels, rien ne casse)
INSTALLS = HERE / "installs-posthog.json"
PLANNING = HERE / "planning.json"
NOMS = HERE / "noms-posts.json"

DOSSIER = {"fr": "fr", "en": "en", "es": "es"}          # compte -> dossier des carrousels
PREFIXE = {"fr": "J", "en": "EN", "es": "ES"}          # « J76 · … », « EN 14 · … », « ES 02 · … »
CRENEAUX = {"fr": ["12:00", "19:30"], "en": ["20:30"], "es": ["21:00"]}  # modifiables dans la page
RECENT_J = 10  # un vieux dossier jamais relevé (anciens formats) n'est pas « prêt »

_lock = threading.Lock()
_cache: dict = {}


def _json(p: Path, defaut):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return defaut


def _runs(acc: str) -> list[Path]:
    out = comptes.sortie(acc)
    if not out.exists():
        return []
    return sorted(p for p in out.iterdir() if p.is_dir() and (p / "tout.json").exists())


def _lire_compte(acc: str) -> dict:
    """Vues par jour, posts (dernier état connu) et vues par heure, sur tous les relevés du compte."""
    runs = _runs(acc)
    cle = (acc, tuple((p.name, (p / "tout.json").stat().st_mtime) for p in runs[-3:]), len(runs))
    with _lock:
        if _cache.get(acc, {}).get("cle") == cle:
            return _cache[acc]["val"]
    jours: dict[str, int] = {}
    posts: dict[str, dict] = {}
    heures: dict[tuple, int] = {}
    for p in runs:
        d = _json(p / "tout.json", {})
        try:
            at = dt.datetime.fromisoformat(d["at"]).date()
        except Exception:
            continue
        for x in (d.get("compte") or {}).get("days", []):
            m = re.fullmatch(r"j-?(\d+)", str(x.get("jour", "")))
            if x.get("serie") == "vues" and m and x.get("valeur") is not None:
                jours[(at - dt.timedelta(days=int(m.group(1)) + 1)).isoformat()] = int(x["valeur"])
        for s in d.get("summary", []):
            posts[s["id"]] = {"id": s["id"], "poste_le": s.get("poste_le"), "vues": s.get("vues"),
                              "completion": s.get("completion_%"), "description": (s.get("description") or "")[:90]}
        for h in d.get("hours", []):
            if h.get("heure") and h.get("vues") is not None:
                heures[(h["id"], h["heure"])] = h["vues"]
    # jours pas encore donnés par TikTok : somme des vues heure par heure des posts (sous-estime un peu)
    estimes: dict[str, int] = {}
    dernier = max(jours) if jours else ""
    for (_, t), v in heures.items():
        j = t[:10]
        if j > dernier:
            estimes[j] = estimes.get(j, 0) + (v or 0)
    val = {"jours": jours, "estimes": estimes, "posts": posts}
    with _lock:
        _cache[acc] = {"cle": cle, "val": val}
    return val


def _noms() -> dict:
    return _json(NOMS, {})


def global_data() -> dict:
    noms = _noms()
    cs = [{k: c.get(k) for k in ("id", "nom", "court")} for c in comptes.liste()]
    par_jour: dict[str, dict] = {}
    posts = []
    for c in cs:
        d = _lire_compte(c["id"])
        for j, v in d["jours"].items():
            par_jour.setdefault(j, {"vues": {}, "estime": {}})["vues"][c["id"]] = v
        for j, v in d["estimes"].items():
            e = par_jour.setdefault(j, {"vues": {}, "estime": {}})
            if c["id"] not in e["vues"]:
                e["vues"][c["id"]] = v
                e["estime"][c["id"]] = True
        for p in d["posts"].values():
            posts.append({**p, "compte": c["id"], "nom": noms.get(p["id"])})
    jours = [{"jour": j, **v} for j, v in sorted(par_jour.items())]
    posts.sort(key=lambda p: p.get("poste_le") or "")
    return {"comptes": cs, "jours": jours, "installs": _json(INSTALLS, None), "posts": posts}


# ------------------------------------------------------------ planning
def _num(nom: str | None, acc: str) -> int | None:
    m = re.match(rf"^{PREFIXE.get(acc, '#')}\s*0*(\d+)\b", nom or "")
    return int(m.group(1)) if m else None


def _carrousels(acc: str, postes: dict[int, dict]) -> list[dict]:
    dossier = DOSSIER.get(acc)
    base = RACINE / dossier if dossier else None
    if not base or not base.is_dir():
        return []
    out, nums = [], set()
    maintenant = dt.datetime.now().timestamp()
    max_poste = max(postes) if postes else 0
    for f in base.iterdir():
        m = re.fullmatch(r"carrousel-0*(\d+)", f.name)
        if not m or not f.is_dir():
            continue
        n = int(m.group(1))
        pngs = sorted(f.glob("*.png"))
        info = f / "POST.json"
        if not pngs or not info.exists():
            continue
        nums.add(n)
        meta = _json(info, {})
        cree = info.stat().st_mtime
        poste = postes.get(n)
        if not poste and n <= max_poste and maintenant - cree > RECENT_J * 86400:
            continue  # vieux dossier jamais relevé (anciens formats)
        if poste and maintenant - dt.datetime.fromisoformat(poste["poste_le"].replace(" ", "T")).timestamp() > 3 * 86400:
            continue  # posté il y a longtemps : inutile dans le planning
        slug = " ".join(p.name for p in pngs[:1]).lower()
        out.append({"cle": f"{dossier}/{f.name}", "compte": acc, "num": n, "titre": meta.get("titre") or f.name,
                    "slides": len(pngs), "etat": "poste" if poste else "pret", "poste": poste,
                    "toeic": "toeic" in slug or "toeic" in (meta.get("caption") or "").lower(),
                    "cree": dt.datetime.fromtimestamp(cree).isoformat(timespec="minutes")})
    # prompts écrits mais pas encore rendus par Antigravity
    pr = base / "prompts"
    if pr.is_dir():
        suf = {"fr": "", "en": "-en", "es": "-es"}.get(acc, "")
        for f in pr.glob("antigravity-carrousel-*.md"):
            m = re.fullmatch(rf"antigravity-carrousel-0*(\d+){suf}(-[a-z0-9-]+)?\.md", f.name)
            if not m or re.search(r"non-utilise|ancien|v\d", f.name):
                continue
            n = int(m.group(1))
            if n in nums or n in postes or n <= max_poste:
                continue
            titre = (f.read_text("utf-8", errors="replace").splitlines() or [""])[0].lstrip("# ").strip()
            titre = re.sub(r"^Prompt Antigravity\s*:\s*", "", titre)
            out.append({"cle": f"{dossier}/prompt-{n}", "compte": acc, "num": n, "titre": titre[:120], "slides": 0,
                        "etat": "a_fabriquer", "poste": None, "toeic": "toeic" in titre.lower(),
                        "cree": dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")})
    out.sort(key=lambda c: c["num"])
    return out


def _plan() -> dict:
    p = _json(PLANNING, {})
    cr = {**CRENEAUX, **(p.get("creneaux") or {})}
    return {"creneaux": cr, "plan": [x for x in p.get("plan", []) if isinstance(x, dict)]}


def _ecrire(p: dict):
    tmp = PLANNING.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(PLANNING)


def planning_get() -> dict:
    noms = _noms()
    cs = [{k: c.get(k) for k in ("id", "nom", "court")} for c in comptes.liste()]
    p = _plan()
    cars, posts = [], []
    for c in cs:
        d = _lire_compte(c["id"])
        postes = {}
        for x in d["posts"].values():
            n = _num(noms.get(x["id"]), c["id"])
            if n is not None:
                postes[n] = {**x, "nom": noms.get(x["id"])}
            posts.append({**x, "compte": c["id"], "nom": noms.get(x["id"])})
        cars += _carrousels(c["id"], postes)
    # un prompt placé avant d'être fabriqué devient son carrousel dès qu'Antigravity a rendu le dossier
    rendus = {c["cle"] for c in cars}
    change = False
    for x in p["plan"]:
        m = re.fullmatch(r"(fr|en|es)/prompt-(\d+)", str(x.get("carrousel", "")))
        if m:
            cle = next((k for k in rendus if re.fullmatch(rf"{m.group(1)}/carrousel-0*{m.group(2)}", k)), None)
            if cle:
                x["carrousel"], change = cle, True
    if change:
        with _lock:
            _ecrire(p)
    faits = {c["cle"] for c in cars if c["etat"] == "poste"}
    plan = [x for x in p["plan"] if x.get("carrousel") not in faits]  # un carrousel posté sort du planning
    posts = [x for x in posts if x.get("poste_le") and x["poste_le"] >= (dt.date.today() - dt.timedelta(days=7)).isoformat()]
    return {"comptes": cs, "creneaux": p["creneaux"], "plan": plan, "carrousels": cars, "posts": posts}


def planning_post(body: dict) -> dict:
    with _lock:
        p = _plan()
        a = body.get("action")
        date, heure, acc, car = (str(body.get(k, "")) for k in ("date", "heure", "compte", "carrousel"))
        if a in ("placer", "retirer"):
            if not re.fullmatch(r"\d{4}-\d\d-\d\d", date) or not re.fullmatch(r"\d\d:\d\d", heure):
                return {"error": "Date ou heure invalide."}
            p["plan"] = [x for x in p["plan"] if not (x.get("date") == date and x.get("heure") == heure and x.get("compte") == acc)
                         and not (a == "placer" and x.get("carrousel") == car)]
            if a == "placer":
                if not re.fullmatch(r"(fr|en|es)/(carrousel|prompt)-\d+", car):
                    return {"error": "Carrousel inconnu."}
                p["plan"].append({"date": date, "heure": heure, "compte": acc, "carrousel": car})
        elif a == "creneaux":
            hs = sorted({h for h in body.get("heures", []) if re.fullmatch(r"\d\d:\d\d", str(h))})
            if not comptes.existe(acc) or not hs or len(hs) > 6:
                return {"error": "Créneaux invalides (1 à 6 heures)."}
            p["creneaux"][acc] = hs
        elif a == "remplir":  # proposition : prêts -> prochains créneaux libres
            p["plan"] = [x for x in p["plan"] if x.get("date", "") >= dt.date.today().isoformat()]
            for x in body.get("plan", []):
                if re.fullmatch(r"\d{4}-\d\d-\d\d", str(x.get("date"))) and re.fullmatch(r"(fr|en|es)/(carrousel|prompt)-\d+", str(x.get("carrousel"))):
                    p["plan"].append({k: str(x.get(k)) for k in ("date", "heure", "compte", "carrousel")})
        else:
            return {"error": "Action inconnue."}
        p["plan"].sort(key=lambda x: (x.get("date", ""), x.get("heure", "")))
        _ecrire(p)
    return {"ok": True}


def couverture(cle: str) -> Path | None:
    if not re.fullmatch(r"(fr|en|es)/carrousel-\d+", cle or ""):
        return None
    d = (RACINE / cle).resolve()
    if RACINE.resolve() not in d.parents or not d.is_dir():
        return None
    pngs = sorted(d.glob("*.png"))
    cov = [p for p in pngs if "couverture" in p.name] or pngs
    return cov[0] if cov else None
