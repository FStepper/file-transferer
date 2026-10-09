"""Packet format.

Every packet is a self-contained file `sNNNNNN_II.vtp`:

    [80-byte header][payload]

The header states which transfer/stripe/index the packet belongs to and
carries the SHA-256 of the payload plus a CRC32 over the header itself, so
every packet can be checked for integrity without the manifest.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from vidtransfer.util import check_cancel

MAGIC = b"VTPK"
VERSION = 1

_BODY = struct.Struct("<4sBBBB16sIQQ32s")
_CRC = struct.Struct("<I")
HEADER_SIZE = _BODY.size + _CRC.size  # 80

PACKET_SUFFIX = ".vtp"
_NAME_RE = re.compile(r"^s(\d{6})_(\d{2})\.vtp$")

READ_CHUNK = 1 << 20


class PacketError(ValueError):
    pass


@dataclass(frozen=True)
class PacketHeader:
    k: int
    m: int
    index: int
    transfer_id: bytes
    stripe: int
    stripe_len: int
    payload_len: int
    sha256: bytes

    def pack(self) -> bytes:
        body = _BODY.pack(
            MAGIC, VERSION, self.k, self.m, self.index, self.transfer_id,
            self.stripe, self.stripe_len, self.payload_len, self.sha256,
        )
        return body + _CRC.pack(zlib.crc32(body))

    @classmethod
    def unpack(cls, data: bytes) -> "PacketHeader":
        if len(data) < HEADER_SIZE:
            raise PacketError("header too short")
        body, crc = data[:_BODY.size], data[_BODY.size:HEADER_SIZE]
        if _CRC.unpack(crc)[0] != zlib.crc32(body):
            raise PacketError("header CRC mismatch")
        magic, version, k, m, index, tid, stripe, stripe_len, payload_len, sha = _BODY.unpack(body)
        if magic != MAGIC:
            raise PacketError("not a VidTransfer packet")
        if version != VERSION:
            raise PacketError(f"unknown packet version {version}")
        return cls(k, m, index, tid, stripe, stripe_len, payload_len, sha)


def packet_name(stripe: int, index: int) -> str:
    return f"s{stripe:06d}_{index:02d}{PACKET_SUFFIX}"


def parse_packet_name(name: str) -> Optional[tuple[int, int]]:
    mo = _NAME_RE.match(name)
    return (int(mo.group(1)), int(mo.group(2))) if mo else None


def block_size(stripe_len: int, k: int) -> int:
    """Size of one block (= packet payload) within a stripe."""
    return max(1, -(-stripe_len // k))


def data_len(stripe_len: int, k: int, index: int) -> int:
    """Actual payload length of data packet `index` (the rest is virtual zero padding)."""
    b = block_size(stripe_len, k)
    return max(0, min(b, stripe_len - index * b))


def read_header(path: Path) -> Optional[PacketHeader]:
    """Read only the header. Returns None if the file is missing or invalid."""
    try:
        with open(path, "rb") as f:
            return PacketHeader.unpack(f.read(HEADER_SIZE))
    except (OSError, PacketError):
        return None


def verify_packet(path: Path, cancel=None) -> Optional[PacketHeader]:
    """Check header, file size and payload SHA-256 (reads the whole file)."""
    try:
        with open(path, "rb") as f:
            hdr = PacketHeader.unpack(f.read(HEADER_SIZE))
            h = hashlib.sha256()
            remaining = hdr.payload_len
            while remaining:
                check_cancel(cancel)
                chunk = f.read(min(READ_CHUNK, remaining))
                if not chunk:
                    return None  # truncated
                h.update(chunk)
                remaining -= len(chunk)
            if f.read(1):
                return None  # longer than expected
    except (OSError, PacketError):
        return None
    return hdr if h.digest() == hdr.sha256 else None
