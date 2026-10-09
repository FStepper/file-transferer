"""User-facing summaries shared by CLI and GUI."""

from __future__ import annotations

from vidtransfer.receiver import ReceiveResult, VerifyResult
from vidtransfer.util import human_bytes


def receive_summary(res: ReceiveResult) -> str:
    if res.complete:
        what = "selected files restored" if res.selected else "restored"
        parts = [f"\"{res.dest.name}\" {what}: {res.files} files, {human_bytes(res.bytes)}, all checksums ok"]
    else:
        parts = [f"\"{res.dest.name}\" only partly restored: {res.files} files "
                 f"({human_bytes(res.bytes)}) are fine"]
        if res.missing:
            parts.append(f", {len(res.missing)} file(s) missing")
    if res.repaired_stripes:
        parts.append(f", {res.repaired_stripes} stripe(s) repaired")
    if res.resumed:
        parts.append(f", resumed ({res.skipped_files} file(s) were already done)")
    lines = ["".join(parts) + "."]
    if not res.complete:
        shown = ", ".join(res.missing[:5]) + (" …" if len(res.missing) > 5 else "")
        if shown:
            lines.append(f"Missing: {shown}")
        lines.append("To complete it, send the same folder again on the sender and then "
                     "receive again into the same target. Details are in the report.")
    if res.without_manifest:
        lines.append("Restored without a manifest – whether files are missing cannot be checked.")
    if res.renamed:
        lines.append(f"{len(res.renamed)} name(s) were adjusted for this system (see report).")
    lines.append(f"→ {res.dest}")
    return "\n".join(lines)


def verify_summary(res: VerifyResult) -> str:
    lines = []
    if res.ok:
        lines.append(f"\"{res.name}\" is fully recoverable: {res.stripes} stripes, "
                     f"{res.files_checked} files ({human_bytes(res.bytes_checked)}) confirmed by SHA-256.")
    elif res.unrecoverable:
        lines.append(f"\"{res.name}\" is currently NOT fully recoverable: "
                     f"{len(res.unrecoverable)} of {res.stripes} stripes have too few valid packets.")
    elif not res.complete:
        lines.append(f"\"{res.name}\": upload not finished yet – the "
                     f"{res.stripes} stripes present so far are recoverable.")
    else:
        lines.append(f"\"{res.name}\": packets are fine, but the content is damaged.")

    if res.degraded:
        lines.append(f"{len(res.degraded)} stripe(s) recoverable only with parity (that is fine).")
    if res.bad_packets:
        shown = ", ".join(res.bad_packets[:8]) + (" …" if len(res.bad_packets) > 8 else "")
        lines.append(f"{len(res.bad_packets)} missing/damaged packet(s): {shown}")
    if res.unrecoverable:
        shown = ", ".join(str(s + 1) for s in res.unrecoverable[:15])
        lines.append(f"Affected stripes: {shown}. Fix: let the cloud finish syncing or "
                     "send the same folder again on the sender (writes only missing packets).")
    for err in res.content_errors[:10]:
        lines.append(f"  {err}")
    return "\n".join(lines)
