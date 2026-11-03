from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .hfs0 import Hfs0
from .keys import KeyStore
from .nca import NcaHeader, parse_nca_header
from .pfs0 import Pfs0
from .ticket import Ticket
from .xci import Xci

VERSION_RE = re.compile(r"\[v(\d+)\]", re.IGNORECASE)
VERSION_RE2 = re.compile(r"v(\d{3,})", re.IGNORECASE)


def version_from_name(path: Path) -> int:
    m = VERSION_RE.search(path.name) or VERSION_RE2.search(path.stem)
    return int(m.group(1)) if m else 0


@dataclass
class ContentEntry:
    name: str
    size: int
    path: Path
    abs_offset: int
    nca: NcaHeader | None = None
    version: int = 0
    package_kind: str | None = None
    package_title_id: int | None = None
    package_group: int | None = None
    package_id: str | None = None

    @property
    def is_nca(self) -> bool:
        return self.name.lower().endswith(".nca")

    @property
    def is_ticket(self) -> bool:
        return self.name.lower().endswith(".tik")

    @property
    def is_cert(self) -> bool:
        return self.name.lower().endswith(".cert")

    @property
    def is_xml(self) -> bool:
        return self.name.lower().endswith(".xml")

    @property
    def group_id(self) -> int | None:
        if self.package_group is not None:
            return self.package_group
        if not self.nca:
            return None
        return self.nca.title_id & 0xFFFFFFFFFFFF0000

    @property
    def kind(self) -> str:
        if not self.nca:
            return "aux"
        if self.package_kind:
            return self.package_kind
        if self.nca.is_dlc:
            return "dlc"
        if self.nca.is_update:
            return "update"
        return "base"


@dataclass
class LoadedContainer:
    path: Path
    kind: str  # "nsp" or "xci"
    entries: list[ContentEntry]
    tickets: list[Ticket] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ncas(self) -> list[ContentEntry]:
        return [e for e in self.entries if e.is_nca]


@dataclass
class ContentGroup:
    group_id: int
    kind: str
    title_id: int
    version: int
    entries: list[ContentEntry]
    package_id: str = ""

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries)


def assign_packages(entries: list[ContentEntry]) -> None:
    """Assign NCAs to their title package using the .cnmt.nca boundary.

    Update NCAs often carry the *base* title ID in their own header; only the
    package's Meta/CNMT NCA identifies the update/DLC title.  Splitting at each
    CNMT is reliable for both NSPs and merged XCIs and lets a newer update
    replace the old one instead of accumulating both.
    """
    ncas = [e for e in entries if e.is_nca]
    if not ncas:
        return
    metas = [e for e in ncas if e.name.lower().endswith(".cnmt.nca")]
    if len(metas) == 1:
        meta = metas[0]
        assert meta.nca is not None
        kind = "update" if meta.nca.is_update else ("dlc" if meta.nca.is_dlc else "base")
        for e in ncas:
            e.package_kind = kind
            e.package_title_id = meta.nca.title_id
            e.package_group = meta.nca.title_id & 0xFFFFFFFFFFFF0000
            e.package_id = f"{meta.path}:{meta.name}"
        return
    current: list[ContentEntry] = []

    def package_meta(segment: list[ContentEntry]) -> ContentEntry:
        if segment[0].name.lower().endswith(".cnmt.nca"):
            return segment[0]
        return segment[-1]

    def allowed_tid(meta: ContentEntry) -> set[int]:
        assert meta.nca is not None
        tid = meta.nca.title_id
        return {tid, tid & ~0x800}

    def flush() -> None:
        nonlocal current
        if not current:
            return
        meta = package_meta(current)
        assert meta.nca is not None
        kind = "update" if meta.nca.is_update else ("dlc" if meta.nca.is_dlc else "base")
        for x in current:
            x.package_kind = kind
            x.package_title_id = meta.nca.title_id
            x.package_group = meta.nca.title_id & 0xFFFFFFFFFFFF0000
            x.package_id = f"{meta.path}:{meta.name}"
        current = []

    for e in ncas:
        if current and current[0].name.lower().endswith(".cnmt.nca") and e.nca is not None:
            meta = current[0]
            assert meta.nca is not None
            if e.nca.title_id not in allowed_tid(meta):
                flush()
        current.append(e)
        if not e.name.lower().endswith(".cnmt.nca"):
            continue
        if len(current) == 1:
            # CNMT-first layout: this meta belongs to the NCAs that follow it.
            continue
        flush()
    flush()


def _scope_versions(entries: list[ContentEntry], path: Path) -> None:
    """Do not give a merged container's update version to unrelated DLC packages.

    A filename like "... [v393216] (1G+1U+4D).xci" carries one version for the
    update, but the DLC packages in the same file are v0.  If every package gets
    v393216, a newer DLC would look older than the bundled copy and be rejected.
    """
    packages = {e.package_id for e in entries if e.is_nca and e.package_id}
    if len(packages) <= 1:
        return
    name = path.name.lower()
    file_version = version_from_name(path)
    for e in entries:
        if not e.is_nca:
            continue
        tid = e.package_title_id
        if tid is not None and f"{tid:016x}" in name:
            e.version = file_version
        else:
            e.version = 0


