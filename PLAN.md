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
  (`vidtransfer send|receive|verify|status`, later `list`).

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
| Restore single files (phase 8) | ✖ | possible (only the required stripes are loaded) |
| Partial recovery if a stripe is lost for good (phase 8) | ✖ (rest is lost) | possible (only affected files are missing) |
| Unpack without manifest (emergency, phase 8) | limited | possible (resync via magic + CRC) |

v1 transfers remain readable. The receiver detects the format via
`manifest.format_version`. A v1 writer is kept (`SendOptions(format_version=1)`)
so that every end-to-end test runs for both formats.

## Unpacking

### Flow

1. **Detect:** a folder containing `manifest*.json` is a transfer folder. A valid
   copy is chosen; a completed one takes precedence.
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

### Partial recovery ("emergency")

If a stripe is unrecoverable for good (upload complete, fewer than k valid
packets, sender no longer available), **„Alles Rettbare wiederherstellen“** is
offered after asking:
- All files whose stripes are intact are unpacked and verified as usual.
- The affected files are listed in the report, with the hint that the sender can
  re-send exactly these stripes. Sending is deterministic and only rewrites
  missing packets.
- **Optional for MP4:** the damaged file is written with zeros in the gaps as
  `name.DAMAGED.mp4`, since players can often still play the rest. This is off by
  default.
- **Without manifest** (all three copies lost): records are located via magic and
  CRC. Without the index, completeness cannot be checked, and this is shown clearly.

### Further features

- `vidtransfer verify <folder>` / button **„Nur prüfen“**: ✔ checks all k+m
  packets of every stripe, then decodes the whole stream and checks every file
  hash, without writing anything. It reports intact, repairable-only-with-parity
  and unrecoverable stripes. This is useful before the sender deletes its data.
  `--quick` checks packets only.
- `vidtransfer list <folder>`: shows the file list and sizes from the index.
- `vidtransfer receive <folder> <dest> --only "Audio/*.wav"`: restores single files
  (loads only the required stripes).

### Safety and platform details

- **Paths:** ✔ no absolute paths, no `..`, no drive letters, no symlinks/devices,
  no collisions with internal files. Everything stays below the destination.
- **Unicode:** paths are normalised to NFC (macOS delivers NFD), so names and
  hashes match across platforms.
- **Windows as destination** (if needed): invalid characters (`:*?"<>|`) and
  reserved names (`CON`, `NUL`, …) are replaced and noted in the report. Long
  paths get the `\\?\` prefix.
- **Case:** APFS and NTFS are case-insensitive by default. If two paths collide,
  one is renamed and a warning is shown.
- **Sleep:** ✔ `caffeinate` runs as long as the app is open, including while unpacking.

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
- m+1 losses → partial recovery: all unaffected files correct, report correct (phase 8)
- All manifests deleted → emergency scan finds all records (phase 8)

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
8. **Unpacking, part 3:** `--only`, `list`, partial recovery, emergency scan
   without manifest, NFC/Windows name handling
