"""
Relève TOUTES les stats TikTok Studio du compte connecté, en lecture seule.

    python scrap_stats.py                 # les 6 derniers posts + stats du compte
    python scrap_stats.py --last 15       # les 15 derniers
    python scrap_stats.py --all           # tous les posts
    python scrap_stats.py --ids <id1>,<id2>
    python scrap_stats.py --no-compte     # sans les stats du compte (plus rapide)
    python scrap_stats.py --headed        # Chrome visible (si TikTok demande une vérification)

Utilise le profil Chrome déjà connecté de tiktok-auto (%LOCALAPPDATA%\\vocly-tiktok-chrome).
Ne publie rien, ne clique sur rien d'autre que des onglets d'analyse.
Si « Lock file can not be created » : un autre script utilise le profil, attends qu'il finisse.

Sortie : stats-out/<AAAA-MM-JJ_HHhMM>/
    resume.csv / resume.json   1 ligne par post (vues, complétion, ‰ likes/favoris, vues à 1h/3h/12h/24h/48h...)
    vues-par-heure.csv         courbe horaire de chaque post (vues, cumul, complétion, temps moyen, abonnés)
    retention-slides.csv       rétention slide par slide (dispo ~36 h après le post)
    audience.csv               âge, sexe, pays, sources de trafic par post
    compte-jours.csv           historiques du compte jour par jour (vues, vues profil, abonnés, likes...; j-0 = dernier jour dispo)
    compte-heures-actives.csv  heures où tes abonnés / spectateurs sont actifs (pour choisir l'heure de post)
    compte-audience.csv        âge, sexe, pays des abonnés et spectateurs, sources de trafic, recherches qui mènent au compte
    raw/                       réponses JSON brutes de TikTok, pour creuser plus tard
La 1re « heure » d'un post est l'heure civile où il est sorti (partielle : posté à 11h45 -> 11h00-12h00).
Prérequis : pip install playwright   (Chrome installé ; pas besoin de « playwright install »)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, Error as PWError
except ImportError:
    sys.exit("Il manque Playwright : pip install playwright")

from predict import interp, real_age_points

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Un profil Chrome et un dossier de relevés par compte (comptes.py) : fr = @vocly.app (historique), en = @vocly.en.
import comptes  # noqa: E402
# Version partagée : le lanceur fixe aussi le navigateur et le message de connexion.
NAVIGATEUR = os.environ.get("TIKTOK_STATS_NAVIGATEUR", "chrome") or None  # vide = Chromium de Playwright
CONNEXION = os.environ.get("TIKTOK_STATS_CONNEXION", "lance « node login.mjs » dans tiktok-auto")
PROFILE = comptes.profil("fr")
STUDIO = "https://www.tiktok.com/tiktokstudio"
HERE = Path(__file__).resolve().parent

# Ce que la page d'analyse d'un post demande à TikTok (relevé dans Studio, nov. 2026) + historiques 7 j.
POST_TYPES = [
    "video_info", "item_search_terms", "video_traffic_source_percent_realtime", "video_retention_rate_realtime",
    "video_view_realtime", "video_total_duration_realtime", "video_per_duration_realtime",
    "video_finish_rate_realtime", "video_new_follower_realtime", "photo_viewed", "video_new_followers",
    "photo_retention_rate_history_7d", "video_vv_history_7d", "video_vv_history_48_hours",
]
VIEWER_TYPES = [
    "video_uv", "video_uv_before", "video_viewer_return_viewer_percent", "video_viewer_new_viewer_percent",
    "video_viewer_follower_percent", "video_viewer_non_follower_percent", "video_viewer_follower_percent_realtime",
    "video_viewer_nonfollower_percent_realtime", "video_viewer_age_percent_realtime",
    "video_viewer_gender_percent_realtime", "video_viewer_location_percent_realtime",
]
EXTRA_TYPES = [  # historiques d'engagement 7 j (renvoient null quand TikTok ne les a pas encore)
    "video_like_history_7d", "video_comment_history_7d", "video_shares_history_7d",
    "video_favorites_history_7d", "video_new_followers_history_7d", "video_finish_rate_history_7d",
]
POST_GROUPS = (POST_TYPES, VIEWER_TYPES,
               ["video_info", {"insigh_type": "unique_viewer_num", "range": 1}, {"insigh_type": "follower_num"}], EXTRA_TYPES)
_16J = {"days": 16, "end_days": 1}
# Ce que les onglets d'analyse du compte demandent à TikTok (relevé dans Studio, nov. 2026), dans le même ordre.
ACCOUNT_REQUESTS = [
    [{"insigh_type": "incentive_vv", "prevCycleStartDays": 16, **_16J}],
    [{"insigh_type": t, **_16J} for t in ("vv_history", "pv_history", "like_history", "comment_history", "share_history",
                                          "follower_num_history", "reached_audience_history")],
    [{"insigh_type": "vv_traffic_source", "days": 7, "end_days": 1}],
    [{"insigh_type": "user_search_terms", "range": 1}],
    [{"insigh_type": "unique_viewer_num", "range": 1}, {"insigh_type": "follower_num"}],
    [{"insigh_type": "top_items", "range": 1, "filter": 1}],
    [{"insigh_type": "unique_viewer", "end_days": 1, "days": 7}, {"insigh_type": "unique_viewer_num", "range": 1},
     {"insigh_type": "unique_viewer_num_before", "range": 1}, {"insigh_type": "returning_viewer", "end_days": 1, "days": 7},
     {"insigh_type": "new_viewer", "end_days": 1, "days": 7}, {"insigh_type": "new_viewer_num", "range": 1},
     {"insigh_type": "new_viewer_num_before", "range": 1}, {"insigh_type": "viewer_active_history_days", "days": 7, "end_days": 1},
     {"insigh_type": "viewer_active_history_hours", "days": 8, "end_days": 1}, {"insigh_type": "viewer_gender_percent", "range": 1},
     {"insigh_type": "top10_other_creators_recommendation"}, {"insigh_type": "top10_other_videos_recommendation"},
     {"insigh_type": "viewer_age_distribution", "range": 1}, {"insigh_type": "viewer_country_city_percent", "range": 1}],
    [{"insigh_type": "follower_active_history_hours", "days": 8, "end_days": 1},
     {"insigh_type": "follower_active_history_days", "days": 8, "end_days": 1}],
    [{"insigh_type": "follower_num_history", **_16J}, {"insigh_type": "follower_num", **_16J},
     {"insigh_type": "net_follower_history", **_16J}],
    [{"insigh_type": "follower_gender_percent", "range": 1}],
    [{"insigh_type": "follower_age_distribution", "range": 1}, {"insigh_type": "follower_num"}],
    [{"insigh_type": "follower_location_percent", "range": 1}],
]
MAX_PARALLEL = 4  # appels à l'API en même temps : pas plus que la page Studio quand elle se charge
POSTS_PER_BATCH = 6  # pour « 25 derniers » ou « tous », une pause d'une seconde entre chaque paquet
FETCH_ALL = """async ([urls, n]) => {
  const out = new Array(urls.length);
  let next = 0;
  const work = async () => {
    while (next < urls.length) {
      const i = next++;
      try {
        const r = await fetch(urls[i], {credentials: 'include'});
        const t = await r.text();
        try { out[i] = JSON.parse(t); } catch { out[i] = {status_code: -1, raw: t.slice(0, 200)}; }
      } catch (e) { out[i] = {status_code: -1, error: String(e).slice(0, 200)}; }
    }
  };
  await Promise.all(Array.from({length: n}, work));
  return out;
}"""
try:  # noms courts des posts (« J45 »...), à compléter à chaque nouveau post
    NAMES = json.loads((HERE / "noms-posts.json").read_text("utf-8"))
except Exception:
    NAMES = {}
BLOCK_RE = re.compile(r"Access Denied|edgesuite|captcha|Faites glisser|Drag the slider|Vérifiez que vous", re.I)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- lecture des réponses TikTok

def val(o):
    """Déballe {"value": {"status": 0, "value": X}} ou {"status": 0, "value": X} -> X."""
    for _ in range(3):
        if isinstance(o, dict) and o.get("status") not in (None, 0) and "list" not in o:
            return None  # status 2 = « pas encore calculé par TikTok »
        if isinstance(o, dict) and "value" in o:
            if o.get("status") not in (None, 0):
                return None
            o = o["value"]
        else:
            break
    return o


def kv_list(o):
    """Liste [{key, value}] -> {key: value}."""
    o = val(o)
    if isinstance(o, list):
        return {str(x.get("key")): x.get("value") for x in o if isinstance(x, dict) and "key" in x}
    return {}


def hist(o):
    """Historique horaire {"list": [{key: ts, value}]} -> [(ts, value)]."""
    if isinstance(o, dict) and isinstance(o.get("list"), list):
        return [(int(x["key"]), x.get("value")) for x in o["list"] if str(x.get("key", "")).isdigit()]
    return []


def merge(dst: dict, src: dict):
    for k, v in (src or {}).items():
        if v is None or k in ("extra", "log_pb", "status_code", "status_msg"):
            continue
        if isinstance(v, dict) and v.get("status") not in (None, 0) and len(v) <= 2 and dst.get(k):
            continue  # ne pas écraser une vraie valeur par « pas encore dispo »
        dst[k] = v


def posted_at(item_id: str) -> datetime:
    return datetime.fromtimestamp(int(item_id) >> 32)


def hourly(d: dict, item_id: str) -> list[dict]:
    """Vues heure par heure depuis le post. TikTok donne l'horaire « temps réel » jusqu'à ~48 h ;
    après, cette série passe en jours (interval 2) et on prend video_vv_history_48_hours (48 premières heures)."""
    rt = d.get("realtime_video_view_history") or {}
    if rt.get("interval") == 1 and rt.get("list"):
        fr = dict(hist(d.get("realtime_finish_rate_history")))
        aw = dict(hist(d.get("realtime_average_watch_time_history")))
        nf = dict(hist(d.get("realtime_new_followers_history")))
        return [{"heure": datetime.fromtimestamp(ts), "vues": v, "completion": fr.get(ts),
                 "temps_moyen_s": aw.get(ts), "abonnes": nf.get(ts)} for ts, v in hist(rt)]
    h48 = d.get("video_vv_history_48_hours")
    if isinstance(h48, list):
        t0 = int(item_id) >> 32
        t0 -= t0 % 3600
        return [{"heure": datetime.fromtimestamp(t0 + 3600 * i), "vues": val(x)}
                for i, x in enumerate(h48) if val(x) is not None]
    return []


# ---------------------------------------------------------------- navigateur

class Studio:
    def __init__(self, pw, headed: bool):
        try:
            self.ctx = pw.chromium.launch_persistent_context(
                str(PROFILE), channel=NAVIGATEUR, headless=not headed, viewport={"width": 1440, "height": 1000},
                locale="fr-FR",
            )
        except PWError as e:
            if "lock" in str(e).lower() or "ProcessSingleton" in str(e):
                sys.exit("Le profil Chrome est déjà utilisé par un autre script (ou Chrome ouvert sur ce profil). "
                         "Attends qu'il finisse, ne le tue pas.")
            raise
        if not any(c["name"] == "sessionid" and c["value"] for c in self.ctx.cookies("https://www.tiktok.com")):
            self.ctx.close()
            sys.exit(f"Pas connecté à TikTok dans ce profil : {CONNEXION}.")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.captured: list[dict] = []
        self.insight_template: str | None = None
        self.page.on("response", self._on_response)

    def _on_response(self, r):
        u = r.url
        if "tiktok.com" not in u or "json" not in (r.headers.get("content-type") or ""):
            return
        if not re.search(r"/(api|tiktokstudio/api|tiktok/v1|aweme)/", u):
            return
        try:
            js = r.json()
        except Exception:
            return
        if "/aweme/v2/data/insight/" in u and self.insight_template is None:
            self.insight_template = u
        self.captured.append({"url": u, "json": js})

    def goto(self, url: str, settle: float = 6.0, ready=None):
        """Ouvre la page et attend que ready() soit vrai, ou `settle` secondes au maximum."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        t0 = time.time()
        while time.time() - t0 < settle:
            try:
                if ready and ready():
                    break
            except PWError:  # la page se recharge encore : on réessaie au tour suivant
                pass
            self.page.wait_for_timeout(100)
        self.check_blocked()

    def check_blocked(self):
        txt = self.page.evaluate("() => document.body ? document.body.innerText.slice(0, 3000) : ''")
        if BLOCK_RE.search(txt or ""):
            self.ctx.close()
            sys.exit("BLOQUÉ par TikTok (vérification/CAPTCHA). Relance plus tard avec --headed et fais la vérif à la main.\n"
                     + (txt or "")[:300])

    def click_tab(self, *labels: str):
        for label in labels:
            try:
                self.page.get_by_text(label, exact=True).first.click(timeout=2500)
                self.page.wait_for_timeout(4000)
                return True
            except Exception:
                continue
        return False

    def api_url(self, types: list, item_id: str | None = None) -> str:
        p = urllib.parse.urlparse(self.insight_template)
        q = dict(urllib.parse.parse_qsl(p.query))
        reqs = [t if isinstance(t, dict) else ({"insigh_type": t, "aweme_id": item_id} if item_id else {"insigh_type": t})
                for t in types]
        q["type_requests"] = json.dumps(reqs, separators=(",", ":"))
        for k in ("X-Bogus", "X-Gnarly", "msToken", "_signature"):
            q.pop(k, None)
        return p.path + "?" + urllib.parse.urlencode(q)

    def insight_many(self, calls: list) -> list:
        """Appelle l'API d'analyse depuis la page Studio (signée par le JS de TikTok), plusieurs requêtes à la fois.
        calls = [(types, id du post ou None)] ; une réponse par appel, dans le même ordre, None si pas d'API."""
        if not self.insight_template:
            return [None] * len(calls)
        try:
            return self.page.evaluate(FETCH_ALL, [[self.api_url(t, i) for t, i in calls], MAX_PARALLEL])
        except Exception as e:
            return [{"status_code": -1, "error": str(e)[:200]}] * len(calls)

    def close(self):
        self.ctx.close()


