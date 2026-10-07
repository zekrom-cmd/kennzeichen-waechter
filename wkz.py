#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wkz.py - Sucht freie (seltene) Wunschkennzeichen im Portal kfz-egov.regioit.de.

Standard: Solingen (SG), Elektro-Kennzeichen (--art normal fuer normale).
Nur lesend - es wird NICHTS reserviert.
Keine externen Abhaengigkeiten, laeuft mit dem Python 3 von macOS.

Beispiele:
  python3 wkz.py --selten                 # alle "Selten"-Presets durchsuchen
  python3 wkz.py TS '*'                   # Buchstaben TS, beliebige Ziffern
  python3 wkz.py '??' '*' --bs gleich --zi schnaps
  python3 wkz.py --selten --diff          # nur Aenderungen zum letzten Lauf
  python3 wkz.py --selten --csv treffer.csv
"""

import argparse
import csv
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://kfz-egov.regioit.de/wkz/"
STATE_DIR = os.path.expanduser("~/.wkz")
MAX_HITS = 100  # das Portal liefert nie mehr als 100 Treffer pro Suche

# Auswahl "Besondere Einstellungen" -> Formularwerte des Portals
BUCHSTABEN = {
    "keine": "1",
    "gleich": "2",       # AA, BB, ...
    "auf": "3",          # AB, DE, ...
    "ab": "4",           # BA, ED, ...
}
ZIFFERN = {
    "keine": "1",
    "schnaps": "2",      # 111, 4444
    "auf": "3",          # 12, 4567
    "ab": "4",           # 21, 7654
    "sym": "5",          # 303, 1661
    "glatt": "6",        # 300, 6000
    "zwilling": "7",     # 1313, 5252
}

# Presets fuer "seltene" Kennzeichen
PRESETS = [
    ("Gleiche Buchstaben + Schnapszahl (AA 111)",   "??", "*", "gleich", "schnaps"),
    ("Gleiche Buchstaben + Zwillinge (AA 1212)",    "??", "*", "gleich", "zwilling"),
    ("Gleiche Buchstaben + symmetrisch (AA 1661)",  "??", "*", "gleich", "sym"),
    ("Gleiche Buchstaben + glatt (AA 3000)",        "??", "*", "gleich", "glatt"),
    ("Gleiche Buchstaben, Ziffern egal",            "??", "*", "gleich", "keine"),
    ("Buchstaben + Ziffern aufsteigend (AB 123)",   "??", "*", "auf",    "auf"),
    ("Buchstaben + Ziffern absteigend (BA 321)",    "??", "*", "ab",     "ab"),
    ("Kurz: 2 Buchstaben + 1 Ziffer (AB 1)",        "??", "?", "keine",  "keine"),
    ("Kurz: 1 Buchstabe + 2 Ziffern (A 12)",        "?",  "??", "keine", "keine"),
    ("Schnapszahlen, Buchstaben egal",              "*",  "*", "keine",  "schnaps"),
    ("Zwillinge, Buchstaben egal",                  "*",  "*", "keine",  "zwilling"),
    ("Symmetrisch, Buchstaben egal",                "*",  "*", "keine",  "sym"),
]

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Kuerzeste ueberhaupt moegliche Kennzeichen (3 Zeichen nach dem SG)
KURZ = [
    ("Kurz: 2 Buchstaben + 1 Ziffer (AB 1)", "??", "?", "keine", "keine"),
    ("Kurz: 1 Buchstabe + 2 Ziffern (A 12)", "?", "??", "keine", "keine"),
]


def zeichen_presets(zeichen):
    """Baut Suchmuster fuer bestimmte Wunsch-Buchstaben/-Ziffern."""
    buchst = [z for z in zeichen.upper() if z in LETTERS]
    ziff = [z for z in zeichen if z in DIGITS]
    jobs = []
    for b in buchst:
        jobs.append(("Buchstabe %s vorn" % b, b + "?", "*", "keine", "keine"))
        jobs.append(("Buchstabe %s hinten" % b, "?" + b, "*", "keine", "keine"))
        jobs.append(("Nur %s (1 Buchstabe)" % b, b, "*", "keine", "keine"))
    for z in ziff:
        for muster in (z, z + z, z + z + z, z + z + z + z):
            jobs.append(("Ziffern %s" % muster, "*", muster, "keine", "keine"))
    return jobs


# --------------------------------------------------------------------------
# HTML-Helfer (bewusst simpel gehalten, damit keine Bibliotheken noetig sind)
# --------------------------------------------------------------------------

def text_of(page):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)))


def hidden_fields(page):
    out = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', page):
        tag = m.group(0)
        n = re.search(r'name="([^"]+)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if n:
            out[html.unescape(n.group(1))] = html.unescape(v.group(1)) if v else ""
    return out


def form_action(page):
    m = re.search(r'<form[^>]*action="([^"]+)"', page)
    if not m:
        raise RuntimeError("Kein Formular auf der Seite gefunden.")
    return urllib.parse.urljoin(BASE, html.unescape(m.group(1)))


def messages(page):
    box = re.search(r'<div id="uimsgcontainer">(.*?)</div>', page, re.S)
    if not box:
        return []
    msgs = [html.unescape(re.sub(r"<[^>]+>", "", li)).strip()
            for li in re.findall(r"<li>(.*?)</li>", box.group(1), re.S)]
    # fuehrenden Zeitstempel entfernen
    return [re.sub(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}:\s*", "", m) for m in msgs if m]


def tree_options(page):
    """Kennzeichenarten, die als Auswahl-Button auf der Seite stehen."""
    out = {}
    for m in re.finditer(r'<button name="(ACTION_EKOLTREEITEM\d+\|\|EXPAND\|\|([A-Z0-9_]+))"[^>]*>(.*?)</button>',
                         page, re.S):
        key = m.group(2)
        label = html.unescape(re.sub(r"<[^>]+>", "", m.group(3))).strip()
        out[key] = (html.unescape(m.group(1)), label)
    return out


def parse_plate(raw):
    m = re.match(r"^([A-ZÄÖÜ]{1,3})-([A-Z]{1,2})(\d{1,4})$", raw)
    if not m:
        return raw, ("", "", 0)
    kreis, buchst, ziff = m.group(1), m.group(2), m.group(3)
    return "%s %s %s" % (kreis, buchst, ziff), (kreis, buchst, int(ziff))


# --------------------------------------------------------------------------
# Portal-Session
# --------------------------------------------------------------------------

class Portal(object):
    def __init__(self, city, art="elektro", delay=1.5, timeout=30, verbose=False):
        self.city = city
        self.art = art
        self.delay = delay
        self.timeout = timeout
        self.verbose = verbose
        self.page = None
        self.info = {}
        self._last_request = 0.0
        self._open_client()

    # -- HTTP ---------------------------------------------------------------
    def _open_client(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
            ("Accept-Language", "de-DE,de;q=0.9"),
        ]

    def _throttle(self):
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _fetch(self, url, data=None):
        self._throttle()
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        last = None
        for attempt in range(3):
            try:
                with self.opener.open(url, body, timeout=self.timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError) as e:
                last = e
                if self.verbose:
                    sys.stderr.write("  Netzwerkfehler (%s), Versuch %d/3\n" % (e, attempt + 1))
                time.sleep(2 * (attempt + 1))
        raise RuntimeError("Verbindung fehlgeschlagen: %s" % last)

    def _submit(self, action_name, extra=None):
        data = hidden_fields(self.page)
        if extra:
            data.update(extra)
        data[action_name] = ""
        self.page = self._fetch(form_action(self.page), data)
        return self.page

    # -- Wizard -------------------------------------------------------------
    def start(self):
        """Bis zur Suchmaske durchklicken."""
        self._open_client()
        url = BASE + "?" + urllib.parse.urlencode({"LICENSEIDENTIFIER": self.city})
        self.page = self._fetch(url)

        if "ACTION_INFOPAGE_NEXT" in self.page:
            self._submit("ACTION_INFOPAGE_NEXT")

        if "ACTION_CHOICEAKZTYPE_NEXT" not in self.page:
            raise RuntimeError("Unerwartete Seite - Portal geaendert? "
                               "Meldungen: %s" % messages(self.page))

        arten = tree_options(self.page)
        if self.art and self.art != "normal":
            btn = self._match_art(arten, self.art)
            if btn is None:
                raise SystemExit("Kennzeichenart '%s' nicht gefunden. Verfuegbar: normal, %s"
                                 % (self.art, ", ".join(sorted(arten))))
            self._submit(btn)

        self._submit("ACTION_CHOICEAKZTYPE_NEXT")
        if "WKZSEARCH_CHARS" not in self.page:
            raise RuntimeError("Suchmaske nicht erreicht. Meldungen: %s" % messages(self.page))
        self.info = self._reservation_info(self.page)
        return self

    @staticmethod
    def _match_art(arten, wanted):
        w = wanted.upper().replace("-", "").replace("_", "")
        for key, (btn, _label) in arten.items():
            k = key.upper().replace("_", "")
            if k.endswith(w) or w in k:
                return btn
        return None

    @staticmethod
    def _reservation_info(page):
        info = {}
        for key, val in re.findall(
                r'<div class="mdg_key">(.*?)</div><div class="mdg_value">(.*?)</div>', page, re.S):
            info[html.unescape(re.sub(r"<[^>]+>", "", key)).strip().rstrip(":")] = \
                html.unescape(re.sub(r"<[^>]+>", "", val)).strip()
        return info

    def _back_to_search(self):
        if "WKZSEARCH_CHARS" in self.page:
            return
        if "ACTION_WKZLISTPAGE_PREVIOUS" in self.page:
            self._submit("ACTION_WKZLISTPAGE_PREVIOUS")
        if "WKZSEARCH_CHARS" not in self.page:
            self.start()

    # -- Suche --------------------------------------------------------------
    def search(self, chars, nums, bs="keine", zi="keine"):
        """Liefert (plates, meldungen). plates = Liste roher Kennzeichen 'SG-TS45'."""
        if self.page is None:
            self.start()
        try:
            self._back_to_search()
            page = self._submit("ACTION_SEARCHPAGE_NEXT", {
                "WKZSEARCH_CHARS": chars.upper(),
                "WKZSEARCH_NUMS": nums,
                "WKZSEARCH_SPECIALCHARS": BUCHSTABEN[bs],
                "WKZSEARCH_SPECIALNUMS": ZIFFERN[zi],
            })
        except RuntimeError:
            # Session verloren -> einmal neu aufbauen und wiederholen
            self.start()
            page = self._submit("ACTION_SEARCHPAGE_NEXT", {
                "WKZSEARCH_CHARS": chars.upper(),
                "WKZSEARCH_NUMS": nums,
                "WKZSEARCH_SPECIALCHARS": BUCHSTABEN[bs],
                "WKZSEARCH_SPECIALNUMS": ZIFFERN[zi],
            })

        if "WKZRESULTLIST_WKZ" not in page:
            return [], messages(page)

        sel = re.search(r'<select name="WKZRESULTLIST_WKZ".*?</select>', page, re.S)
        plates = re.findall(r'<option value="([^"]+)"', sel.group(0)) if sel else []
        return plates, []


# --------------------------------------------------------------------------
# Suchlauf
# --------------------------------------------------------------------------

DIGITS = "0123456789"
VERBOTEN = ("HJ", "KZ", "NS", "SA", "SS")

# Meldungen, die beim Aufsplitten normal sind und keine echten Fehler darstellen
HARMLOS = ("keine freien Kennzeichen", "Paradoxe Suchkombination",
           "nicht erlaubt", "ungültig", "ungueltig")


def _paar_ok(pair, bs):
    """Passt ein konkretes Buchstabenpaar zum gewaehlten Buchstaben-Filter?
    'auf'/'ab' meinen beim Portal direkt benachbarte Buchstaben (AB, BC / BA, CB)."""
    if pair in VERBOTEN:
        return False
    if len(pair) != 2:
        return True
    a, b = pair[0], pair[1]
    if bs == "gleich":
        return a == b
    if bs == "auf":
        return ord(b) == ord(a) + 1
    if bs == "ab":
        return ord(a) == ord(b) + 1
    return True


def refine_chars(pat, bs="keine"):
    """Zerlegt ein Buchstaben-Muster in engere Teilmuster (Buchstaben: 1 oder 2).
    Kombinationen, die der Buchstaben-Filter ohnehin ausschliesst, werden gar
    nicht erst angefragt - das spart bei 'gleich'/'auf'/'ab' hunderte Requests."""
    if pat == "*":
        cand = ["?", "??"]
    elif pat == "?":
        cand = list(LETTERS)
    elif pat == "??":
        cand = [L + "?" for L in LETTERS]
    elif len(pat) == 2 and pat[0] in LETTERS and pat[1] in "?*":
        cand = ([pat[0]] if pat[1] == "*" else []) + [pat[0] + L for L in LETTERS]
    elif len(pat) == 2 and pat[1] in LETTERS and pat[0] in "?*":
        cand = [L + pat[1] for L in LETTERS]
    else:
        return []
    return [c for c in cand if "?" in c or "*" in c or _paar_ok(c, bs)]


def refine_nums(pat):
    """Zerlegt ein Ziffern-Muster (Ziffern: 1 bis 4 Stellen)."""
    if pat == "*":
        return ["?", "??", "???", "????"]
    i = pat.find("?")
    if i < 0:
        return []
    return [pat[:i] + d + pat[i + 1:] for d in DIGITS]


def run_query(portal, label, chars, nums, bs, zi, expand, quiet, max_suchen=400):
    """Fuehrt eine Suche aus. Mit expand wird bei 100 Treffern so lange
    feiner aufgeteilt, bis keine Teilsuche mehr am Limit haengt."""
    todo = [(chars, nums)]
    found = set()
    truncated = False
    msgs = []
    done = 0

    while todo:
        if done >= max_suchen:
            truncated = True
            if not quiet:
                sys.stdout.write(" " * 72 + "\r")
                print("      Limit von %d Suchen erreicht (--max-suchen erhoehen)" % max_suchen)
            break
        c, n = todo.pop(0)
        plates, m = portal.search(c, n, bs, zi)
        done += 1
        for msg in m:
            if msg not in msgs and not any(h in msg for h in HARMLOS):
                msgs.append(msg)
        found.update(plates)

        if len(plates) < MAX_HITS:
            continue
        if not expand:
            truncated = True
            continue

        parts = [(sub, n) for sub in refine_chars(c, bs)] or \
                [(c, sub) for sub in refine_nums(n)]
        if parts:
            todo.extend(parts)
        else:
            truncated = True

        if not quiet and todo:
            sys.stdout.write("      splitte auf ... %d Suchen erledigt, %d offen, %d Treffer\r"
                             % (done, len(todo), len(found)))
            sys.stdout.flush()

    if not quiet and done > 1:
        sys.stdout.write(" " * 72 + "\r")

    return {
        "label": label,
        "chars": chars, "nums": nums, "bs": bs, "zi": zi,
        "plates": sorted(found, key=lambda p: parse_plate(p)[1]),
        "messages": msgs,
        "truncated": truncated,
        "suchen": done,
    }


def profile_id(city, art, jobs, expand=False):
    """Snapshots werden je Suchprofil getrennt gehalten - sonst vergleicht
    man Aepfel mit Birnen."""
    raw = "|".join("%s;%s;%s;%s" % (c, n, b, z) for _l, c, n, b, z in jobs)
    raw += "|expand" if expand else ""
    return "%s-%s-%s" % (city, art, hashlib.md5(raw.encode("utf-8")).hexdigest()[:8])


def load_snapshot(pid):
    try:
        with open(os.path.join(STATE_DIR, "snapshot-%s.json" % pid), "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return None


def save_snapshot(pid, city, art, plates):
    if not os.path.isdir(STATE_DIR):
        os.makedirs(STATE_DIR)
    with open(os.path.join(STATE_DIR, "snapshot-%s.json" % pid), "w", encoding="utf-8") as f:
        json.dump({"zeit": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "stadt": city, "art": art,
                   "kennzeichen": sorted(plates)}, f, ensure_ascii=False, indent=1)


def kurzliste(plates, limit=40):
    if not plates:
        return "-"
    namen = [parse_plate(p)[0] for p in plates[:limit]]
    rest = len(plates) - len(namen)
    return ", ".join(namen) + (" ... (+%d weitere)" % rest if rest > 0 else "")


def main():
    ap = argparse.ArgumentParser(
        description="Freie (seltene) Wunschkennzeichen suchen - nur lesend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Platzhalter:  ? = genau ein Zeichen,  * = beliebig viele
Regeln des Amtes: 1 Buchstabe + 2-4 Ziffern  oder  2 Buchstaben + 1-3 Ziffern.
HJ, KZ, NS, SA, SS sind nicht erlaubt.

Buchstaben-Filter (--bs): """ + ", ".join(BUCHSTABEN) + """
Ziffern-Filter   (--zi): """ + ", ".join(ZIFFERN))
    ap.add_argument("chars", nargs="?", help="Buchstaben, z.B. TS, A?, ?? oder *")
    ap.add_argument("nums", nargs="?", default="*", help="Ziffern, z.B. 88, 1?3 oder * (Standard)")
    ap.add_argument("--bs", default="keine", choices=sorted(BUCHSTABEN), help="Buchstaben-Sonderfilter")
    ap.add_argument("--zi", default="keine", choices=sorted(ZIFFERN), help="Ziffern-Sonderfilter")
    ap.add_argument("--selten", action="store_true", help="alle Selten-Presets durchsuchen")
    ap.add_argument("--kurz", action="store_true",
                    help="nur die kuerzest moeglichen Kennzeichen (z.B. SG AB 1, SG A 12)")
    ap.add_argument("--zeichen", metavar="XYZ4",
                    help="Wunschbuchstaben/-ziffern, z.B. --zeichen XCZ4")
    ap.add_argument("--enthaelt", metavar="TEXT",
                    help="Ergebnis nachtraeglich filtern, z.B. --enthaelt X (mehrere: X,C,Z)")
    ap.add_argument("--stadt", default="solingen_stadt", help="LICENSEIDENTIFIER (Standard: solingen_stadt)")
    ap.add_argument("--art", default="elektro",
                    help="elektro (Standard, E-Auto), normal, saison, saisonelektro, "
                         "historisch, saisonhistorisch, krad")
    ap.add_argument("--arten", action="store_true", help="verfuegbare Kennzeichenarten anzeigen")
    ap.add_argument("--expand", action="store_true",
                    help="bei 100 Treffern automatisch ueber A-Z aufsplitten (mehr Requests)")
    ap.add_argument("--max-suchen", type=int, default=400, metavar="N", dest="max_suchen",
                    help="Obergrenze an Einzelsuchen je Muster bei --expand (Standard 400)")
    ap.add_argument("--diff", action="store_true", help="nur Aenderungen zum letzten Lauf zeigen")
    ap.add_argument("--csv", metavar="DATEI", help="Ergebnis als CSV speichern")
    ap.add_argument("--json", metavar="DATEI", help="Ergebnis als JSON speichern")
    ap.add_argument("--intervall", type=float, metavar="MIN",
                    help="alle X Minuten wiederholen und Aenderungen melden")
    ap.add_argument("--pause", type=float, default=1.5, metavar="SEK",
                    help="Wartezeit zwischen Requests (Standard 1.5s - bitte nicht kleiner)")
    ap.add_argument("-q", "--quiet", action="store_true", help="weniger Ausgabe")
    args = ap.parse_args()

    if not any([args.selten, args.kurz, args.zeichen, args.chars, args.arten]):
        ap.error("Bitte Buchstaben angeben oder --selten / --kurz / --zeichen benutzen. "
                 "Hilfe: --help")

    portal = Portal(args.stadt, args.art, delay=max(0.5, args.pause), verbose=not args.quiet)

    if args.arten:
        portal._open_client()
        page = portal._fetch(BASE + "?" + urllib.parse.urlencode({"LICENSEIDENTIFIER": args.stadt}))
        portal.page = page
        portal._submit("ACTION_INFOPAGE_NEXT")
        print("Verfuegbare Kennzeichenarten fuer '%s':" % args.stadt)
        print("  normal            (Voreinstellung)")
        for key, (_btn, label) in sorted(tree_options(portal.page).items()):
            print("  %-18s %s" % (key.split("_")[-1].lower(), label))
        return 0

    jobs = []
    if args.selten:
        jobs += PRESETS
    if args.kurz:
        jobs += KURZ
    if args.zeichen:
        jobs += zeichen_presets(args.zeichen)
    if args.chars:
        jobs += [("Eigene Suche", args.chars, args.nums, args.bs, args.zi)]
    # Doppelte Muster nur einmal abfragen
    gesehen, eindeutig = set(), []
    for j in jobs:
        if j[1:] not in gesehen:
            gesehen.add(j[1:])
            eindeutig.append(j)
    jobs = eindeutig

    def one_pass():
        portal.start()
        if not args.quiet:
            print("Portal: %s | %s | Gebuehr %s"
                  % (args.stadt,
                     portal.info.get("Kennzeichenart", args.art),
                     portal.info.get("Gebuehr", portal.info.get("Gebühr", "?"))))
            print("-" * 62)

        results = []
        for label, chars, nums, bs, zi in jobs:
            if not args.quiet and not args.diff:
                print("* %s  [%s %s]" % (label, chars, nums))
            res = run_query(portal, label, chars, nums, bs, zi, args.expand,
                            args.quiet, args.max_suchen)
            if args.enthaelt:
                noetig = [t.strip().upper() for t in args.enthaelt.split(",") if t.strip()]
                res["plates"] = [p for p in res["plates"]
                                 if any(t in p.split("-", 1)[-1] for t in noetig)]
            results.append(res)
            if res["messages"] and not args.quiet:
                for m in res["messages"]:
                    print("    ! %s" % m)
            if not args.quiet and not args.diff:
                n = len(res["plates"])
                print("    %d frei%s" % (n, " (unvollstaendig - --expand / --max-suchen nutzen)"
                                         if res["truncated"] else ""))
                if n:
                    pretty = [parse_plate(p)[0] for p in res["plates"]]
                    for i in range(0, len(pretty), 6):
                        print("      " + "   ".join("%-12s" % x for x in pretty[i:i + 6]).rstrip())
                print()
        return results

    def report(results):
        alle = sorted({p for r in results for p in r["plates"]}, key=lambda p: parse_plate(p)[1])
        print("=" * 62)
        print("Gesamt: %d freie Kennzeichen (%s)" % (len(alle), time.strftime("%d.%m.%Y %H:%M")))

        pid = profile_id(args.stadt, args.art, jobs, args.expand)
        prev = load_snapshot(pid)
        if prev is not None:
            old = set(prev["kennzeichen"])
            neu = [p for p in alle if p not in old]
            weg = sorted(old - set(alle), key=lambda p: parse_plate(p)[1])
            print("Vergleich mit Lauf vom %s:" % prev["zeit"])
            print("  NEU frei (%d): %s" % (len(neu), kurzliste(neu)))
            print("  weg      (%d): %s" % (len(weg), kurzliste(weg)))
        else:
            print("(erster Lauf fuer dieses Suchprofil - wird als Vergleichsbasis gespeichert)")
        save_snapshot(pid, args.stadt, args.art, alle)

        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Kennzeichen", "Kreis", "Buchstaben", "Ziffern", "Kategorie"])
                for r in results:
                    for p in r["plates"]:
                        pretty, (k, b, z) = parse_plate(p)
                        w.writerow([pretty, k, b, z, r["label"]])
            print("CSV geschrieben: %s" % args.csv)

        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump({"zeit": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "stadt": args.stadt, "art": args.art,
                           "treffer": results}, f, ensure_ascii=False, indent=1)
            print("JSON geschrieben: %s" % args.json)

        print("Reservieren (2 Stueck, 3 Monate, manuell):")
        print("  %s?LICENSEIDENTIFIER=%s" % (BASE, args.stadt))

    try:
        while True:
            report(one_pass())
            if not args.intervall:
                break
            print("\nNaechster Lauf in %g Minuten - Abbruch mit Strg+C\n" % args.intervall)
            time.sleep(args.intervall * 60)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 130
    except (RuntimeError, SystemExit) as e:
        sys.stderr.write("Fehler: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
