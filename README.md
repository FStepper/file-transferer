# VidTransfer

Transfers large folders (MP4, WAV, …) reliably via **Google Drive for desktop**,
even over an unstable connection. The folder is split into packets with
Reed-Solomon parity. If packets go missing or arrive damaged, the receiver
reconstructs them automatically, and every file is verified with SHA-256 at the end.

> The app's user interface is in German; this documentation is in English.

## Usage (Mac)

**Sending**
1. Drag a folder (e.g. `Shoot Day 3`) onto the **VidTransfer icon** in the Dock or Finder.
2. The app creates the folder `Shoot Day 3` in Google Drive and writes the packets into it.
3. Google Drive uploads the packets. **Keep the app open until the upload is done**,
   because it prevents the Mac from going to sleep.

Something went wrong (crash, restart, folder changed)? Just drag the same folder
onto the app again. Existing packets are skipped and damaged ones replaced.

**Receiving (unpacking)**
1. Drag the folder `Shoot Day 3` from Google Drive onto the VidTransfer icon,
   or click **„Aus Drive empfangen …“**.
2. Choose the destination (default: Downloads).
3. The app verifies the packets, repairs missing ones, unpacks everything and
   verifies every file. This already works while the sender is still uploading;
   the app then waits for the missing packets.

How unpacking stays safe:
- Everything is written to `Shoot Day 3 (receiving)` first. Only once every file has
  been verified is the folder renamed to `Shoot Day 3`.
- Each file is first written as a hidden `.<name>.vtpart` and only gets its real
  name once its SHA-256 matches. A half-written or corrupt file never appears under
  its real name.
- **Interrupted?** (cancelled, crash, Mac switched off) Start receiving again and
  pick the same destination. Finished files are skipped, the rest is completed.
- Before anything is written, the app checks that there is **enough free space**.
- Packets are downloaded and verified **in parallel**, a few stripes ahead of the
  one being unpacked. A packet that hangs (e.g. a Drive placeholder that does not
  download) is skipped after 5 minutes and replaced by parity.
- At the end, `VidTransfer-Report.txt` in the restored folder lists every file with
  its size and SHA-256.

**Verify only**

**„Nur prüfen …“** (or `vidtransfer verify`) downloads and checks all packets and
the whole content, without unpacking anything. It reports whether the transfer is
completely recoverable. This is useful before the sender deletes the original data.

> Tip: In Drive's "Stream files" mode, packets are only downloaded when they are
> read. That works, but it is slower. You can mark the transfer folder as
> "Available offline" in Drive.

## Fault tolerance

| Setting | Default | Meaning |
|---|---|---|
| `k` | 10 | data packets per stripe |
| `m` | 3 | parity packets per stripe: up to 3 of 13 packets may be missing or damaged |
| `block_mib` | 32 | packet size in MiB |
| `zstd_level` | 3 | compression (hardly helps for MP4, a few percent for WAV; 0 = off) |

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
$VT receive "<Drive folder>" ~/Downloads    # resumes automatically if interrupted
```

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
