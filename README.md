# File Transferer

Transfers large folders (MP4, WAV, …) reliably via a synced **cloud folder** –
by default **Google Drive for desktop**, which the app finds by itself – even over
an unstable connection. The folder is split into packets with
Reed-Solomon parity. If packets go missing or arrive damaged, the receiver
reconstructs them automatically, and every file is verified with SHA-256 at the end.

## Usage

The window has a switch at the top: **Send | Receive**. The app remembers the
last mode. Each mode shows one drop zone and the list of deliveries; every
delivery has one status line and at most one button. Everything else is in
**Settings** (computer name, cloud folder, inbox, verifying a
delivery, log, advanced parameters).

**Sending**
1. Drag a folder (e.g. `Shoot Day 3`) onto the window or onto the File Transferer
   icon in the Dock – or click **Choose folder …**. Loose files can be
   dropped too; they become one delivery called `Files <date>`.
2. The card shows *Preparing – 45 % · about 6 min left*
3. When it says **✔ In the cloud**, the app's job is done. The cloud client
   (Google Drive) then uploads in the background – the Mac should stay online until
   it shows the folder as synced. The app keeps the Mac awake only while it is preparing.

Something went wrong (crash, restart, folder changed)? Click **Resume** or
**Send again** on the card, or drop the same folder again. Existing packets are
skipped and damaged ones replaced.

**Receiving**
1. Switch to **Receive** – that is all. The app watches the cloud folder
   and receives every new delivery **automatically** into the inbox, by default
   `~/Movies/File Transferer` (changeable in the settings). The card shows who sent it.
2. While the sender is still uploading, the card says *Still uploading
   (35 %)*; receiving runs alongside and waits for the rest.
3. The app verifies the packets, repairs missing ones, unpacks everything and
   verifies every file.
4. **✔ Received** – **Show in Finder** opens the folder. The link
   **Free up cloud space** deletes the delivery from the cloud folder
   (after asking; the received files stay). Google Drive frees the space once its
   trash is emptied.

Deliveries that were already in the cloud folder before automatic receiving was used
for the first time, and deliveries sent from this computer, are not fetched
unasked – they appear as cards with a button **Receive**. A delivery can also
be opened by hand with **Open delivery …** or by dragging it onto the window.
Automatic receiving can be switched off in the settings.

Problems are shown in one plain sentence with one button that helps, e.g.
*"The cloud folder cannot be reached. Is the cloud app running?"* →
**Start cloud app** (starts Google Drive on the Mac), or *"This computer needs 35 GB more free space."* →
**Choose another location**. The technical details are behind "Details"
(or right-click on a card).

How unpacking stays safe:
- Everything is written to `Shoot Day 3 (receiving)` first. Only once every file has
  been verified is the folder renamed to `Shoot Day 3`.
- Each file is first written as a hidden `.<name>.vtpart` and only gets its real
  name once its SHA-256 matches. A half-written or corrupt file never appears under
  its real name.
- **Interrupted?** (stopped, crash, Mac switched off) Click **Resume**. The app
  jumps straight to the first unfinished file; finished files and the packets
  before it are not read again.
- Before anything is written, the app checks that there is **enough free space**.
- Packets are downloaded and verified **in parallel**, a few stripes ahead of the
  one being unpacked. A packet that hangs (e.g. a Drive placeholder that does not
  download) is skipped after 5 minutes and replaced by parity.
- At the end, `file-transferer-report.txt` in the restored folder lists every file with
  its size and SHA-256.

**If parts are lost for good**

If some packets are missing for good (the upload is complete, but Google Drive
does not have them), automatic receiving restores everything else into a folder
`Shoot Day 3 (incomplete)`. The card says how many files are missing and asks to
send the folder again on the sending Mac (only the missing packets are written).
As soon as they have arrived, the app completes the delivery by itself and renames
the folder to `Shoot Day 3` (or click **Get missing files**). For deliveries
opened by hand, the card offers **Get everything else now**.

If the content list (manifest) of a delivery is unreadable, the app offers an
emergency recovery from the packets alone. Every restored file is still
verified, but the app cannot tell whether files are missing.

**Verify only**

**Settings → Verify delivery …** (or `file-transferer verify`) downloads and
checks all packets and the whole content, without unpacking anything. It reports
whether the delivery is completely recoverable.

**Is everything there? (completeness check)**

**Settings → Check completeness …** (or `file-transferer status <folder>`) compares
the packet files in a delivery folder with the manifest, by file name only. It is
instant and downloads nothing. It says how many packets are missing, which stripes
cannot be restored yet and how many more packets they need, e.g.
`Stripe 1: missing 01, 03, 04, 09, 11, 12 (3 more needed)`.
`file-transferer status <folder> --missing` prints just the names of the missing
packet files, one per line (exit code 2 if any are missing).

The app uses the same check while receiving: when a delivery stays incomplete, its
card and its details name the missing packets and say the sender's upload may have
stopped. A waiting card also notes when no new packet has arrived for 15 minutes.

Why this happens: the sending app is done as soon as the packets are in the local
cloud folder; the cloud client uploads them afterwards and can stop (Mac asleep,
offline, client paused or in error). The sender can fix it by opening Google Drive,
waiting until it shows everything as synced, and sending the folder again (only the
missing packets are written).

