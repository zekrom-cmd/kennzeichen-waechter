#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
waechter.py - Meldet per ntfy, wenn in Solingen ein kurzes E-Kennzeichen frei wird.

Fragt bei jedem Lauf ab:
  - alle 3-stelligen (SG AB 1, SG A 12)
  - die knappen 4-stelligen: ein Buchstabe mit Schnapszahl, glatter Zahl oder
    Folge (SG A 444, SG A 300, SG A 123) und Doppelbuchstabe mit Schnaps- oder
    glatter Zahl (SG AA 44, SG AA 30)
5-stellige bewusst nicht.
Fuer den Bruder zusaetzlich JS kurz oder mit Schnaps-/glatter Zahl/Folge
(SG JS 7, SG JS 77, SG JS 777, SG JS 200, SG JS 123) - Meldung an NTFY_TOPIC_BRUDER,
solange der fehlt an NTFY_TOPIC.

Reihenfolge: X, Y, Z zuerst (Lieblinge), dann der Rest. Jeder Fund wird sofort
gemeldet, nicht erst am Ende des Laufs - morgens um 7 zaehlt jede Minute.
Drei Portal-Sitzungen suchen gleichzeitig (PARALLEL).

Nur lesend - es wird nichts reserviert. Gedaechtnis: zustand.json.
Eine Suche, die fehlschlaegt, behaelt ihren alten Stand. So entstehen
keine falschen "neu frei"-Meldungen.

  NTFY_TOPIC=... python3 waechter.py          # normaler Lauf
  python3 waechter.py --trocken                # nur anzeigen, nichts senden
