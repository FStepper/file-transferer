# VidTransfer – Plan

Ziel: Einen Ordner mit großen MP4-/WAV-Dateien über eine instabile Verbindung
übertragen und auf der Gegenseite **zuverlässig wieder entpacken**. Der Transport
läuft über den lokal installierten Google-Drive-Client (Google Drive for desktop).
Zielplattform ist **macOS** (Apple Silicon M2, ggf. Intel), entwickelt wird unter
Windows. Die App wird als eigenständiges `.app`-Bundle ausgeliefert, also ohne
Python-Installation.

## Bedienung

- **Senden:** Ordner auf das App-Symbol (Finder/Dock) ziehen. Die App legt in
  Google Drive einen Ordner **mit demselben Namen** an und schreibt dort die Pakete hinein.
- **Entpacken:** Den Ordner aus Google Drive auf das App-Symbol ziehen und das Ziel
  wählen. Die App prüft und repariert die Pakete, entpackt sie und prüft jede Datei
  per SHA-256 (Details: [Entpacken](#entpacken)).
- Alternativ gibt es Buttons im Fenster und eine CLI
  (`vidtransfer send|receive|verify|status|list`).

## Pipeline

```
Sender:    Ordner → Container v2 (pro Datei: Record-Header + roh/zstd) → Stripes
           → Reed-Solomon (zfec): k Daten- + m Paritätspakete → Drive-Ordner
Empfänger: Index lesen → Pakete vorab laden + prüfen (parallel) → fehlende/kaputte
           per Parität rekonstruieren → Records → Dateien (atomar) + SHA-256
```

- **Kompression pro Datei:** MP4/MOV/MXF werden roh gespeichert, weil sie sich
  praktisch nicht weiter komprimieren lassen. WAV/AIFF und Sonstiges werden per
  zstd Level 3 komprimiert, aber nur, wenn eine Probe (erste 4 MiB) mindestens 3 %
  spart. zstd ist schnell genug, dass es nie der Engpass ist.
- **Fehlertoleranz:** Standard ist k = 10 und m = 3 bei 32 MiB pro Paket. Aus
  *beliebigen* 10 der 13 Pakete eines Stripes lässt sich der Stripe vollständig
  wiederherstellen. Der Mehraufwand liegt bei 30 %.
- **Paketformat (`sNNNNNN_II.vtp`):** 80-Byte-Header (Magic, Version, k, m,
  Index, Transfer-ID, Stripe-Nr., Stripe-Länge, Payload-Länge, SHA-256, CRC32),
  danach die Nutzdaten. Jedes Paket ist damit für sich allein prüfbar.
- **Manifest:** Liegt 3× redundant vor (`manifest.json`, `.2`, `.3`) und hat eine
  eigene Prüfsumme. Es enthält Parameter, Paket-Hashes und den **Datei-Index**
  (siehe unten).
- **Speicher/Platz:** Es wird immer nur ein Stripe lokal zwischengespeichert
  (≈ 0,8 GB). Ins Drive kommen nur fertige Pakete (tmp-Datei + atomares Umbenennen).

## Container-Format v2 (Grundlage für gutes Entpacken)

v1 ist ein einziger zstd-komprimierter tar-Strom. Das ist einfach, hat aber eine
Schwäche: Ab einer nicht rettbaren Stelle ist **alles Folgende** verloren, und
man kann nicht gezielt zu einer Datei springen. v2 ersetzt tar + Gesamt-zstd
durch einen eigenen, einfachen Container:

```
Strom = Record | Record | … | End-Record

Record = [Record-Header][Daten]
  Record-Header (fest + variabel, mit CRC32):
    Magic "VTRC", Version, Typ (Datei/Ordner/Ende), Flags (roh|zstd),
    Pfad (UTF-8, NFC), Größe original, Größe gespeichert, mtime, Modus,
    SHA-256 des Originals, CRC32 über den Header
  Daten: Originalbytes oder genau ein zstd-Frame
```

Dazu kommt im Manifest ein **Index**: für jede Datei Pfad, Größe, SHA-256, mtime,
Kompression sowie der **Offset und die Länge im Strom**. Daraus ergibt sich direkt,
welche Stripes eine Datei belegt.

Was das bringt:

| Fähigkeit | v1 (tar+zstd) | v2 |
|---|---|---|
| Entpacken während des Uploads (Streaming) | ✔ | ✔ (Record-Header sind selbstbeschreibend) |
| Fortsetzen nach Abbruch | ✖ (von vorn) | ✔ (fertige Dateien überspringen, an Offset springen) |
| Einzelne Dateien wiederherstellen | ✖ | ✔ (nur die benötigten Stripes laden) |
| Teilrettung bei nicht rettbarem Stripe | ✖ (Rest verloren) | ✔ (nur betroffene Dateien fehlen) |
| Ohne Manifest entpacken (Notfall) | eingeschränkt | ✔ (Resync über Magic + CRC) |

v1-Transfers bleiben lesbar. Der Empfänger erkennt das Format an
`manifest.format_version`.

## Entpacken

### Ablauf

1. **Erkennen:** Ein Ordner mit `manifest*.json` gilt als Transfer-Ordner. Eine
   gültige Kopie wird gewählt, eine abgeschlossene hat Vorrang.
2. **Vorabprüfung**, bevor etwas geschrieben wird:
   - **Freier Speicher** am Ziel ≥ Summe der Originalgrößen + 1 Stripe +
     5 % Reserve. Andernfalls gibt es eine klare Meldung mit den genauen Zahlen.
   - **Ziel:** `<Ziel>/<Name>`. Existiert der Ordner schon und stammt er vom
     *selben* Transfer (Statusdatei, siehe unten), wird **fortgesetzt**. Sonst wird
     `<Name> (2)` angelegt.
   - **Schneller Status** nur über Header und Dateigrößen: Wie viele Stripes sind
     vollständig, reparierbar oder fehlen noch? Das wird in der GUI angezeigt,
     bevor gelesen wird.
3. **Pakete beschaffen (Prefetch):** Ein Thread-Pool (4 Threads) liest die
   Pakete der nächsten 2–3 Stripes voraus. Im Drive-Modus „Streamen“ stößt genau
   das den Download an, und zwar parallel statt Datei für Datei. Dabei wird gleich
   der SHA-256 geprüft (hashlib gibt den GIL frei, nutzt also mehrere Kerne).
   - Zuerst werden die Datenpakete gelesen. Paritätspakete werden nur
     angefasst, wenn ein Datenpaket fehlt oder defekt ist. Das spart Download.
   - **Timeout pro Paket:** Kommt nach 5 min kein Fortschritt (Platzhalter hängt),
     gilt das Paket vorerst als fehlend. Steht Parität bereit, geht es damit
     weiter. Sonst wird später erneut versucht.
4. **Stripe zusammensetzen:** Bei k gültigen Datenpaketen werden sie direkt
   gelesen, sonst wird per zfec rekonstruiert (in 1-MiB-Scheiben, wenig RAM).
   Gibt es weniger als k gültige Pakete, wird gewartet (Upload läuft noch / Drive
   synchronisiert noch), mit Anzeige „Stripe 17: 8/10 – warte …“.
5. **Records schreiben:**
   - Jede Datei wird zuerst als `.<name>.vtpart` geschrieben. Erst wenn die
     Länge stimmt und der **SHA-256 passt**, wird sie per `fsync` gesichert und
     atomar umbenannt. Danach wird die mtime gesetzt. Eine halbe oder falsche Datei
     liegt also nie unter dem echten Namen.
   - Ordner (auch leere) werden angelegt, ihre mtimes am Ende gesetzt.
   - Nach jeder fertigen Datei wird die Statusdatei aktualisiert.
6. **Abschluss:**
   - Jede Datei im Index muss vorhanden und geprüft sein. Es darf keine Dateien
     geben, die nicht im Index stehen.
   - Ein **Empfangsbericht** (`VidTransfer-Bericht.txt`) wird geschrieben: Dateien,
     Größen, SHA-256, reparierte Stripes, Dauer.
   - Erst danach wird `<Name> (wird empfangen)` in `<Name>` umbenannt.
   - Anschließend gibt es eine Mitteilung und die Buttons **„Im Finder zeigen“**
     und optional **„Transfer-Ordner in Drive löschen …“** (mit Rückfrage, nie
     automatisch).

### Fortsetzen nach Abbruch

- Im Arbeitsordner liegt `.vidtransfer-state.json` mit Transfer-ID, Format-Version
  und der Liste fertiger, geprüfter Dateien (Pfad, Größe, SHA-256).
- Beim Neustart werden fertige Dateien kurz geprüft (Größe + mtime, bei
  Unsicherheit SHA-256) und übersprungen. Das Lesen beginnt beim Stripe der ersten
  unfertigen Datei. Mit v2 ist das ein direkter Sprung über den Index.
- `.vtpart`-Reste werden verworfen und neu geschrieben.

### Teilrettung („Notfall“)

Ist ein Stripe endgültig nicht rettbar (Upload abgeschlossen, weniger als k
gültige Pakete, Sender nicht mehr verfügbar), gibt es nach Rückfrage
**„Alles Rettbare wiederherstellen“**:
- Alle Dateien, deren Stripes intakt sind, werden normal entpackt und geprüft.
- Die betroffenen Dateien werden im Bericht aufgeführt, mit dem Hinweis, dass der
  Sender genau diese Stripes neu senden kann. Der Sendelauf ist deterministisch und
  schreibt nur fehlende Pakete neu.
- **Optional bei MP4:** Die beschädigte Datei wird mit Nullen an den Lücken als
  `name.BESCHÄDIGT.mp4` geschrieben, weil Player oft den Rest noch abspielen. Das
  ist standardmäßig aus.
- **Ohne Manifest** (alle drei Kopien verloren): Records werden über Magic und CRC
  gefunden. Ohne Index lässt sich nicht prüfen, ob alles vollständig ist. Das
  wird klar angezeigt.

### Weitere Funktionen

- `vidtransfer verify <ordner>` bzw. Button **„Nur prüfen“**: lädt und prüft alle
  Pakete und meldet, ob alles wiederherstellbar ist, ohne zu entpacken. Das ist
  nützlich, bevor der Sender seine Daten löscht.
- `vidtransfer list <ordner>`: zeigt Dateiliste und Größen aus dem Index.
- `vidtransfer receive <ordner> <ziel> --only "Audio/*.wav"`: stellt einzelne
  Dateien wieder her (lädt nur die nötigen Stripes).

### Sicherheit und Plattform-Details

- **Pfade:** keine absoluten Pfade, kein `..`, keine Symlinks/Geräte. Alles bleibt
  unter dem Zielordner.
- **Unicode:** Pfade werden auf NFC normalisiert (macOS liefert NFD). Damit
  stimmen Namen und Hashes plattformübergreifend.
- **Windows als Ziel** (falls nötig): unzulässige Zeichen (`:*?"<>|`) und
  reservierte Namen (`CON`, `NUL`, …) werden ersetzt und im Bericht vermerkt.
  Lange Pfade bekommen das Präfix `\\?\`.
- **Groß/klein:** APFS und NTFS unterscheiden standardmäßig nicht zwischen Groß-
  und Kleinschreibung. Kollidieren zwei Pfade, wird umbenannt und gewarnt.
- **Ruhezustand:** `caffeinate` läuft auch während des Entpackens.

## Robustheit (Senden)

- **Sender abgebrochen:** Einfach neu starten. Der Strom ist deterministisch,
  bereits vorhandene identische Pakete werden übersprungen. Ein erneuter Sendelauf
  repariert auch kaputte Pakete im Drive.
- **Manifest während des Uploads:** Wird nach jedem Stripe aktualisiert (Index bis
  dahin, Status `in_progress`). So kann der Empfänger schon fertige Dateien
  zuordnen.
- **Ruhezustand:** Solange die App offen ist, verhindert `caffeinate`, dass der
  Mac einschläft. Das ist wichtig, damit Drive weiter hochladen kann.

## Tests (Entpacken)

- Roundtrip v1 und v2, leere Dateien und Ordner, Unicode/NFD-Namen, sehr viele kleine Dateien
- Pro Stripe bis zu m Pakete gelöscht, mit Bitfehlern oder abgeschnitten → identisches Ergebnis
- m+1 Verluste in einem Stripe → Teilrettung: alle nicht betroffenen Dateien korrekt, Bericht korrekt
- Abbruch mitten in einer Datei → Fortsetzen überspringt fertige Dateien, `.vtpart` wird neu geschrieben
- Zu wenig Speicherplatz → klare Meldung, nichts geschrieben
- Hängendes Paket (simulierter blockierender Read) → Timeout, Parität wird genutzt
- Bösartige Pfade (`../x`, absolut) → werden abgelehnt
- Alle Manifeste gelöscht → Notfall-Scan findet alle Records

## Umsetzung

1. ✔ Kern: Paketformat, Erasure Coding, Sender und Empfänger (v1), Tests mit verlorenen und kaputten Paketen
2. ✔ Drive-Erkennung (`~/Library/CloudStorage/GoogleDrive-*/My Drive`), Konfiguration
3. ✔ Tk-GUI mit Drag & Drop auf das App-Symbol (`::tk::mac::OpenDocument`), Fortschritt
4. ✔ PyInstaller-`.app` (arm64 + x86_64) per Build-Skript und GitHub Actions
5. **Entpacken, Teil 1:** Speicherplatzprüfung, atomares Schreiben pro Datei,
   Statusdatei + Fortsetzen, Empfangsbericht, `verify`, GUI-Buttons „Nur prüfen“ /
   „Im Finder zeigen“
6. **Entpacken, Teil 2:** Prefetch-Pool mit paralleler Prüfung und Timeout
7. **Container v2:** Record-Format, Kompression pro Datei, Index im Manifest
   (inkrementell), v1 bleibt lesbar
8. **Entpacken, Teil 3 (braucht v2):** Sprung beim Fortsetzen, `--only`,
   Teilrettung, Notfall-Scan ohne Manifest