**Can the app see the upload state on the sender?** Not yet.
`file-transferer diag <delivery folder>` (run it on the Mac that sent, while Drive
is still uploading) writes what macOS reports about a few files (extended attributes,
Spotlight metadata, whether a file is online-only) to
`~/file-transferer-diagnostics.txt`. It only reads. If it shows an upload state,
the sender can show real progress.

> Tip: In Drive's "Stream files" mode, packets are only downloaded when they are
> read. That works, but it is slower. You can mark the delivery folder as
> "Available offline" in Drive.

## Other clouds, or testing locally without Google Drive

The cloud folder defaults to the Google Drive folder found on the computer, but it
can be any folder – another sync client's folder, or a local one for testing: in
**Settings**, click **Change …** next to "Cloud folder" and choose any local
folder, e.g. `D:\File Transferer-Test`. The app keeps that choice and never switches
back to the Drive folder by itself. Sending writes the delivery folder there. In
mode "Receive" the app watches the same folder; deliveries sent from this
computer appear as cards with **Receive** (they are not fetched unasked).

Every folder dialog remembers the folder that was opened last. On the command
line, the target is given with `--drive`:

```bash
file-transferer send "D:\Footage\Shoot" --drive "D:\File Transferer-Test"
file-transferer receive "D:\File Transferer-Test\Shoot" "D:\Restored"
```

To keep test settings (and the delivery list) separate from your normal ones,
set the environment variable `FILE_TRANSFERER_CONFIG_DIR` to another folder before
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
`~/Library/Application Support/File Transferer/config.json`. The log is in
`~/Library/Logs/File Transferer/`.

## Installation

Prebuilt app: `file-transferer-macOS-arm64.zip` (Apple Silicon: M1/M2/…) or
`file-transferer-macOS-x86_64.zip` (Intel; also runs on Apple Silicon via Rosetta).
The ZIP contains a folder with `file-transferer.app`, the install script
`Install File Transferer.command` and `README-INSTALL.txt`.

Once per Mac, double-click **`Install File Transferer.command`**. The script:
- copies the app to **Applications** (asks for an admin password if needed),
- removes the macOS block on the app (it is not notarized by Apple),
- asks for the cloud folder (same folder on every Mac; lists the Google Drive
  folders it finds, including shared drives, and suggests a subfolder
  `File Transferer`), this Mac's name, whether the Mac mainly sends or receives, the
  inbox (receiving Macs) and whether File Transferer should start at login.

Because the script itself is not notarized either, macOS blocks it the first
time: right-click → **Open**, or on macOS 15 and later **System Settings ›
Privacy & Security › Open Anyway**. Alternatively, run it in Terminal:

```bash
bash "Install File Transferer.command"
```

All answers can be changed later in the app's settings. The script stores them
through the app's own command line (`file-transferer config …`), so it never edits
settings files itself. Details, including how to set up a shared Drive folder for
different Google accounts: `packaging/README-INSTALL.txt`.

The first time the app accesses Google Drive, Movies or Downloads, macOS asks for
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

Result: `dist/<arch>/file-transferer.app` and `dist/file-transferer-macOS-<arch>.zip`
(folder with the app, the install script and `README-INSTALL.txt`).

The install script can be tried without a Mac (it then only prints the macOS
commands):

```bash
VT_DRY_RUN=1 VT_SRC_APP=<any folder> VT_DEST_DIR=<folder> VT_APP_BIN="python -m file_transferer" \
  bash "packaging/Install File Transferer.command"
```
Alternatively, the GitHub workflow `build-macos` builds both variants (start it
manually or push a `v*` tag).

## Command line

```bash
VT=/Applications/file-transferer.app/Contents/MacOS/file-transferer
$VT send    ~/Videos/ShootDay3
$VT status  "$HOME/Library/CloudStorage/GoogleDrive-…/My Drive/ShootDay3"   # quick, downloads nothing
$VT verify  "<Drive folder>"                # check all packets + content, no unpacking
$VT list    "<Drive folder>"                # files, sizes, availability (downloads nothing)
$VT receive "<Drive folder>" ~/Downloads    # resumes automatically if interrupted
$VT receive "<Drive folder>" ~/Downloads --only "Audio/*.wav" --only "clip01.mp4"
$VT receive "<Drive folder>" ~/Downloads --salvage            # restore everything that is left
$VT receive "<Drive folder>" ~/Downloads --without-manifest   # emergency: packets only
$VT config                                  # show the settings
$VT config --list-drives                    # Google Drive folders found (incl. shared drives)
$VT config --drive "<folder>" --sender "Kamera-Mac" --mode receive --login on
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
.\.venv\Scripts\python -m file_transferer        # GUI
```

`packaging\build_windows.ps1` builds a test EXE for Windows (folders can be dropped
onto the EXE). **The Mac app cannot be built on Windows**, because PyInstaller does
not cross-compile. There are two ways:
- Push the repo to GitHub and start *Actions → build-macos → Run workflow*.
  The ZIPs for arm64 and x86_64 are then available as artifacts.
- Run `./packaging/build_mac.sh` on the M2.

Conventions: everything is in English – the user interface, CLI output, code,
docs and generated files. There is no translation layer.

The design is described in [PLAN.md](PLAN.md).
