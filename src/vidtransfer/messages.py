"""User-facing summaries shared by CLI and GUI (UI language: German)."""

from __future__ import annotations

from vidtransfer.receiver import ReceiveResult, VerifyResult
from vidtransfer.util import human_bytes


def receive_summary(res: ReceiveResult) -> str:
    if res.complete:
        what = "ausgewählte Dateien wiederhergestellt" if res.selected else "wiederhergestellt"
        parts = [f"„{res.dest.name}“ {what}: {res.files} Dateien, {human_bytes(res.bytes)}, alle Prüfsummen ok"]
    else:
        parts = [f"„{res.dest.name}“ nur teilweise wiederhergestellt: {res.files} Dateien "
                 f"({human_bytes(res.bytes)}) sind in Ordnung"]
        if res.missing:
            parts.append(f", {len(res.missing)} Datei(en) fehlen")
    if res.repaired_stripes:
        parts.append(f", {res.repaired_stripes} Stripe(s) repariert")
    if res.resumed:
        parts.append(f", fortgesetzt ({res.skipped_files} Datei(en) waren schon fertig)")
    lines = ["".join(parts) + "."]
    if not res.complete:
        shown = ", ".join(res.missing[:5]) + (" …" if len(res.missing) > 5 else "")
        if shown:
            lines.append(f"Fehlend: {shown}")
        lines.append("Zum Vervollständigen auf dem Sender denselben Ordner erneut senden und dann "
                     "erneut in dasselbe Ziel empfangen. Details stehen im Bericht.")
    if res.without_manifest:
        lines.append("Ohne Manifest wiederhergestellt – ob Dateien fehlen, lässt sich nicht prüfen.")
    if res.renamed:
        lines.append(f"{len(res.renamed)} Name(n) wurden für dieses System angepasst (siehe Bericht).")
    lines.append(f"→ {res.dest}")
    return "\n".join(lines)


def verify_summary(res: VerifyResult) -> str:
    lines = []
    if res.ok:
        lines.append(f"„{res.name}“ ist vollständig wiederherstellbar: {res.stripes} Stripes, "
                     f"{res.files_checked} Dateien ({human_bytes(res.bytes_checked)}) per SHA-256 bestätigt.")
    elif res.unrecoverable:
        lines.append(f"„{res.name}“ ist derzeit NICHT vollständig wiederherstellbar: "
                     f"{len(res.unrecoverable)} von {res.stripes} Stripes haben zu wenige gültige Pakete.")
    elif not res.complete:
        lines.append(f"„{res.name}“: Upload noch nicht abgeschlossen – die bisher vorhandenen "
                     f"{res.stripes} Stripes sind wiederherstellbar.")
    else:
        lines.append(f"„{res.name}“: Pakete in Ordnung, aber der Inhalt ist fehlerhaft.")

    if res.degraded:
        lines.append(f"{len(res.degraded)} Stripe(s) nur mit Parität wiederherstellbar (das ist in Ordnung).")
    if res.bad_packets:
        shown = ", ".join(res.bad_packets[:8]) + (" …" if len(res.bad_packets) > 8 else "")
        lines.append(f"{len(res.bad_packets)} fehlende/defekte Paket(e): {shown}")
    if res.unrecoverable:
        shown = ", ".join(str(s + 1) for s in res.unrecoverable[:15])
        lines.append(f"Betroffene Stripes: {shown}. Abhilfe: Drive fertig synchronisieren lassen oder "
                     "auf dem Sender denselben Ordner erneut senden (schreibt nur fehlende Pakete).")
    for err in res.content_errors[:10]:
        lines.append(f"  {err}")
    return "\n".join(lines)
