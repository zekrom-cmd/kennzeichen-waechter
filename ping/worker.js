// kennzeichen-ping - Cloudflare-Worker mit Minuten-Cron.
//
// GitHub drosselt den Cron des Waechters auf alle paar Stunden. Dieser Worker
// ruft jede Minute die Startseite des Portals ab und startet den Waechter per
// workflow_dispatch:
//   - sofort, wenn das Portal nach einer Stoerung wieder antwortet
//   - sonst alle TAKT_MIN Minuten, solange es laeuft
// Bei einer Stoerung startet er nichts. Der GitHub-Cron bleibt als Reserve.
//
// Morgen-Fenster (MORGEN, z. B. "06:50-07:30", Berlin-Zeit): Das Portal gibt
// abgelaufene Reservierungen taeglich gegen 7 Uhr frei. Dann startet der Worker
// einmal eine Dauerschleife des Waechters bis zum Fensterende und schickt in
// der Zeit keine weiteren Starts.
//
// Zusaetzlich fragt er die Wunsch-Kennzeichen (WUNSCH, z. B. "XX3,Z7") jede
// Minute einzeln ab und schickt sofort einen ntfy-Push, wenn eins frei wird.
// Wuensche aus WUNSCH_BRUDER gehen zusaetzlich an NTFY_TOPIC_BRUDER.
// Nur lesend - es wird nichts reserviert.
//
// Fahrzeugbrief (Bankbriefauskunft, /abb/): Ist das Secret BRIEF_ZB2 gesetzt
// (Nummer der ZB Teil II), fragt er alle 2 Minuten ab, ob der Brief in der
// Zulassungsstelle vorliegt, und schickt einen dringenden Push, sobald sich die
// Antwort von "liegt nicht vor" aendert. Die Nummer steht nur im Secret, nicht im Repo.
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
  constructor(zeitbudget = 24000, basis = BASE) {
    this.basis = basis;
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
    return this.hole(new URL(unescape(f[1]), this.basis).toString(), { ...versteckt(this.seite), ...extra, [aktion]: "" });
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

async function starteWaechter(env, minuten = 0) {
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
      body: JSON.stringify(minuten ? { ref: "main", inputs: { minuten: String(minuten) } } : { ref: "main" }),
    },
  );
  return r.status === 204 ? null : "GitHub " + r.status + ": " + (await r.text()).slice(0, 200);
}

async function melde(env, titel, text, bruder = false, klick = PORTAL, prio = "urgent") {
  const themen = [...new Set([env.NTFY_TOPIC, bruder && env.NTFY_TOPIC_BRUDER].filter(Boolean))];
  if (!themen.length) return console.log("NTFY_TOPIC fehlt - " + titel);
  for (const t of themen) {
    const senden = (token) => fetch("https://ntfy.sh/", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) },
      body: JSON.stringify({ topic: t, title: titel, message: text, priority: prio === "urgent" ? 5 : 3, click: klick }),
    });
    // Mit Token ueber den ntfy-Account (eigenes Limit); wird er abgelehnt, anonym senden
    let r = env.NTFY_TOKEN ? await senden(env.NTFY_TOKEN) : null;
    if (!r || r.status === 401 || r.status === 403) r = await senden(null);
    if (!r.ok) console.log("ntfy " + r.status + " fuer " + titel);
  }
}

// -- Ablauf -------------------------------------------------------------------

// Tag und Restminuten im Morgen-Fenster (Berlin-Zeit), rest = 0 ausserhalb
function morgenFenster(env, t) {
  const m = (env.MORGEN || "").match(/^(\d\d):(\d\d)-(\d\d):(\d\d)$/);
  const teil = Object.fromEntries(
    new Intl.DateTimeFormat("de-DE", {
      timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(t).map((x) => [x.type, x.value]),
  );
  const tag = `${teil.year}-${teil.month}-${teil.day}`;
  if (!m) return { tag, rest: 0 };
  const jetzt = +teil.hour * 60 + +teil.minute;
  const von = +m[1] * 60 + +m[2];
  const bis = +m[3] * 60 + +m[4];
  return { tag, rest: jetzt >= von && jetzt < bis ? bis - jetzt : 0 };
}

// [wunsch, buchstaben, ziffern, fuerBruder]
function wuensche(env) {
  const liste = (v, bruder) => (v || "").toUpperCase().split(",")
    .map((w) => w.trim().match(/^([A-Z]{1,2})(\d{1,4})$/)).filter(Boolean).map((m) => [...m, bruder]);
  return [...liste(env.WUNSCH, false), ...liste(env.WUNSCH_BRUDER, true)];
}

async function pruefeWuensche(env, sitzung, alt) {
  const liste = wuensche(env);
  const stand = { ...(alt || {}) };
  if (!liste.length) return stand;
  try {
    await sitzung.zurSuche();
    for (const [w, b, z, bruder] of liste) {
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
          await melde(env, `SG ${b} ${z}E ist frei!`, `Wunschkennzeichen um ${uhr} frei gesehen - schnell reservieren.`, bruder);
        }
      }
    }
  } catch (e) {
    console.log("Wunsch-Abfrage fehlgeschlagen: " + e);
  }
  return stand;
}

