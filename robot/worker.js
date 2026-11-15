// Relevé horaire en ligne des compteurs PUBLICS des posts TikTok d'un ou plusieurs comptes (Cloudflare Worker + D1).
// Aucun identifiant TikTok : il lit la page publique de chaque post, comme n'importe quel visiteur.
// Pourquoi : TikTok ne garde pas l'historique heure par heure des likes, favoris, partages et commentaires ;
// ce relevé le construit, même quand l'ordinateur est éteint. predict.py le récupère via /snaps.
//
// Comptes suivis : variable COMPTES (noms TikTok sans @, séparés par des virgules).
//
//   GET  /                 page d'état (derniers relevés, posts suivis, formulaire d'ajout)
//   GET  /snaps?since=TS   données en JSON (lues par predict.py)
//   GET  /ajouter?url=...  ajoute un post (lien TikTok) ; vérifie qu'il est bien d'un compte suivi
//   GET  /run              relance un relevé tout de suite (au plus 1 fois toutes les 5 min)
// Cron : toutes les heures.

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36";
const KEEP_DAYS = 7;
const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, ajoute INTEGER, source TEXT)",
  "CREATE TABLE IF NOT EXISTS snaps (id TEXT, at INTEGER, vues INTEGER, likes INTEGER, favoris INTEGER, partages INTEGER, commentaires INTEGER, PRIMARY KEY (id, at))",
  "CREATE TABLE IF NOT EXISTS runs (at INTEGER PRIMARY KEY, source TEXT, suivis INTEGER, ok INTEGER, decouverts TEXT, erreurs TEXT)",
];
const t0 = id => Number(BigInt(id) >> 32n);
const nowS = () => Math.floor(Date.now() / 1000);
const users = env => String(env.COMPTES || "").split(",").map(u => u.trim().replace(/^@/, "")).filter(Boolean);

async function get(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9", Accept: "text/html" }, redirect: "follow" });
  return { status: r.status, html: r.status === 200 ? await r.text() : "" };
}

// Compteurs d'un post, lus dans le JSON intégré à sa page publique (sans tout parser : moins de CPU).
async function statsFor(id, user) {
  const { status, html } = await get(`https://www.tiktok.com/@${user}/video/${id}`);
  if (status !== 200) return { err: `http ${status}` };
  if (!html.includes(`"id":"${id}"`)) return { err: "post introuvable sur la page" };
  if (!html.includes(`"uniqueId":"${user}"`)) return { err: "pas un post de @" + user };
  const m = html.match(/"statsV2":(\{[^{}]*\})/) || html.match(/"stats":(\{"diggCount"[^{}]*\})/);
  if (!m) return { err: "compteurs absents (page bloquée ?)" };
  const s = JSON.parse(m[1]);
  const n = k => (s[k] == null ? null : Number(s[k]));
  return { user, vues: n("playCount"), likes: n("diggCount"), favoris: n("collectCount"), partages: n("shareCount"), commentaires: n("commentCount") };
}

// Compte connu (colonne compte) ou, sinon, on essaie chaque compte suivi.
async function stats(env, id, user) {
  const list = user ? [user] : users(env);
  let last = { err: "aucun compte suivi" };
  for (const u of list) {
    last = await statsFor(id, u);
    if (!last.err || list.length === 1) return last;
  }
  return list.length > 1 ? { err: "pas un post des comptes suivis" } : last;
}

// Nouveaux posts : la page publique « embed » du profil liste les derniers posts.
async function discover(user) {
  const { status, html } = await get(`https://www.tiktok.com/embed/@${user}`);
  if (status !== 200) return { err: `@${user} : profil http ${status}`, ids: [] };
  const re = new RegExp(`@${user.replace(/\./g, "\\.")}/(?:video|photo)/(\\d{17,20})`, "g");
  const ids = [...new Set([...html.matchAll(re)].map(m => m[1]))];
  return { ids: ids.filter(i => nowS() - t0(i) < KEEP_DAYS * 86400) };
}