# ---------------------------------------------------------------- liste des posts

def post_links(st: Studio) -> list[str]:
    hrefs = st.page.evaluate("""() => [...document.querySelectorAll('a[href]')].map(a => a.href)""")
    return [m.group(1) for h in hrefs if re.search(r"/(video|photo)/\d+|tiktokstudio/analytics/\d+", h)
            for m in [re.search(r"(\d{17,})", h)] if m]


def list_posts(st: Studio, want: int | None) -> list[str]:
    counts: list[int] = []

    def ready():  # la liste s'affiche d'un bloc : prête quand elle existe et ne bouge plus depuis 0,2 s
        counts.append(len(set(post_links(st))))
        return len(counts) >= 3 and counts[-1] > 0 and counts[-1] == counts[-3]

    st.goto(f"{STUDIO}/content", settle=10, ready=ready)
    ids: list[str] = []
    stable = 0
    while True:
        new = post_links(st)
        before = len(ids)
        for i in new:
            if i not in ids:
                ids.append(i)
        if want and len(ids) >= want:
            break
        stable = stable + 1 if len(ids) == before else 0
        if stable >= 3:
            break
        st.page.mouse.wheel(0, 4000)
        st.page.evaluate("() => document.querySelectorAll('*').forEach(e => { if (e.scrollHeight > e.clientHeight + 50) e.scrollTop = e.scrollHeight })")
        st.page.wait_for_timeout(1500)
    ids.sort(key=int, reverse=True)
    return ids[:want] if want else ids


