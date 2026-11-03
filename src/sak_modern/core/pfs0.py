from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import FormatError

PFS0_MAGIC = b"PFS0"
PFS0_ENTRY_SIZE = 0x18


@dataclass(frozen=True)
class Pfs0Entry:
    name: str
    offset: int
    size: int
    abs_offset: int

    @property
    def end(self) -> int:
        return self.abs_offset + self.size


class Pfs0:
    def __init__(self, path: Path, entries: list[Pfs0Entry], data_offset: int, header_size: int):
        self.path = path
        self.entries = entries
        self.data_offset = data_offset
        self.header_size = header_size
        self.by_name = {e.name: e for e in entries}

    @classmethod
    def open(cls, path: str | Path) -> "Pfs0":
        p = Path(path)
        with p.open("rb") as f:
            header = f.read(0x10)
            if len(header) != 0x10 or header[:4] != PFS0_MAGIC:
                raise FormatError(f"{p.name}: not a PFS0/NSP container")
            count, string_table_size, _reserved = struct.unpack_from("<III", header, 4)
            if count > 1_000_000 or string_table_size > 64 * 1024 * 1024:
                raise FormatError(f"{p.name}: implausible PFS0 header")
            table = f.read(count * PFS0_ENTRY_SIZE)
            strings = f.read(string_table_size)
            data_offset = 0x10 + count * PFS0_ENTRY_SIZE + string_table_size
            entries: list[Pfs0Entry] = []
            for i in range(count):
                offset, size, name_offset, _ = struct.unpack_from("<QQII", table, i * PFS0_ENTRY_SIZE)
                end = strings.find(b"\x00", name_offset)
                if end < 0:
                    end = len(strings)
                name = strings[name_offset:end].decode("utf-8", "replace")
                entries.append(Pfs0Entry(name, offset, size, data_offset + offset))
        return cls(p, entries, data_offset, data_offset)

    def read_entry(self, entry: Pfs0Entry, length: int | None = None) -> bytes:
        with self.path.open("rb") as f:
            f.seek(entry.abs_offset)
            return f.read(entry.size if length is None else min(length, entry.size))


def build_pfs0_layout(files: list[tuple[str, int]], *, alignment: int = 0x200) -> tuple[bytes, list[int]]:
    """Build just the PFS0 header and return per-file relative offsets.

    Offsets are chosen so every file's absolute position is 512-byte aligned,
    which is the invariant the old NSC_BUILDER path occasionally violated.
    """
    names = [name.encode("utf-8") for name, _ in files]
    string_table = b"\x00".join(names) + b"\x00"
    raw_header_size = 0x10 + len(files) * PFS0_ENTRY_SIZE + len(string_table)
    header_size = (raw_header_size + 0xF) & ~0xF
    string_pad = header_size - raw_header_size
    data_start = header_size

    offsets: list[int] = []
    cursor = data_start
    for _name, size in files:
        pad = (-cursor) % alignment
        cursor += pad
        offsets.append(cursor - data_start)
        cursor += size

    header = bytearray()
    header += PFS0_MAGIC
    header += struct.pack("<III", len(files), len(string_table) + string_pad, 0)
    name_offset = 0
    for name in names:
        # placeholder offset is filled below
        header += b"\x00" * 8 + b"\x00" * 8 + struct.pack("<I", name_offset) + b"\x00" * 4
        name_offset += len(name) + 1
    header += string_table + b"\x00" * string_pad
    for i, (_name, size) in enumerate(files):
        off = offsets[i]
        struct.pack_into("<QQ", header, 0x10 + i * PFS0_ENTRY_SIZE, off, size)
    return bytes(header), offsets
