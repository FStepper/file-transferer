"""Reed-Solomon-Erasure-Coding für einen Stripe (über zfec).

Ein Stripe sind `stripe_len` Bytes des Datenstroms. Er wird in k gleich große
Blöcke der Länge B aufgeteilt (der letzte wird virtuell mit Nullen aufgefüllt),
daraus entstehen m Paritätsblöcke. Aus beliebigen k der k+m Pakete lässt sich
der Stripe wiederherstellen.

Gearbeitet wird spaltenweise in Scheiben von `SLICE` Bytes, damit nie mehr als
(k+m) × SLICE im Speicher liegen – unabhängig von der Paketgröße.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import zfec

from vidtransfer.packet import HEADER_SIZE, PacketHeader, block_size, data_len, packet_name
from vidtransfer.util import check_cancel

SLICE = 1 << 20


def _pad(chunk: bytes, n: int) -> bytes:
    return chunk if len(chunk) == n else chunk + bytes(n - len(chunk))


def encode_stripe(
    stripe_file: Path,
    stripe_len: int,
    k: int,
    m: int,
    stripe: int,
    transfer_id: bytes,
    out_dir: Path,
    cancel=None,
) -> list[tuple[Path, PacketHeader]]:
    """Erzeugt die k+m Pakete eines Stripes in `out_dir`."""
    b = block_size(stripe_len, k)
    lens = [data_len(stripe_len, k, i) for i in range(k)] + [b] * m
    paths = [out_dir / packet_name(stripe, i) for i in range(k + m)]
    hashes = [hashlib.sha256() for _ in paths]
    encoder = zfec.Encoder(k, k + m) if m else None
    parity_nums = tuple(range(k, k + m))

    files = [open(p, "w+b") for p in paths]
    try:
        for f in files:
            f.write(bytes(HEADER_SIZE))  # Platzhalter, Header kommt am Ende
        with open(stripe_file, "rb") as src:
            for off in range(0, b, SLICE):
                check_cancel(cancel)
                n = min(SLICE, b - off)
                blocks = []
                for i in range(k):
                    want = max(0, min(n, lens[i] - off))
                    chunk = b""
                    if want:
                        src.seek(i * b + off)
                        chunk = src.read(want)
                        if len(chunk) != want:
                            raise IOError("Stripe-Datei kürzer als erwartet")
                        files[i].write(chunk)
                        hashes[i].update(chunk)
                    blocks.append(_pad(chunk, n))
                if encoder:
                    for j, par in enumerate(encoder.encode(tuple(blocks), parity_nums)):
                        files[k + j].write(par)
                        hashes[k + j].update(par)

        result = []
        for i, (f, p) in enumerate(zip(files, paths)):
            hdr = PacketHeader(k, m, i, transfer_id, stripe, stripe_len, lens[i], hashes[i].digest())
            f.seek(0)
            f.write(hdr.pack())
            result.append((p, hdr))
        return result
    finally:
        for f in files:
            f.close()


def decode_stripe(
    packets: dict[int, Path],
    stripe_len: int,
    k: int,
    m: int,
    out_file: Path,
    cancel=None,
) -> None:
    """Rekonstruiert die Stripe-Nutzdaten aus mindestens k gültigen Paketen.

    `packets` bildet Paketindex → Pfad ab (nur bereits geprüfte Pakete!).
    """
    if len(packets) < k:
        raise ValueError(f"nur {len(packets)} von mindestens {k} Paketen vorhanden")
    b = block_size(stripe_len, k)
    # Datenpakete bevorzugen – die müssen nicht "gerechnet" werden
    nums = tuple(sorted(packets)[:k])
    payload_lens = [data_len(stripe_len, k, i) if i < k else b for i in nums]
    lens = [data_len(stripe_len, k, i) for i in range(k)]
    decoder = zfec.Decoder(k, k + m)

    srcs = [open(packets[i], "rb") for i in nums]
    try:
        with open(out_file, "wb") as out:
            out.truncate(stripe_len)
            for off in range(0, b, SLICE):
                check_cancel(cancel)
                n = min(SLICE, b - off)
                blocks = []
                for src, plen in zip(srcs, payload_lens):
                    want = max(0, min(n, plen - off))
                    chunk = b""
                    if want:
                        src.seek(HEADER_SIZE + off)
                        chunk = src.read(want)
                    blocks.append(_pad(chunk, n))
                primary = decoder.decode(tuple(blocks), nums)
                for i, blk in enumerate(primary):
                    want = max(0, min(n, lens[i] - off))
                    if want:
                        out.seek(i * b + off)
                        out.write(bytes(blk[:want]) if len(blk) != want else blk)
    finally:
        for s in srcs:
            s.close()