# ---------------------------------------------------------------- un post

def scrape_post_pages(st: Studio, item_id: str) -> dict:
    """Plan B si l'API refuse : ouvrir les pages d'analyse du post et capter ce qu'elles chargent."""
    data: dict = {}
    for tab in ("", "/viewers", "/engagement"):
        st.captured = []
        st.goto(f"{STUDIO}/analytics/{item_id}{tab}", settle=7)
        for r in st.captured:
            if "/data/insight/" in r["url"] or "/analytics/insights" in r["url"]:
                merge(data, r["json"])
    return data


def scrape_posts(st: Studio, ids: list[str]) -> dict:
    """{id: (données, mode)}. Tout passe par l'API d'un coup ; un post refusé retombe sur ses pages d'analyse."""
    out: dict = {}
    for c in range(0, len(ids), POSTS_PER_BATCH):
        if c:
            time.sleep(1)
        batch = ids[c:c + POSTS_PER_BATCH]
        answers = st.insight_many([(types, i) for i in batch for types in POST_GROUPS])
        for n, item_id in enumerate(batch):
            data: dict = {}
            complete = True
            for types, js in zip(POST_GROUPS, answers[n * len(POST_GROUPS):]):
                if js and js.get("status_code") == 0:
                    merge(data, js)
                elif types is not EXTRA_TYPES:  # EXTRA_TYPES est optionnel
                    complete = False
                    break
            out[item_id] = (data, "api") if complete else (scrape_post_pages(st, item_id), "pages")
    return out