// -- Fahrzeugbrief (Bankbriefauskunft) ---------------------------------------

const ABB = "https://kfz-egov.regioit.de/abb/";
const ABB_START = ABB + "?LICENSEIDENTIFIER=solingen_stadt";
const ALARM_NACH = 60 * 60000; // so lange darf die Abfrage scheitern, bevor ein leiser Hinweis kommt

const klartext = (seite) =>
  unescape(seite.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/g, "").replace(/<[^>]+>/g, " ")).replace(/\s+/g, " ").trim();

// Liefert den Ergebnis-Satz, z. B. "Die Zulassungsbescheinigung Teil II bzw. der Fahrzeugbrief liegt nicht vor."
async function fragBrief(nummer) {
  const s = new Sitzung(20000, ABB);
  await s.hole(ABB_START, null, 15000);
  if (!s.seite.includes("ACTION_CONTEXTSTART_NEXT")) throw new Error("Startseite unerwartet (HTTP " + s.status + ")");
  await s.klick("ACTION_CONTEXTSTART_NEXT");
  if (!s.seite.includes("USERINPUT_ZB2NR")) throw new Error("Eingabemaske fehlt: " + meldungen(s.seite).join(" / "));
  await s.klick("ACTION_CONTEXTUSERINPUT_NEXT", { USERINPUT_ZB2NR: nummer, USERINPUT_AKZ: "", USERINPUT_NACHNAME: "" });
  const t = klartext(s.seite);
  const m = t.match(/Name des Fahrzeughalters:\s*(.*?)\s*(Downloads|Impressum|$)/);
  if (!t.includes("Anfrage vom") || !m || !m[1]) throw new Error("kein Ergebnis: " + (meldungen(s.seite).join(" / ") || t.slice(0, 150)));
  return m[1];
}

// ZB-II-Nummer: 2 Buchstaben + 6 Ziffern (z. B. AB123456). Das Portal nimmt auch
// Unsinn an und sagt dann "liegt nicht vor" - darum vorher pruefen.
const zb2 = (env) => (env.BRIEF_ZB2 || "").trim().toUpperCase();
const zb2Ok = (n) => /^[A-Z]{2}\d{6}$/.test(n);
const zb2Kurz = (n) => (zb2Ok(n) ? n.slice(0, 2) + "…" + n.slice(-2) : `ungueltig (${n.length} Zeichen, erwartet 2 Buchstaben + 6 Ziffern)`);

