// kennzeichen-ping - Cloudflare-Worker mit Minuten-Cron.
//
// GitHub drosselt den Cron des Waechters auf alle paar Stunden. Dieser Worker
// ruft jede Minute die Startseite des Portals ab und startet den Waechter per
// workflow_dispatch:
//   - sofort, wenn das Portal nach einer Stoerung wieder antwortet
//   - sonst alle TAKT_MIN Minuten, solange es laeuft
// Bei einer Stoerung startet er nichts. Der GitHub-Cron bleibt als Reserve.
//
// Zusaetzlich fragt er die Wunsch-Kennzeichen (WUNSCH, z. B. "XX3,Z7") jede
// Minute einzeln ab und schickt sofort einen ntfy-Push, wenn eins frei wird.
// Nur lesend - es wird nichts reserviert.
//
// KV wird nur bei Aenderungen beschrieben (Free-Plan: 1000 Schreibvorgaenge/Tag).
// GET / zeigt den Stand als JSON.

const BASE = "https://kfz-egov.regioit.de/wkz/";
const PORTAL = BASE + "?LICENSEIDENTIFIER=solingen_stadt";
const STOERUNG = ["Datenbank-Problem", "aktuell nicht genutzt", "Wartung"];

// -- Portal-Sitzung (wie wkz.Portal, nur das Noetigste) -----------------------

const entities = { amp: "&", quot: '"', lt: "<", gt: ">", apos: "'" };
const unescape = (s) =>
  s.replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) =>
    e[0] === "#" ? String.fromCodePoint(e[1].toLowerCase() === "x" ? parseInt(e.slice(2), 16) : +e.slice(1))
      : entities[e.toLowerCase()] ?? m);

function versteckt(seite) {
  const out = {};
  for (const [tag] of seite.matchAll(/<input[^>]*type="hidden"[^>]*>/g)) {
    const n = tag.match(/name="([^"]+)"/);
    const v = tag.match(/value="([^"]*)"/);
    if (n) out[unescape(n[1])] = v ? unescape(v[1]) : "";
  }
  return out;
}

function meldungen(seite) {
  const box = seite.match(/<div id="uimsgcontainer">([\s\S]*?)<\/div>/);
  if (!box) return [];
  return [...box[1].matchAll(/<li>([\s\S]*?)<\/li>/g)]
    .map((m) => unescape(m[1].replace(/<[^>]+>/g, "")).trim())
    .map((m) => m.replace(/^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}:\s*/, ""))
    .filter(Boolean);
}

class Sitzung {
  // Minuten-Crons duerfen nicht ewig laufen; das Portal braucht nach Stoerungen
  // bis zu 5 s pro Seite. Nach ZEITBUDGET bricht die Sitzung ab, der Stand wird
  // trotzdem gespeichert.
  constructor(zeitbudget = 24000) {
    this.ende = Date.now() + zeitbudget;
    this.kekse = new Map();
    this.seite = "";
    this.status = 0;
  }

  // Weiterleitungen selbst folgen, damit kein Session-Cookie verloren geht
  async hole(url, daten, ms = 12000) {
    let methode = daten ? "POST" : "GET";
    let body = daten ? new URLSearchParams(daten).toString() : undefined;
    for (let i = 0; i < 5; i++) {
      const rest = this.ende - Date.now();
      if (rest < 1500) throw new Error("Zeitbudget aufgebraucht");
      const headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) kennzeichen-ping",
        "Accept-Language": "de-DE,de;q=0.9",
        Cookie: [...this.kekse].map(([k, v]) => k + "=" + v).join("; "),
      };
      if (body) headers["Content-Type"] = "application/x-www-form-urlencoded";
      const r = await fetch(url, { method: methode, body, headers, redirect: "manual", signal: AbortSignal.timeout(Math.min(ms, rest)) });
      for (const c of r.headers.getSetCookie()) {
        const kv = c.split(";")[0];
        const j = kv.indexOf("=");
        if (j > 0) this.kekse.set(kv.slice(0, j).trim(), kv.slice(j + 1).trim());
      }
      const ziel = r.headers.get("location");
      if (r.status >= 300 && r.status < 400 && ziel) {
        url = new URL(ziel, url).toString();
        methode = "GET";
        body = undefined;
        continue;
      }
      this.status = r.status;
      this.seite = await r.text();
      return this.seite;
    }
    throw new Error("zu viele Weiterleitungen");
  }

  klick(aktion, extra = {}) {
    const f = this.seite.match(/<form[^>]*action="([^"]+)"/);
    if (!f) throw new Error("kein Formular auf der Seite");
    return this.hole(new URL(unescape(f[1]), BASE).toString(), { ...versteckt(this.seite), ...extra, [aktion]: "" });
  }

  // Erster Schritt = Erreichbarkeits-Pruefung
  async oeffne() {
    try {
      await this.hole(PORTAL, null, 15000);
    } catch (e) {
      return { an: false, grund: e.name === "TimeoutError" ? "Zeitueberschreitung" : String(e) };
    }
    if (this.status !== 200) return { an: false, grund: "HTTP " + this.status };
    const stoerung = STOERUNG.find((h) => this.seite.includes(h));
    if (stoerung) return { an: false, grund: stoerung };
    if (!this.seite.includes("ACTION_INFOPAGE_NEXT") && !this.seite.includes("ACTION_CHOICEAKZTYPE_NEXT"))
      return { an: false, grund: "unerwartete Seite" };
    return { an: true };
  }

  // Bis zur Suchmaske fuer E-Kennzeichen durchklicken
  async zurSuche() {
    if (this.seite.includes("ACTION_INFOPAGE_NEXT")) await this.klick("ACTION_INFOPAGE_NEXT");
    if (!this.seite.includes("ACTION_CHOICEAKZTYPE_NEXT"))
      throw new Error("Kennzeichenart-Seite fehlt: " + meldungen(this.seite).join(" / "));
    const knopf = [...this.seite.matchAll(/<button name="(ACTION_EKOLTREEITEM\d+\|\|EXPAND\|\|([A-Z0-9_]+))"/g)]
      .find((m) => m[2].replace(/_/g, "").includes("ELEKTRO"));
    if (!knopf) throw new Error("Knopf fuer E-Kennzeichen nicht gefunden");
    await this.klick(unescape(knopf[1]));
    await this.klick("ACTION_CHOICEAKZTYPE_NEXT");
    if (!this.seite.includes("WKZSEARCH_CHARS"))
      throw new Error("Suchmaske nicht erreicht: " + meldungen(this.seite).join(" / "));
  }

  // true = frei, false = vergeben, null = unklar
  async istFrei(buchst, ziffern) {
    if (!this.seite.includes("WKZSEARCH_CHARS") && this.seite.includes("ACTION_WKZLISTPAGE_PREVIOUS"))
      await this.klick("ACTION_WKZLISTPAGE_PREVIOUS");
    await this.klick("ACTION_SEARCHPAGE_NEXT", {
      WKZSEARCH_CHARS: buchst,
      WKZSEARCH_NUMS: ziffern,
      WKZSEARCH_SPECIALCHARS: "1",
      WKZSEARCH_SPECIALNUMS: "1",
    });
    const sel = this.seite.match(/<select name="WKZRESULTLIST_WKZ"[\s\S]*?<\/select>/);
    if (sel) return [...sel[0].matchAll(/<option value="([^"]+)"/g)].some((m) => m[1] === `SG-${buchst}${ziffern}`);
    if (meldungen(this.seite).some((m) => m.includes("keine freien"))) return false;
    return null;
  }
}

