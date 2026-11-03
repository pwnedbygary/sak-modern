from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import FormatError

HFS0_MAGIC = b"HFS0"
HFS0_ENTRY_SIZE = 0x40


@dataclass(frozen=True)
class Hfs0Entry:
    name: str
    offset: int
    size: int
    hashed_size: int
    hash: bytes
    abs_offset: int


@dataclass
class Hfs0:
    path: Path
    entries: list[Hfs0Entry]
    data_offset: int
    header_size: int

    def by_name(self, name: str) -> Hfs0Entry:
        for e in self.entries:
            if e.name == name:
                return e
        raise FormatError(f"{self.path.name}: missing HFS0 partition '{name}'")

    @classmethod
    def open(cls, path: str | Path, offset: int = 0) -> "Hfs0":
        p = Path(path)
        with p.open("rb") as f:
            f.seek(offset)
            header = f.read(0x10)
            if len(header) != 0x10 or header[:4] != HFS0_MAGIC:
                raise FormatError(f"{p.name}: not an HFS0 partition at {offset:#x}")
            count, string_table_size, _ = struct.unpack_from("<III", header, 4)
            if count > 1_000_000 or string_table_size > 64 * 1024 * 1024:
                raise FormatError(f"{p.name}: implausible HFS0 header")
            table = f.read(count * HFS0_ENTRY_SIZE)
            strings = f.read(string_table_size)
            data_offset = offset + 0x10 + count * HFS0_ENTRY_SIZE + string_table_size
            entries: list[Hfs0Entry] = []
            for i in range(count):
                base = i * HFS0_ENTRY_SIZE
                rel, size, name_offset, hashed_size = struct.unpack_from("<QQII", table, base)
                end = strings.find(b"\x00", name_offset)
                if end < 0:
                    end = len(strings)
                name = strings[name_offset:end].decode("utf-8", "replace")
                digest = table[base + 0x20:base + 0x40]
                entries.append(
                    Hfs0Entry(name, rel, size, hashed_size, digest, data_offset + rel)
                )
        header_size = 0x10 + count * HFS0_ENTRY_SIZE + string_table_size
        return cls(p, entries, data_offset, header_size)


def build_hfs0(files: list[tuple[str, int, bytes, int]], *, alignment: int = 0x200) -> tuple[bytes, list[int]]:
    """files: (name, size, sha256 of first hashed_size bytes, hashed_size)."""
    names = [name.encode("utf-8") for name, *_ in files]
    string_table = b"\x00".join(names) + b"\x00"
    raw_header = 0x10 + len(files) * HFS0_ENTRY_SIZE + len(string_table)
    header_size = (raw_header + 0xF) & ~0xF
    # HFS0 data starts immediately after the string table; pad the header to 0x200
    # for the root/secure partitions so offsets stay media-aligned.
    header_size = (header_size + alignment - 1) // alignment * alignment
    string_pad = header_size - raw_header
    data_start = header_size

    offsets: list[int] = []
    cursor = data_start
    for _name, size, _hash, _hashed in files:
        pad = (-cursor) % alignment
        cursor += pad
        offsets.append(cursor - data_start)
        cursor += size

    header = bytearray()
    header += HFS0_MAGIC
    header += struct.pack("<III", len(files), len(string_table) + string_pad, 0)
    name_offset = 0
    for i, (name, _size, digest, hashed_size) in enumerate(files):
        if len(digest) != 32:
            raise ValueError("HFS0 hash must be 32 bytes")
        header += struct.pack("<QQII", offsets[i], _size, name_offset, hashed_size)
        header += b"\x00" * 8
        header += digest
        name_offset += len(name.encode("utf-8")) + 1
    header += string_table + b"\x00" * string_pad
    return bytes(header), offsets
