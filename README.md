# VidTransfer

Transfers large folders (MP4, WAV, …) reliably via **Google Drive for desktop**,
even over an unstable connection. The folder is split into packets with
Reed-Solomon parity. If packets go missing or arrive damaged, the receiver
reconstructs them automatically, and every file is verified with SHA-256 at the end.

> The app's user interface is in German; this documentation is in English.

## Usage

The window has a switch at the top: **Senden | Empfangen**. The app remembers the
last mode. Each mode shows one drop zone and the list of deliveries; every
delivery has one status line and at most one button. Everything else is in
**Einstellungen** (computer name, Google Drive folder, inbox, verifying a
delivery, log, advanced parameters).

**Sending**
1. Drag a folder (e.g. `Shoot Day 3`) onto the window or onto the VidTransfer
   icon in the Dock – or click **„Ordner auswählen …“**. Loose files can be
   dropped too; they become one delivery called `Dateien <date>`.
2. The card shows *Wird vorbereitet – 45 % · noch ca. 6 Min.*
3. When it says **✔ Liegt in Google Drive**, the app's job is done. Google Drive
   then uploads in the background – the Mac should stay online until Google Drive
   shows it as synced. The app keeps the Mac awake only while it is preparing.

Something went wrong (crash, restart, folder changed)? Click **Fortsetzen** or
**Erneut senden** on the card, or drop the same folder again. Existing packets are
skipped and damaged ones replaced.

**Receiving**
1. Switch to **Empfangen**, then drag the delivery folder from Google Drive onto
   the window (or click **„Lieferung auswählen …“**). Dropping a delivery in mode
   „Senden“ works as well – the app switches over by itself.
2. The delivery goes straight into the inbox, by default `~/Movies/VidTransfer`
   (changeable in the settings). The card shows who sent it.
3. The app verifies the packets, repairs missing ones, unpacks everything and
   verifies every file. This already works while the sender is still uploading;
   the card then says *Wartet auf Google Drive …*.
4. **✔ Angekommen** – **„Im Finder zeigen“** opens the folder.

Problems are shown in one plain sentence with one button that helps, e.g.
*„Der Google-Drive-Ordner ist nicht erreichbar. Läuft Google Drive?“* →
**Google Drive öffnen**, or *„Auf diesem Computer fehlen 35 GB Speicherplatz.“* →
**Anderen Speicherort wählen**. The technical details are behind „Details“
(or right-click on a card).

How unpacking stays safe:
- Everything is written to `Shoot Day 3 (receiving)` first. Only once every file has
  been verified is the folder renamed to `Shoot Day 3`.
- Each file is first written as a hidden `.<name>.vtpart` and only gets its real
  name once its SHA-256 matches. A half-written or corrupt file never appears under
  its real name.
- **Interrupted?** (stopped, crash, Mac switched off) Click **Fortsetzen**. The app
  jumps straight to the first unfinished file; finished files and the packets
  before it are not read again.
- Before anything is written, the app checks that there is **enough free space**.
- Packets are downloaded and verified **in parallel**, a few stripes ahead of the
  one being unpacked. A packet that hangs (e.g. a Drive placeholder that does not
  download) is skipped after 5 minutes and replaced by parity.
- At the end, `VidTransfer-Report.txt` in the restored folder lists every file with
  its size and SHA-256.

**If parts are lost for good**

If some packets are missing for good (the upload is complete, but Google Drive
does not have them), the card says *„Ein Teil der Lieferung fehlt in Google
Drive.“* → **Alles andere jetzt holen**. The result is a folder
`Shoot Day 3 (incomplete)`; the card says how many files are missing and asks to
send the folder again on the sending Mac (only the missing packets are written).
Then click **Fehlende Dateien holen** – the folder is completed and renamed to
`Shoot Day 3`.

If the content list (manifest) of a delivery is unreadable, the app offers an
emergency recovery from the packets alone. Every restored file is still
verified, but the app cannot tell whether files are missing.

**Verify only**

**Einstellungen → „Lieferung prüfen …“** (or `vidtransfer verify`) downloads and
checks all packets and the whole content, without unpacking anything. It reports
whether the delivery is completely recoverable.

> Tip: In Drive's "Stream files" mode, packets are only downloaded when they are
> read. That works, but it is slower. You can mark the delivery folder as
> "Available offline" in Drive.

## Testing locally without Google Drive

