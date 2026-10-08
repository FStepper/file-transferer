# VidTransfer

Überträgt große Ordner (MP4, WAV, …) zuverlässig über **Google Drive for desktop**,
auch wenn die Verbindung instabil ist. Der Ordner wird in Pakete mit
Reed-Solomon-Parität zerlegt. Fehlen Pakete oder kommen sie beschädigt an,
rekonstruiert der Empfänger sie automatisch. Jede Datei wird am Ende per SHA-256
geprüft.

## Bedienung (Mac)

**Senden**
1. Einen Ordner (z. B. `Drehtag 3`) auf das **VidTransfer-Symbol** im Dock oder Finder ziehen.
2. Die App legt in Google Drive den Ordner `Drehtag 3` an und schreibt dort die Pakete hinein.
3. Google Drive lädt die Pakete hoch. **Die App so lange geöffnet lassen**, denn sie
   verhindert, dass der Mac in den Ruhezustand geht.

Bricht etwas ab (Absturz, Neustart, Ordner geändert)? Einfach denselben Ordner erneut
draufziehen. Vorhandene Pakete werden übersprungen, defekte ersetzt.

**Empfangen**
1. Den Ordner `Drehtag 3` aus Google Drive auf das VidTransfer-Symbol ziehen.
2. Das Ziel wählen (Standard: Downloads).
3. Die App prüft die Pakete, repariert fehlende, entpackt alles und prüft jede Datei.
   Das geht auch schon, während der Sender noch hochlädt. Die App wartet dann auf
   die fehlenden Pakete.

Beim Empfang entsteht zunächst `Drehtag 3 (wird empfangen)`. Erst wenn alles
geprüft ist, wird der Ordner in `Drehtag 3` umbenannt.

> Tipp: Im Drive-Modus „Streamen“ lädt Drive die Pakete erst beim Lesen herunter.
> Das funktioniert, ist aber langsamer. Für den Transfer-Ordner kann man in Drive
> „Offline verfügbar machen“ wählen.

## Fehlertoleranz

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `k` | 10 | Datenpakete pro Stripe |
| `m` | 3 | Paritätspakete pro Stripe: bis zu 3 von 13 Paketen dürfen fehlen oder defekt sein |
| `block_mib` | 32 | Paketgröße in MiB |
| `zstd_level` | 3 | Kompression (bringt bei MP4 fast nichts, bei WAV ein paar Prozent; 0 = aus) |

Der Mehraufwand liegt bei `m / k` = 30 %. Ändern lässt sich das in
`~/Library/Application Support/VidTransfer/config.json`. Das Log liegt unter
`~/Library/Logs/VidTransfer/`.

## Installation

Fertige App: `VidTransfer-macOS-arm64.zip` (Apple Silicon: M1/M2/…) bzw.
`VidTransfer-macOS-x86_64.zip` (Intel; läuft über Rosetta auch auf Apple Silicon).
Entpacken und `VidTransfer.app` nach **Programme** ziehen.

Die App ist nicht von Apple notarisiert. Beim ersten Start daher **Rechtsklick → Öffnen**.
Meldet macOS „App ist beschädigt“, einmal im Terminal ausführen:

```bash
xattr -dr com.apple.quarantine /Applications/VidTransfer.app
```

Beim ersten Zugriff fragt macOS nach der Erlaubnis für Google Drive bzw.
Downloads. Bitte erlauben.

## App bauen

Auf dem Mac (Python ≥ 3.10 von [python.org](https://www.python.org/downloads/macos/)):

```bash
./packaging/build_mac.sh
```

Die Intel-Version lässt sich auf dem M2 ebenfalls bauen (läuft über Rosetta):

```bash
./packaging/build_mac.sh x86_64
```

Ergebnis: `dist/<arch>/VidTransfer.app` und `dist/VidTransfer-macOS-<arch>.zip`.
Alternativ baut der GitHub-Workflow `build-macos` beide Varianten (manuell starten
oder ein Tag `v*` pushen).

## Kommandozeile

```bash
/Applications/VidTransfer.app/Contents/MacOS/VidTransfer send   ~/Videos/Drehtag3
/Applications/VidTransfer.app/Contents/MacOS/VidTransfer status "~/Library/CloudStorage/GoogleDrive-…/My Drive/Drehtag3"
/Applications/VidTransfer.app/Contents/MacOS/VidTransfer receive "<Drive-Ordner>" ~/Downloads
```

## Entwicklung (Windows)

Entwickelt wird unter Windows. Der Kern, die CLI und die GUI laufen dort genauso.
Nur das `.app`-Bundle und der Drop aufs Dock-Symbol sind Mac-spezifisch.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m vidtransfer        # GUI
```

Eine Test-EXE für Windows (Ordner lassen sich auf die EXE ziehen) baut
`packaging\build_windows.ps1`. **Die Mac-App lässt sich nicht unter Windows bauen**,
denn PyInstaller kann nicht cross-kompilieren. Es gibt zwei Wege:
- Repo nach GitHub pushen und unter *Actions → build-macos → Run workflow* starten.
  Das ZIP für arm64 und x86_64 liegt danach als Artefakt bereit.
- Auf dem M2 `./packaging/build_mac.sh` ausführen.

Der Aufbau ist in [PLAN.md](PLAN.md) beschrieben.
