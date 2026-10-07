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

Nur lesend - es wird nichts reserviert. Gedaechtnis: zustand.json.
Eine Suche, die fehlschlaegt, behaelt ihren alten Stand. So entstehen
keine falschen "neu frei"-Meldungen.

  NTFY_TOPIC=... python3 waechter.py          # normaler Lauf
  python3 waechter.py --trocken                # nur anzeigen, nichts senden
"""

import argparse
import json
import os
import sys
import time
import urllib.request

import wkz

K = "keine"
JOBS = (
    [("?", "??", K, K)] + [(b + "?", "?", K, K) for b in wkz.LETTERS]          # alle 3-stelligen
    + [("?", "???", K, z) for z in ("schnaps", "glatt", "auf", "ab")]          # A 444, A 300, A 123, A 321
    + [("??", "??", "gleich", z) for z in ("schnaps", "glatt")]                # AA 44, AA 30
)
# Bewusst nicht: AB 44 / AB 30 / AA 47 - davon ist noch die Haelfte frei, das waeren
# pro Lauf ueber 1000 Suchen bei einem Portal, das nur ~1 Suche pro Sekunde schafft.
PORTAL = "https://kfz-egov.regioit.de/wkz/?LICENSEIDENTIFIER=solingen_stadt"
ZUSTAND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zustand.json")


def schluessel(job):
    return "|".join(job)


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


def melde(topic, neu):
    infos = sorted((beschreibung(p) for p in neu), key=lambda x: (x[2], x[0]))
    kurz = any(n == 3 for _t, _b, n in infos) or any(c in t.split()[1] for t, _b, _n in infos for c in "XYZ")
    titel = ("%s frei" % infos[0][0]) if len(infos) == 1 else "%d Kennzeichen neu frei" % len(infos)
    text = "\n".join("%s – %s" % (t, b) for t, b, _n in infos[:25])
    if len(infos) > 25:
        text += "\n… und %d weitere" % (len(infos) - 25)
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic, data=text.encode("utf-8"), method="POST",
        headers={"Title": titel, "Priority": "urgent" if kurz else "high", "Click": PORTAL})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trocken", action="store_true", help="nichts senden, nur ausgeben")
    args = ap.parse_args()

    try:
        zustand = json.load(open(ZUSTAND, encoding="utf-8"))
    except (IOError, ValueError):
        zustand = {"jobs": {}}
    erster_lauf = not zustand["jobs"]

    t0 = time.time()
    portal = wkz.Portal("solingen_stadt", "elektro", delay=0.5)
    portal.start()

    neu, weg, fehler, suchen = set(), set(), [], 0
    for job in JOBS:
        plates, n = frage_ab(portal, job)
        suchen += n
        key = schluessel(job)
        if plates is None:
            fehler.append(key)
            continue
        alt = set(zustand["jobs"].get(key, plates))
        neu |= set(plates) - alt
        weg |= alt - set(plates)
        zustand["jobs"][key] = plates

    alle = {p for ps in zustand["jobs"].values() for p in ps}
    # Nur bei Aenderungen schreiben - sonst wuerde jeder Lauf einen Commit erzeugen
    if neu or weg or erster_lauf:
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

    if neu and not erster_lauf and not args.trocken:
        topic = os.environ.get("NTFY_TOPIC")
        if not topic:
            print("NTFY_TOPIC fehlt - keine Meldung gesendet")
            return 1
        melde(topic, neu)
    return 0


if __name__ == "__main__":
    sys.exit(main())