def load_container(path: str | Path, ks: KeyStore) -> LoadedContainer:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(p)
    with p.open("rb") as f:
        magic = f.read(4)
    if magic in (b"NSZ0", b"XCZ0"):
        from .errors import UnsupportedFormat
        raise UnsupportedFormat(
            f"{p.name}: compressed NSZ/XCZ is not supported yet. Decompress it first "
            "(for example with nsz), then bundle the resulting NSP/XCI."
        )
    if magic == b"PFS0":
        return _load_nsp(p, ks)
    if magic == b"HFS0":
        # Rare: a bare secure partition. Reject clearly rather than misparsing.
        from .errors import UnsupportedFormat
        raise UnsupportedFormat(f"{p.name}: bare HFS0 partitions are not supported")
    # XCI magic is at 0x100 HEAD; check explicitly.
    with p.open("rb") as f:
        f.seek(0x100)
        if f.read(4) == b"HEAD":
            return _load_xci(p, ks)
    from .errors import FormatError
    raise FormatError(f"{p.name}: unrecognized container (expected NSP/PFS0 or XCI)")


def _load_nsp(p: Path, ks: KeyStore) -> LoadedContainer:
    pfs = Pfs0.open(p)
    entries: list[ContentEntry] = []
    tickets: list[Ticket] = []
    warnings: list[str] = []
    version = version_from_name(p)
    for e in pfs.entries:
        if e.name.lower().endswith(".ncz"):
            from .errors import UnsupportedFormat
            raise UnsupportedFormat(f"{p.name}: contains NCZ-compressed NCAs; decompress first")
        nca = None
        if e.name.lower().endswith(".nca"):
            data = pfs.read_entry(e, 0xC00)
            nca = parse_nca_header(data, ks)
            expected = nca.nca_size
            if expected and expected != e.size:
                warnings.append(
                    f"{p.name}: {e.name} header says {expected} bytes, entry is {e.size} bytes"
                )
        if e.name.lower().endswith(".tik"):
            tickets.append(Ticket.parse(pfs.read_entry(e)))
        entries.append(ContentEntry(e.name, e.size, p, e.abs_offset, nca, version))
    assign_packages(entries)
    _scope_versions(entries, p)
    return LoadedContainer(p, "nsp", entries, tickets, warnings)


def _load_xci(p: Path, ks: KeyStore) -> LoadedContainer:
    xci = Xci.open(p)
    if xci.secure is None:
        from .errors import FormatError
        raise FormatError(f"{p.name}: XCI has no secure partition")
    entries: list[ContentEntry] = []
    warnings: list[str] = []
    version = version_from_name(p)
    for e in xci.secure.entries:
        if e.name.lower().endswith(".ncz"):
            from .errors import UnsupportedFormat
            raise UnsupportedFormat(f"{p.name}: contains NCZ-compressed NCAs; decompress first")
        if not e.name.lower().endswith(".nca"):
            continue
        with p.open("rb") as f:
            f.seek(e.abs_offset)
            nca = parse_nca_header(f.read(0xC00), ks)
        if nca.nca_size and nca.nca_size != e.size:
            warnings.append(
                f"{p.name}: {e.name} header says {nca.nca_size} bytes, entry is {e.size} bytes"
            )
        entries.append(ContentEntry(e.name, e.size, p, e.abs_offset, nca, version))
    assign_packages(entries)
    _scope_versions(entries, p)
    return LoadedContainer(p, "xci", entries, [], warnings)


def group_contents(loaded: list[LoadedContainer]) -> tuple[list[ContentGroup], list[ContentGroup], list[ContentGroup]]:
    groups: dict[tuple[int, int], ContentGroup] = {}
    for container in loaded:
        for e in container.ncas:
            assert e.nca is not None
            package_title = e.package_title_id if e.package_title_id is not None else e.nca.title_id
            key = (e.package_id or str(e.path), e.group_id or 0, package_title)
            g = groups.get(key)
            if g is None:
                g = ContentGroup(
                    group_id=e.group_id or 0,
                    kind=e.kind,
                    title_id=package_title,
                    version=e.version,
                    entries=[],
                    package_id=e.package_id or str(e.path),
                )
                groups[key] = g
            g.entries.append(e)
            g.version = max(g.version, e.version)
    base: list[ContentGroup] = []
    updates: list[ContentGroup] = []
    dlcs: list[ContentGroup] = []
    for g in groups.values():
        kind = g.kind
        # A DLC package whose title ID happens to end in ...000 can look like a
        # base by title ID alone.  Only a Program NCA makes a package a base.
        if kind == "base" and not any(e.nca and e.nca.content_type == 0 for e in g.entries):
            kind = "dlc"
        if kind == "base":
            base.append(g)
        elif kind == "update":
            updates.append(g)
        else:
            dlcs.append(g)
    return base, updates, dlcs
