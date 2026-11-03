from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .crypto import sha256
from .errors import FormatError, SakError
from .hfs0 import Hfs0
from .keys import KeyStore
from .nca import parse_nca_header
from .pfs0 import Pfs0
from .xci import MEDIA_SIZE, XCI_PREFIX_SIZE, Xci, card_size_for


@dataclass
class VerifyReport:
    path: Path
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.ok = False
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _hash_region(path: Path, offset: int, size: int) -> bytes:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        f.seek(offset)
        remaining = size
        while remaining:
            chunk = f.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                raise FormatError(f"short read at {offset:#x}")
            h.update(chunk)
            remaining -= len(chunk)
    return h.digest()


def _check_hfs0(rep: VerifyReport, h: Hfs0, label: str) -> None:
    for e in h.entries:
        if e.hashed_size and e.hashed_size <= e.size:
            actual = _hash_region(h.path, e.abs_offset, e.hashed_size)
            if actual != e.hash:
                rep.error(f"{label}/{e.name}: HFS0 hash mismatch")
        if e.size and e.abs_offset + e.size > h.path.stat().st_size:
            rep.error(f"{label}/{e.name}: entry extends past end of file")


def _check_nca_headers(rep: VerifyReport, path: Path, entries, ks: KeyStore) -> None:
    for e in entries:
        if not e.name.lower().endswith(".nca"):
            continue
        with path.open("rb") as f:
            f.seek(e.abs_offset if hasattr(e, "abs_offset") else e.offset)
            raw = f.read(0xC00)
        try:
            nca = parse_nca_header(raw, ks)
        except Exception as exc:
            rep.error(f"{e.name}: {exc}")
            continue
        size = getattr(e, "size", None)
        if size and nca.nca_size and nca.nca_size != size:
            rep.warn(f"{e.name}: header size {nca.nca_size:#x} != container size {size:#x}")


def verify_container(path: str | Path, ks: KeyStore) -> VerifyReport:
    p = Path(path)
    rep = VerifyReport(p)
    with p.open("rb") as f:
        magic = f.read(4)
        f.seek(0x100)
        xci_magic = f.read(4)
    if magic == b"PFS0":
        pfs = Pfs0.open(p)
        _check_nca_headers(rep, p, pfs.entries, ks)
        for e in pfs.entries:
            if e.abs_offset + e.size > p.stat().st_size:
                rep.error(f"{e.name}: extends past end of file")
        rep.details.append(f"PFS0 entries: {len(pfs.entries)}")
        return rep
    if xci_magic != b"HEAD":
        raise FormatError(f"{p.name}: not an NSP or XCI")
    xci = Xci.open(p)
    with p.open("rb") as f:
        head = f.read(0x200)
    root_header_size = struct.unpack_from("<Q", head, 0x138)[0]
    root_hash = head[0x140:0x160]
    with p.open("rb") as f:
        f.seek(xci.hfs0_offset)
        root_bytes = f.read(root_header_size)
    if sha256(root_bytes) != root_hash:
        rep.error("XCI root HFS0 hash does not match header")
    declared_secure = struct.unpack_from("<I", head, 0x104)[0] * MEDIA_SIZE
    secure_entry = next((e for e in xci.root.entries if e.name == "secure"), None)
    if secure_entry is not None and secure_entry.abs_offset != declared_secure:
        rep.error("XCI secure-offset field does not match the root secure entry")
    _check_hfs0(rep, xci.root, "root")
    if xci.secure:
        _check_hfs0(rep, xci.secure, "secure")
        _check_nca_headers(rep, p, xci.secure.entries, ks)
        rep.details.append(f"secure NCAs: {len(xci.secure.entries)}")
    capacity_byte, capacity = 0, 0
    try:
        capacity_byte, capacity = card_size_for(p.stat().st_size)
    except SakError as exc:
        rep.error(str(exc))
    order = [0xFA, 0xF8, 0xF0, 0xE0, 0xE1, 0xE2]
    if capacity and xci.card_size_byte != capacity_byte:
        # The bytes are not numerically ordered; compare ranks, not values.
        declared_rank = order.index(xci.card_size_byte) if xci.card_size_byte in order else -1
        needed_rank = order.index(capacity_byte)
        if declared_rank < needed_rank:
            rep.error(
                f"declared card size {xci.card_size_byte:#x} is smaller than needed "
                f"({capacity_byte:#x}, {capacity / (1 << 30):.0f} GiB)"
            )
        else:
            rep.warn(f"card size {xci.card_size_byte:#x} is larger than necessary ({capacity_byte:#x})")
    rep.details.append(f"size: {p.stat().st_size} bytes")
    rep.details.append(f"card size byte: {xci.card_size_byte:#x}")
    return rep
