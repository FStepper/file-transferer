# VidTransfer – Plan

Goal: transfer a folder of large MP4/WAV files over an unstable connection and
**reliably unpack it again** on the other side. Transport is handled by the
locally installed Google Drive client (Google Drive for desktop). The target
platform is **macOS** (Apple Silicon M2, possibly Intel); development happens on
Windows. The app ships as a self-contained `.app` bundle, so no Python
installation is needed.

## Usage

- **Send:** drag a folder onto the app icon (Finder/Dock). The app creates a
  folder **with the same name** in Google Drive and writes the packets into it.
- **Unpack:** drag the folder from Google Drive onto the app icon and choose a
  destination. The app verifies and repairs the packets, unpacks them and checks
  every file with SHA-256 (details: [Unpacking](#unpacking)).
- Alternatively there are buttons in the window and a CLI
  (`vidtransfer send|receive|verify|status|list`).
- The UI is currently aimed at technical users; the plan for a much simpler UI
  is in [Simplified UI and usability](#simplified-ui-and-usability).

## Pipeline

```
Sender:    folder → container v2 (per file: record header + raw/zstd) → stripes
           → Reed-Solomon (zfec): k data + m parity packets → Drive folder
Receiver:  read index → prefetch + verify packets (in parallel) → reconstruct
           missing/damaged ones from parity → records → files (atomic) + SHA-256
```

(Transfers created before phase 7 use format v1, a zstd-compressed tar stream;
they remain readable.)

- **Per-file compression:** ✔ video, audio-codec, image and archive formats
  (MP4/MOV/MXF/BRAW/…, MP3/AAC/FLAC, JPEG/HEIC/RAW, ZIP/…) are stored raw, since
  they hardly compress any further. WAV/AIFF and everything else use zstd level 3,
  but only if a sample (first 4 MiB) saves at least 3 %. zstd is fast enough never
  to be the bottleneck (benchmark: 1.5 GB in ~10 s including parity).
- **Fault tolerance:** the default is k = 10 and m = 3 with 32 MiB per packet.
  *Any* 10 of a stripe's 13 packets fully restore the stripe. The overhead is 30 %.
- **Packet format (`sNNNNNN_II.vtp`):** 80-byte header (magic, version, k, m,
  index, transfer ID, stripe number, stripe length, payload length, SHA-256,
  CRC32), followed by the payload. Every packet can be checked on its own.
- **Manifest:** stored three times (`manifest.json`, `.2`, `.3`) with its own
  checksum. It contains the parameters, packet hashes and the **file index**
  (see below).
- **Memory/disk:** only one stripe is buffered locally at a time (≈ 0.8 GB). Only
  finished packets go into Drive (temporary file + atomic rename).

## Container format v2 ✔ (foundation for good unpacking)

v1 is a single zstd-compressed tar stream. That is simple, but it has a weakness:
everything **after** an unrecoverable spot is lost, and there is no way to jump
straight to one file. v2 (`container.py`, the default since phase 7) replaces
tar + whole-stream zstd with a simple container of its own:

```
stream = record | record | … | end record

record header (30 bytes fixed + path, CRC32-protected):
  magic "VTRC", version, type (file/dir/end), flags (raw|zstd), reserved,
  original size (u64), mtime (i64), mode (u32), path length (u16), path (UTF-8)
file data:
  raw:  exactly <original size> bytes
  zstd: chunks of [u32 length][zstd frame of ≤ 4 MiB original data], ended by u32 0
file trailer (48 bytes, CRC32-protected):
  magic "VTRE", SHA-256 of the original data, stored data size (u64)
```

Design decisions:
- **Hash in a trailer, not the header:** the sender streams every file exactly
  once; the SHA-256 is only known at the end.
- **Chunked zstd frames:** the compressed size need not be known in advance (no
  temporary copy of large WAV files), and damage stays confined to a 4 MiB chunk.
- **Every file is verified on its own** via its trailer, so the receiver gives
  each file its real name immediately, without waiting for the final manifest.
- **Determinism:** same input → same bytes (fixed headers, deterministic
  compression decisions), so re-sending still skips identical packets.
- **Source changes:** if a file grows or shrinks while it is being sent, sending
  stops with a clear error (`SourceChangedError`) instead of producing a broken record.

In addition, the manifest contains an **index**: for every file its path, size,
mtime, SHA-256 and compression, plus its **offset and length in the stream**
(directories: path, mtime, offset). All stripes except the last are exactly
k × block_size bytes long, so the index directly tells which stripes a file
occupies. While sending, the manifest is rewritten after every stripe, so the
index grows as files are finished.

What this enables:

| Capability | v1 (tar+zstd) | v2 |
|---|---|---|
| Unpack while the upload is running (streaming) | ✔ | ✔ (records are self-describing) |
| Verify a file before the final manifest exists | ✖ (stays `.vtpart`) | ✔ (trailer hash) |
| Resume after interruption | ✔ (finished files skipped, but the stream is re-read from the start) | ✔ (jumps straight to the first unfinished file; earlier stripes are never loaded) |
| Restore single files | ✖ | ✔ (only the required stripes are loaded) |
| Partial recovery if a stripe is lost for good | ✖ (rest is lost) | ✔ (only affected files are missing) |
| Unpack without manifest (emergency) | sequential only, no recovery after damage | ✔ (resync via magic + CRC) |

v1 transfers remain readable. The receiver detects the format via
`manifest.format_version`. A v1 writer is kept (`SendOptions(format_version=1)`)
so that every end-to-end test runs for both formats.

## Unpacking

### Flow

1. **Detect:** a folder containing `manifest*.json` (or at least `*.vtp`
   packets, see emergency mode) is a transfer folder. A valid copy is chosen; a
   completed one takes precedence.
   - With a complete v2 index, files are processed *targeted*: the receiver
     jumps from file to file via the index (basis for `--only`, resume and
     partial recovery). While the sender is still indexing, records are read
     sequentially.
2. **Pre-checks before anything is written:** ✔
   - **Free space** at the destination ≥ sum of original sizes + 1 stripe + 5 %
     reserve. Otherwise a clear message with the exact numbers is shown.
   - **Destination:** `<dest>/<name>`, unpacked into `<name> (receiving)` first.
     If that working folder already exists and belongs to the *same* transfer
     (state file, see below), the receive is **resumed**; otherwise
     `<name> (2)` is used.
   - **Quick status** from headers and file sizes only: how many stripes are
     complete, repairable or still missing. It is shown in the GUI log before
     reading starts.
3. **Fetch packets (prefetch):** ✔ a pool of 4 daemon threads (`prefetch.py`)
   reads and hashes the packets of the current stripe and the next 2 stripes
   ahead. In Drive's "Stream files" mode, this is exactly what triggers the
   download, in parallel instead of file by file. hashlib releases the GIL, so
   several cores are used.
   - Data packets are read first. Parity packets are only requested for data
     packets that are missing, damaged or stalled. This saves downloads.
   - **Timeout per packet:** a read that makes no progress for 5 min (stuck
     placeholder) counts as *stalled*. The stripe continues with parity, and a
     replacement worker is started (up to 16 threads) so the queue keeps moving.
     The stalled read cannot be interrupted, but if it finishes later, its result
     is used.
   - **Cache:** results are kept per packet and reused as long as the file is
     unchanged (size + mtime). If Drive syncs a new version, the packet is
     checked again. `verify` therefore hashes every packet exactly once.
   - **Transient read errors:** a packet that fails is read once more before
     the stripe gives up. Background: on the development PC, cold reads
     occasionally returned single flipped bits (reproduced with a stdlib-only
     test, most likely the RAM configuration). VidTransfer never produced wrong
     output, because every error was caught by the packet, zstd or file hashes.
     The re-read now also avoids needless failures. A stream that still decodes
     wrongly raises a `TransferError`, and receiving again resumes.
4. **Assemble the stripe:** ✔ with k valid data packets they are read directly,
   otherwise zfec reconstructs the stripe (in 1 MiB slices, little RAM). With
   fewer than k valid packets, the receiver waits (upload still running / Drive
   still syncing) and shows "Warte auf Stripe 17 (8/10 Pakete)".
5. **Write files:** ✔
   - Every file is written as `.<name>.vtpart` first and `fsync`ed. Only when its
     **SHA-256 matches** is it renamed atomically to its real name, and its mtime
     is set. A half-written or wrong file therefore never appears under its real
     name. v2: the hash comes from the record trailer (and is cross-checked with
     the manifest index if it is already there), so the rename happens right away.
   - v1 only: if the hash is not known yet (sender still running), the file stays
     as `.vtpart` and is verified and renamed once the final manifest arrives.
   - Directories (including empty ones) are created; their mtimes are set at the
     end (v2: from the manifest, since skipped directory records are not read).
   - The state file is updated after every finished file.
6. **Finish:** ✔
   - Every file in the manifest must be present and verified.
   - A **receive report** (`VidTransfer-Report.txt`) is written into the restored
     folder: files, sizes, SHA-256, repaired stripes, duration.
   - Only then is `<name> (receiving)` renamed to `<name>`.
   - Then a notification is shown, and **„Im Finder zeigen“** becomes available.
   - Later, optionally: **„Transfer-Ordner in Drive löschen …“** (with
     confirmation, never automatic).

### Resuming after interruption ✔

- The working folder contains `.vidtransfer-state.json` with the transfer ID and
  the finished files (path, size, SHA-256, mtime), both verified ones and
  written-but-unverified ones (`.vtpart`).
- On restart, finished files are checked quickly (size + mtime) and skipped. A
  file that was changed in between is written again. With v1, the stream is still
  read from the start (CPU only, packets are local by then). ✔ With v2, the
  receiver jumps directly to the first unfinished file via the index; the stripes
  before it are never loaded or downloaded.
- Leftover `.vtpart` files from an interrupted write are overwritten.
- Tested with a hard-killed process: the resume skips the finished file and the
  result is byte-identical.

### Partial recovery ✔

If a stripe is unrecoverable for good (upload complete, fewer than k valid
packets even after re-reading):
- In wait mode the receiver keeps waiting while the upload is running. Once the
  upload is complete and a stripe stays short for 10 minutes without change, it
  stops with `StripeUnavailableError`, and the GUI asks whether everything else
  should be restored (CLI: `receive --salvage`).
- All files whose stripes are intact are unpacked and verified as usual. Files
  that touch a lost stripe are skipped; their stripes are not loaded again.
- The result goes to `<name> (incomplete)`. The report lists the missing files
  and the lost stripes, with the hint that the sender can simply send the same
  folder again (sending is deterministic and only rewrites missing packets).
- The `(incomplete)` folder keeps its state file. Receiving again into the same
  destination completes it and renames it to `<name>`.
- Not implemented (optional idea): writing damaged MP4s with zero-filled gaps as
  `name.DAMAGED.mp4`.

### Emergency without manifest ✔

If all three manifest copies are unreadable (`receive --without-manifest`; the
GUI offers it when a folder contains packets but no readable manifest):
- A stand-in manifest is built from the packet headers (transfer ID, k, m,
  stripe count and length, block size; format detected from the first bytes).
- Records are read sequentially; every v2 file is still verified via its trailer.
- After a lost stripe or a damaged record, the reader re-synchronises on the
  next valid record header (magic + CRC) and continues.
- Without the file list, completeness cannot be checked; the report and the
  summary say so clearly. v1 transfers can be read this way, but without
  recovery after damage.

### Further features

- `vidtransfer verify <folder>` / button **„Nur prüfen“**: ✔ checks all k+m
  packets of every stripe, then decodes the whole stream and checks every file
  hash, without writing anything. It reports intact, repairable-only-with-parity
  and unrecoverable stripes. This is useful before the sender deletes its data.
  `--quick` checks packets only, `--without-manifest` works in emergency mode.
- `vidtransfer list <folder>`: ✔ file list with size, compression and a quick
  availability check per file (headers only, downloads nothing).
- `vidtransfer receive <folder> <dest> --only "Audio/*.wav"`: ✔ restores single
  files (glob on the path or the file name, or a folder prefix, case-insensitive;
  repeatable). Only the stripes of these files are loaded; unrelated folders are
  not created. Requires format v2.

### Safety and platform details ✔

- **Paths:** no absolute paths, no `..`, no symlinks/devices, no collisions with
  internal files. Everything stays below the destination.
- **Unicode:** paths are normalised to NFC on both sides (macOS delivers NFD),
  so names and hashes match across platforms.
- **Windows as destination:** invalid characters (`<>:"|?*\`), trailing dots/
  spaces and reserved names (`CON`, `NUL`, …) are replaced. Long paths get the
  `\\?\` prefix. A name like `C:` can therefore never become a drive path.
- **Case:** APFS and NTFS are case-insensitive by default. If two paths differ
  only in case, the second one gets a ` (2)` suffix; distinct source folders
  stay distinct.
- The name mapping is stored in the receive state (stable when resuming) and
  every renamed path is listed in the report.
- Fixed along the way: a top-level name containing `:` (valid on macOS) used to
  be skipped as unsafe.
- **Sleep:** `caffeinate` runs as long as the app is open, including while unpacking.

## Robustness (sending)

- **Sender interrupted:** just start again. The stream is deterministic, identical
  packets already present are skipped. Sending again also repairs damaged packets
  in Drive (existing packets are fully verified).
- **Manifest during the upload:** ✔ updated after every stripe (index so far,
  packet hashes, status `in_progress`), so the receiver can already map finished
  files. A completed manifest is never downgraded when the same folder is re-sent.
- **Source changed while sending:** ✔ clear error instead of a broken record.
- **Sleep:** as long as the app is open, `caffeinate` prevents the Mac from going
  to sleep. This matters so that Drive keeps uploading.

## Tests (unpacking)

- ✔ Roundtrip, empty files and folders, non-ASCII names, compression levels
- ✔ Up to m packets per stripe deleted, with bit errors or truncated → identical result
- ✔ m+1 losses in one stripe → clear error; `verify` reports the stripe as unrecoverable
- ✔ Cancelled receive → resume skips finished files; a file changed in between is rewritten
- ✔ Hashes unknown while the sender is running → v1: files stay `.vtpart` until the
  final manifest; v2: files are verified via their trailer and renamed immediately
- ✔ Not enough free space → clear message, nothing left behind
- ✔ Malicious paths (`../x`, absolute, drive letter, internal names) → rejected
- ✔ Report contains every file and hash; no leftovers (`.vtpart`, state file) after success
- ✔ Checks run in parallel; parity is only read when needed
- ✔ Hanging packet (simulated blocking read) → timeout, parity is used; more
  stalled reads than workers do not block the queue; too many → clear error
- ✔ Transient read errors (packet fails once) → re-read succeeds, also in `verify`
- ✔ Misread stream → `TransferError`, the next receive resumes and succeeds
- ✔ `verify` hashes every packet only once; changed packets are checked again
- ✔ Every end-to-end test runs for v2 and v1 (v1 transfers stay readable)
- ✔ v2: per-file compression decisions; index offsets point at the right records;
  manifest index grows while sending; resume never touches stripes before the
  first unfinished file; growing source file detected; damaged record header or
  file data detected (CRC / trailer)
- ✔ `--only`: only matching files restored, only their stripes loaded, no
  unrelated folders; by name, glob and folder; no match → clear error; v1 → clear error
- ✔ Lost stripe → error names the stripe; salvage restores all unaffected files
  into `(incomplete)`, report lists the missing file; after the packets are back,
  receiving again completes the folder and renames it
- ✔ Wait mode gives up after the timeout once the upload is complete
- ✔ `list` marks files in lost stripes as unavailable
- ✔ All manifests deleted → stand-in manifest matches the real one; everything is
  restored; with an additional lost stripe, files after the damage are found via resync
- ✔ Names: Windows rules, reserved names, case collisions, stable mapping on
  resume, NFC on both sides, `:` in a top-level name, paths longer than 260 characters

## Simplified UI and usability

The engine is robust, but the current window is built for technical users. A
non-technical user should be able to send and receive without knowing what a
stripe, a packet or a manifest is, and without ever wondering "is it done?".

### What is too complex today

- **Jargon everywhere:** "Stripe 3: 8/10 Pakete bereit", "Parität", "Manifest",
  "Transfer-Ordner", "Senden nach: C:\…". The log is a technical log.
- **Too many choices on one screen:** three buttons ("Ordner senden",
  "Aus Drive empfangen", "Nur prüfen"), target path, log, cancel, reveal, open log.
- **Receiving means searching:** the user has to find the right transfer folder
  inside Google Drive in a folder dialog, then pick a destination.
- **"Is it done?" has no answer:** after packing, the app says "Google Drive
  lädt jetzt hoch" but cannot tell when the upload has finished or whether the
  recipient got everything. This is the biggest practical gap.
- **Errors are text dumps** without a clear next step.
- **Drop only onto the app icon**, not onto the window; single files are refused.
- **First start:** Gatekeeper warnings, no check whether Google Drive is set up.

### Principles

1. **One job per Mac:** a sender Mac only *sends*, the receiver Mac only
   *receives* (automatically). Everything else happens automatically or is
   hidden in the settings window.
2. **Plain language** with a fixed glossary (below); numbers in %, GB and minutes.
3. **The app says what happens next and when it is done**, on both sides.
4. **One action per problem:** every error message names the cause in one
   sentence and offers one button that fixes it.
5. **Safe defaults, no configuration:** nothing is deleted without asking;
   existing files are never overwritten; resuming is automatic.

### Glossary (UI wording)

| Technical term | Shown to the user as |
|---|---|
| transfer / transfer folder | **Lieferung** |
| packing, stripes, parity | **Wird vorbereitet** (one progress bar) |
| packets in Drive being uploaded | **Wird hochgeladen** |
| receiving, verifying, reconstructing | **Wird empfangen** (repairs only as a note: "beschädigte Teile wurden automatisch repariert") |
| manifest, index, state file | not shown at all |
| "(incomplete)", lost stripes | **Unvollständig – x Dateien fehlen noch** + action |
| verify | not a separate action; happens automatically (see U10) |
| Drive target folder | shown only in the settings |

### Decisions (2026-10-09)

- **Setup:** two **sender Macs** and one **receiver Mac**. The roles are fixed.
  Only one sender uploads at a time, so simultaneous sends do not happen.
- **No setup assistant and no pre-flight checks** (former U7/U8 dropped).
- **No Apple Developer account:** no notarization. Installation is done once by a
  technical person with an install script (U15).
- **UI toolkit stays Tk** for now. The window gets far fewer elements and
  becomes role-specific: a sender Mac only shows sending, the receiver Mac only
  shows receiving.

### Consequences of two senders

- **No sender subfolders:** both senders write directly into the shared Drive
  folder, as today. Collisions would only occur if two Macs sent a folder with
  the same name at the same moment, before Drive has synced – this does not
  happen in this setup. Sending folders with the same name one after another is
  already handled: the sender sees the existing (synced) delivery and uses
  `<name> (2)`.
- **Sender name in the manifest**, so the receiver can show "von Kamera-Mac".
  It defaults to the Mac's computer name (`scutil --get ComputerName`) and can be
  changed in the settings.
- **Shared Drive folder:** all three Macs must see the same folder. Either all
  three use the same Google account, or one folder (e.g. `VidTransfer`) is shared
  with edit rights and added to "Meine Ablage" on the other accounts (or a
  shared drive is used). Drive detection must also find `Shared drives` /
  `Geteilte Ablagen` folders. This is documented as a one-time setup step.

### Target concept (Tk)

**Sender Mac** – one drop zone, one list:

```
┌─────────────────────────────────────────────────────┐
│ VidTransfer                              Einstellungen │
│                                                     │
│  ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐  │
│     Ordner mit Videos hierher ziehen                │
│  │              [Ordner auswählen …]              │  │
│   ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│                                                     │
│  Drehtag 3 · 212 GB                                 │
│  ███████████░░░░░░░░  Wird hochgeladen       [Stopp] │
│  ─────────────────────────────────────────────────  │
│  Drehtag 2 · 140 GB                                 │
│  ✔ Beim Empfänger angekommen · gestern 18:40         │
└─────────────────────────────────────────────────────┘
```

**Receiver Mac** – no buttons for the normal case:

```
┌─────────────────────────────────────────────────────┐
│ VidTransfer                              Einstellungen │
│ Neue Lieferungen kommen automatisch nach            │
│ Filme › VidTransfer                                 │
│                                                     │
│  Drehtag 3 · von Kamera-Mac · 212 GB                │
│  ██████░░░░░░░░░░░░  Wird noch hochgeladen (35 %)    │
│  ─────────────────────────────────────────────────  │
│  Interview · von Ton-Mac · 18 GB                    │
│  ✔ Angekommen · heute 09:12          [Im Finder zeigen] │
│  ─────────────────────────────────────────────────  │
│  Drehtag 1 · von Kamera-Mac · 96 GB                 │
│  ⚠ 2 Dateien fehlen noch – werden nachgeliefert      │
└─────────────────────────────────────────────────────┘
```

What disappears from the main window: the target path line, "Aus Drive
empfangen", "Nur prüfen", "Log öffnen", the log text area, the general cancel
button and the status line. They move to the settings window or become automatic.

Each delivery is one row (card) with name, size (receiver: sender name), one
progress bar while it is running, one status line and at most one button. The
status steps are the same on both sides:
*Wird vorbereitet → Wird hochgeladen → Wird empfangen → Angekommen ✔*, plus
*Pausiert*, *Wartet auf Google Drive* and *Unvollständig – Aktion nötig*.
The list is kept across restarts.

**Settings window** (small, rarely needed): role (Sender / Empfänger), sender
name, Drive folder, inbox folder, "Lieferung manuell öffnen …" (old receive and
verify functions), "Protokoll öffnen", "Erweitert" (k, m, packet size, compression).

### Measures

**Phase 9 – sender window and wording**
- **U1 Plain language:** replace every user-facing text according to the
  glossary; progress as %, GB and remaining time; repairs only as a reassuring note.
- **U2 Role-specific main window (Tk):** sender layout as above; delivery cards as
  a vertical list of `ttk.Frame` rows with one `ttk.Progressbar`, one status label
  and one button; the drop zone as a `Canvas` with a dashed border that also opens
  the folder dialog on click. Role from the settings (default: Sender).
- **U3 Hide technical details:** log, receive/verify by hand and advanced
  parameters move to the settings window. "Details" on a card shows the last
  log lines of that delivery.
- **U4 Error catalogue:** a fixed list of plain-language messages, each with one
  action, e.g. "Google Drive läuft nicht." → [Google Drive öffnen]; "Auf diesem
  Mac fehlen 35 GB Speicherplatz." → [Anderen Speicherort wählen]; "Eine Datei
  hat sich während des Sendens verändert." → [Erneut senden]. Technical details go
  into "Details kopieren"; no stack traces.
- **U9 Drop onto the window, files too:** dropping onto the Dock icon already
  works. Drop onto the window needs `tkdnd` (package `tkinterdnd2`, ships the
  native library for macOS arm64/x86_64); to be checked in the PyInstaller build.
  If that fails: keep the Dock icon plus a click on the drop zone. Single files
  and several items are accepted: every dropped folder becomes one delivery;
  loose files become one delivery named "Dateien <Datum>".
- **U20 Sender name in the manifest** (see above), plus the delivery history.

**Phase 10 – receiver window and automatic receiving**
- **U5/U6 Automatic receiving:** the receiver Mac watches the Drive folder and
  receives every new delivery automatically into the inbox (default
  `~/Movies/VidTransfer/<folder name>`), including
  resuming after restarts and the partial-recovery decision (automatic: restore
  everything that is there and wait for the rest). A notification says when a
  delivery has arrived.
- **U21 Upload progress on the receiver:** while the sender is still uploading,
  the receiver can already tell how far it is by counting the packets that have
  arrived against the expected number (from the in-progress manifest) –
  "Wird noch hochgeladen (35 %)".
- Manual receiving (old flow) stays available under "Lieferung manuell öffnen …".

**Phase 11 – "is it done?" back channel through Drive**
- **U10 Receipt and receiver status:** the receiver writes `status/receiver.json`
  (progress) and `status/received.json` (everything verified) into the delivery
  folder. The sender's card then shows "Wird empfangen – 60 %" and finally
  "Beim Empfänger angekommen ✔". Each side writes only its own files, so Drive
  never has to merge conflicting versions.
- **U11 Self-healing:** if parts are lost for good, the receiver writes
  `status/request.json`; if the sender's app is open and the source folder still
  exists, it re-sends exactly these packets automatically. The cards show
  "Fehlende Teile werden nachgeliefert".
- **U12 Guided clean-up:** after the receipt, the sender card offers [Platz in
  Google Drive freigeben (212 GB)], with the hint that Drive only frees the space
  after its trash is emptied. Never automatic.

**Phase 12 – upload status and keeping awake**
- **U13 Upload status from Google Drive:** spike on whether Google Drive's macOS
  File Provider reports the upload state per file (`NSURLUbiquitousItemIsUploadedKey`
  and related keys via PyObjC). If yes: real "Hochgeladen ✔" on the sender and
  `caffeinate` only until then. If not: the receipt (U10) and the receiver's packet
  count (U21) are the source of truth.
- **U14 Time estimates** for preparing and uploading; "Sie können den Mac jetzt
  zuklappen" only when it is really safe.

**Phase 13 – installation**
- **U15 Install script instead of notarization:** a double-clickable
  `VidTransfer installieren.command` next to the app copies it to /Applications,
  removes the quarantine flag (`xattr -dr com.apple.quarantine`), asks once
  whether this Mac is a sender or the receiver, and sets the role, sender name
  and Drive folder. On the receiver it also installs a LaunchAgent so the app
  starts at login (U16). This is done once per Mac by a technical person; the
  users never see Gatekeeper dialogs or settings.
- **U16 Start at login on the receiver Mac** (LaunchAgent, see U15). A menu bar
  icon is not possible with Tk alone and is postponed.

**Cross-cutting**
- **U17 Notifications** only for: arrived, action needed (receiver); arrived at
  the receiver (sender).
- **U18 Look and accessibility within Tk:** larger fonts for names and status,
  clear contrast, dark mode via `ttk` aqua, keyboard operation. Moving to an HTML UI
  (`pywebview`) stays an option for later.
- **U19 Usability tests:** 1–2 people per role, tasks "Schicke den Ordner
  Drehtag 3" (sender) and "Wo ist die Lieferung von heute?" (receiver), without
  help. Repeat after phases 9 and 10.

Dropped by decision: setup assistant (U7), pre-flight checks before sending (U8),
signed and notarized DMG.

## Implementation

1. ✔ Core: packet format, erasure coding, sender and receiver (v1), tests with lost and damaged packets
2. ✔ Drive detection (`~/Library/CloudStorage/GoogleDrive-*/My Drive`), configuration
3. ✔ Tk GUI with drag & drop onto the app icon (`::tk::mac::OpenDocument`), progress
4. ✔ PyInstaller `.app` (arm64 + x86_64) via build script and GitHub Actions
5. ✔ **Unpacking, part 1:** free-space check, atomic per-file writes, state file +
   resume, receive report, `verify`, GUI buttons „Nur prüfen“ / „Im Finder zeigen“
6. ✔ **Unpacking, part 2:** prefetch pool with parallel verification, stall
   timeout, result cache, re-read on failure
7. ✔ **Container v2:** record format, per-file compression, index in the manifest
   (incremental), jump on resume, v1 stays readable
8. ✔ **Unpacking, part 3:** `--only`, `list`, partial recovery, emergency mode
   without manifest, NFC/Windows name handling
9. **UI, part 1 – sender:** plain-language texts and error catalogue, role-specific
   main window with drop zone and delivery cards, settings window, sender name
   in the manifest, drop onto the window, single files (U1–U4, U9, U20)
10. **UI, part 2 – receiver:** automatic receiving into the inbox, delivery list
    with sender name, upload progress from the packet count (U5, U6, U21)
11. **Back channel:** receiver status and receipt in the delivery folder,
    "arrived" on the sender, automatic re-send of missing parts, guided clean-up
    (U10–U12)
12. **Upload status:** spike on the macOS File Provider upload state; keep awake
    until uploaded; time estimates (U13, U14)
13. **Installation:** install script (quarantine, role, sender name, Drive folder),
    start at login on the receiver Mac (U15, U16)
