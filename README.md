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