// -- GitHub und ntfy ----------------------------------------------------------

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

async function melde(env, titel, text) {
  if (!env.NTFY_TOPIC) return console.log("NTFY_TOPIC fehlt - " + titel);
  await fetch("https://ntfy.sh/" + env.NTFY_TOPIC, {
    method: "POST",
    body: text,
    headers: { Title: titel, Priority: "urgent", Click: PORTAL },
  });
}

// -- Ablauf -------------------------------------------------------------------

function wuensche(env) {
  return (env.WUNSCH || "").toUpperCase().split(",").map((w) => w.trim().match(/^([A-Z]{1,2})(\d{1,4})$/)).filter(Boolean);
}

async function pruefeWuensche(env, sitzung, alt) {
  const liste = wuensche(env);
  const stand = { ...(alt || {}) };
  if (!liste.length) return stand;
  try {
    await sitzung.zurSuche();
    for (const [w, b, z] of liste) {
      const frei = await sitzung.istFrei(b, z);
      if (frei === null) {
        console.log(`${b} ${z}: unklar - ` + meldungen(sitzung.seite).join(" / "));
        continue;
      }
      if (frei !== stand[w]?.frei) {
        stand[w] = { frei, seit: Date.now() };
        console.log(`SG ${b} ${z}E ist jetzt ${frei ? "FREI" : "vergeben"}`);
        if (frei) {
          const uhr = new Date().toLocaleTimeString("de-DE", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit" });
          await melde(env, `SG ${b} ${z}E ist frei!`, `Wunschkennzeichen um ${uhr} frei gesehen - schnell reservieren.`);
        }
      }
    }
  } catch (e) {
    console.log("Wunsch-Abfrage fehlgeschlagen: " + e);
  }
  return stand;
}

async function lauf(env) {
  const alt = (await env.STAND.get("stand", "json")) || { an: null, seit: 0, gestartet: 0 };
  const jetzt = Date.now();
  const sitzung = new Sitzung();
  const p = await sitzung.oeffne();
  const neu = { ...alt, grund: p.an ? undefined : p.grund };

  if (p.an !== alt.an) {
    neu.an = p.an;
    neu.seit = jetzt;
  }
  const wiederDa = p.an && alt.an === false;
  const faellig = p.an && jetzt - alt.gestartet >= Number(env.TAKT_MIN) * 60000;
  if (wiederDa || faellig) {
    neu.gestartet = jetzt;
    neu.fehler = (await starteWaechter(env)) || undefined;
    console.log((wiederDa ? "Portal wieder da, " : "") + "Waechter gestartet" + (neu.fehler ? " - " + neu.fehler : ""));
  } else if (!p.an) {
    console.log("Portal aus: " + p.grund);
  }
  if (p.an) neu.wunsch = await pruefeWuensche(env, sitzung, alt.wunsch);

  // Nur schreiben, wenn sich etwas Wesentliches geaendert hat (grund allein zaehlt nicht)
  const kern = (s) => JSON.stringify([s.an, s.seit, s.gestartet, s.fehler, s.wunsch]);
  if (kern(neu) !== kern(alt)) await env.STAND.put("stand", JSON.stringify(neu));
}

export default {
  async scheduled(_event, env) {
    await lauf(env);
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
      wunsch: Object.fromEntries(
        wuensche(env).map(([w, b, z]) => [`SG ${b} ${z}E`, s.wunsch?.[w]
          ? `${s.wunsch[w].frei ? "frei" : "vergeben"} seit ${zeit(s.wunsch[w].seit)}` : "noch nicht geprueft"]),
      ),
    });
  },
};
