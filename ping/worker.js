// kennzeichen-ping - Cloudflare-Worker mit Minuten-Cron.
//
// GitHub drosselt den Cron des Waechters auf alle paar Stunden. Dieser Worker
// fragt jede Minute nur die Startseite des Portals ab (eine Anfrage, keine Suche)
// und startet den Waechter per workflow_dispatch:
//   - sofort, wenn das Portal nach einer Stoerung wieder antwortet
//   - sonst alle TAKT_MIN Minuten, solange es laeuft
// Bei einer Stoerung startet er nichts. Der GitHub-Cron bleibt als Reserve.
//
// KV wird nur bei Aenderungen beschrieben (Free-Plan: 1000 Schreibvorgaenge/Tag).
// GET / zeigt den Stand als JSON.

const PORTAL = "https://kfz-egov.regioit.de/wkz/?LICENSEIDENTIFIER=solingen_stadt";
const STOERUNG = ["Datenbank-Problem", "aktuell nicht genutzt", "Wartung"];

async function portalLaeuft() {
  try {
    const r = await fetch(PORTAL, {
      signal: AbortSignal.timeout(25000),
      headers: { "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) kennzeichen-ping" },
    });
    const html = await r.text();
    if (!r.ok) return { an: false, grund: "HTTP " + r.status };
    const stoerung = STOERUNG.find((h) => html.includes(h));
    if (stoerung) return { an: false, grund: stoerung };
    if (!html.includes("ACTION_INFOPAGE_NEXT") && !html.includes("ACTION_CHOICEAKZTYPE_NEXT"))
      return { an: false, grund: "unerwartete Seite" };
    return { an: true };
  } catch (e) {
    return { an: false, grund: e.name === "TimeoutError" ? "Zeitueberschreitung" : String(e) };
  }
}

async function starteWaechter(env) {
  if (!env.GITHUB_TOKEN) return "GITHUB_TOKEN fehlt";
  const r = await fetch(
    `https://api.github.com/repos/${env.REPO}/actions/workflows/${env.WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: "Bearer " + env.GITHUB_TOKEN,
        Accept: "application/vnd.github+json",
        "User-Agent": "kennzeichen-ping",
      },
      body: JSON.stringify({ ref: "main" }),
    },
  );
  return r.status === 204 ? null : "GitHub " + r.status + ": " + (await r.text()).slice(0, 200);
}

async function lauf(env) {
  const alt = (await env.STAND.get("stand", "json")) || { an: null, seit: 0, gestartet: 0 };
  const jetzt = Date.now();
  const p = await portalLaeuft();
  const neu = { ...alt, grund: p.an ? undefined : p.grund };
  let geaendert = false;

  if (p.an !== alt.an) {
    neu.an = p.an;
    neu.seit = jetzt;
    geaendert = true;
  }
  const wiederDa = p.an && alt.an === false;
  const faellig = p.an && jetzt - alt.gestartet >= Number(env.TAKT_MIN) * 60000;
  if (wiederDa || faellig) {
    neu.gestartet = jetzt;
    neu.fehler = (await starteWaechter(env)) || undefined;
    geaendert = true;
    console.log((wiederDa ? "Portal wieder da, " : "") + "Waechter gestartet" + (neu.fehler ? " - " + neu.fehler : ""));
  } else if (!p.an) {
    console.log("Portal aus: " + p.grund);
  }
  if (geaendert) await env.STAND.put("stand", JSON.stringify(neu));
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(lauf(env));
  },
  async fetch(_req, env) {
    const s = (await env.STAND.get("stand", "json")) || {};
    const zeit = (t) => (t ? new Date(t).toLocaleString("de-DE", { timeZone: "Europe/Berlin" }) : null);
    return Response.json({
      portal: s.an === true ? "erreichbar" : s.an === false ? "gestoert" : "noch nicht geprueft",
      seit: zeit(s.seit),
      letzter_grund: s.grund || null,
      waechter_zuletzt_gestartet: zeit(s.gestartet),
      fehler_beim_start: s.fehler || null,
    });
  },
};