The target for sending does not have to be Google Drive: in **Einstellungen**,
click **„Ändern …“** next to „Senden nach (Google Drive)“ and choose any local
folder, e.g. `D:\VidTransfer-Test`. The app keeps that choice and never switches
back to the Drive folder by itself. Sending writes the delivery folder there; to
receive, drop that delivery folder onto the window in mode „Empfangen“.

Every folder dialog remembers the folder that was opened last. On the command
line, the target is given with `--drive`:

```bash
vidtransfer send "D:\Footage\Shoot" --drive "D:\VidTransfer-Test"
vidtransfer receive "D:\VidTransfer-Test\Shoot" "D:\Restored"
```

To keep test settings (and the delivery list) separate from your normal ones,
set the environment variable `VIDTRANSFER_CONFIG_DIR` to another folder before
starting the app.

## Fault tolerance

| Setting | Default | Meaning |
|---|---|---|
| `k` | 10 | data packets per stripe |
| `m` | 3 | parity packets per stripe: up to 3 of 13 packets may be missing or damaged |
| `block_mib` | 32 | packet size in MiB |
| `zstd_level` | 3 | compression level; 0 = off |

Compression is decided per file: video and other already compressed formats
(MP4, MOV, MXF, BRAW, JPEG, …) are stored as they are; everything else (e.g. WAV)
is compressed only if a sample shrinks by at least 3 %.

The overhead is `m / k` = 30 %. You can change these settings in
`~/Library/Application Support/VidTransfer/config.json`. The log is in
`~/Library/Logs/VidTransfer/`.

## Installation

Prebuilt app: `VidTransfer-macOS-arm64.zip` (Apple Silicon: M1/M2/…) or
`VidTransfer-macOS-x86_64.zip` (Intel; also runs on Apple Silicon via Rosetta).
Unzip it and drag `VidTransfer.app` into **Applications**.

The app is not notarized by Apple, so open it the first time via **right-click → Open**.
If macOS says the app "is damaged", run this once in Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/VidTransfer.app
```

The first time the app accesses Google Drive or Downloads, macOS asks for
permission. Please allow it.

## Building the app

On the Mac (Python ≥ 3.10 from [python.org](https://www.python.org/downloads/macos/)):

```bash
./packaging/build_mac.sh
```

The Intel version can also be built on the M2 (runs via Rosetta):

```bash
./packaging/build_mac.sh x86_64
```

Result: `dist/<arch>/VidTransfer.app` and `dist/VidTransfer-macOS-<arch>.zip`.
Alternatively, the GitHub workflow `build-macos` builds both variants (start it
manually or push a `v*` tag).

## Command line

```bash
VT=/Applications/VidTransfer.app/Contents/MacOS/VidTransfer
$VT send    ~/Videos/ShootDay3
$VT status  "$HOME/Library/CloudStorage/GoogleDrive-…/My Drive/ShootDay3"   # quick, downloads nothing
$VT verify  "<Drive folder>"                # check all packets + content, no unpacking
$VT list    "<Drive folder>"                # files, sizes, availability (downloads nothing)
$VT receive "<Drive folder>" ~/Downloads    # resumes automatically if interrupted
$VT receive "<Drive folder>" ~/Downloads --only "Audio/*.wav" --only "clip01.mp4"
$VT receive "<Drive folder>" ~/Downloads --salvage            # restore everything that is left
$VT receive "<Drive folder>" ~/Downloads --without-manifest   # emergency: packets only
```

`--only` matches the path or the file name (wildcards allowed, not
case-sensitive) or a folder (`"Audio/"`), and loads only the parts of the
delivery these files are in.

File names are adapted where the destination requires it (e.g. `:` on Windows,
two names that differ only in case); every renamed file is listed in the report.

Exit codes: `0` success, `1` error, `2` transfer incomplete or not recoverable
(`status`/`verify`), `130` cancelled.

## Development (Windows)

Development happens on Windows. The core, the CLI and the GUI run there just the
same; only the `.app` bundle and dropping onto the Dock icon are Mac-specific.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m vidtransfer        # GUI
```

`packaging\build_windows.ps1` builds a test EXE for Windows (folders can be dropped
onto the EXE). **The Mac app cannot be built on Windows**, because PyInstaller does
not cross-compile. There are two ways:
- Push the repo to GitHub and start *Actions → build-macos → Run workflow*.
  The ZIPs for arm64 and x86_64 are then available as artifacts.
- Run `./packaging/build_mac.sh` on the M2.

Conventions: code comments, docstrings, log messages and generated files are in
English; texts shown to the user (GUI, CLI output, error messages, macOS permission
prompts) are in German.

The design is described in [PLAN.md](PLAN.md).
