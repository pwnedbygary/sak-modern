from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import FormatError
from .hfs0 import Hfs0

XCI_MAGIC = b"HEAD"
XCI_PREFIX_SIZE = 0xF000
XCI_HEADER_SIZE = 0x190
XCI_GAME_INFO_SIZE = 0x70
XCI_SIG_PADDING_SIZE = 0x6E00
XCI_CERT_SIZE = 0x8000
MEDIA_SIZE = 0x200


@dataclass
class Xci:
    path: Path
    size: int
    card_size_byte: int
    secure_offset: int
    hfs0_offset: int
    root: Hfs0
    secure: Hfs0 | None
    game_info: bytes

    @classmethod
    def open(cls, path: str | Path) -> "Xci":
        p = Path(path)
        with p.open("rb") as f:
            head = f.read(0x200)
            if len(head) < 0x200 or head[0x100:0x104] != XCI_MAGIC:
                raise FormatError(f"{p.name}: not an XCI container")
            secure_offset = struct.unpack_from("<I", head, 0x104)[0] * MEDIA_SIZE
            hfs0_offset = struct.unpack_from("<Q", head, 0x130)[0] or XCI_PREFIX_SIZE
            card_size_byte = head[0x10D]
            game_info = head[0x190:0x200]
        root = Hfs0.open(p, hfs0_offset)
        secure_entry = next((e for e in root.entries if e.name == "secure"), None)
        secure = Hfs0.open(p, secure_entry.abs_offset) if secure_entry else None
        return cls(p, p.stat().st_size, card_size_byte, secure_offset, hfs0_offset, root, secure, game_info)


def card_size_for(total_size: int) -> tuple[int, int]:
    """Return (byte, capacity) for the smallest standard gamecard that fits."""
    capacities = [
        (0xFA, 1 << 30),
        (0xF8, 2 << 30),
        (0xF0, 4 << 30),
        (0xE0, 8 << 30),
        (0xE1, 16 << 30),
        (0xE2, 32 << 30),
    ]
    for byte, capacity in capacities:
        if total_size <= capacity:
            return byte, capacity
    raise FormatError(
        f"Bundle is {total_size / (1 << 30):.2f} GiB; a single Switch gamecard is limited to 32 GiB."
    )


def build_xci_header(*, secure_offset: int, root_header_size: int, root_hash: bytes,
                     total_size: int, card_size_byte: int) -> bytes:
    if len(root_hash) != 32:
        raise ValueError("root hash must be SHA-256")
    h = bytearray()
    h += b"\x00" * 0x100  # gamecard signature is not verifiable/needed on CFW; tools randomize it
    h += XCI_MAGIC
    h += struct.pack("<I", secure_offset // MEDIA_SIZE)
    h += b"\xFF\xFF\xFF\xFF"
    h += b"\x00"
    h += bytes([card_size_byte, 0x00, 0x00])
    h += (0x8750F4C0A9C5A966).to_bytes(8, "big")
    h += struct.pack("<Q", max((total_size - 1) // MEDIA_SIZE, 0))
    h += bytes.fromhex("5B408B145E277E81E5BF677C94888D7B")
    h += struct.pack("<Q", XCI_PREFIX_SIZE)
    h += struct.pack("<Q", root_header_size)
    h += root_hash
    h += bytes.fromhex(
        "1AB7C7B263E74E44CD3C68E40F7EF4A4D6571551D043FCA8ECF5C489F2C66E7E"
    )
    h += struct.pack("<III", 1, 2, 0)
    h += struct.pack("<I", secure_offset // MEDIA_SIZE)
    if len(h) != XCI_HEADER_SIZE:
        raise AssertionError(f"XCI header size {len(h):#x} != {XCI_HEADER_SIZE:#x}")
    return bytes(h)


def build_game_info(total_size: int) -> bytes:
    # NSC_BUILDER-compatible fallback game-info block; content is non-authoritative.
    large = total_size >= 4 << 30
    if large:
        return bytes.fromhex(
            "9298F35088F09F7DA89A60D4CBA6F96FA45BB6ACABC751F95D3987426B38C3F2"
            "10DA0B700E5ECE29A13CBE1DA6D052CBF2087CE9AF590538570D78B9CDD27FBE"
            "B4A0AC2ADFF9BA77754DD6675AC76223506B3BDABCB2E212FA465111AB7D51AF"
            "C8B5B2B21C4B3F40654598620282ADD6"
        )
    return bytes.fromhex(
        "9109FF82971EE9935011CA063F3C4D87A13D28A9928D74F149919EB782E1F0CF"
        "E4A5A3BDF978295CD52639A4991BDB1FED841779A3F85D23AA4242135616F518"
        "7C03CF0D97E5D218FDB245381FD1CF8DFB796FBEDA4BF7F7D6B128CE89BC9EAA"
        "8552D42F597C5DB866C67BB0DD8EEA11"
    )


def xci_prefix(total_size: int, *, secure_offset: int, root_header_size: int,
               root_hash: bytes, card_size_byte: int) -> bytes:
    header = build_xci_header(
        secure_offset=secure_offset,
        root_header_size=root_header_size,
        root_hash=root_hash,
        total_size=total_size,
        card_size_byte=card_size_byte,
    )
    assert len(header) == XCI_HEADER_SIZE
    game_info = build_game_info(total_size)
    assert len(game_info) == XCI_GAME_INFO_SIZE
    prefix = header + game_info + b"\x00" * XCI_SIG_PADDING_SIZE + b"\xFF" * XCI_CERT_SIZE
    if len(prefix) != XCI_PREFIX_SIZE:
        raise AssertionError("XCI prefix size mismatch")
    return prefix