"""

import argparse
import json
import os
import queue
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import wkz

K = "keine"
PARALLEL = 3  # Sitzungen gleichzeitig - das Portal braucht 2,5-5 s pro Suche, die Pause davor kaum
VORNE = "XYZ"
BRUDER = "JS"
JOBS_BRUDER = (
    [(BRUDER, "?", K, K)]
    + [(BRUDER, "??", K, z) for z in ("schnaps", "glatt")]                       # JS 77, JS 70
    + [(BRUDER, "???", K, z) for z in ("schnaps", "glatt", "auf", "ab")]         # JS 777, JS 700, JS 123
)
JOBS = (
    [j for b in VORNE for j in ((b, "??", K, K), (b + "?", "?", K, K))]           # X 12, XA 1 ... zuerst
    + [("?", "??", K, K)]                                                         # A 12 (eine Suche, X/Y/Z doppelt)
    + [(b + "?", "?", K, K) for b in wkz.LETTERS if b not in VORNE]               # AB 1
    + JOBS_BRUDER                                                                 # vor den 4-stelligen
    + [("?", "???", K, z) for z in ("schnaps", "glatt", "auf", "ab")]          # A 444, A 300, A 123, A 321
    + [("??", "??", "gleich", z) for z in ("schnaps", "glatt")]                # AA 44, AA 30
)
# Bewusst nicht: AB 44 / AB 30 / AA 47 - davon ist noch die Haelfte frei, das waeren
# pro Lauf ueber 1000 Suchen bei einem Portal, das nur ~1 Suche pro Sekunde schafft.
PORTAL = "https://kfz-egov.regioit.de/wkz/?LICENSEIDENTIFIER=solingen_stadt"
PORTAL_STOERUNG = ("Datenbank-Problem", "aktuell nicht genutzt", "Wartung")
ZUSTAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zustand.json")


def schluessel(job):
    return "|".join(job)


def deckt(job, roh):
    """Faellt Kennzeichen roh (z. B. 'SG-X12') unter einen ungefilterten Job?"""
    chars, nums, bs, zi = job
    m = re.match(r"SG-([A-Z]+)(\d+)$", roh)
    return bool(m) and bs == zi == K and all(
        len(muster) == len(wert) and all(c in ("?", w) for c, w in zip(muster, wert))
        for muster, wert in ((chars, m.group(1)), (nums, m.group(2))))


def umfasst(alt, neu):
    """Deckt der ungefilterte Job alt alles ab, was Job neu findet?"""
    return alt[2] == alt[3] == K and all(
        len(a) == len(n) and all(x == "?" or x == y for x, y in zip(a, n))
        for a, n in ((alt[0], neu[0]), (alt[1], neu[1])))


def teile(chars, nums, bs):
    t = wkz.refine_chars(chars, bs)
    if t:
        return [(c, nums) for c in t]
    i = nums.find("?")
    if i < 0:
        return []
    return [(chars, nums[:i] + d + nums[i + 1:]) for d in ("123456789" if i == 0 else "0123456789")]


def suche(portal, chars, nums, bs, zi):
    """Liste freier Kennzeichen oder None bei Fehler."""
    for versuch in range(3):
        try:
            plates, msgs = portal.search(chars, nums, bs, zi)
            if plates:
                return plates
            if "WKZSEARCH_CHARS" in (portal.page or "") and any(
                    h in m for m in msgs for h in ("keine freien", "ungültig", "Paradoxe", "nicht erlaubt")):
                return []
            raise RuntimeError(msgs)
        except Exception:
            time.sleep(3 + 5 * versuch)
            try:
                portal.start()
            except Exception:
                pass
    return None


def frage_ab(portal, job):
    """Ein Job inkl. Aufteilen bei 100 Treffern. None, wenn irgendeine Teilsuche fehlschlug."""
    chars, nums, bs, zi = job
    offen, alle, suchen = [(chars, nums)], set(), 0
    while offen:
        c, n = offen.pop(0)
        plates = suche(portal, c, n, bs, zi)
        suchen += 1
        if plates is None:
            return None, suchen
        alle.update(plates)
        if len(plates) >= wkz.MAX_HITS:
            offen += teile(c, n, bs)
    return sorted(alle), suchen


def beschreibung(roh):
    _k, (kreis, b, z) = wkz.parse_plate(roh)
    d = str(z)
    teile_ = ["%d Zeichen" % (len(b) + len(d))]
    if len(d) >= 2 and len(set(d)) == 1:
        teile_.append("Schnapszahl")
    elif len(d) >= 2 and set(d[1:]) == {"0"}:
        teile_.append("glatte Zahl")
    elif len(d) >= 2 and (d in "1234567890" or d in "9876543210"):
        teile_.append("Folge")
    elif len(d) >= 3 and d == d[::-1]:
        teile_.append("Spiegelzahl")
    if len(b) == 2 and b[0] == b[1]:
        teile_.append("Doppelbuchstabe")
    return "SG %s %sE" % (b, d), ", ".join(teile_), len(b) + len(d)


def gut(roh):
    """X/Y/Z, Doppelbuchstabe oder Schnapszahl - nur dafuer klingelt es laut."""
    _k, (_kreis, b, z) = wkz.parse_plate(roh)
    d = str(z)
    return any(c in b for c in VORNE) or (len(b) == 2 and b[0] == b[1]) or (len(d) >= 2 and len(set(d)) == 1)


def melde(topic, neu, dringend=False, praefix=""):
    # Die guten zuerst, dann die kuerzesten - das erste steht im Titel und hinter "Am Auto ansehen"
    liste = sorted(neu, key=lambda p: (not gut(p), beschreibung(p)[2], beschreibung(p)[0]))
    infos = [beschreibung(p) for p in liste]
    titel = praefix + "%s frei" % infos[0][0] + (" (+%d %s)" % (len(infos) - 1, "weiteres" if len(infos) == 2 else "weitere") if len(infos) > 1 else "")
    text = "\n".join(("★ " if gut(p) else "") + "%s – %s" % (t, b) for p, (t, b, _n) in zip(liste, infos[:25]))
    if len(infos) > 25:
        text += "\n… und %d weitere" % (len(infos) - 25)
    _k, (kreis, b, z) = wkz.parse_plate(liste[0])
    knoepfe = [("Portal öffnen", PORTAL), ("Am Auto ansehen", "https://takeo.cx/kennzeichen/?k=%s-%s-%dE" % (kreis, b, z))]
    # laut (5) nur bei guten Funden und beim Bruder, der Rest kommt still (2)
    sende(topic, titel, text, 5 if dringend or gut(liste[0]) else 2, knoepfe)


def sende(topic, titel, text, prio, knoepfe=()):
    # Als JSON statt Kopfzeilen: Umlaute im Titel und Knoepfe kommen so sauber an
    nachricht = {"topic": topic, "title": titel, "message": text, "priority": prio, "click": PORTAL,
                 "actions": [{"action": "view", "label": l, "url": u} for l, u in knoepfe]}
    headers = {"Content-Type": "application/json"}
    # Mit Token zaehlt die Nachricht zum ntfy-Account (Pro-Limits) statt zur geteilten Runner-IP (250/Tag)
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(nachricht).encode("utf-8"), method="POST", headers=headers)
    urllib.request.urlopen(req, timeout=20).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trocken", action="store_true", help="nichts senden, nur ausgeben")
    ap.add_argument("--testpush", action="store_true", help="nur eine Test-Meldung an NTFY_TOPIC senden")
    args = ap.parse_args()

    if args.testpush:
        # Beispiel-Fund im echten Format, deutlich als Test markiert
        weg = "über den ntfy-Account (Token)" if os.environ.get("NTFY_TOKEN") else "anonym (ohne Token)"
        melde(os.environ["NTFY_TOPIC"], {"SG-YY5", "SG-QW6", "SG-N57"}, praefix="TEST – ")
        print("Test-Meldung gesendet, " + weg)
        return 0

    try:
        zustand = json.load(open(ZUSTAND, encoding="utf-8"))
    except (IOError, ValueError):
        zustand = {"jobs": {}}
    erster_lauf = not zustand["jobs"]
    alt_alle = {p for ps in zustand["jobs"].values() for p in ps}
    jobs_vorher = set(zustand["jobs"])
    topic = os.environ.get("NTFY_TOPIC")
    topic_bruder = os.environ.get("NTFY_TOPIC_BRUDER") or topic

    t0 = time.time()
    portale = queue.Queue()
    try:
        for _ in range(PARALLEL):
            portale.put(wkz.Portal("solingen_stadt", "elektro", delay=0).start())
    except RuntimeError as e:
        # Portal meldet selbst eine Stoerung - kein Fehler des Waechters, still ueberspringen.
        # Alles andere (z. B. geaenderte Seite) soll weiter als Fehler auffallen.
        if any(h in str(e) for h in PORTAL_STOERUNG):
            print("%s | Portal gestoert, Lauf uebersprungen: %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), e))
            return 0
        raise

    def mit_portal(job):
        # Jede Sitzung hat ihr eigenes Cookie - sie duerfen sich nicht ueberholen
        portal = portale.get()
        try:
            return frage_ab(portal, job)
        finally:
            portale.put(portal)

    neu, weg, fehler, suchen, push_fehler = set(), set(), [], 0, []
    gemeldet = set()  # (Thema, Kennzeichen): X/Y/Z und JS stecken in zwei Jobs - nur einmal melden
    pool = ThreadPoolExecutor(PARALLEL)
    laeufe = {pool.submit(mit_portal, job): job for job in JOBS}   # Reihenfolge bleibt: X/Y/Z starten zuerst
    for fertig in as_completed(laeufe):
        job = laeufe[fertig]
        plates, n = fertig.result()
        suchen += n
        key = schluessel(job)
        if plates is None:
            fehler.append(key)
            continue
        if key in zustand["jobs"]:
            alt = set(zustand["jobs"][key])
        elif alt_alle and job[2] == job[3] == K and any(umfasst(k.split("|"), job) for k in jobs_vorher):
            # Neuer Job nach einem Umbau: Stand aus den alten Jobs uebernehmen,
            # sonst waeren seine freien Kennzeichen beim ersten Lauf unbekannt.
            # Deckte vorher kein Job diesen Bereich ab, gilt der erste Lauf als Ausgangsstand (keine Push-Flut).
            alt = {p for p in alt_alle if deckt(job, p)}
        else:
            alt = set(plates)
        fuer_bruder = job in JOBS_BRUDER
        an = topic_bruder if fuer_bruder else topic
        neu_hier = {p for p in set(plates) - alt if (an, p) not in gemeldet}
        gemeldet |= {(an, p) for p in neu_hier}
        neu |= neu_hier
        weg |= alt - set(plates)
        zustand["jobs"][key] = plates
        if neu_hier and not erster_lauf and not args.trocken:
            if not an:
                push_fehler.append("NTFY_TOPIC fehlt")
                continue
            try:
                melde(an, neu_hier, dringend=fuer_bruder)
            except Exception as e:
                push_fehler.append(str(e))
    pool.shutdown()

    # Jobs, die es nicht mehr gibt, rauswerfen - aber nur nach einem fehlerfreien Lauf,
    # sonst fehlt ihr Stand, solange der neue Job noch keinen eigenen hat
    aktuell = {schluessel(j) for j in JOBS}
    umgebaut = not fehler and jobs_vorher != aktuell
    if umgebaut:
        zustand["jobs"] = {k: v for k, v in zustand["jobs"].items() if k in aktuell}

    alle = {p for ps in zustand["jobs"].values() for p in ps}
    # Nur bei Aenderungen schreiben - sonst wuerde jeder Lauf einen Commit erzeugen
    if neu or weg or erster_lauf or umgebaut:
        zustand["zeit"] = time.strftime("%Y-%m-%d %H:%M:%S")
        zustand["frei_gesamt"] = len(alle)
        zustand["letzte_aenderung"] = {"neu": sorted(neu), "weg": sorted(weg)}
        with open(ZUSTAND, "w", encoding="utf-8") as f:
            json.dump(zustand, f, ensure_ascii=False, indent=0, sort_keys=True)

    print("%s | %d Suchen in %.0f s | %d frei ueberwacht | neu: %s | weg: %s | Fehler: %s"
          % (time.strftime("%Y-%m-%d %H:%M:%S"), suchen, time.time() - t0, len(alle),
             ", ".join(beschreibung(p)[0] for p in sorted(neu)) or "-",
             ", ".join(beschreibung(p)[0] for p in sorted(weg)) or "-",
             ", ".join(fehler) or "-"))

    if push_fehler:
        print("Meldung nicht gesendet: " + "; ".join(push_fehler))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
