"""Reed-Solomon erasure coding for one stripe (via zfec).

A stripe is `stripe_len` bytes of the data stream. It is split into k blocks of
equal length B (the last one is virtually padded with zeros), from which m
parity blocks are computed. Any k of the k+m packets restore the stripe.

Work is done column-wise in slices of `SLICE` bytes, so never more than
(k+m) × SLICE bytes are held in memory, regardless of the packet size.
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
    """Create the k+m packets of a stripe in `out_dir`."""
    b = block_size(stripe_len, k)
    lens = [data_len(stripe_len, k, i) for i in range(k)] + [b] * m
    paths = [out_dir / packet_name(stripe, i) for i in range(k + m)]
    hashes = [hashlib.sha256() for _ in paths]
    encoder = zfec.Encoder(k, k + m) if m else None
    parity_nums = tuple(range(k, k + m))

    files = [open(p, "w+b") for p in paths]
    try:
        for f in files:
            f.write(bytes(HEADER_SIZE))  # placeholder, real header is written at the end
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
                            raise IOError("stripe file shorter than expected")
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
    """Reconstruct the stripe payload from at least k valid packets.

    `packets` maps packet index → path (already verified packets only!).
    """
    if len(packets) < k:
        raise ValueError(f"only {len(packets)} of at least {k} packets available")
    b = block_size(stripe_len, k)
    # Prefer data packets – they need no computation
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
