from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .crypto import sha256
from .errors import BundleError, FormatError
from .hfs0 import build_hfs0
from .keys import KeyStore
from .model import ContentEntry, ContentGroup, LoadedContainer, group_contents, load_container
from .nca import parse_nca_header, rewrite_nca_for_xci
from .pfs0 import build_pfs0_layout
from .xci import XCI_PREFIX_SIZE, card_size_for, xci_prefix

READ_CHUNK = 8 * 1024 * 1024
Progress = Callable[[int, int, str], None]


@dataclass
class PreparedNca:
    name: str
    size: int
    source_path: Path
    source_offset: int
    header: bytes
    was_rewritten: bool = False

    @property
    def header_hash(self) -> bytes:
        return sha256(self.header[:0x200])


@dataclass
class BundlePlan:
    base: ContentGroup
    update: ContentGroup | None
    dlcs: list[ContentGroup]
    containers: list[LoadedContainer]
    warnings: list[str] = field(default_factory=list)

    @property
    def title_id(self) -> int:
        return self.base.title_id

    @property
    def version(self) -> int:
        if self.update:
            return self.update.version
        return self.base.version

    @property
    def total_input_size(self) -> int:
        return sum(e.size for g in [self.base, self.update, *self.dlcs] if g for e in g.entries)

    def summary(self) -> dict:
        return {
            "title_id": f"{self.title_id:016X}",
            "version": self.version,
            "base": self.base,
            "update": self.update,
            "dlcs": self.dlcs,
            "warnings": self.warnings,
        }


def make_plan(paths: list[str | Path], ks: KeyStore) -> BundlePlan:
    loaded = [load_container(p, ks) for p in paths]
    return plan_from_loaded(loaded, preferred=loaded[1:])


def plan_from_loaded(loaded: list[LoadedContainer], preferred: list[LoadedContainer] | None = None) -> BundlePlan:
    bases, updates, dlcs = group_contents(loaded)
    if not bases:
        raise BundleError(
            "No base game content was found. Select the base XCI/NSP, not just an update or DLC."
        )
    # A merged XCI/NSP can contain several content groups, but only one base group.
    base_groups = {g.group_id: g for g in bases}
    if len(base_groups) > 1:
        ids = ", ".join(f"{g:016X}" for g in sorted(base_groups))
        raise BundleError(f"Inputs contain more than one base game: {ids}. Bundle one game at a time.")
    base = bases[0]

    preferred_paths = {c.path for c in (preferred or [])}

    def is_preferred(g: ContentGroup) -> bool:
        return any(e.path in preferred_paths for e in g.entries)

    def highest(groups: list[ContentGroup]) -> list[ContentGroup]:
        chosen: dict[int, ContentGroup] = {}
        for g in groups:
            old = chosen.get(g.title_id)
            key = (g.version, is_preferred(g), g.total_size)
            old_key = (old.version, is_preferred(old), old.total_size) if old else None
            if old is None or key > old_key:
                chosen[g.title_id] = g
        return list(chosen.values())

    update = None
    matching_updates = [g for g in updates if g.group_id == base.group_id]
    if matching_updates:
        update = highest(matching_updates)[0]
    selected_dlcs = [
        g for g in highest([g for g in dlcs if g.group_id == base.group_id])
    ]
    selected_dlcs.sort(key=lambda g: g.title_id)

    warnings: list[str] = []
    for c in loaded:
        warnings.extend(c.warnings)
    foreign = sorted({g.title_id for g in updates + dlcs if g.group_id != base.group_id})
    for tid in foreign:
        warnings.append(
            f"Ignoring content {tid:016X} because it belongs to a different base title."
        )
    if len([g for g in updates if g.group_id == base.group_id]) > 1:
        kept = update.title_id if update else 0
        warnings.append(
            "Multiple updates were supplied; keeping the highest version"
            + (f" ({kept:016X})." if kept else ".")
        )
    if update and update.version == 0:
        warnings.append(
            "Update version could not be read from the filename; confirm the correct update was selected."
        )
    return BundlePlan(base, update, selected_dlcs, loaded, warnings)


def _selected_entries(plan: BundlePlan) -> list[ContentEntry]:
    entries: list[ContentEntry] = []
    for group in [plan.base, plan.update, *plan.dlcs]:
        if not group:
            continue
        # Keep the CNMT last inside each package.  This preserves package
        # boundaries in the output and matches Nintendo's own layout.
        ordered = sorted(group.entries, key=lambda e: (e.name.lower().endswith(".cnmt.nca"), e.name))
        entries.extend(ordered)
    # Deduplicate by name; identical content is common when merging an already-merged base.
    seen: dict[str, ContentEntry] = {}
    for e in entries:
        old = seen.get(e.name)
        if old is None:
            seen[e.name] = e
        elif old.size != e.size or _quick_hash(old) != _quick_hash(e):
            raise BundleError(
                f"Content name collision for {e.name}: two different files have the same name."
            )
    return list(seen.values())