async function schema(env) {
  for (const q of SCHEMA) await env.DB.prepare(q).run();
  try { await env.DB.prepare("ALTER TABLE posts ADD COLUMN compte TEXT").run(); } catch (e) { /* déjà là */ }
}

async function run(env, source) {
  await schema(env);
  const now = nowS();
  const errs = [], found = [];
  for (const u of users(env)) {
    const disc = await discover(u);
    if (disc.err) errs.push(disc.err);
    for (const id of disc.ids) {
      found.push(id);
      await env.DB.prepare("INSERT OR IGNORE INTO posts (id, ajoute, source, compte) VALUES (?, ?, 'profil', ?)").bind(id, now, u).run();
    }
  }
  const { results } = await env.DB.prepare("SELECT id, compte FROM posts").all();
  const live = results.filter(r => now - t0(r.id) < KEEP_DAYS * 86400);
  let ok = 0;
  for (const p of live) {
    try {
      const s = await stats(env, p.id, p.compte);
      if (s.err) { errs.push(`${p.id}: ${s.err}`); continue; }
      if (!p.compte) await env.DB.prepare("UPDATE posts SET compte = ? WHERE id = ?").bind(s.user, p.id).run();
      await env.DB.prepare("INSERT OR REPLACE INTO snaps (id, at, vues, likes, favoris, partages, commentaires) VALUES (?, ?, ?, ?, ?, ?, ?)")
        .bind(p.id, now, s.vues, s.likes, s.favoris, s.partages, s.commentaires).run();
      ok++;
    } catch (e) { errs.push(`${p.id}: ${String(e).slice(0, 120)}`); }
  }
  await env.DB.prepare("INSERT OR REPLACE INTO runs (at, source, suivis, ok, decouverts, erreurs) VALUES (?, ?, ?, ?, ?, ?)")
    .bind(now, source, live.length, ok, found.join(","), errs.join(" | ")).run();
  return { at: now, suivis: live.length, ok, decouverts: found, erreurs: errs };
}