def summarize(item_id: str, d: dict, now: datetime) -> dict:
    vi = d.get("video_info") or {}
    s = vi.get("statistics") or {}
    t0 = posted_at(item_id)
    views = val(d.get("realtime_total_video_views")) or s.get("play_count") or 0
    cum, buckets = 0, []
    for x in hourly(d, item_id):
        cum += x["vues"] or 0
        buckets.append((x["heure"].timestamp(), cum))
    pts = real_age_points(int(item_id) >> 32, buckets, (now - t0).total_seconds() / 3600, views)
    per_k =lambda x: round(1000 * x / views, 1) if views and x is not None else None
    traffic = kv_list(d.get("video_traffic_source_percent_realtime"))
    gender = kv_list(d.get("video_viewer_gender_percent_realtime"))
    ages = kv_list(d.get("video_viewer_age_percent_realtime"))
    loc = val(d.get("video_viewer_location_percent_realtime")) or {}
    countries = {c.get("country_name"): c.get("country_vv_percent") for c in (loc.get("country_percent_list") or [])} \
        if isinstance(loc, dict) else {}
    followers = val(d.get("realtime_new_followers"))
    ret = retention(d)
    pct = lambda x: round(100 * x, 1) if isinstance(x, (int, float)) else None
    return {
        "id": item_id,
        "nom": NAMES.get(item_id, ""),
        "poste_le": t0.strftime("%Y-%m-%d %H:%M"),
        "age_h": round((now - t0).total_seconds() / 3600, 1),
        "description": (vi.get("desc") or "").replace("\n", " ")[:90],
        "type": "photo" if vi.get("image_post_info") else "video",
        "vues": views,
        "likes": s.get("digg_count"),
        "commentaires": s.get("comment_count"),
        "partages": s.get("share_count"),
        "favoris": s.get("collect_count"),
        "abonnes_gagnes": followers,
        "spectateurs_uniques": val(d.get("unique_viewer_num")),
        "completion_%": pct(val(d.get("video_finish_rate_realtime"))),
        "temps_moyen_s": val(d.get("video_per_duration_realtime")),
        "temps_total_s": val(d.get("video_total_duration_realtime")),
        "likes_‰": per_k(s.get("digg_count")),
        "favoris_‰": per_k(s.get("collect_count")),
        "partages_‰": per_k(s.get("share_count")),
        "commentaires_‰": per_k(s.get("comment_count")),
        "abonnes_‰": per_k(followers),
        **{f"vues_{h}h": round(v) if (v := interp(pts, h)) is not None else None for h in (1, 3, 6, 12, 24, 48)},
        "pour_toi_%": pct(traffic.get("For You")),
        "recherche_%": pct(traffic.get("Search")),
        "profil_%": pct(traffic.get("Personal Profile")),
        "femmes_%": pct(gender.get("female_vv")),
        "age_18_24_%": pct(ages.get("18-24")),
        "age_25_34_%": pct(ages.get("25-34")),
        "france_%": pct(countries.get("FR")),
        "top_pays": ", ".join(f"{k} {pct(v)}" for k, v in sorted(countries.items(), key=lambda x: -(x[1] or 0))[:4]),
        "slides_retention_%": " / ".join(str(pct(x)) for x in ret) if ret else "",
        "abonnes_compte": val(d.get("follower_num")),
    }