async function pruefeBrief(env) {
  const nummer = zb2(env);
  if (!nummer) return;
  if (!zb2Ok(nummer)) return console.log("BRIEF_ZB2 ungueltig: " + nummer.length + " Zeichen");
  const alt = (await env.STAND.get("brief", "json")) || {};
  const neu = { ...alt };
  const jetzt = Date.now();
  try {
    const satz = await fragBrief(nummer);
    neu.fehlerSeit = undefined;
    neu.fehlerGemeldet = undefined;
    if (satz !== alt.satz) {
      neu.satz = satz;
      neu.seit = jetzt;
      console.log("Brief: " + satz);
      // Erste Abfrage mit "liegt nicht vor" = Ausgangslage, kein Push
      if (!/liegt nicht vor/i.test(satz))
        await melde(env, /liegt\b.*\bvor/i.test(satz) ? "Fahrzeugbrief ist da!" : "Fahrzeugbrief: neue Auskunft",
          satz + "\n\nJetzt bei der Zulassungsstelle Solingen anmelden.", false, ABB_START);
    }
  } catch (e) {
    console.log("Brief-Abfrage fehlgeschlagen: " + e);
    neu.fehlerSeit = alt.fehlerSeit || jetzt;
    neu.letzterFehler = String(e).slice(0, 200);
    if (!alt.fehlerGemeldet && jetzt - neu.fehlerSeit >= ALARM_NACH) {
      neu.fehlerGemeldet = true;
      await melde(env, "Brief-Abfrage klemmt", "Seit einer Stunde keine Antwort von der Bankbriefauskunft: " + neu.letzterFehler, false, ABB_START, "default");
    }
  }
  // Nur bei Aenderung schreiben (letzterFehler allein zaehlt nicht)
  const kern = (b) => JSON.stringify([b.satz, b.seit, b.fehlerSeit, b.fehlerGemeldet]);
  if (kern(neu) !== kern(alt)) await env.STAND.put("brief", JSON.stringify(neu));
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
  const morgen = morgenFenster(env, jetzt);
  if (p.an && morgen.rest && alt.morgen !== morgen.tag) {
    // Einmal pro Tag; schlaegt der Start fehl, versucht er es in der naechsten Minute wieder
    neu.gestartet = jetzt;
    neu.fehler = (await starteWaechter(env, morgen.rest)) || undefined;
    if (!neu.fehler) neu.morgen = morgen.tag;
    console.log(`Morgen-Schleife fuer ${morgen.rest} Min. gestartet` + (neu.fehler ? " - " + neu.fehler : ""));
  } else if (morgen.rest) {
    // Schleife laeuft schon (oder Portal aus) - nichts dazwischenschieben
    if (!p.an) console.log("Portal aus: " + p.grund);
  } else if (wiederDa || faellig) {
    neu.gestartet = jetzt;
    neu.fehler = (await starteWaechter(env)) || undefined;
    console.log((wiederDa ? "Portal wieder da, " : "") + "Waechter gestartet" + (neu.fehler ? " - " + neu.fehler : ""));
  } else if (!p.an) {
    console.log("Portal aus: " + p.grund);
  }
  if (p.an) neu.wunsch = await pruefeWuensche(env, sitzung, alt.wunsch);

  // Nur schreiben, wenn sich etwas Wesentliches geaendert hat (grund allein zaehlt nicht)
  const kern = (s) => JSON.stringify([s.an, s.seit, s.gestartet, s.fehler, s.wunsch, s.morgen]);
  if (kern(neu) !== kern(alt)) await env.STAND.put("stand", JSON.stringify(neu));
}

export default {
  async scheduled(event, env) {
    // Brief nur jede 2. Minute, unabhaengig vom Kennzeichen-Portal
    const brief = new Date(event.scheduledTime).getUTCMinutes() % 2 === 0 ? pruefeBrief(env) : null;
    await Promise.all([lauf(env), brief].filter(Boolean).map((p) => p.catch((e) => console.log("Fehler: " + e))));
  },
  async fetch(_req, env) {
    const s = (await env.STAND.get("stand", "json")) || {};
    const b = (await env.STAND.get("brief", "json")) || {};
    const zeit = (t) => (t ? new Date(t).toLocaleString("de-DE", { timeZone: "Europe/Berlin" }) : null);
    return Response.json({
      portal: s.an === true ? "erreichbar" : s.an === false ? "gestoert" : "noch nicht geprueft",
      seit: zeit(s.seit),
      letzter_grund: s.grund || null,
      waechter_zuletzt_gestartet: zeit(s.gestartet),
      fehler_beim_start: s.fehler || null,
      morgen_fenster: env.MORGEN || null,
      morgen_schleife_zuletzt: s.morgen || null,
      wunsch: Object.fromEntries(
        wuensche(env).map(([w, b, z]) => [`SG ${b} ${z}E`, s.wunsch?.[w]
          ? `${s.wunsch[w].frei ? "frei" : "vergeben"} seit ${zeit(s.wunsch[w].seit)}` : "noch nicht geprueft"]),
      ),
      // Ohne Nummer - die Seite ist oeffentlich
      fahrzeugbrief: !env.BRIEF_ZB2 ? "aus (Secret BRIEF_ZB2 fehlt)" : {
        nummer: zb2Kurz(zb2(env)),
        ...(!zb2Ok(zb2(env)) ? { auskunft: "keine Abfrage - Nummer pruefen" } : b.satz
          ? { auskunft: b.satz, seit: zeit(b.seit), stoerung_seit: zeit(b.fehlerSeit), letzter_fehler: b.fehlerSeit ? b.letzterFehler : null }
          : { auskunft: "noch nicht geprueft" }),
      },
    });
  },
};
