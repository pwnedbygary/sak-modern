import struct
from pathlib import Path

from sak_modern.core.hfs0 import Hfs0, build_hfs0
from sak_modern.core.pfs0 import Pfs0, build_pfs0_layout
from sak_modern.core.xci import card_size_for


def test_pfs0_layout_aligns_every_file(tmp_path: Path):
    files = [("a.nca", 1000), ("b.nca", 2000), ("c.tik", 704)]
    header, offsets = build_pfs0_layout(files)
    data_start = len(header)
    for (name, size), off in zip(files, offsets):
        assert (data_start + off) % 0x200 == 0
    blob = bytearray(header)
    for (name, size), off in zip(files, offsets):
        blob.extend(b"\x00" * ((data_start + off) - len(blob)))
        blob.extend(bytes([len(name)]) * size)
    p = tmp_path / "x.nsp"
    p.write_bytes(blob)
    pfs = Pfs0.open(p)
    assert [e.name for e in pfs.entries] == [n for n, _ in files]
    assert [e.size for e in pfs.entries] == [s for _, s in files]
    assert all(e.abs_offset % 0x200 == 0 for e in pfs.entries)


def test_hfs0_roundtrip_and_hash_offset(tmp_path: Path):
    hashes = [bytes([i]) * 32 for i in range(3)]
    files = [("update", 0x200, hashes[0], 0x200), ("normal", 0x200, hashes[1], 0x200),
             ("secure", 0x400, hashes[2], 0x200)]
    header, offsets = build_hfs0(files)
    assert len(header) % 0x200 == 0
    blob = bytearray(header)
    for _name, size, _h, _hs in files:
        blob.extend(b"\x11" * size)
    p = tmp_path / "x.hfs0"
    p.write_bytes(blob)
    h = Hfs0.open(p)
    assert [e.name for e in h.entries] == [n for n, *_ in files]
    assert h.entries[0].hash == hashes[0]
    assert all(e.abs_offset % 0x200 == 0 for e in h.entries)


def test_card_sizes():
    assert card_size_for(1 << 30)[0] == 0xFA
    assert card_size_for((4 << 30) - 1)[0] == 0xF0
    assert card_size_for((4 << 30) + 1)[0] == 0xE0
    assert card_size_for((16 << 30) + 1)[0] == 0xE2


def test_write_entry_skips_header_in_source(tmp_path: Path):
    from sak_modern.core.bundle import _write_entry
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(256)) * 16)  # 0x1000
    dst = tmp_path / "dst.bin"
    header = b"HDR" * 0x400  # 0xC00
    with dst.open("wb") as f:
        _write_entry(f, src, 0, 0x1000, header)
    assert dst.read_bytes() == header + src.read_bytes()[0xC00:0x1000]