def _quick_hash(entry: ContentEntry) -> str:
    h = hashlib.sha256()
    with entry.path.open("rb") as f:
        f.seek(entry.abs_offset)
        remaining = min(entry.size, 0x200)
        while remaining:
            chunk = f.read(min(READ_CHUNK, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _tickets_for(plan: BundlePlan, entries: list[ContentEntry]):
    by_rights = {}
    for c in plan.containers:
        for t in c.tickets:
            by_rights.setdefault(t.rights_id, t)
    return by_rights


def prepare_ncas(plan: BundlePlan, ks: KeyStore, *, for_xci: bool) -> list[PreparedNca]:
    entries = _selected_entries(plan)
    tickets = _tickets_for(plan, entries)
    prepared: list[PreparedNca] = []
    for e in entries:
        assert e.nca is not None
        with e.path.open("rb") as f:
            f.seek(e.abs_offset)
            raw_header = f.read(0xC00)
        if len(raw_header) != 0xC00:
            raise FormatError(f"{e.name}: truncated NCA header")
        nca = parse_nca_header(raw_header, ks)
        rewritten = False
        header = raw_header
        if nca.has_rights:
            ticket = tickets.get(nca.rights_id)
            if ticket is not None:
                title_key = ticket.title_key(ks, nca.key_generation)
                if for_xci:
                    header = rewrite_nca_for_xci(nca, ks, title_key)
                    rewritten = True
                # For NSP output, keep the rights ID and the ticket.
            else:
                raise FormatError(
                    f"{e.name}: title-key NCA has no matching ticket; cannot convert it to "
                    "ticketless form without the title key."
                )
        prepared.append(PreparedNca(e.name, e.size, e.path, e.abs_offset, header, rewritten))
    return prepared


def _write_entry(dst, src_path: Path, src_offset: int, size: int, header: bytes | None = None,
                 on_bytes: Callable[[int], None] | None = None):
    with src_path.open("rb") as src:
        src.seek(src_offset)
        if header:
            dst.write(header)
            if on_bytes:
                on_bytes(len(header))
            # Skip the header we already wrote; otherwise the payload is
            # shifted by 0xC00 and every NCA after it is corrupt.
            src.seek(src_offset + len(header))
            remaining = size - len(header)
        else:
            remaining = size
        while remaining:
            chunk = src.read(min(READ_CHUNK, remaining))
            if not chunk:
                raise BundleError(f"Unexpected end of file while reading {src_path.name}")
            dst.write(chunk)
            if on_bytes:
                on_bytes(len(chunk))
            remaining -= len(chunk)


def estimate_xci(plan: BundlePlan, ks: KeyStore) -> tuple[int, int, int]:
    ncas = prepare_ncas(plan, ks, for_xci=True)
    secure_files = [(n.name, n.size, n.header_hash, 0x200) for n in ncas]
    secure_header, secure_offsets = build_hfs0(secure_files, alignment=0x200)
    secure_end = max((off + n.size for n, off in zip(ncas, secure_offsets)), default=0)
    secure_total = (len(secure_header) + secure_end + 0x1FF) & ~0x1FF
    total = XCI_PREFIX_SIZE + 0x200 + 0x400 + secure_total
    byte, capacity = card_size_for(total)
    return total, byte, capacity


def build_xci(plan: BundlePlan, ks: KeyStore, out_dir: Path, *, name: str | None = None,
              dry_run: bool = False, progress: Progress | None = None) -> Path:
    ncas = prepare_ncas(plan, ks, for_xci=True)
    secure_files = [(n.name, n.size, n.header_hash, 0x200) for n in ncas]
    secure_header, secure_offsets = build_hfs0(secure_files, alignment=0x200)
    secure_end = max((off + n.size for n, off in zip(ncas, secure_offsets)), default=0)
    secure_total = (len(secure_header) + secure_end + 0x1FF) & ~0x1FF
    secure_offset = XCI_PREFIX_SIZE + 0x200 + 0x400
    if secure_offset % 0x200:
        raise AssertionError("secure offset must be media-aligned")
    total_size = secure_offset + secure_total
    card_byte, capacity = card_size_for(total_size)

    empty = b"HFS0" + b"\x00" * 12 + b"\x00" * 0x1F0
    empty_hash = sha256(empty)
    secure_hash = sha256(secure_header)
    root_header, _ = build_hfs0(
        [
            ("update", 0x200, empty_hash, 0x200),
            ("normal", 0x200, empty_hash, 0x200),
            ("secure", secure_total, secure_hash, len(secure_header)),
        ],
        alignment=0x200,
    )
    if len(root_header) != 0x200:
        raise AssertionError(f"unexpected root header size {len(root_header):#x}")
    root_hash = sha256(root_header)
    prefix = xci_prefix(
        total_size,
        secure_offset=secure_offset,
        root_header_size=len(root_header),
        root_hash=root_hash,
        card_size_byte=card_byte,
    )
    out = out_dir / (name or default_name(plan, "xci"))
    if dry_run:
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    done = 0
    total = sum(n.size for n in ncas) or 1

    def on_bytes(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done, total, "Writing XCI")
    try:
        with tmp.open("wb") as dst:
            dst.write(prefix)
            dst.write(root_header)
            dst.write(empty)
            dst.write(empty)
            dst.write(secure_header)
            cursor = len(secure_header)
            data_base = len(secure_header)
            for nca, rel in zip(ncas, secure_offsets):
                target = data_base + rel
                if cursor > target:
                    raise AssertionError("HFS0 offsets overlap")
                dst.write(b"\x00" * (target - cursor))
                _write_entry(dst, nca.source_path, nca.source_offset, nca.size, nca.header, on_bytes)
                cursor = target + nca.size
            end = secure_offset + secure_total
            written = dst.tell()
            if written < end:
                dst.write(b"\x00" * (end - written))
            elif written > end:
                raise AssertionError("XCI overflow")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out


def build_nsp(plan: BundlePlan, ks: KeyStore, out_dir: Path, *, name: str | None = None,
              dry_run: bool = False, progress: Progress | None = None) -> Path:
    ncas = prepare_ncas(plan, ks, for_xci=False)
    items: list[tuple[str, int, Path, int, bytes | None]] = [
        (n.name, n.size, n.source_path, n.source_offset, n.header if n.was_rewritten else None)
        for n in ncas
    ]
    included_rights = set()
    included_titles = set()
    selected = _selected_entries(plan)
    for e in selected:
        if e.package_title_id is not None:
            included_titles.add(e.package_title_id)
        elif e.nca is not None:
            included_titles.add(e.nca.title_id)
        with e.path.open("rb") as f:
            f.seek(e.abs_offset)
            raw = f.read(0xC00)
        try:
            h = parse_nca_header(raw, ks)
            if h.has_rights:
                included_rights.add(h.rights_id)
        except Exception:
            pass
    seen_names = {i[0] for i in items}
    included_ticket = False
    for container in plan.containers:
        # Only take aux files from containers that actually contributed selected content.
        contributed = any(e.path == container.path for e in selected)
        if not contributed:
            continue
        for e in container.entries:
            if e.is_nca or e.name in seen_names:
                continue
            if e.is_ticket:
                from .ticket import Ticket
                with e.path.open("rb") as f:
                    f.seek(e.abs_offset)
                    t = Ticket.parse(f.read(e.size))
                ticket_title = int.from_bytes(t.rights_id[:8], "big")
                if t.rights_id not in included_rights and ticket_title not in included_titles:
                    continue
                included_ticket = True
            elif e.is_cert:
                if not included_ticket:
                    # Certs are emitted after their matching ticket is seen; a second pass
                    # below handles certs that precede tickets in the source container.
                    continue
            items.append((e.name, e.size, e.path, e.abs_offset, None))
            seen_names.add(e.name)
    # Second pass: add certs/xml for containers that contributed tickets/content.
    for container in plan.containers:
        if not any(e.path == container.path for e in selected):
            continue
        for e in container.entries:
            if e.name in seen_names or e.is_nca or e.is_ticket:
                continue
            if e.is_cert and not included_ticket:
                continue
            items.append((e.name, e.size, e.path, e.abs_offset, None))
            seen_names.add(e.name)
    header, offsets = build_pfs0_layout([(i[0], i[1]) for i in items])
    out = out_dir / (name or default_name(plan, "nsp"))
    if dry_run:
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    done = 0
    total = sum(i[1] for i in items) or 1

    def on_bytes(n: int) -> None:
        nonlocal done
        done += n
        if progress:
            progress(done, total, "Writing NSP")
    try:
        with tmp.open("wb") as dst:
            dst.write(header)
            cursor = len(header)
            data_base = len(header)
            for item, rel in zip(items, offsets):
                target = data_base + rel
                if cursor > target:
                    raise AssertionError("PFS0 offsets overlap")
                dst.write(b"\x00" * (target - cursor))
                name, size, path, abs_offset, replacement = item
                _write_entry(dst, path, abs_offset, size, replacement, on_bytes)
                cursor = target + size
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return out


def default_name(plan: BundlePlan, ext: str) -> str:
    base = plan.base.entries[0].path
    stem = base.name
    for suffix in (".xci", ".nsp", ".nsz", ".xcz"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = re.sub(r"\s*\[[0-9A-Fa-f]{16}\].*$", "", stem)
    stem = re.sub(r"\s*\[v\d+\].*$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*\([^)]*\)\s*$", "", stem)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "game"
    dlc_count = len(plan.dlcs)
    bits = f"1G+{1 if plan.update else 0}U"
    if dlc_count:
        bits += f"+{dlc_count}D"
    return f"{stem} [{plan.title_id:016X}] [v{plan.version}] ({bits}).{ext}"
