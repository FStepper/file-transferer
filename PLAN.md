# VidTransfer – Plan

Ziel: Einen Ordner mit großen MP4-/WAV-Dateien über eine instabile Verbindung
übertragen. Der Transport läuft über den lokal installierten Google-Drive-Client
(Google Drive for desktop). Zielplattform ist **macOS** (Apple Silicon M2, ggf.
Intel). Die App wird als eigenständiges `.app`-Bundle ausgeliefert, also ohne
Python-Installation.

## Bedienung

- **Senden:** Ordner auf das App-Symbol (Finder/Dock) ziehen. Die App legt in
  Google Drive einen Ordner **mit demselben Namen** an und schreibt dort die Pakete hinein.
- **Empfangen:** Den Ordner aus Google Drive auf das App-Symbol ziehen und das Ziel wählen.
  Die App prüft und repariert die Pakete, entpackt sie und prüft jede Datei per SHA-256.
- Alternativ gibt es Buttons im Fenster und eine CLI (`vidtransfer send|receive|status`).

## Pipeline

```
Sender:    Ordner → tar-Stream → zstd (Level 3) → Stripes (k × Blockgröße)
           → Reed-Solomon (zfec): k Daten- + m Paritätspakete → Drive-Ordner
Empfänger: Pakete prüfen (Header-CRC + SHA-256) → fehlende/kaputte per Parität
           rekonstruieren → zstd → tar → Dateien + SHA-256-Prüfung
```

- **Kompression:** MP4 lässt sich praktisch nicht weiter komprimieren, WAV um
  einige Prozent. zstd Level 3 ist schnell genug, dass es nie der Engpass ist.
- **Fehlertoleranz:** Standard ist k = 10 und m = 3 bei 32 MiB pro Paket. Aus
  *beliebigen* 10 der 13 Pakete eines Stripes lässt sich der Stripe vollständig
  wiederherstellen. Der Mehraufwand liegt bei 30 %.
- **Paketformat (`sNNNNNN_II.vtp`):** 80-Byte-Header (Magic, Version, k, m,
  Index, Transfer-ID, Stripe-Nr., Stripe-Länge, Payload-Länge, SHA-256, CRC32),
  danach die Nutzdaten. Jedes Paket ist damit für sich allein prüfbar.
- **Manifest:** Liegt 3× redundant vor (`manifest.json`, `.2`, `.3`) und hat eine
  eigene Prüfsumme. Es enthält Dateiliste, Datei-Hashes, Paket-Hashes und Parameter.
- **Speicher/Platz:** Es wird immer nur ein Stripe lokal zwischengespeichert
  (≈ 0,8 GB). Ins Drive kommen nur fertige Pakete (tmp-Datei + atomares Umbenennen).

## Robustheit

- **Sender abgebrochen:** Einfach neu starten. Der tar/zstd-Stream ist
  deterministisch, bereits vorhandene identische Pakete werden übersprungen. Ein
  erneuter Sendelauf repariert auch kaputte Pakete im Drive.
- **Empfang während des Uploads:** Der Empfänger arbeitet Stripe für Stripe und
  wartet, bis jeweils mindestens k gültige Pakete angekommen sind.
- **Ruhezustand:** Solange die App offen ist, verhindert `caffeinate`, dass der
  Mac einschläft. Das ist wichtig, damit Drive weiter hochladen kann.

## Umsetzung

1. Kern: Paketformat, Erasure Coding, Sender und Empfänger, Tests mit verlorenen und kaputten Paketen
2. Drive-Erkennung (`~/Library/CloudStorage/GoogleDrive-*/My Drive`), Konfiguration
3. Tk-GUI mit Drag & Drop auf das App-Symbol (`::tk::mac::OpenDocument`), Fortschritt
4. PyInstaller-`.app` (arm64 + x86_64) per Build-Skript und GitHub Actions