def retention(d: dict) -> list:
    """Rétention par slide (photo_retention_percent), quand TikTok l'a calculée."""
    for k in ("photo_retention_percent", "photo_retention_rate_history_7d", "photo_viewed"):
        v = val(d.get(k))
        if isinstance(v, list) and v:
            if isinstance(v[0], dict):
                v = [x.get("value") for x in sorted(v, key=lambda x: float(x.get("key", 0) or 0))]
            if all(isinstance(x, (int, float)) for x in v):
                return v
    return []


# ---------------------------------------------------------------- compte

def scrape_account(st: Studio) -> dict:
    acc: dict = {}
    answers = st.insight_many([(group, None) for group in ACCOUNT_REQUESTS])
    if all(js and js.get("status_code") == 0 for js in answers):
        for js in answers:
            merge(acc, js)
        return acc
    # Plan B si l'API refuse : ouvrir les onglets d'analyse du compte et capter ce qu'ils chargent.
    st.captured = []
    st.goto(f"{STUDIO}/analytics", settle=8)
    for labels in (("Contenu", "Content"), ("Spectateurs", "Viewers"), ("Abonnés", "Followers"),
                   ("Vue d'ensemble", "Overview")):
        st.click_tab(*labels)
    for r in st.captured:
        if "/data/insight/" in r["url"] or "/analytics/insights" in r["url"]:
            merge(acc, r["json"])
    return acc


