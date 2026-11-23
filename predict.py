"""
Prédiction des vues d'un post TikTok à 48 h, à partir de ses premières heures.

    python predict.py              # test à l'aveugle : chaque post est prédit avec les posts sortis avant lui
    python predict.py --json ID    # ce que lit la page pour ce post (stats_server.py -> /api/prediction)
    python predict.py --compte en  # même chose pour un autre compte

Données : TOUS les relevés du compte (stats-out/ pour FR, stats-out-en/ pour EN...), fusionnés post par post en une seule courbe, à l'âge réel et croissante.
TikTok ne donne l'heure par heure que pendant ~48 h : chaque relevé fait dans ces 48 h est gardé.

Méthode (voir forecast) :
  1. La courbe du post : ses vues × la hausse typique des 6 derniers posts entre cet âge et 48 h, maillon par
     maillon (un post de 20 h sert déjà pour les maillons d'avant 20 h).
  2. Le niveau habituel : ses vues + ce qu'un post récent fait encore après cet âge.
  3. Avant 12 h on passe progressivement du 2 au 1 : au début, les premières heures disent peu de la fin.

Une 2e vague (vues au moins doublées entre 12 h et 48 h) ne se prévoit pas dans les premières heures :
elle est affichée à part, comme un scénario.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import comptes  # noqa: E402  (dossier de relevés de chaque compte)

OUT = comptes.sortie("fr")  # compte par défaut ; chaque fonction prend acc= pour les autres comptes


def out_dir(acc: str = "fr") -> Path:
    return comptes.sortie(acc if comptes.existe(acc) else "fr")
HORIZON = 48
MIN_AGE = 2  # avant 2 h (1re heure partielle), on n'affiche pas de chiffre
IMPUTE_MIN = 24  # un post de 24 à 48 h compte déjà dans le test : son reste jusqu'à 48 h est estimé (« ≈ »)
AGES = [2, 3, 4, 6, 9, 12, 18, 24]  # âges testés
LINKS = [2, 3, 4, 6, 9, 12, 18, 24, 36, 48]  # maillons de la hausse typique
LINK_POSTS = 6  # la hausse typique vient des 6 derniers posts
LEVEL_POSTS = 5  # le niveau habituel vient des 5 derniers posts
LEVEL_MIN_AGE = 6  # un post plus jeune ne dit pas encore son niveau
BLEND_UNTIL = 12  # à partir de 12 h, seule la courbe du post compte
ERR_POSTS = 8  # la fourchette vient des erreurs des 8 derniers posts testés, sans ceux à 2e vague
WAVE_RATIO = 2.0  # 2e vague : vues au moins doublées entre 12 h et 48 h
WAVE_MIN_AGE = 36  # plus jeune, on ne sait pas encore si le post en a eu une
WAVE_WINDOW = 8  # on compte les 2e vagues sur les 8 derniers posts assez vieux
WAVE_UNTIL = 24  # passé 24 h sans signe, on n'affiche plus le scénario 2e vague
WAVE_FROM_AGE = 8  # alerte « ça repart » : seulement après la 8e heure...
WAVE_PIC = 900  # ... quand une heure pleine dépasse 900 vues (J41 et J42 l'ont fait, aucun autre post) ; à remonter si le compte grossit

METRICS = {  # affichage seulement : ces métriques n'entrent pas dans le calcul
    "vues": ("Vues", ""),
    "completion": ("Complétion", "% qui vont au bout"),
    "favoris_k": ("Favoris", "pour 1 000 vues"),
    "abonnes_k": ("Abonnés gagnés", "pour 1 000 vues"),
}


# ---------------------------------------------------------------- lecture des relevés

def val(o):
    for _ in range(3):
        if isinstance(o, dict) and "value" in o:
            if o.get("status") not in (None, 0):
                return None
            o = o["value"]
        elif isinstance(o, dict) and o.get("status") not in (None, 0):
            return None
        else:
            break
    return o


def hist(o):
    if isinstance(o, dict) and isinstance(o.get("list"), list):
        return [(int(x["key"]), x.get("value")) for x in o["list"] if str(x.get("key", "")).isdigit()]
    return []


def snap_time(f: Path) -> float:
    """Heure du relevé : nom du dossier (2026-11-07_14h19), sinon date du fichier."""
    for p in (f.parent, f.parent.parent):
        m = re.fullmatch(r"(\d{4})-(\d\d)-(\d\d)_(\d\d)h(\d\d)(?:-auto)?", p.name)
        if m:
            return datetime(*map(int, m.groups())).timestamp()
    return f.stat().st_mtime


def load_raw(f: Path) -> dict | None:
    try:
        d = json.loads(f.read_text("utf-8"))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if "tabs" in d:  # ancien format : réponses captées onglet par onglet
        out: dict = {}
        for tab in (d.get("tabs") or {}).values():
            for r in (tab or {}).get("responses") or []:
                if "/data/insight/" in r.get("url", "") or "/analytics/insights" in r.get("url", ""):
                    for k, v in (r.get("json") or {}).items():
                        if v is None or k in ("extra", "log_pb", "status_code", "status_msg"):
                            continue
                        if isinstance(v, dict) and v.get("status") not in (None, 0) and out.get(k):
                            continue
                        out[k] = v
        return out
    return d


def data_stamp(acc: str = "fr") -> float:
    cf = cloud_file(acc)
    files = list(out_dir(acc).rglob("post-*.json")) + ([cf] if cf and cf.exists() else [])
    return max((f.stat().st_mtime for f in files), default=0)


# ---------------------------------------------------------------- relevé en ligne (Cloudflare, toutes les heures)
# Le robot tiktok-auto/cloud/worker.js lit chaque heure les compteurs publics (vues, likes, favoris, partages,
# commentaires) des posts de moins de 7 jours, même PC éteint. On en garde une copie locale, ajoutée au calcul.

CLOUD = os.environ.get("TIKTOK_STATS_CLOUD", "")  # vide = pas de robot
CLOUD_FILE = OUT / "cloud-snaps.jsonl"
CLOUD_STATE: dict = {"t": 0}


def cloud_file(acc: str = "fr") -> Path | None:
    """Le robot en ligne ne suit que le compte principal : pas de copie en ligne pour les autres comptes."""
    return CLOUD_FILE if acc == "fr" else None


def sync_cloud(min_gap: float = 300, acc: str = "fr"):
    """Récupère les nouveaux relevés en ligne et signale au robot les posts récents qu'il ne suit pas encore."""
    if not CLOUD or acc != "fr" or time.time() - CLOUD_STATE["t"] < min_gap:
        return
    CLOUD_STATE["t"] = time.time()
    import urllib.request
    try:
        have, last = set(), 0
        if CLOUD_FILE.exists():
            for line in CLOUD_FILE.read_text("utf-8").splitlines():
                try:
                    s = json.loads(line)
                except Exception:
                    continue
                have.add((s["id"], s["at"]))
                last = max(last, s["at"])
        req = urllib.request.Request(f"{CLOUD}/snaps?since={max(0, last - 7200)}", headers={"User-Agent": "vocly-stats"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        new = [s for s in data.get("snaps", []) if (s["id"], s["at"]) not in have]
        if new:
            with open(CLOUD_FILE, "a", encoding="utf-8") as f:
                for s in new:
                    f.write(json.dumps(s) + "\n")
        local = {f.stem[5:] for f in OUT.rglob("post-*.json") if re.fullmatch(r"post-\d{17,}\.json", f.name)}
        online = {p["id"] for p in data.get("posts", [])}
        missing = [i for i in local - online if time.time() - (int(i) >> 32) < 7 * 86400]
        if missing:
            urllib.request.urlopen(urllib.request.Request(f"{CLOUD}/ajouter?source=pc&id={','.join(sorted(missing))}",
                                                          headers={"User-Agent": "vocly-stats"}), timeout=60).read()
        CLOUD_STATE.update(runs=data.get("runs", [])[:6], err=None, new=len(new))
    except Exception as e:  # hors ligne : on continue avec la copie locale
        CLOUD_STATE.update(err=str(e)[:200])


def cloud_items(acc: str = "fr") -> list[dict]:
    out = []
    cf = cloud_file(acc)
    if not cf or not cf.exists():
        return out
    for line in cf.read_text("utf-8").splitlines():
        try:
            s = json.loads(line)
        except Exception:
            continue
        stats = {"play_count": s.get("vues"), "collect_count": s.get("favoris")}
        out.append({"pid": s["id"], "at": float(s["at"]), "mt": float(s["at"]),
                    "releve": "en-ligne-" + datetime.fromtimestamp(s["at"]).strftime("%Y-%m-%d_%Hh%M"),
                    "d": {"realtime_total_video_views": s.get("vues"), "video_info": {"statistics": stats}}})
    return out


KEEP = ["realtime_total_video_views", "realtime_video_view_history", "realtime_finish_rate_history",
        "realtime_new_followers_history", "video_vv_history_48_hours"]
_RAW: dict = {}  # fichier -> (date de modif, données utiles) : chaque fichier n'est lu qu'une fois


def scan(acc: str = "fr") -> list[dict]:
    """Tous les fichiers de relevé de post, avec l'heure et le nom du relevé dont ils viennent."""
    items = []
    OUT = out_dir(acc)
    for f in OUT.rglob("post-*.json"):
        if not re.fullmatch(r"post-\d{17,}\.json", f.name):
            continue
        mt = f.stat().st_mtime
        c = _RAW.get(f)
        if not c or c[0] != mt:
            d = load_raw(f)
            slim = None
            if d:
                slim = {k: d[k] for k in KEEP if k in d}
                slim["video_info"] = {"statistics": (d.get("video_info") or {}).get("statistics") or {}}
            _RAW[f] = c = (mt, slim)
        if c[1] is None:
            continue
        folder = f.parent.parent if f.parent.name == "raw" else f.parent
        items.append({"pid": f.stem[5:], "at": snap_time(f), "mt": mt, "releve": folder.name if folder != OUT else "racine", "d": c[1]})
    return items + cloud_items(acc)


# ---------------------------------------------------------------- la courbe d'un post

def quant(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    i = q * (len(xs) - 1)
    lo, hi = math.floor(i), math.ceil(i)
    return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def interp(pts: list, a: float) -> float | None:
    """Valeur à l'âge a sur une courbe [(âge, vues)] d'âges croissants. None au-delà de ce qu'on connaît."""
    if not pts or a < 0 or a > pts[-1][0] + 1e-6:
        return None
    i = bisect.bisect_left(pts, (a,))
    if i == 0:
        return pts[0][1]
    if i == len(pts):
        return pts[-1][1]
    (x0, y0), (x1, y1) = pts[i - 1], pts[i]
    return y0 + (y1 - y0) * (a - x0) / (x1 - x0)


def real_age_points(t0: float, buckets: list, age: float, total: float | None) -> list:
    """[(âge réel, vues)] depuis des cumuls par heure d'horloge [(début de l'heure, cumul)].
    Un post sorti à 11h45 n'a que 15 min dans sa 1re heure : « 3 heures d'horloge » n'y font que 2 h 15."""
    if not buckets:
        return []
    pts = [(0.0, 0.0)]
    for start, cum in buckets:
        x = (start + 3600 - t0) / 3600
        if x > age:  # heure en cours, incomplète : le total du relevé est plus juste
            break
        if x > 0:
            pts.append((x, max(cum, pts[-1][1])))
    if age > pts[-1][0]:
        pts.append((age, max(pts[-1][1], buckets[-1][1], total or 0)))
    return pts


def add_real_age(d: dict) -> dict:
    """Pour la page : vues à 1/3/6/12/24/48 h d'âge réel (les relevés stockent des heures d'horloge) et courbe de chaque post."""
    by: dict = {}
    for h in d.get("hours", []):
        by.setdefault(h["id"], []).append(h)
    for p in d.get("summary", []):
        hours = sorted(by.get(p["id"], []), key=lambda h: h["heure"])
        buckets = [(datetime.strptime(h["heure"], "%Y-%m-%d %H:%M").timestamp(), h["cumul"]) for h in hours]
        pts = real_age_points(int(p["id"]) >> 32, buckets, p.get("age_h") or 0, p.get("vues"))
        p["courbe"] = [[round(x, 2), round(y)] for x, y in pts]
        for h in (1, 3, 6, 12, 24, 48):
            v = interp(pts, h)
            p[f"vues_{h}h"] = None if v is None else round(v)
    return d


def curve(rows: list, snaps: list) -> list:
    """Une seule courbe par post, toujours croissante : les heures terminées de TikTok, puis les totaux relevés
    depuis (dans l'heure par heure, l'heure en cours est incomplète)."""
    done = [(r["end_age"], r["cumul"]) for r in rows if r["complete"]]
    last = done[-1][0] if done else 0.0
    tail = [(s["age"], s["vues"]) for s in snaps if s["age"] > last]
    if not tail:
        done += [(r["end_age"], r["cumul"]) for r in rows if not r["complete"] and r["end_age"] > last]
    # La somme des heures dépasse le total affiché de 1 à 2 % : on la recale pour que la courbe finisse sur le vrai total.
    base = [(0.0, 0.0)] + done
    ratios = [s["vues"] / y for s in snaps if (y := interp(base, s["age"])) and y >= 200]
    k = quant(ratios[-5:], 0.5) if ratios else 1.0
    pts, top = [(0.0, 0.0)], 0.0
    for x, y in [(x, y * k) for x, y in done] + tail:
        top = max(top, y)
        if x > pts[-1][0]:
            pts.append((x, top))
        else:
            pts[-1] = (pts[-1][0], top)
    return pts


def load_posts(until: float | None = None, items: list | None = None) -> dict:
    """{id: {t0, snaps, rows, pts}} fusionné sur tous les relevés (ou seulement ceux faits avant `until`)."""
    posts: dict = {}
    for it in (items if items is not None else scan()):
        if until is not None and it["at"] > until:
            continue
        pid, d, at = it["pid"], it["d"], it["at"]
        t0 = int(pid) >> 32
        P = posts.setdefault(pid, {"id": pid, "t0": t0, "snaps": [], "hours": {}, "src": {}})
        st = (d.get("video_info") or {}).get("statistics") or {}
        views = val(d.get("realtime_total_video_views")) or st.get("play_count")
        if views:
            P["snaps"].append({"at": at, "age": (at - t0) / 3600, "vues": views, "favoris": st.get("collect_count")})
        rt = d.get("realtime_video_view_history") or {}
        series = {}
        if rt.get("interval") == 1:
            series = {"vues": dict(hist(rt)), "completion": dict(hist(d.get("realtime_finish_rate_history"))),
                      "abonnes": dict(hist(d.get("realtime_new_followers_history")))}
        elif isinstance(d.get("video_vv_history_48_hours"), list):
            b = t0 - t0 % 3600
            series = {"vues": {b + 3600 * i: val(x) for i, x in enumerate(d["video_vv_history_48_hours"])
                               if val(x) is not None}}
        for field, s in series.items():
            for ts, v in s.items():
                if v is None:
                    continue
                key = (ts + 3600 <= at, at)  # heure terminée d'abord, puis relevé le plus récent
                if (ts, field) not in P["src"] or key > P["src"][(ts, field)]:
                    P["hours"].setdefault(ts, {})[field] = v
                    P["src"][(ts, field)] = key
    for P in posts.values():
        P["snaps"].sort(key=lambda s: s["at"])
        b = P["t0"] - P["t0"] % 3600
        rows, cum = [], 0
        for ts in sorted(P["hours"]):
            if ts < b:
                continue
            srcs = [k for (t, _), k in P["src"].items() if t == ts]
            cum += P["hours"][ts].get("vues") or 0
            complete = all(c for c, _ in srcs)
            end = ts + 3600 if complete else min(ts + 3600, max(a for _, a in srcs))
            rows.append({"ts": ts, "end_age": (end - P["t0"]) / 3600, "cumul": cum, "complete": complete, **P["hours"][ts]})
        P["rows"] = rows
        P["pts"] = curve(rows, P["snaps"])
        P["last_at"] = P["t0"] + P["pts"][-1][0] * 3600
        del P["hours"], P["src"]
    return {k: P for k, P in posts.items() if len(P["rows"]) >= 2}


def names() -> dict:
    try:
        return json.loads((HERE / "noms-posts.json").read_text("utf-8"))
    except Exception:
        return {}


def age_now(P) -> float:
    return P["pts"][-1][0]


def views_at(P, a: float) -> float | None:
    return interp(P["pts"], a)


# ---------------------------------------------------------------- métriques (affichage seulement)

def wavg(rows, field):
    rs = [r for r in rows if r.get(field) is not None]
    den = sum(r.get("vues") or 0 for r in rs)
    return sum((r.get("vues") or 0) * r[field] for r in rs) / den if den >= 30 else None


def snap_at(P, a: float, field: str):
    """(compteur, vues) à l'âge a si deux relevés proches l'encadrent, ou un relevé à ±30 % d'âge."""
    S = [(s["age"], s[field], s["vues"]) for s in P["snaps"] if s.get(field) is not None]
    for (x0, y0, v0), (x1, y1, v1) in zip(S, S[1:]):
        if x0 <= a <= x1 and x1 - x0 <= max(6.0, a):
            t = (a - x0) / (x1 - x0) if x1 > x0 else 0
            return y0 + (y1 - y0) * t, v0 + (v1 - v0) * t
    near = [s for s in S if abs(s[0] - a) <= max(0.5, 0.3 * a)]
    if near:
        s = min(near, key=lambda s: abs(s[0] - a))
        return s[1], s[2]
    return None


def features(P, a: float) -> dict:
    """Ce qu'on sait d'un post à a heures, et rien de ce qui vient après."""
    V = views_at(P, a)
    if not V:
        return {}
    rows = [r for r in P["rows"] if r["end_age"] <= a + 0.25]
    f = {"vues": V, "completion": wavg(rows, "completion")}
    ab = [r for r in rows if r.get("abonnes") is not None]
    if sum(r.get("vues") or 0 for r in ab) >= 30:
        f["abonnes_k"] = 1000 * sum(r["abonnes"] for r in ab) / sum(r.get("vues") or 0 for r in ab)
    s = snap_at(P, a, "favoris")
    if s and s[1]:
        f["favoris_k"] = 1000 * s[0] / s[1]
    return {k: v for k, v in f.items() if v is not None}


def why_table(T, order, a):
    """Les métriques du post face aux 6 derniers posts au même âge."""
    others = [R for R in order if R["t0"] < T["t0"] and age_now(R) >= a][-LINK_POSTS:]
    fT = features(T, a)
    out = []
    for k, (label, unit) in METRICS.items():
        vs = sorted(v for R in others if (v := features(R, a).get(k)) is not None)
        x = fT.get(k)
        row = {"k": k, "label": label, "unit": unit, "v": x, "n": len(vs)}
        if vs:
            row["med"] = quant(vs, 0.5)
            if x is not None:
                row.update(rank=1 + sum(1 for v in vs if v > x), of=len(vs) + 1)
        out.append(row)
    return out


# ---------------------------------------------------------------- la méthode

def ordered(posts: dict) -> list:
    return sorted(posts.values(), key=lambda P: P["t0"])


def known_age(R, T, a: float) -> float:
    """L'âge qu'avait R quand T avait a heures : on ne regarde jamais plus loin."""
    return min(age_now(R), a + (T["t0"] - R["t0"]) / 3600)


def chain(order, T, a: float, end: float = HORIZON) -> float | None:
    """log de la hausse typique de l'âge a à `end`, maillon par maillon (médiane des 6 derniers posts qui ont atteint le maillon)."""
    if a >= end:
        return 0.0
    refs = [R for R in order if R["t0"] < T["t0"]]
    grid = [a] + [g for g in LINKS if a < g < end] + [end]
    total = 0.0
    for x, y in zip(grid, grid[1:]):
        L = [math.log(views_at(R, y) / views_at(R, x)) for R in refs if known_age(R, T, a) >= y and views_at(R, x)]
        if len(L) < 2:
            return None
        total += quant(L[-LINK_POSTS:], 0.5)
    return total


def level(order, T, a: float) -> float | None:
    """Vues à 48 h typiques des 5 derniers posts, avec seulement ce qu'on savait à ce moment-là."""
    vals = []
    for R in [R for R in order if R["t0"] < T["t0"]][-LEVEL_POSTS:]:
        ka = known_age(R, T, a)
        if ka >= HORIZON:
            vals.append(math.log(views_at(R, HORIZON)))
        elif ka >= LEVEL_MIN_AGE and views_at(R, ka):
            c = chain(order, R, ka)
            if c is not None:
                vals.append(math.log(views_at(R, ka)) + c)
    return math.exp(quant(vals, 0.5)) if len(vals) >= 2 else None


def forecast(order, T, a: float) -> dict | None:
    """Prévision à 48 h de T à l'âge a, avec seulement les posts sortis avant lui."""
    v = views_at(T, a)
    c = chain(order, T, a) if v else None
    if c is None:
        return None
    from_curve = v * math.exp(c)
    lv = level(order, T, a)
    w = min(1.0, a / BLEND_UNTIL) ** 2  # 3 % à 2 h, 25 % à 6 h, 100 % à 12 h
    central = from_curve
    if lv is not None:
        from_level = v + lv * (1 - math.exp(-c))  # déjà fait + ce qu'il reste à faire à un post habituel
        central = math.exp(w * math.log(from_curve) + (1 - w) * math.log(from_level))
    return {"a": a, "vues": v, "central": max(v, central), "niveau": lv, "c": c}


def truths(order) -> dict:
    """{id: (vues à 48 h, estimée ?)}."""
    out = {}
    for P in order:
        v = views_at(P, HORIZON)
        if v:
            out[P["id"]] = (v, False)
        elif age_now(P) >= IMPUTE_MIN and (c := chain(order, P, age_now(P))) is not None:
            out[P["id"]] = (views_at(P, age_now(P)) * math.exp(c), True)
    return out


def is_wave(P, v48: float) -> bool:
    v12 = views_at(P, 12)
    return bool(v12 and v48 / v12 >= WAVE_RATIO)


def waves(order) -> dict:
    """Combien des derniers posts ont eu une 2e vague, et combien de vues elle leur a rapportées après 12 h."""
    L = [(P, views_at(P, min(age_now(P), HORIZON)), views_at(P, 12))
         for P in order if age_now(P) >= WAVE_MIN_AGE and views_at(P, 12)]
    yes = [(P, v - v12) for P, v, v12 in L if v / v12 >= WAVE_RATIO]
    recents = L[-WAVE_WINDOW:]
    return {"nb": sum(1 for _, v, v12 in recents if v / v12 >= WAVE_RATIO), "sur": len(recents),
            "posts": [P for P, _ in yes],
            "bonus": math.exp(sum(math.log(g) for _, g in yes) / len(yes)) if yes else None}


def evaluate(posts) -> dict:
    """Test à l'aveugle : chaque post est prédit à chaque âge avec seulement les posts sortis avant lui.
    Les fourchettes viennent de ces erreurs réelles, sur les derniers posts sans 2e vague."""
    order = ordered(posts)
    real = truths(order)
    res: dict = {a: [] for a in AGES}
    for T in order:
        if T["id"] not in real:
            continue
        tot, approx = real[T["id"]]
        wave = is_wave(T, tot)
        for a in AGES:
            if a > age_now(T) - (6 if approx else 0):  # si sa cible est estimée, le test doit garder une marge
                continue
            f = forecast(order, T, a)
            if f:
                res[a].append({"id": T["id"], "pred": f["central"], "err": math.log(f["central"] / tot), "vague": wave})
    band = {}
    for a, L in res.items():
        e = [abs(x["err"]) for x in L if not x["vague"]][-ERR_POSTS:]
        if len(e) >= 4:  # erreur médiane : la moitié des posts tombe dans ± cette erreur
            band[a] = {"k": max(1.05, math.exp(quant(e, 0.5))), "n": len(e)}
    return {"order": order, "reel": real, "res": res, "band": band, "vagues": waves(order)}


def wave_alert(T):
    """Une 2e vague démarre : après la 8e heure, une heure pleine au-dessus de 900 vues. Indicatif, 2 exemples seulement."""
    pic = max((r.get("vues") or 0 for r in T["rows"] if r["complete"] and r["end_age"] > WAVE_FROM_AGE), default=0)
    return {"pic": pic} if pic >= WAVE_PIC else None


def predict_post(T, ev) -> dict | None:
    """Prévision à 48 h d'un post de 2 à 48 h, avec sa fourchette et le scénario 2e vague."""
    a = age_now(T)
    if not (MIN_AGE <= a < HORIZON):
        return None
    f = forecast(ev["order"], T, a)
    if not f:
        return None
    ab = max([x for x in ev["band"] if x <= a] or [min(ev["band"], default=AGES[0])])
    b = ev["band"].get(ab, {"k": 2.0, "n": 0})
    alert = wave_alert(T)
    bonus = ev["vagues"]["bonus"]
    wave = f["central"] + bonus if bonus and (a < WAVE_UNTIL or alert) else None
    return {**f, "age_test": ab, "k": b["k"], "n_test": b["n"], "bas": max(f["vues"], f["central"] / b["k"]),
            "haut": f["central"] * b["k"], "vague": wave, "alerte": alert}


def after48(order) -> dict | None:
    """Ce que les derniers posts assez vieux ont encore pris entre 48 h et 3 jours."""
    L = [views_at(P, 72) / views_at(P, HORIZON) for P in order if age_now(P) >= 72 and views_at(P, HORIZON)][-LEVEL_POSTS:]
    return {"n": len(L), "ratio": quant(L, 0.5)} if L else None


# ---------------------------------------------------------------- journal des prédictions
# Une ligne par post et par relevé : ce que l'outil prévoyait à ce moment-là. Quand le post atteint 48 h, on compare.
# Le fichier garde aussi les prédictions des anciennes méthodes (version < MODEL_VERSION) : elles ne comptent plus.

JOURNAL = OUT / "journal-predictions.jsonl"  # compte FR (journal_file(acc) pour les autres)
MODEL_VERSION = 3
_LOCK = threading.Lock()
JOURNAL_AGES = [(2, 6), (6, 12), (12, 24), (24, 48)]


def journal_file(acc: str = "fr") -> Path:
    return out_dir(acc) / "journal-predictions.jsonl"


def read_journal(acc: str = "fr") -> list[dict]:
    out = []
    try:
        for line in journal_file(acc).read_text("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    except FileNotFoundError:
        pass
    return out


def update_journal(items, acc: str = "fr") -> list[dict]:
    OUT = out_dir(acc)
    J = read_journal(acc)
    done = {e["releve"] for e in J if e.get("type") == "releve"}
    groups: dict = {}
    for it in items:
        g = groups.setdefault(it["releve"], {"releve": it["releve"], "at": 0, "pids": set(), "mt": 0})
        g["at"] = max(g["at"], it["at"])
        g["mt"] = max(g["mt"], it["mt"])
        g["pids"].add(it["pid"])
    now = time.time()
    new = []
    for g in sorted(groups.values(), key=lambda g: g["at"]):
        writing = not (OUT / g["releve"] / "tout.json").exists() and now - g["mt"] < 600
        if g["releve"] in done or writing:  # relevé en cours d'écriture (tout.json arrive à la fin) : on attend
            continue
        replay = now - g["at"] > 1800
        posts = load_posts(until=g["at"] + 1, items=items)
        ev = evaluate(posts)
        for pid in sorted(g["pids"]):
            T = posts.get(pid)
            pr = predict_post(T, ev) if T else None
            if pr:
                new.append({"type": "prediction", "id": pid, "releve": g["releve"], "at": T["last_at"], "age": pr["a"],
                            "vues": pr["vues"], "central": pr["central"], "bas": pr["bas"], "haut": pr["haut"],
                            "vague": pr["vague"], "rejoue": replay, "version": MODEL_VERSION, "ecrit": now})
        new.append({"type": "releve", "releve": g["releve"], "at": g["at"], "rejoue": replay, "ecrit": now})
    if new:
        with open(journal_file(acc), "a", encoding="utf-8") as f:
            for e in new:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return [e for e in J + new if e.get("type") == "prediction"]


def journal_view(J, posts) -> dict:
    """Erreur par tranche d'âge, avec une seule voix par post (55 lignes sur 2 posts ne font pas 55 preuves)."""
    checked: dict = {}
    waiting = set()
    for e in J:
        if e.get("version", 0) < MODEL_VERSION:
            continue
        P = posts.get(e["id"])
        real = views_at(P, HORIZON) if P else None
        if real:
            checked.setdefault(e["id"], []).append((e["age"], abs(math.log(e["central"] / real))))
        else:
            waiting.add(e["id"])
    rows = []
    for lo, hi in JOURNAL_AGES:
        errs = []
        for L in checked.values():
            here = [x for age, x in L if lo <= age < hi]
            if here:
                errs.append(sum(here) / len(here))
        rows.append({"de": lo, "a": hi, "posts": len(errs), "erreur": math.exp(quant(errs, 0.5)) if errs else None})
    return {"posts_verifies": len(checked), "attente": len(waiting), "tranches": rows}


# ---------------------------------------------------------------- ce que lit la page

_CACHES: dict = {}  # compte -> dernier calcul


def model(acc: str = "fr"):
    acc = acc if comptes.existe(acc) else "fr"
    with _LOCK:
        sync_cloud(acc=acc)
        stamp = data_stamp(acc)
        C = _CACHES.setdefault(acc, {})
        if C.get("stamp") != stamp:
            items = scan(acc)
            J = update_journal(items, acc)
            posts = load_posts(items=items)
            C.update(stamp=stamp, posts=posts, journal=J, ev=evaluate(posts))
        return C["posts"], C["ev"], C["journal"]


def prediction(pid: str | None, acc: str = "fr"):
    posts, ev, J = model(acc)
    N = names()
    nm = lambda P: N.get(P["id"], "") or datetime.fromtimestamp(P["t0"]).strftime("%d/%m %Hh%M")
    order = ev["order"]
    test = []
    for T in reversed(order):
        if T["id"] not in ev["reel"]:
            continue
        tot, approx = ev["reel"][T["id"]]
        at = {a: {"pred": x["pred"], "err": x["err"]} for a in AGES for x in ev["res"][a] if x["id"] == T["id"]}
        if at:
            test.append({"id": T["id"], "nom": nm(T), "reel": tot, "approx": approx, "vague": is_wave(T, tot), "at": at})
    wv = ev["vagues"]
    out = {
        "horizon": HORIZON, "fiabilite": ev["band"], "test": test[:ERR_POSTS],
        "vagues": {"nb": wv["nb"], "sur": wv["sur"], "noms": [nm(P).split(" · ")[0] for P in wv["posts"]]},
        "apres48": after48(order), "journal": journal_view(J, posts),
        "en_ligne": {"url": CLOUD if acc == "fr" else "", "derniers": (CLOUD_STATE.get("runs") or []) if acc == "fr" else [],
                     "erreur": CLOUD_STATE.get("err") if acc == "fr" else None},
    }
    T = posts.get(pid or "")
    if not T:
        return out
    a = age_now(T)
    out["post"] = {"id": T["id"], "nom": nm(T), "age": a, "vues": views_at(T, a)}
    out["pourquoi"] = why_table(T, order, min(a, HORIZON))
    if a >= HORIZON:
        out["post"].update(fini=True, reel=views_at(T, HORIZON))
        return out
    if a < MIN_AGE:
        out["post"]["trop_tot"] = True
        return out
    pr = predict_post(T, ev)
    if not pr:
        return out
    v, c = pr["vues"], pr["c"]
    path, share = [], 0.0
    for t in [a] + list(range(math.ceil(a + 0.01), HORIZON + 1)):
        if t > a and c > 0:
            share = max(share, min(1.0, (chain(order, T, a, t) or 0.0) / c))
        path.append([t] + [v * (pr[k] / v) ** (share if c > 0 else 1.0) for k in ("central", "bas", "haut")])
    refs = [R for R in order if R["t0"] < T["t0"]][-LEVEL_POSTS:]
    out["prevision"] = {
        "central": pr["central"], "bas": pr["bas"], "haut": pr["haut"], "vague": pr["vague"], "alerte": pr["alerte"],
        "k": pr["k"], "n_test": pr["n_test"], "age_test": pr["age_test"], "vues": v, "niveau": pr["niveau"],
        "chemin": path,
        "courbe": [[t, views_at(T, t)] for t in range(1, math.floor(a) + 1)] + [[a, v]],
        "voisins": [{"nom": nm(R).split(" · ")[0], "courbe": [[t, views_at(R, t)] for t in range(1, HORIZON + 1)
                                                              if t <= known_age(R, T, a)]} for R in refs],
    }
    return out


# ---------------------------------------------------------------- compte calme ou chaud (rapport A11, 17 nov 2026)
# Sur 28 escaliers FR + EN : quand les AUTRES posts du compte avaient fait >= 30 000 vues dans les 24 h avant la
# publication, 0 post sur 12 a percé (>= 25 K à 48 h) ; compte calme, 7 sur 16. La vague d'un compte ne gêne pas
# les autres comptes. Seuil mesuré sur nos comptes (plancher ~10 K par post) : à revoir pour un compte d'une autre taille.

SEUIL_CHAUD = 30000
_HEAT: dict = {}  # compte -> (date des données, posts)


def chaleur(acc: str = "fr") -> dict:
    """Vues faites par tous les posts du compte sur les 24 dernières heures connues, et quand il redeviendrait calme."""
    acc = acc if comptes.existe(acc) else "fr"
    with _LOCK:
        sync_cloud(acc=acc)
        stamp = data_stamp(acc)
        c = _HEAT.get(acc)
        if not c or c[0] != stamp:
            c = _HEAT[acc] = (stamp, load_posts(items=scan(acc)))
        posts = c[1]
    out = {"compte": acc, "seuil": SEUIL_CHAUD}
    if not posts:
        return {**out, "etat": "inconnu"}
    ref = max(P["last_at"] for P in posts.values())  # heure des données les plus récentes

    def gained(P, t1, t2):  # vues gagnées entre deux heures (avant le post : 0 ; après ses données : plus rien)
        v = lambda t: 0.0 if t <= P["t0"] else views_at(P, min((t - P["t0"]) / 3600, age_now(P))) or 0.0
        return max(0.0, v(t2) - v(t1))

    N = names()
    per = sorted(((gained(P, ref - 86400, ref), P) for P in posts.values()), key=lambda x: -x[0])
    total = sum(g for g, _ in per)
    rythme = sum(gained(P, ref - 7200, ref) for P in posts.values()) / 2  # vues par heure, sur les 2 dernières heures
    au_plus_tot = None
    if total >= SEUIL_CHAUD:  # même si plus aucune vue n'arrivait, les 24 h glissantes ne passent sous le seuil qu'à cette heure
        for k in range(1, 25):
            if sum(gained(P, ref + 3600 * k - 86400, ref) for P in posts.values()) < SEUIL_CHAUD:
                au_plus_tot = ref + 3600 * k
                break
    age = (time.time() - ref) / 3600
    etat = "inconnu" if age > 24 else "chaud" if total >= SEUIL_CHAUD else "calme"
    return {**out, "etat": etat, "vues24": round(total), "rythme_h": round(rythme), "rythme_max": round(SEUIL_CHAUD / 24),
            "ref": ref, "age_donnees_h": round(age, 1), "au_plus_tot": au_plus_tot,
            "top": [{"id": P["id"], "nom": N.get(P["id"], "") or datetime.fromtimestamp(P["t0"]).strftime("%d/%m %Hh%M"),
                     "vues24": round(g)} for g, P in per[:2] if g >= 1000]}


# ---------------------------------------------------------------- rapport console

def report(acc: str = "fr"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    N = names()
    posts, ev, J = model(acc)
    print("Test à l'aveugle : prévu / réel à 48 h, chaque post prédit avec les posts sortis avant lui (≈ : cible estimée)")
    print("post".ljust(24) + "réel".rjust(9) + "".join(f"{a:>7}h" for a in AGES))
    for T in ev["order"]:
        if T["id"] not in ev["reel"]:
            continue
        tot, approx = ev["reel"][T["id"]]
        cells = "".join(f"{math.exp(x['err']):>8.2f}" if (x := next((x for x in ev["res"][a] if x["id"] == T["id"]), None)) else "       -"
                        for a in AGES)
        print((N.get(T["id"], T["id"])[:20] + (" *" if is_wave(T, tot) else "")).ljust(24) + (("≈" if approx else "") + f"{tot:.0f}").rjust(9) + cells)
    print("\n* : 2e vague, exclue de la fourchette")
    print("Erreur habituelle (×) par âge :", {a: round(b["k"], 2) for a, b in ev["band"].items()})
    wv = ev["vagues"]
    print(f"2e vagues : {wv['nb']} des {wv['sur']} derniers posts, environ +{wv['bonus'] or 0:.0f} vues après 12 h")


if __name__ == "__main__":
    ACC = sys.argv[sys.argv.index("--compte") + 1] if "--compte" in sys.argv[:-1] else "fr"
    if "--json" in sys.argv:
        sys.stdout.reconfigure(encoding="utf-8")
        i = sys.argv.index("--json")
        pid = sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("--") else None
        print(json.dumps(prediction(pid, ACC), ensure_ascii=False, indent=1, default=str)[:6000])
    else:
        report(ACC)
