"""User-facing summaries shared by CLI and GUI."""

from __future__ import annotations

from file_transferer.receiver import ReceiveResult, VerifyResult
from file_transferer.util import human_bytes


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


# ---------------------------------------------------------------- completeness of a delivery

def _index_list(indexes: list[int]) -> str:
    return ", ".join(f"{i:02d}" for i in indexes)


def stripe_line(sp) -> str:
    """One line per stripe that lacks packets, e.g. 'Stripe 1: missing 01, 03 (3 more needed)'."""
    total = len(sp.present) + len(sp.missing)
    if not sp.present:
        what = f"all {total} packets missing"
    else:
        what = f"missing {_index_list(sp.missing)}"
    need = f"{sp.short_by} more needed" if sp.short_by else "enough packets are there"
    return f"Stripe {sp.stripe + 1}: {what} ({need})"


def completeness_summary(check) -> str:
    """Short text for the card: how much is missing and what that means."""
    missing = check.expected - check.present
    if not missing:
        return "All packets are in the cloud folder."
    head = f"{missing} of {check.expected} packets are missing in the cloud folder"
    if not check.complete:
        return head + " – the sender has not finished uploading yet."
    if not check.blocked:
        return head + ", but everything can still be restored."
    stripes = ", ".join(str(s.stripe + 1) for s in check.blocked[:6]) + (" …" if len(check.blocked) > 6 else "")
    return (f"{head}. Stripe{'s' if len(check.blocked) > 1 else ''} {stripes} cannot be restored yet "
            f"({check.more_needed} more packets needed). The sender's upload may have stopped – "
            "check Google Drive on the sending Mac and send the folder again.")


def completeness_report(check, max_names: int = 12) -> str:
    """Full text for details and the console. `max_names` limits the packet list (0 = all)."""
    lines = [f"\"{check.name}\": {check.present} of {check.expected} packets are in the folder."]
    lines.append(completeness_summary(check))
    short = [s for s in check.stripes if s.missing]
    if short:
        lines.append("")
        lines.extend(stripe_line(s) for s in short)
    names = check.missing_names
    if names and max_names != -1:
        shown = names if not max_names else names[:max_names]
        lines.append("")
        lines.append("Missing packets: " + ", ".join(shown)
                     + (f" … and {len(names) - len(shown)} more" if len(shown) < len(names) else ""))
    return "\n".join(lines)