def account_tables(acc: dict):
    """Historiques du compte -> (par jour, heures actives, répartitions d'audience)."""
    days, hours, split = [], [], []
    names = {"vv_history": "vues", "pv_history": "vues_profil", "like_history": "likes", "comment_history": "commentaires",
             "share_history": "partages", "follower_num_history": "abonnes_total", "net_follower_history": "abonnes_nets",
             "reached_audience_history": "audience_atteinte", "new_viewer": "nouveaux_spectateurs",
             "returning_viewer": "spectateurs_fideles", "unique_viewer": "spectateurs_uniques",
             "follower_active_history_days": "abonnes_actifs", "viewer_active_history_days": "spectateurs_actifs"}
    for k, v in acc.items():
        if not isinstance(v, list) or not v or not isinstance(v[0], dict):
            continue
        if k.endswith("_active_history_hours"):  # une liste de 24 valeurs par jour
            qui = "abonnés" if k.startswith("follower") else "spectateurs"
            grid = [val(x) for x in v if isinstance(val(x), list) and len(val(x)) == 24]
            for i, row in enumerate(grid):
                hours.append({"qui": qui, "jour": f"j-{len(grid) - 1 - i}", **{f"{h}h": row[h] for h in range(24)}})
            if grid:
                hours.append({"qui": qui, "jour": "moyenne",
                              **{f"{h}h": round(sum(r[h] for r in grid) / len(grid), 1) for h in range(24)}})
            continue
        if k in names:
            for i, x in enumerate(v):
                rel = x.get("days_before_today")
                days.append({"serie": names[k], "jour": f"j{rel}" if rel is not None else f"j-{len(v) - 1 - i}",
                             "valeur": val(x)})
    for k, v in acc.items():
        if isinstance(v, dict) and isinstance(v.get("country_percent_list"), list):
            for c in v["country_percent_list"]:
                split.append({"serie": k, "cle": c.get("country_name"), "part_%": round(100 * (c.get("country_vv_percent") or 0), 1)})
        else:
            for key, x in kv_list(v).items():
                if isinstance(x, (int, float)):
                    split.append({"serie": k, "cle": key, "part_%": round(100 * x, 1)})
    return days, hours, split


# ---------------------------------------------------------------- écriture

def write_csv(path: Path, rows: list[dict]):
    if not rows:
        return
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig : s'ouvre bien dans Excel
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow({k: (str(v).replace(".", ",") if isinstance(v, float) else v) for k, v in r.items()})