const json = (o, code = 200) => new Response(JSON.stringify(o), { status: code, headers: { "content-type": "application/json; charset=utf-8", "access-control-allow-origin": "*" } });
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const when = s => new Date(s * 1000).toLocaleString("fr-FR", { timeZone: "Europe/Paris", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

async function statusPage(env, msg = "") {
  await schema(env);
  const U = users(env), names = U.map(u => "@" + u).join(", ");
  const runs = (await env.DB.prepare("SELECT * FROM runs ORDER BY at DESC LIMIT 12").all()).results;
  const posts = (await env.DB.prepare(
    "SELECT p.id, p.source, p.compte, COUNT(s.at) n, MAX(s.at) last, (SELECT likes FROM snaps WHERE id = p.id ORDER BY at DESC LIMIT 1) likes, (SELECT vues FROM snaps WHERE id = p.id ORDER BY at DESC LIMIT 1) vues FROM posts p LEFT JOIN snaps s ON s.id = p.id GROUP BY p.id ORDER BY p.id DESC LIMIT 20").all()).results;
  const html = `<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Relevé TikTok</title>
<style>:root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 16px}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid #8884;text-align:left}input{width:100%;font:inherit;padding:10px;border-radius:10px;border:1px solid #8888;box-sizing:border-box}button{font:inherit;padding:10px 16px;border-radius:10px;border:0;background:#2446c8;color:#fff;margin-top:8px}.m{color:#888}.ok{color:#1f8a4c}.bad{color:#c2352b}</style></head><body>
<h1>Relevé TikTok ${esc(names)}</h1><p class="m">Toutes les heures, en ligne : vues, likes, favoris, partages et commentaires publics des posts de moins de ${KEEP_DAYS} jours.</p>
${msg ? `<p><b>${esc(msg)}</b></p>` : ""}
<form action="/ajouter"><label>Ajouter un post (colle son lien TikTok)</label><input name="url" placeholder="https://www.tiktok.com/@${esc(U[0] || "")}/photo/…" required><button>Ajouter</button></form>
<h2>Posts suivis</h2><table><tr><th>Post</th><th>Compte</th><th>Posté</th><th>Relevés</th><th>Vues</th><th>Likes</th><th>Ajouté par</th></tr>
${posts.map(p => `<tr><td>${p.id}</td><td>${p.compte ? "@" + esc(p.compte) : "–"}</td><td>${when(t0(p.id))}</td><td>${p.n}</td><td>${p.vues ?? "–"}</td><td>${p.likes ?? "–"}</td><td class="m">${esc(p.source)}</td></tr>`).join("")}</table>
<h2>Derniers relevés</h2><table><tr><th>Quand</th><th>OK</th><th>Problèmes</th></tr>
${runs.map(r => `<tr><td>${when(r.at)}</td><td class="${r.ok === r.suivis ? "ok" : "bad"}">${r.ok} / ${r.suivis}</td><td class="m">${esc(r.erreurs || "")}</td></tr>`).join("")}</table>
</body></html>`;
  return new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env, "cron"));
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/snaps") {
      await schema(env);
      const since = Number(url.searchParams.get("since") || 0);
      const snaps = (await env.DB.prepare("SELECT * FROM snaps WHERE at > ? ORDER BY at").bind(since).all()).results;
      const posts = (await env.DB.prepare("SELECT * FROM posts").all()).results;
      const runs = (await env.DB.prepare("SELECT * FROM runs ORDER BY at DESC LIMIT 48").all()).results;
      return json({ snaps, posts, runs });
    }
    if (url.pathname === "/ajouter") {
      await schema(env);
      const raw = url.searchParams.get("url") || url.searchParams.get("id") || "";
      const ids = raw.split(",").map(x => (x.match(/(\d{17,20})/) || [])[1]).filter(Boolean);
      if (!ids.length) return statusPage(env, "Lien non reconnu : colle le lien d'un post TikTok.");
      const done = [];
      for (const id of ids) {
        if (nowS() - t0(id) > KEEP_DAYS * 86400) { done.push(`${id} : plus de ${KEEP_DAYS} jours, ignoré`); continue; }
        const known = await env.DB.prepare("SELECT id FROM posts WHERE id = ?").bind(id).first();
        if (known) { done.push(`${id} : déjà suivi`); continue; }
        const s = await stats(env, id);  // vérifie que c'est bien un post d'un compte suivi
        if (s.err) { done.push(`${id} : ${s.err}`); continue; }
        await env.DB.prepare("INSERT OR IGNORE INTO posts (id, ajoute, source, compte) VALUES (?, ?, ?, ?)").bind(id, nowS(), url.searchParams.get("source") || "manuel", s.user).run();
        await env.DB.prepare("INSERT OR REPLACE INTO snaps (id, at, vues, likes, favoris, partages, commentaires) VALUES (?, ?, ?, ?, ?, ?, ?)")
          .bind(id, nowS(), s.vues, s.likes, s.favoris, s.partages, s.commentaires).run();
        done.push(`${id} : ajouté (${s.vues} vues, ${s.likes} likes)`);
      }
      if ((request.headers.get("accept") || "").includes("text/html")) return statusPage(env, done.join(" · "));
      return json({ resultat: done });
    }
    if (url.pathname === "/run") {
      await schema(env);
      const last = await env.DB.prepare("SELECT MAX(at) at FROM runs").first();
      if (last?.at && nowS() - last.at < 300) return json({ error: "Dernier relevé il y a moins de 5 min." }, 429);
      return json(await run(env, "manuel"));
    }
    if (url.pathname === "/") return statusPage(env);
    return new Response("Introuvable", { status: 404 });
  },
};
