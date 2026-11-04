# Kennzeichen-Wächter Solingen

Fragt tagsüber alle 5 Minuten (Worker `ping/`, Var `TAKT_MIN`) das Wunschkennzeichen-Portal
der Stadt Solingen ab und meldet per [ntfy](https://ntfy.sh), wenn ein kurzes
E-Kennzeichen neu frei wird. Nur lesend – es wird nichts reserviert.

Überwacht:

- alle 3-stelligen (SG AB 1E, SG A 12E)
- ein Buchstabe mit Schnapszahl, glatter Zahl oder Folge (SG A 444E, SG A 300E, SG A 123E)
- Doppelbuchstabe mit Schnaps- oder glatter Zahl (SG AA 44E, SG AA 30E)
- für den Bruder: JS kurz oder mit Schnaps-/glatter Zahl/Folge (SG JS 7E, SG JS 77E,
  SG JS 777E, SG JS 200E, SG JS 123E) – Meldung an `NTFY_TOPIC_BRUDER`, solange das
  Secret fehlt an `NTFY_TOPIC`

Das Portal schafft etwa eine Suche pro Sekunde, ein Lauf braucht rund 60 Suchen.
X, Y und Z werden in jedem Lauf zuerst abgefragt, und jeder Fund wird sofort gemeldet
(nicht erst am Ende des Laufs).
`zustand.json` ist das Gedächtnis und wird nur bei Änderungen committet.
Secrets: `NTFY_TOPIC`, optional `NTFY_TOPIC_BRUDER`.

## Sofort-Start: Worker `ping/`

GitHub drosselt den Cron oft auf alle paar Stunden. Der Cloudflare-Worker in `ping/` fragt deshalb
jede Minute nur die Startseite des Portals ab und startet den Wächter per `workflow_dispatch`:
sofort, wenn das Portal nach einer Störung wieder antwortet, sonst alle 5 Minuten. Solange das
Portal gestört ist, startet er nichts. Der GitHub-Cron bleibt als Reserve.

Morgen-Fenster: Abgelaufene Reservierungen gibt das Portal täglich gegen 7:00 frei.
Zwischen 06:50 und 07:30 (Var `MORGEN`) startet der Worker deshalb einmal eine Dauerschleife
(`workflow_dispatch` mit `minuten`), die Runde um Runde durchläuft – ein Fund ist dann nach
wenigen Minuten gemeldet statt nach bis zu 14.

Wunsch-Kennzeichen prüft der Worker jede Minute einzeln: `WUNSCH` (Push an `NTFY_TOPIC`) und
`WUNSCH_BRUDER` (Push zusätzlich an das Worker-Secret `NTFY_TOPIC_BRUDER`).

Einrichten (einmalig):

1. GitHub → Settings → Developer settings → Fine-grained tokens → neues Token:
   nur Repository `kennzeichen-waechter`, Berechtigung **Actions: Read and write**.
2. Im Ordner `ping/`: `wrangler secret put GITHUB_TOKEN` (Token einfügen) und `wrangler deploy`.

Stand ansehen: `https://kennzeichen-ping.<konto>.workers.dev/` (JSON), Protokoll: `wrangler tail`.