def main():
    ap = argparse.ArgumentParser(description="Relevé complet des stats TikTok Studio (lecture seule).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--last", type=int, default=6, help="nombre de derniers posts (défaut 6)")
    g.add_argument("--all", action="store_true", help="tous les posts")
    g.add_argument("--ids", help="IDs séparés par des virgules")
    ap.add_argument("--account", default="fr", help="fr = @vocly.app, en = @vocly.en (version partagée : fr, c2, c3...)")
    ap.add_argument("--no-compte", action="store_true", help="ne pas relever les stats du compte")
    ap.add_argument("--headed", action="store_true", help="Chrome visible")
    ap.add_argument("--out", help="dossier de sortie (défaut stats-out/<date_heure>)")
    a = ap.parse_args()

    global PROFILE
    if not comptes.existe(a.account):
        sys.exit(f"Compte inconnu : {a.account} (comptes : {', '.join(c['id'] for c in comptes.liste())})")
    PROFILE = comptes.profil(a.account)
    now = datetime.now()
    out = Path(a.out) if a.out else comptes.sortie(a.account) / now.strftime("%Y-%m-%d_%Hh%M")
    (out / "raw").mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    with sync_playwright() as pw:
        st = Studio(pw, a.headed)
        try:
            if a.ids:
                ids = [i.strip() for i in a.ids.split(",") if i.strip()]
            else:
                ids = list_posts(st, None if a.all else a.last)
            log(f"{len(ids)} post(s) à relever : {', '.join(ids)}")
            if not ids:
                sys.exit("Aucun post trouvé sur la page Contenu.")

            # Ouvre une page d'analyse : sert de modèle pour appeler l'API directement (beaucoup plus rapide).
            st.goto(f"{STUDIO}/analytics/{ids[0]}", settle=10, ready=lambda: st.insight_template)

            summary, hours, slides, audience = [], [], [], []
            scraped = scrape_posts(st, ids)
            for n, item_id in enumerate(ids, 1):
                d, mode = scraped[item_id]
                (out / "raw" / f"post-{item_id}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")
                row = summarize(item_id, d, now)
                summary.append(row)
                log(f"[{n}/{len(ids)}] {row['poste_le']}  {row['vues']:>7} vues  complétion {row['completion_%']} %  "
                    f"({mode})  {row['description'][:45]}")

                cum = 0
                for h, x in enumerate(hourly(d, item_id), start=1):
                    cum += x["vues"] or 0
                    hours.append({"id": item_id, "heure_n": h, "heure": x["heure"].strftime("%Y-%m-%d %H:%M"),
                                  "vues": x["vues"], "cumul": cum, "completion": x.get("completion"),
                                  "temps_moyen_s": x.get("temps_moyen_s"), "abonnes": x.get("abonnes")})
                for i, r in enumerate(retention(d), start=1):
                    slides.append({"id": item_id, "slide": i, "retention_%": round(100 * r, 1)})
                loc = val(d.get("video_viewer_location_percent_realtime")) or {}
                for kind, src in (("source", kv_list(d.get("video_traffic_source_percent_realtime"))),
                                  ("sexe", kv_list(d.get("video_viewer_gender_percent_realtime"))),
                                  ("age", kv_list(d.get("video_viewer_age_percent_realtime"))),
                                  ("pays", {c.get("country_name"): c.get("country_vv_percent")
                                            for c in (loc.get("country_percent_list") or [])} if isinstance(loc, dict) else {})):
                    for k, v in src.items():
                        audience.append({"id": item_id, "type": kind, "cle": k, "part_%": round(100 * v, 1) if v is not None else None})

            account = {}
            if not a.no_compte:
                account = scrape_account(st)
                (out / "raw" / "compte.json").write_text(json.dumps(account, ensure_ascii=False, indent=1), "utf-8")
        finally:
            st.close()

    acc_days, acc_hours, acc_split = account_tables(account)

    write_csv(out / "resume.csv", summary)
    (out / "resume.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), "utf-8")
    write_csv(out / "vues-par-heure.csv", hours)
    write_csv(out / "compte-jours.csv", acc_days)
    write_csv(out / "compte-heures-actives.csv", acc_hours)
    write_csv(out / "compte-audience.csv", acc_split)
    write_csv(out / "retention-slides.csv", slides)
    write_csv(out / "audience.csv", audience)
    (out / "tout.json").write_text(json.dumps({  # lu par la page stats_server.py
        "at": now.isoformat(timespec="minutes"), "summary": summary, "hours": hours, "slides": slides,
        "audience": audience, "compte": {"followers": val(account.get("follower_num")), "days": acc_days,
                                         "active_hours": acc_hours, "split": acc_split},
    }, ensure_ascii=False), "utf-8")

    log(f"\nTerminé en {time.time() - t_start:.0f} s -> {out}")
    if account:
        log(f"Abonnés : {val(account.get('follower_num'))}")
        for r in acc_hours:
            if r["jour"] == "moyenne":
                top = sorted(range(24), key=lambda h: -(r[f"{h}h"] or 0))[:4]
                log(f"Heures où tes {r['qui']} sont le plus actifs : {', '.join(f'{h}h' for h in sorted(top))}")
    log(f"{'posté le':<17}{'vues':>8}{'compl.%':>9}{'moy.s':>7}{'likes‰':>8}{'fav‰':>6}{'abo‰':>6}{'3h':>7}{'12h':>7}{'24h':>8}")
    for r in summary:
        f = lambda x: "-" if x is None else x
        log(f"{r['poste_le']:<17}{f(r['vues']):>8}{f(r['completion_%']):>9}{f(r['temps_moyen_s']):>7}"
            f"{f(r['likes_‰']):>8}{f(r['favoris_‰']):>6}{f(r['abonnes_‰']):>6}{f(r['vues_3h']):>7}{f(r['vues_12h']):>7}{f(r['vues_24h']):>8}")


if __name__ == "__main__":
    main()
