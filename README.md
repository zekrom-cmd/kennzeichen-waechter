# Kennzeichen-Wächter Solingen

Fragt alle 10 Minuten (GitHub-Cron, real eher 10–20 Min.) das Wunschkennzeichen-Portal
der Stadt Solingen ab und meldet per [ntfy](https://ntfy.sh), wenn ein kurzes
E-Kennzeichen neu frei wird. Nur lesend – es wird nichts reserviert.

Überwacht:

- alle 3-stelligen (SG AB 1E, SG A 12E)
- ein Buchstabe mit Schnapszahl, glatter Zahl oder Folge (SG A 444E, SG A 300E, SG A 123E)
- Doppelbuchstabe mit Schnaps- oder glatter Zahl (SG AA 44E, SG AA 30E)

Das Portal schafft etwa eine Suche pro Sekunde, ein Lauf braucht rund 60 Suchen.
`zustand.json` ist das Gedächtnis und wird nur bei Änderungen committet.
Secret: `NTFY_TOPIC`.

## Sofort-Start: Worker `ping/`

GitHub drosselt den Cron oft auf alle paar Stunden. Der Cloudflare-Worker in `ping/` fragt deshalb
jede Minute nur die Startseite des Portals ab und startet den Wächter per `workflow_dispatch`:
sofort, wenn das Portal nach einer Störung wieder antwortet, sonst alle 10 Minuten. Solange das
Portal gestört ist, startet er nichts. Der GitHub-Cron bleibt als Reserve.

Einrichten (einmalig):

1. GitHub → Settings → Developer settings → Fine-grained tokens → neues Token:
   nur Repository `kennzeichen-waechter`, Berechtigung **Actions: Read and write**.
2. Im Ordner `ping/`: `wrangler secret put GITHUB_TOKEN` (Token einfügen) und `wrangler deploy`.

Stand ansehen: `https://kennzeichen-ping.<konto>.workers.dev/` (JSON), Protokoll: `wrangler tail`.
