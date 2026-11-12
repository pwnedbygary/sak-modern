"""NUP (NX Package Format) v1 - plaintext container for unencrypted NX dumps.

NUP is a clean-room container for decrypted Nintendo Switch 1 / Switch 2 game
dumps.  Instead of encrypted NCAs it stores the ExeFS program files and the
full RomFS tree directly, so a base game, its updates and its add-ons can live
in a single keyless file.  Reference specification (v1.0-draft):
https://github.com/a55uka/nup (``SPEC.md``).

Disk layout, all integers little-endian::

    0x00                  superblock, 0x80 bytes
    header_size (0x80)    TOC: packages, dirs, files, children, string pool
    blob_offset           blob region: 16-byte-aligned file extents
    file_size             per-package meta TLV blocks (optional)

NUP is not an NCA container, so it does not travel through
:mod:`sak_modern.core.model`.  This module offers

* :func:`open_nup` / :class:`NupContainer` - strict reader (raw + zstd codecs)
* :func:`build_nup` - producer (folder tree -> ``.nup``)
* :func:`collect_packages` - folder discovery (single, wrapped and merged)
* :func:`extract_nup` - ``.nup`` -> folder (canonical tree or split layout)
* :func:`verify_nup` - structural and content verification
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .errors import FormatError

try:  # optional extra; only needed for codec 1 (zstd) and compressed output
    import zstandard as _zstd
except ImportError:  # pragma: no cover - exercised only without the extra
    _zstd = None


# ---------------------------------------------------------------------------
# constants (SPEC.md section 8)
# ---------------------------------------------------------------------------

NUP_MAGIC = b"NUP\x01"
NUP_MAJOR = 1

SUPERBLOCK_SIZE = 0x80
TOC_PREFIX_SIZE = 0x30
PACKAGE_RECORD_SIZE = 0x40
DIR_RECORD_SIZE = 0x18
FILE_RECORD_SIZE = 0x50
EXTENT_ALIGNMENT = 16

TOC_MAGIC = 0x01434F54  # b"TOC" + format version 1

CODEC_RAW = 0
CODEC_ZSTD = 1
CODEC_NAMES = {CODEC_RAW: "raw", CODEC_ZSTD: "zstd"}
CODEC_IDS = {name: ident for ident, name in CODEC_NAMES.items()}

KIND_BASE = 0
KIND_UPDATE = 1
KIND_ADDON = 2
KIND_ADDON_UPDATE = 3
KIND_SYSTEM = 4
KIND_NAMES = {
    KIND_BASE: "base",
    KIND_UPDATE: "update",
    KIND_ADDON: "addon",
    KIND_ADDON_UPDATE: "addon_update",
    KIND_SYSTEM: "system",
}
KIND_IDS = {name: ident for ident, name in KIND_NAMES.items()}

FLAG_STANDALONE_UPDATE = 0x01

TLV_NPDM = 0x0001
TLV_NACP = 0x0002
TLV_ICON = 0x0003
TLV_DISPLAY_VERSION = 0x0004
TLV_TITLE_NAME = 0x0005
TLV_PUBLISHER = 0x0006
TLV_BUILD_ID = 0x0007
TLV_SOURCE_NAME = 0x0008
TLV_LOCALIZED_TITLES = 0x0009
TLV_LOCALIZED_ICONS = 0x000A
TLV_NAMES = {
    TLV_NPDM: "npdm",
    TLV_NACP: "nacp",
    TLV_ICON: "icon",
    TLV_DISPLAY_VERSION: "display_version",
    TLV_TITLE_NAME: "title_name",
    TLV_PUBLISHER: "publisher",
    TLV_BUILD_ID: "build_id",
    TLV_SOURCE_NAME: "source_name",
    TLV_LOCALIZED_TITLES: "localized_titles",
    TLV_LOCALIZED_ICONS: "localized_icons",
}

NO_DIR = 0xFFFFFFFF

EXEFS = "exefs"
ROMFS = "romfs"
DATA = "data"
META_DIR = "meta"
UPDATES_DIR = "updates"
DLC_DIR = "dlc"

#: Dump-tool artefacts that must never end up in a NUP tree (SPEC.md 6.3).
RESERVED_ARTIFACT_NAMES = frozenset({".listing"})

_CHUNK = 1 << 20
_KNOWN_TITLE_ID_TOP = (0x01, 0x04, 0x05)
_MAX_TREE_DEPTH = 256
#: Sanity ceiling for the TOC region.  A 186,592-file container needs ~15 MiB;
#: this stops a hostile or corrupt superblock from reserving gigabytes.
_MAX_TOC_SIZE = 512 << 20
_DOTTED_VERSION = re.compile(r"(?<!\d)(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d)")


class NupError(FormatError):
    """A NUP container is malformed, unsupported, or inconsistent."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _decode_version(version: int) -> str:
    """Render the NCM ``version`` u32 as ``major.minor.micro``."""
    return f"{(version >> 16) & 0xFF}.{(version >> 8) & 0xFF}.{version & 0xFF}"


def _sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _hash_file(path: Path) -> tuple[bytes, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return digest.digest(), total


def _align(value: int, alignment: int = EXTENT_ALIGNMENT) -> int:
    return (value + alignment - 1) // alignment * alignment


def _require_zstd() -> None:
    if _zstd is None:  # pragma: no cover - depends on the optional extra
        raise NupError(
            "this NUP uses zstd-compressed extents; install the optional extra "
            "with: pip install 'sak-modern[zstd]'"
        )


def _decode_name(pool: bytes, offset: int, length: int, *, where: str) -> str:
    if offset < 0 or length < 0 or offset + length > len(pool):
        raise NupError(
            f"{where}: string reference (offset {offset}, length {length}) falls "
            f"outside the {len(pool)}-byte string pool"
        )
    return pool[offset : offset + length].decode("utf-8", "replace")


def _safe_parts(raw: str, *, where: str) -> tuple[str, ...]:
    """Split a stored node name into path components, refusing traversal.

    SPEC.md 6.1.5: node names are labels and must never be trusted as paths by
    the consumer.  A container produced elsewhere may hold names such as
    ``../../etc/passwd``, so they are rejected instead of followed.
    """
    if not raw:
        raise NupError(f"{where}: empty node name")
    if "\x00" in raw:
        raise NupError(f"{where}: node name contains a NUL byte")
    parts = tuple(
        part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")
    )
    if not parts:
        raise NupError(f"{where}: node name {raw!r} does not name anything")
    for part in parts:
        if part == "..":
            raise NupError(f"{where}: node name {raw!r} escapes the output tree")
        if ":" in part:
            # Rejects "C:" and also drive-relative "C:evil", which Windows
            # joins by discarding the base directory entirely.
            raise NupError(
                f"{where}: node name {raw!r} contains a ':' (drive or stream "
                f"separator)"
            )
    return parts


def _looks_like_title_id(value: int) -> bool:
    """NX title IDs start 0x01 (Switch 1) or 0x04/0x05 (Switch 2 / add-on).

    The low three nibbles of a title ID are always zero, which is what
    separates a real program ID from an arbitrary 64-bit word in the NPDM.
    """
    if not value or value >> 56 not in _KNOWN_TITLE_ID_TOP:
        return False
    return (value & 0xFFF) == 0


def npdm_program_id(npdm: bytes | None) -> int:
    """Extract the program ID from NPDM (``META``) bytes.

    Switch 1 NPDMs place it at ``ACI0 + 0x00``; the Switch 2 NPDM revision seen
    in shipping dumps moves it to ``ACI0 + 0x10``.  Both candidates are
    bounds-checked and validated against the NX title-ID shape, so an
    unreadable NPDM yields ``0`` rather than a plausible-looking wrong value.
    """
    if not npdm or len(npdm) < 0x80 or npdm[0:4] != b"META":
        return 0
    (aci0_offset,) = struct.unpack_from("<I", npdm, 0x70)
    for bump in (0x00, 0x10):
        at = aci0_offset + bump
        if at + 8 > len(npdm):
            continue
        (candidate,) = struct.unpack_from("<Q", npdm, at)
        if _looks_like_title_id(candidate):
            return candidate
    return 0


def _parse_localized(payload: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield ``(language tag, data)`` pairs from a 0x0009 / 0x000A payload."""
    pos = 0
    limit = len(payload)
    while pos + 1 <= limit:
        lang_len = payload[pos]
        pos += 1
        if pos + lang_len + 4 > limit:
            break
        lang = payload[pos : pos + lang_len].decode("ascii", "replace")
        pos += lang_len
        (data_len,) = struct.unpack_from("<I", payload, pos)
        pos += 4
        if pos + data_len > limit:
            break
        yield lang, payload[pos : pos + data_len]
        pos += data_len


# ---------------------------------------------------------------------------
# reader model
# ---------------------------------------------------------------------------


@dataclass
class NupDir:
    """One directory record (SPEC.md 3.2)."""

    name: str
    first_child: int
    child_count: int
    index: int = -1

    @property
    def is_root(self) -> bool:
        return self.name == ""

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.name or '<root>'}/ ({self.child_count} children)"


@dataclass
class NupFile:
    """One file record (SPEC.md 3.4)."""

    name: str
    size: int
    extent_offset: int
    extent_size: int
    sha256: bytes
    codec: int
    codec_flags: int
    index: int = -1

    @property
    def sha256_hex(self) -> str:
        return self.sha256.hex()

    @property
    def codec_name(self) -> str:
        return CODEC_NAMES.get(self.codec, f"unknown({self.codec})")

    @property
    def is_compressed(self) -> bool:
        return self.codec != CODEC_RAW


@dataclass
class NupPackage:
    """One package record plus its decoded meta TLVs (SPEC.md 3.1 and 5)."""

    title_id: int
    root_dir: int
    exefs_dir: int
    romfs_dir: int
    version: int
    kind: int
    flags: int
    meta_offset: int
    meta_size: int
    meta: dict[int, bytes] = field(default_factory=dict)

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"unknown({self.kind})")

    @property
    def standalone_update(self) -> bool:
        return bool(self.flags & FLAG_STANDALONE_UPDATE)

    @property
    def title_id_hex(self) -> str:
        return f"{self.title_id:016x}"

    def _text(self, tlv: int) -> str | None:
        raw = self.meta.get(tlv)
        if not raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @property
    def npdm(self) -> bytes | None:
        return self.meta.get(TLV_NPDM)

    @property
    def nacp(self) -> bytes | None:
        return self.meta.get(TLV_NACP)

    @property
    def icon(self) -> bytes | None:
        return self.meta.get(TLV_ICON)

    @property
    def build_id(self) -> bytes | None:
        return self.meta.get(TLV_BUILD_ID)

    @property
    def title_name(self) -> str | None:
        return self._text(TLV_TITLE_NAME)

    @property
    def publisher(self) -> str | None:
        return self._text(TLV_PUBLISHER)

    @property
    def source_name(self) -> str | None:
        return self._text(TLV_SOURCE_NAME)

    @property
    def display_version(self) -> str:
        """TLV 0x0004 when present, else the decoded NCM version.

        SPEC.md 3.1.1: the TLV is authoritative because some titles encode the
        NCM version non-standardly.
        """
        text = self._text(TLV_DISPLAY_VERSION)
        return text if text else _decode_version(self.version)

    @property
    def localized_titles(self) -> dict[str, str]:
        raw = self.meta.get(TLV_LOCALIZED_TITLES)
        if not raw:
            return {}
        return {
            lang: payload.decode("utf-8", "replace")
            for lang, payload in _parse_localized(raw)
        }

    @property
    def program_id(self) -> int:
        """Program ID from the NPDM TLV, or ``0`` when unreadable."""
        return npdm_program_id(self.npdm) if self.npdm else 0


@dataclass
class NupSummary:
    """Flat, UI-friendly view of one package."""

    index: int
    title_id: int
    kind: int
    kind_name: str
    version: int
    display_version: str
    title_name: str | None
    standalone_update: bool
    file_count: int
    total_size: int
    exefs_dir: int
    romfs_dir: int
    meta_types: tuple[int, ...]


class NupContainer:
    """A parsed, validated NUP container; obtain one from :func:`open_nup`.

    Only the TOC is parsed up front.  File bytes are read, decompressed and
    verified on demand.
    """

    def __init__(
        self,
        path: Path,
        *,
        header_size: int,
        header_version: int,
        toc_offset: int,
        toc_size: int,
        toc_sha256: bytes,
        blob_offset: int,
        file_size: int,
        flags: int,
        uuid: bytes,
        packages: list[NupPackage],
        dirs: list[NupDir],
        files: list[NupFile],
        children: list[int],
        warnings: list[str] | None = None,
    ) -> None:
        self.path = path
        self.header_size = header_size
        self.header_version = header_version
        self.toc_offset = toc_offset
        self.toc_size = toc_size
        self.toc_sha256 = toc_sha256
        self.blob_offset = blob_offset
        self.file_size = file_size
        self.flags = flags
        self.uuid = uuid
        self.packages = packages
        self.dirs = dirs
        self.files = files
        self.children = children
        self.warnings = list(warnings or [])

    # -- basics ----------------------------------------------------------

    @property
    def kind(self) -> str:
        return "nup"

    @property
    def package_count(self) -> int:
        return len(self.packages)

    @property
    def primary(self) -> NupPackage:
        if not self.packages:
            raise NupError("NUP contains no packages")
        return self.packages[0]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<NupContainer {self.path.name} packages={len(self.packages)} "
            f"dirs={len(self.dirs)} files={len(self.files)}>"
        )

    # -- tree ------------------------------------------------------------

    def iter_tree(
        self, package: NupPackage
    ) -> Iterator[tuple[tuple[str, ...], NupDir | NupFile]]:
        """Depth-first ``(path parts, node)`` walk of one package's tree.

        Children are visited in their stored order, which producers must keep
        sorted bytewise by name (SPEC.md 3.3).
        """
        ndir = len(self.dirs)
        active: set[int] = set()

        def visit(dir_index: int, parts: tuple[str, ...], depth: int = 0):
            if depth > _MAX_TREE_DEPTH:
                raise NupError(
                    f"directory nesting exceeds {_MAX_TREE_DEPTH} levels; "
                    f"refusing to walk a pathological tree"
                )
            if dir_index >= ndir:
                raise NupError(
                    f"directory index {dir_index} is out of range ({ndir} dirs)"
                )
            if dir_index in active:
                raise NupError(f"directory cycle detected at index {dir_index}")
            active.add(dir_index)
            node = self.dirs[dir_index]
            yield parts, node
            start = node.first_child
            stop = start + node.child_count
            if stop > len(self.children):
                raise NupError(
                    f"directory {node.name!r} child range [{start}, {stop}) exceeds "
                    f"the {len(self.children)}-slot children array"
                )
            for slot in range(start, stop):
                node_id = self.children[slot]
                if node_id < ndir:
                    child = self.dirs[node_id]
                    yield from visit(
                        node_id,
                        parts + _safe_parts(child.name, where="directory"),
                        depth + 1,
                    )
                else:
                    file_index = node_id - ndir
                    if file_index >= len(self.files):
                        raise NupError(
                            f"node id {node_id} refers to file {file_index}, but "
                            f"only {len(self.files)} file records exist"
                        )
                    child = self.files[file_index]
                    yield parts + _safe_parts(child.name, where="file"), child
            active.discard(dir_index)

        yield from visit(package.root_dir, ())

    def package_files(
        self, package: NupPackage
    ) -> list[tuple[tuple[str, ...], NupFile]]:
        """Every file in one package as ``(path parts, record)``."""
        return [
            (parts, node)
            for parts, node in self.iter_tree(package)
            if isinstance(node, NupFile)
        ]

    def package_dirs(self, package: NupPackage) -> list[tuple[tuple[str, ...], NupDir]]:
        """Every directory in one package as ``(path parts, record)``."""
        return [
            (parts, node)
            for parts, node in self.iter_tree(package)
            if isinstance(node, NupDir)
        ]

    def child_dir(self, package: NupPackage, name: str) -> int:
        """Index of a direct child directory of the package root, or ``NO_DIR``."""
        root = self.dirs[package.root_dir]
        ndir = len(self.dirs)
        for slot in range(root.first_child, root.first_child + root.child_count):
            node_id = self.children[slot]
            if node_id < ndir and self.dirs[node_id].name == name:
                return node_id
        return NO_DIR

    def find(
        self, package: NupPackage, parts: Sequence[str]
    ) -> NupFile | NupDir | None:
        """Resolve a path inside one package, or ``None``."""
        target = _safe_parts("/".join(parts), where="lookup") if parts else ()
        for node_parts, node in self.iter_tree(package):
            if node_parts == target:
                return node
        return None

    # -- content ---------------------------------------------------------

    def read_extent(self, node: NupFile) -> bytes:
        """Raw stored bytes of one file's extent, without decompression."""
        if node.extent_size == 0:
            return b""
        with self.path.open("rb") as handle:
            handle.seek(node.extent_offset)
            data = handle.read(node.extent_size)
        if len(data) != node.extent_size:
            raise NupError(
                f"{node.name!r}: truncated extent (expected {node.extent_size} "
                f"bytes at 0x{node.extent_offset:x}, got {len(data)})"
            )
        return data

    def read(self, node: NupFile, *, verify: bool = True) -> bytes:
        """Decompressed content of one file, optionally SHA-256 verified."""
        if node.size == 0 and node.extent_size == 0:
            data = b""
        else:
            data = self.read_extent(node)
            if node.codec == CODEC_ZSTD:
                _require_zstd()
                try:
                    data = _zstd.ZstdDecompressor().decompress(
                        data, max_output_size=max(node.size, 1)
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced as NupError
                    raise NupError(
                        f"{node.name!r}: zstd decompression failed: {exc}"
                    ) from exc
            elif node.codec != CODEC_RAW:
                raise NupError(
                    f"{node.name!r}: unsupported codec {node.codec} "
                    f"({node.codec_name}); NUP v1 defines raw and zstd"
                )
        if len(data) != node.size:
            raise NupError(
                f"{node.name!r}: decoded {len(data)} bytes, record says {node.size}"
            )
        if verify:
            digest = _sha256_bytes(data)
            if digest != node.sha256:
                raise NupError(
                    f"{node.name!r}: SHA-256 mismatch (record {node.sha256_hex}, "
                    f"content {digest.hex()})"
                )
        return data

    def copy_to(self, node: NupFile, handle, *, verify: bool = True) -> int:
        """Stream one file's content into *handle*; returns bytes written."""
        if node.size == 0 and node.extent_size == 0:
            return 0
        if node.codec == CODEC_RAW and node.extent_size == node.size:
            digest = hashlib.sha256()
            written = 0
            with self.path.open("rb") as source:
                source.seek(node.extent_offset)
                remaining = node.extent_size
                while remaining:
                    block = source.read(min(_CHUNK, remaining))
                    if not block:
                        raise NupError(
                            f"{node.name!r}: truncated extent at "
                            f"0x{node.extent_offset + written:x}"
                        )
                    digest.update(block)
                    handle.write(block)
                    written += len(block)
                    remaining -= len(block)
            if verify:
                digest_value = digest.digest()
                if digest_value != node.sha256:
                    raise NupError(
                        f"{node.name!r}: SHA-256 mismatch (record "
                        f"{node.sha256_hex}, content {digest_value.hex()})"
                    )
            return written
        data = self.read(node, verify=verify)
        handle.write(data)
        return len(data)

    # -- reporting -------------------------------------------------------

    def summaries(self) -> list[NupSummary]:
        """One :class:`NupSummary` per package, in TOC order."""
        out: list[NupSummary] = []
        for index, package in enumerate(self.packages):
            entries = self.package_files(package)
            out.append(
                NupSummary(
                    index=index,
                    title_id=package.title_id,
                    kind=package.kind,
                    kind_name=package.kind_name,
                    version=package.version,
                    display_version=package.display_version,
                    title_name=package.title_name,
                    standalone_update=package.standalone_update,
                    file_count=len(entries),
                    total_size=sum(node.size for _, node in entries),
                    exefs_dir=package.exefs_dir,
                    romfs_dir=package.romfs_dir,
                    meta_types=tuple(sorted(package.meta)),
                )
            )
        return out


# ---------------------------------------------------------------------------
# reader
# ---------------------------------------------------------------------------


def is_nup(path: str | os.PathLike[str]) -> bool:
    """True when *path* starts with the ``NUP\\x01`` magic."""
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == NUP_MAGIC
    except (OSError, ValueError):
        return False


def open_nup(path: str | os.PathLike[str], *, verify_toc: bool = True) -> NupContainer:
    """Parse a NUP container, applying every validation rule from SPEC.md 2."""
    container_path = Path(path)
    if not container_path.is_file():
        raise NupError(f"{container_path} is not a file")
    actual_size = container_path.stat().st_size
    warnings: list[str] = []

    with container_path.open("rb") as handle:
        head = handle.read(SUPERBLOCK_SIZE)
        if len(head) < SUPERBLOCK_SIZE:
            raise NupError(
                f"{container_path.name} is {len(head)} bytes; a NUP superblock "
                f"needs {SUPERBLOCK_SIZE}"
            )
        if head[0:4] != NUP_MAGIC:
            raise NupError(
                f"{container_path.name} is not a NUP container (magic "
                f"{head[0:4]!r}, expected {NUP_MAGIC!r})"
            )
        header_size, header_version = struct.unpack_from("<HH", head, 0x04)
        toc_offset, toc_size = struct.unpack_from("<QQ", head, 0x08)
        toc_sha256 = head[0x18:0x38]
        blob_offset, declared_size = struct.unpack_from("<QQ", head, 0x38)
        package_count, flags = struct.unpack_from("<II", head, 0x48)
        uuid = head[0x50:0x60]
        reserved = head[0x60:0x80]

        if header_size < SUPERBLOCK_SIZE:
            raise NupError(f"header_size 0x{header_size:x} is below the 0x80 minimum")
        if header_version < 1:
            raise NupError(
                f"header_version {header_version} is invalid; the superblock "
                f"version starts at 1"
            )
        if header_version > 1:
            warnings.append(
                f"header_version {header_version} is newer than v1; fields "
                f"beyond 0x80 are ignored as SPEC.md 2 requires"
            )
        if toc_offset != header_size:
            raise NupError(
                f"toc_offset 0x{toc_offset:x} must equal header_size "
                f"0x{header_size:x}"
            )
        if blob_offset % EXTENT_ALIGNMENT:
            raise NupError(
                f"blob_offset 0x{blob_offset:x} is not {EXTENT_ALIGNMENT}-byte "
                f"aligned"
            )
        if blob_offset < toc_offset + toc_size:
            raise NupError(
                f"blob_offset 0x{blob_offset:x} overlaps the TOC (ends at "
                f"0x{toc_offset + toc_size:x})"
            )
        # Bound the TOC before it is read: without this a hostile toc_size sends
        # handle.read() into a MemoryError instead of a NupError.
        if toc_offset + toc_size > actual_size:
            raise NupError(
                f"TOC [0x{toc_offset:x}, 0x{toc_offset + toc_size:x}) runs past "
                f"the end of the file (0x{actual_size:x})"
            )
        if blob_offset > actual_size:
            raise NupError(
                f"blob_offset 0x{blob_offset:x} is past the end of the file "
                f"(0x{actual_size:x})"
            )
        if toc_size > _MAX_TOC_SIZE:
            raise NupError(
                f"toc_size 0x{toc_size:x} exceeds the {_MAX_TOC_SIZE >> 20} MiB "
                f"ceiling this reader accepts"
            )
        if declared_size != actual_size:
            raise NupError(
                f"superblock declares file_size 0x{declared_size:x} but the file "
                f"is 0x{actual_size:x} bytes"
            )
        if package_count < 1:
            raise NupError("package_count must be at least 1")
        if flags:
            raise NupError(
                f"unknown superblock flags 0x{flags:x}; NUP v1 defines none"
            )
        if any(reserved):
            warnings.append("superblock reserved bytes are not zero")

        handle.seek(toc_offset)
        toc = handle.read(toc_size)
        if len(toc) != toc_size:
            raise NupError(
                f"TOC at 0x{toc_offset:x} runs past the end of the file "
                f"({len(toc)} of {toc_size} bytes available)"
            )

    if verify_toc:
        digest = _sha256_bytes(toc)
        if digest != toc_sha256:
            raise NupError(
                f"TOC SHA-256 mismatch: superblock has {toc_sha256.hex()}, "
                f"computed {digest.hex()}"
            )

    if len(toc) < TOC_PREFIX_SIZE:
        raise NupError(f"TOC is only {len(toc)} bytes; the header needs 0x30")

    magic, toc_package_count = struct.unpack_from("<II", toc, 0x00)
    if magic != TOC_MAGIC:
        raise NupError(f"bad TOC magic 0x{magic:08x} (expected 0x{TOC_MAGIC:08x})")
    if toc_package_count != package_count:
        raise NupError(
            f"TOC package_count {toc_package_count} disagrees with the "
            f"superblock's {package_count}"
        )
    dir_count, file_count = struct.unpack_from("<QQ", toc, 0x08)
    string_pool_off, string_pool_size = struct.unpack_from("<QQ", toc, 0x18)
    (toc_reserved,) = struct.unpack_from("<Q", toc, 0x28)
    if toc_reserved:
        warnings.append("TOC reserved field is not zero")

    packages_offset = TOC_PREFIX_SIZE
    dirs_offset = packages_offset + PACKAGE_RECORD_SIZE * package_count
    files_offset = dirs_offset + DIR_RECORD_SIZE * dir_count
    children_offset = files_offset + FILE_RECORD_SIZE * file_count
    if children_offset > len(toc):
        raise NupError(
            f"TOC record arrays need 0x{children_offset:x} bytes but the TOC is "
            f"only 0x{len(toc):x}"
        )
    if string_pool_off > len(toc) or string_pool_off + string_pool_size > len(toc):
        raise NupError(
            f"string pool [0x{string_pool_off:x}, "
            f"0x{string_pool_off + string_pool_size:x}) falls outside the "
            f"0x{len(toc):x}-byte TOC"
        )
    if string_pool_off < children_offset:
        raise NupError(
            f"string_pool_off 0x{string_pool_off:x} overlaps the children array, "
            f"which ends at 0x{children_offset:x}"
        )
    if (string_pool_off - children_offset) % 4:
        raise NupError(
            "children array is not a whole number of 4-byte slots before the "
            "string pool"
        )

    pool = toc[string_pool_off : string_pool_off + string_pool_size]

    packages: list[NupPackage] = []
    for index in range(package_count):
        base = packages_offset + index * PACKAGE_RECORD_SIZE
        title_id, root_dir, exefs_dir, romfs_dir, version = struct.unpack_from(
            "<QIIII", toc, base
        )
        kind = toc[base + 0x18]
        pkg_flags = toc[base + 0x19]
        reserved2 = toc[base + 0x1A : base + 0x1C]
        (reserved3,) = struct.unpack_from("<I", toc, base + 0x1C)
        meta_offset, meta_size = struct.unpack_from("<QI", toc, base + 0x20)
        if kind >= 5:
            raise NupError(f"package {index} has reserved kind {kind}")
        if pkg_flags & ~FLAG_STANDALONE_UPDATE:
            raise NupError(
                f"package {index} has unknown flags 0x{pkg_flags:x}; NUP v1 "
                f"defines bit 0 only"
            )
        if any(reserved2) or reserved3:
            warnings.append(f"package {index} reserved fields are not zero")
        if meta_size and meta_offset + meta_size > actual_size:
            raise NupError(
                f"package {index} meta block [0x{meta_offset:x}, "
                f"0x{meta_offset + meta_size:x}) runs past the end of the file"
            )
        packages.append(
            NupPackage(
                title_id=title_id,
                root_dir=root_dir,
                exefs_dir=exefs_dir,
                romfs_dir=romfs_dir,
                version=version,
                kind=kind,
                flags=pkg_flags,
                meta_offset=meta_offset,
                meta_size=meta_size,
            )
        )

    dirs: list[NupDir] = []
    for index in range(dir_count):
        base = dirs_offset + index * DIR_RECORD_SIZE
        name_off, name_len, first_child, child_count = struct.unpack_from(
            "<IIII", toc, base
        )
        dirs.append(
            NupDir(
                name=_decode_name(pool, name_off, name_len, where=f"dir {index}"),
                first_child=first_child,
                child_count=child_count,
                index=index,
            )
        )

    files: list[NupFile] = []
    for index in range(file_count):
        base = files_offset + index * FILE_RECORD_SIZE
        name_off, name_len, size, extent_offset, extent_size = struct.unpack_from(
            "<IIQQQ", toc, base
        )
        sha = toc[base + 0x20 : base + 0x40]
        codec = toc[base + 0x40]
        codec_flags = toc[base + 0x41]
        name = _decode_name(pool, name_off, name_len, where=f"file {index}")
        if codec not in CODEC_NAMES:
            raise NupError(
                f"file {index} ({name!r}) uses unknown codec {codec}; NUP v1 "
                f"defines raw (0) and zstd (1)"
            )
        if codec_flags:
            warnings.append(f"file {index} ({name!r}) has non-zero codec_flags")
        if extent_size == 0:
            if size != 0:
                raise NupError(
                    f"file {index} ({name!r}) has an empty extent but a size of "
                    f"{size} bytes"
                )
            if extent_offset != 0:
                raise NupError(
                    f"file {index} ({name!r}) has an empty extent but offset "
                    f"0x{extent_offset:x}; SPEC.md 3.6 requires zero-length "
                    f"files to use offset 0"
                )
            if codec != CODEC_RAW:
                warnings.append(
                    f"file {index} ({name!r}) is zero-length but declares codec "
                    f"{codec}; SPEC.md 3.6 expects raw"
                )
        else:
            if extent_offset % EXTENT_ALIGNMENT:
                raise NupError(
                    f"file {index} ({name!r}) extent offset 0x{extent_offset:x} "
                    f"is not {EXTENT_ALIGNMENT}-byte aligned"
                )
            if extent_offset < blob_offset:
                raise NupError(
                    f"file {index} ({name!r}) extent at 0x{extent_offset:x} "
                    f"starts before the blob region at 0x{blob_offset:x}"
                )
            if extent_offset + extent_size > actual_size:
                raise NupError(
                    f"file {index} ({name!r}) extent [0x{extent_offset:x}, "
                    f"0x{extent_offset + extent_size:x}) runs past the end of "
                    f"the file"
                )
        files.append(
            NupFile(
                name=name,
                size=size,
                extent_offset=extent_offset,
                extent_size=extent_size,
                sha256=sha,
                codec=codec,
                codec_flags=codec_flags,
                index=index,
            )
        )

    slot_count = (string_pool_off - children_offset) // 4
    children = (
        list(struct.unpack_from(f"<{slot_count}I", toc, children_offset))
        if slot_count
        else []
    )

    container = NupContainer(
        path=container_path,
        header_size=header_size,
        header_version=header_version,
        toc_offset=toc_offset,
        toc_size=toc_size,
        toc_sha256=toc_sha256,
        blob_offset=blob_offset,
        file_size=actual_size,
        flags=flags,
        uuid=uuid,
        packages=packages,
        dirs=dirs,
        files=files,
        children=children,
        warnings=warnings,
    )

    # Cross-check the convenience pointers and walk every tree once, so a
    # malformed child graph fails here instead of during extraction.
    for index, package in enumerate(packages):
        if package.root_dir >= dir_count:
            raise NupError(
                f"package {index} root_dir {package.root_dir} is not a valid "
                f"directory index"
            )
        for label in ("exefs_dir", "romfs_dir"):
            value = getattr(package, label)
            if value != NO_DIR and value >= dir_count:
                raise NupError(
                    f"package {index} {label} {value} is not a valid directory "
                    f"index"
                )
        list(container.package_files(package))

    _load_meta_blocks(container)
    return container


def _load_meta_blocks(container: NupContainer) -> None:
    """Decode each package's meta TLV list (SPEC.md section 5)."""
    if not any(package.meta_size for package in container.packages):
        return
    with container.path.open("rb") as handle:
        for index, package in enumerate(container.packages):
            if not package.meta_size:
                continue
            handle.seek(package.meta_offset)
            block = handle.read(package.meta_size)
            if len(block) != package.meta_size:
                raise NupError(
                    f"package {index} meta block is truncated ({len(block)} of "
                    f"{package.meta_size} bytes)"
                )
            pos = 0
            while pos + 8 <= len(block):
                tlv_type, _reserved, size = struct.unpack_from("<HHI", block, pos)
                if pos + 8 + size > len(block):
                    container.warnings.append(
                        f"package {index} meta TLV 0x{tlv_type:04x} size {size} "
                        f"overruns the block; ignoring the remainder"
                    )
                    break
                package.meta[tlv_type] = block[pos + 8 : pos + 8 + size]
                pos += 8 + size


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def verify_nup(container: NupContainer, *, check_hashes: bool = True) -> list[str]:
    """Return a list of human-readable problems; empty means healthy."""
    problems: list[str] = []
    seen_extents: dict[tuple[int, int], tuple[str, bytes]] = {}
    spans: list[tuple[int, int, str]] = []
    empty_hash = _sha256_bytes(b"")
    for package in container.packages:
        try:
            entries = container.package_files(package)
        except NupError as exc:
            problems.append(f"package {package.title_id:016x}: {exc}")
            continue
        if not entries:
            problems.append(f"package {package.title_id:016x} contains no files")
        for parts, node in entries:
            label = "/".join(parts)
            already_reported = False
            if node.extent_size:
                key = (node.extent_offset, node.extent_size)
                previous = seen_extents.setdefault(key, (label, node.sha256))
                if previous[1] != node.sha256:
                    problems.append(
                        f"{label}: extent 0x{node.extent_offset:x} is also used by "
                        f"{previous[0]} with a different SHA-256; SPEC.md 3.6 "
                        f"forbids sharing an extent across different content"
                    )
                spans.append(
                    (node.extent_offset, node.extent_offset + node.extent_size, label)
                )
            elif node.size == 0 and node.sha256 != empty_hash:
                # Zero-length content still has one correct hash; a wrong record
                # here is the only integrity signal an empty file carries.
                problems.append(
                    f"{label}: zero-length file records SHA-256 "
                    f"{node.sha256_hex}, which is not the hash of empty content"
                )
                already_reported = True
            if check_hashes and not already_reported:
                try:
                    container.read(node, verify=True)
                except NupError as exc:
                    problems.append(f"{label}: {exc}")

    # Extents must not partially overlap.  Identical spans are legitimate
    # dedup; anything else means two records claim the same bytes.
    spans.sort()
    for (start_a, end_a, label_a), (start_b, end_b, label_b) in zip(spans, spans[1:]):
        if start_b < end_a and (start_a, end_a) != (start_b, end_b):
            problems.append(
                f"{label_b}: extent 0x{start_b:x}..0x{end_b:x} partially overlaps "
                f"{label_a} (0x{start_a:x}..0x{end_a:x})"
            )
    return problems


# ---------------------------------------------------------------------------
# writer: plan model
# ---------------------------------------------------------------------------


@dataclass
class PackageSource:
    """One package to be packed into a NUP.

    Either *root* points at a folder whose children are ``exefs`` / ``romfs`` /
    ``data``, or *container* + *package_index* reference a package inside an
    existing ``.nup`` that is being re-bundled.

    *exclude* names first-level subdirectories that belong to sibling packages
    (``updates`` and ``dlc`` in a merged tree) and must not be packed twice.
    """

    root: Path | None = None
    kind: int = KIND_BASE
    title_id: int = 0
    version: int = 0
    meta: dict[int, bytes] = field(default_factory=dict)
    origin: str = ""
    exclude: tuple[str, ...] = ()
    container: "NupContainer | None" = None
    package_index: int = 0
    #: Add-on content index, from ``dlc/<title-id>/<index>/``.  Two add-on
    #: packages may share a title ID and differ only by this index.
    ordinal: int = 0

    @property
    def from_container(self) -> bool:
        return self.container is not None

    @property
    def kind_name(self) -> str:
        return KIND_NAMES.get(self.kind, f"unknown({self.kind})")

    def __str__(self) -> str:  # pragma: no cover - logging aid
        return f"{self.kind_name} {self.title_id:016x} v{self.version} @ {self.root}"


@dataclass
class _FilePlan:
    parts: tuple[str, ...]
    source: Path | None = None
    record: "NupFile | None" = None
    container: "NupContainer | None" = None
    size: int = 0
    sha256: bytes = b""
    codec: int = CODEC_RAW
    payload: bytes | None = None
    extent_size: int = 0
    extent_offset: int = 0
    index: int = -1
    canonical: "_FilePlan | None" = None


@dataclass
class _DirPlan:
    name: str
    dirs: dict[str, "_DirPlan"] = field(default_factory=dict)
    files: dict[str, _FilePlan] = field(default_factory=dict)
    index: int = -1
    first_child: int = 0
    child_count: int = 0


@dataclass
class _PackagePlan:
    source: PackageSource
    root: _DirPlan


# ---------------------------------------------------------------------------
# writer: folder discovery
# ---------------------------------------------------------------------------


def _looks_like_package_dir(path: Path) -> bool:
    return any((path / name).is_dir() for name in (EXEFS, ROMFS, DATA))


def _iter_source_files(
    root: Path, exclude: Sequence[str] = ()
) -> Iterator[tuple[tuple[str, ...], Path]]:
    """Yield ``(relative parts, path)`` for every payload file under *root*."""
    skip = set(exclude)
    for current, dirnames, filenames in os.walk(root):
        here = Path(current)
        rel = here.relative_to(root)
        prefix: tuple[str, ...] = () if rel == Path(".") else rel.parts
        if prefix and prefix[0] == META_DIR:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in RESERVED_ARTIFACT_NAMES and not (not prefix and d in skip)
        )
        for filename in sorted(filenames):
            if filename in RESERVED_ARTIFACT_NAMES:
                continue
            yield prefix + (filename,), here / filename


def _read_meta_dir(root: Path) -> dict[int, bytes]:
    """Reconstruct meta TLVs from an extracted ``meta/`` directory."""
    meta: dict[int, bytes] = {}
    meta_dir = root / META_DIR
    if not meta_dir.is_dir():
        return meta
    simple = {
        "title.txt": TLV_TITLE_NAME,
        "publisher.txt": TLV_PUBLISHER,
        "display-version.txt": TLV_DISPLAY_VERSION,
        "source.txt": TLV_SOURCE_NAME,
        "nacp.bin": TLV_NACP,
        "build-id.bin": TLV_BUILD_ID,
    }
    for filename, tlv in simple.items():
        candidate = meta_dir / filename
        if candidate.is_file():
            meta[tlv] = candidate.read_bytes()
    for candidate in sorted(meta_dir.glob("icon.*")):
        meta[TLV_ICON] = candidate.read_bytes()
        break
    for candidate in sorted(meta_dir.glob("tlv-0x*.bin")):
        try:
            tlv = int(candidate.stem.split("-", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        meta.setdefault(tlv, candidate.read_bytes())
    return meta


def _parse_hex_name(text: str) -> int | None:
    stripped = text.strip()
    if len(stripped) != 16:
        return None
    try:
        value = int(stripped, 16)
    except ValueError:
        return None
    return value or None


def _parse_decimal_name(text: str) -> int | None:
    stripped = text.strip()
    if not stripped.isdigit() or len(stripped) > 10:
        return None
    return int(stripped, 10)


def _parse_dotted_version(text: str) -> int | None:
    """Encode ``2.1.11`` as the NCM version u32 ``0x02010B``."""
    match = _DOTTED_VERSION.search(text)
    if not match:
        return None
    major, minor, micro = (int(part) for part in match.groups())
    return (major << 16) | (minor << 8) | micro


def _first_dotted(names: Sequence[str]) -> str | None:
    """The first ``x.y.z`` version string found in *names*, if any."""
    for name in names:
        match = _DOTTED_VERSION.search(name)
        if match:
            return match.group(0)
    return None


def _kind_from_names(names: Sequence[str], *, has_exefs: bool) -> int:
    """Infer a package kind from the wrapper folder names it was found under.

    A 16-digit hex wrapper is an add-on title ID, but only when it describes an
    add-on: DLC dumps are ``<title-id>/<index>/romfs`` with no ``exefs``, and a
    *base* dump is sometimes wrapped in its own title ID (``<id>/exefs``).
    Requiring a missing ``exefs`` (or a second path level) keeps that layout a
    base instead of silently turning it into an add-on.
    """
    lowered = [name.lower() for name in names]
    if any("update" in name or "patch" in name for name in lowered):
        return KIND_UPDATE
    if any("dlc" in name or "addon" in name or "add-on" in name for name in lowered):
        return KIND_ADDON
    hex_names = any(_parse_hex_name(name) is not None for name in names)
    if hex_names and (not has_exefs or len(names) > 1):
        return KIND_ADDON
    if not has_exefs:
        return KIND_ADDON
    if any(_parse_decimal_name(name) is not None for name in names):
        return KIND_UPDATE
    return KIND_BASE


def _metadata_from_names(names: Sequence[str]) -> tuple[int, int]:
    """``(title_id, version)`` inferred from wrapper folder names."""
    title_id = 0
    version = 0
    for name in names:
        if not title_id:
            title_id = _parse_hex_name(name) or 0
        if not version:
            version = _parse_decimal_name(name) or 0
    if not version:
        for name in names:
            version = _parse_dotted_version(name) or 0
            if version:
                break
    return title_id, version


def _merged_children(path: Path) -> list[PackageSource]:
    """Packages described by an ``updates/`` and/or ``dlc/`` sub-tree."""
    out: list[PackageSource] = []
    updates = path / UPDATES_DIR
    if updates.is_dir():
        for child in sorted(updates.iterdir()):
            if not child.is_dir() or not _looks_like_package_dir(child):
                continue
            out.append(
                PackageSource(
                    root=child,
                    kind=KIND_UPDATE,
                    version=_parse_decimal_name(child.name) or 0,
                    origin=f"{UPDATES_DIR}/{child.name}",
                )
            )
    dlc = path / DLC_DIR
    if dlc.is_dir():
        for addon in sorted(dlc.iterdir()):
            if not addon.is_dir():
                continue
            addon_tid = _parse_hex_name(addon.name) or 0
            used: set[int] = set()

            def take_ordinal(name: str) -> int:
                """Numeric index folders keep their number; odd names get a free one.

                Guaranteeing uniqueness here is what stops two add-on packages
                from colliding on the same slot and one of them being dropped.
                """
                ordinal = int(name) if name.isdigit() else 0
                while ordinal in used:
                    ordinal += 1
                used.add(ordinal)
                return ordinal

            if _looks_like_package_dir(addon):
                out.append(
                    PackageSource(
                        root=addon,
                        kind=KIND_ADDON,
                        title_id=addon_tid,
                        origin=f"{DLC_DIR}/{addon.name}",
                        ordinal=take_ordinal(""),
                    )
                )
                continue
            for index_dir in sorted(addon.iterdir()):
                if not index_dir.is_dir() or not _looks_like_package_dir(index_dir):
                    continue
                out.append(
                    PackageSource(
                        root=index_dir,
                        kind=KIND_ADDON,
                        title_id=addon_tid,
                        origin=f"{DLC_DIR}/{addon.name}/{index_dir.name}",
                        ordinal=take_ordinal(index_dir.name),
                    )
                )
    return out


def _collect_from_dir(path: Path, *, depth: int = 0) -> list[PackageSource]:
    """Resolve one input folder into package sources (see :func:`collect_packages`)."""
    if _looks_like_package_dir(path):
        # A package folder can *also* be a merged root that carries updates/
        # and dlc/ next to its own exefs/romfs.  Those siblings become their
        # own packages, so the base must not walk into them.
        merged = _merged_children(path)
        kind = _kind_from_names([path.name], has_exefs=(path / EXEFS).is_dir())
        base = PackageSource(root=path, kind=kind, origin=path.name)
        if kind != KIND_BASE:
            # Only non-base folders carry a meaningful version/title ID in
            # their own name; a base game's title must come from its NPDM.
            base.title_id, base.version = _metadata_from_names([path.name])
        if merged:
            consumed: list[str] = []
            if (path / UPDATES_DIR).is_dir() and any(
                item.kind == KIND_UPDATE for item in merged
            ):
                consumed.append(UPDATES_DIR)
            if (path / DLC_DIR).is_dir() and any(
                item.kind == KIND_ADDON for item in merged
            ):
                consumed.append(DLC_DIR)
            base.exclude = tuple(consumed)
            return [base, *merged]
        return [base]

    merged = _merged_children(path)
    if merged:
        return merged

    # Descend through wrapper folders until a package folder is reached.
    chain: list[str] = []
    current = path
    while not _looks_like_package_dir(current):
        children = [child for child in sorted(current.iterdir()) if child.is_dir()]
        if len(children) != 1:
            break
        current = children[0]
        chain.append(current.name)

    if _looks_like_package_dir(current):
        has_exefs = (current / EXEFS).is_dir()
        title_id, version = _metadata_from_names(chain)
        tail = chain[-1] if chain else ""
        return [
            PackageSource(
                root=current,
                kind=_kind_from_names(chain, has_exefs=has_exefs),
                title_id=title_id,
                version=version,
                origin="/".join([path.name, *chain]),
                ordinal=int(tail) if tail.isdigit() and len(tail) <= 4 else 0,
            )
        ]

    # A folder that holds several dump folders - the everyday
    # ".../Game Name/{BASE GAME, Update 2.1.11, DLC}" layout - contributes one
    # package per child.  This is the only way a single-folder picker can reach
    # a base+update+DLC set.  Symlinked children are skipped so a link cycle
    # cannot recurse, and the depth cap bounds pathological nesting.
    if depth < 3:
        collected: list[PackageSource] = []
        for child in sorted(
            item
            for item in path.iterdir()
            if item.is_dir()
            and not item.is_symlink()
            and item.name not in RESERVED_ARTIFACT_NAMES
        ):
            try:
                collected.extend(_collect_from_dir(child, depth=depth + 1))
            except NupError:
                continue
        if collected:
            return collected

    raise NupError(
        f"{path}: no exefs/ or romfs/ found, and no wrapped, merged, or "
        f"multi-dump package layout was recognised"
    )


def collect_packages(
    paths: Sequence[str | os.PathLike[str]],
    *,
    kind: str | int | None = None,
    version: int | None = None,
    title_id: int | None = None,
    replace: bool = True,
) -> list[PackageSource]:
    """Turn one or more folders into :class:`PackageSource` records.

    Recognised on-disk shapes, tried in order:

    1. **Package folder** - contains ``exefs`` and/or ``romfs`` (the shape dump
       tools emit and :func:`extract_nup` writes).
    2. **Merged tree** - ``exefs``/``romfs`` at the root plus
       ``updates/<decimal-version>/`` and ``dlc/<title-id>/<index>/``.
    3. **Wrapped folder** - one chain of single-child folders leading to a
       package folder.  A 16-digit hex name is an add-on title ID, a decimal
       name is the NCM version, and ``base`` / ``update`` / ``dlc`` keywords
       select the kind.

    ``kind``, ``version`` and ``title_id`` override the inference.

    An input may also be an existing ``.nup`` file, whose packages are folded
    into the output.  That is the re-bundle path: pass an existing container
    plus a newer update folder and the old update package is replaced.  Set
    *replace* to ``False`` to keep every version instead.
    """
    sources: list[PackageSource] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise NupError(f"{path} does not exist")
        if path.is_file():
            sources.extend(_collect_from_container(path))
            continue
        sources.extend(_collect_from_dir(path))

    if not sources:
        raise NupError(
            "no package folders found; expected a folder containing exefs/ "
            "and/or romfs"
        )

    # Explicit overrides describe the folders the caller supplied; packages
    # inherited from an existing container keep their stored metadata.
    folder_sources = [source for source in sources if not source.from_container]
    overridden_versions: set[int] = set()
    if kind is not None:
        resolved = KIND_IDS[kind] if isinstance(kind, str) else int(kind)
        for source in folder_sources:
            source.kind = resolved
    if version is not None:
        for source in folder_sources:
            source.version = int(version)
            # An explicit version wins over a name-derived display version, so
            # `--version 99` does not still report "2.1.11" from the folder.
            overridden_versions.add(id(source))
    if title_id is not None:
        for source in folder_sources:
            source.title_id = int(title_id)

    for source in sources:
        if source.from_container:
            continue
        npdm_path = _find_npdm(source.root)
        if npdm_path is not None:
            npdm_bytes = npdm_path.read_bytes()
            source.meta.setdefault(TLV_NPDM, npdm_bytes)
            if not source.title_id:
                source.title_id = npdm_program_id(npdm_bytes)
            if TLV_BUILD_ID not in source.meta:
                build_id = _build_id_from_main(source.root)
                if build_id:
                    source.meta[TLV_BUILD_ID] = build_id
        for tlv, payload in _read_meta_dir(source.root).items():
            if tlv == TLV_DISPLAY_VERSION and id(source) in overridden_versions:
                # An explicit --version overrides a stored display version too,
                # otherwise inspect would still report the old name-derived one.
                continue
            source.meta.setdefault(tlv, payload)
        # A wrapper name may carry a marketing version (``... Update 2.1.11``)
        # that differs from the exported NCM version (``458752``).  SPEC.md
        # 3.1.1 keeps the NCM version authoritative and puts the display
        # version in TLV 0x0004, so record both.
        if (
            source.origin
            and source.version
            and id(source) not in overridden_versions
        ):
            dotted = _first_dotted([source.origin])
            if dotted:
                encoded = _parse_dotted_version(dotted)
                if encoded is not None and encoded != source.version:
                    source.meta.setdefault(
                        TLV_DISPLAY_VERSION, dotted.encode("utf-8")
                    )
        if not source.version:
            version_file = source.root / META_DIR / "version.txt"
            if version_file.is_file():
                try:
                    source.version = int(
                        version_file.read_text(encoding="utf-8").strip()
                    )
                except (OSError, ValueError):
                    pass
    # An update package belongs to the base application's title ID, and dumps
    # frequently ship an update with no NPDM of its own (or an NPDM whose
    # program ID is unreadable), so inherit the base's ID instead of demanding
    # a value the container already implies.
    base_title_id = next(
        (
            source.title_id
            for source in sources
            if source.kind == KIND_BASE and source.title_id
        ),
        0,
    )
    for source in sources:
        if source.kind == KIND_UPDATE and not source.title_id and base_title_id:
            source.title_id = base_title_id

    for source in sources:
        if source.title_id:
            continue
        if source.from_container:
            raise NupError(
                f"{source.origin}: the source container has no title ID for this "
                f"package"
            )
        raise NupError(
            f"{source.root}: cannot determine a title ID - the folder name is not "
            f"a title ID, exefs/main.npdm does not expose one, and no base package "
            f"was supplied to inherit from. Pass --title-id explicitly."
        )

    if replace:
        sources = _resolve_packages(sources)
    _order_sources(sources)
    return sources


def _collect_from_container(path: Path) -> list[PackageSource]:
    """Packages contributed by an existing ``.nup`` input (merge / re-bundle)."""
    if not is_nup(path):
        with path.open("rb") as handle:
            magic = handle.read(4)
        raise NupError(
            f"{path} is neither a folder nor a NUP container (magic {magic!r})"
        )
    container = open_nup(path)
    return [
        PackageSource(
            root=None,
            kind=package.kind,
            title_id=package.title_id,
            version=package.version,
            meta=dict(package.meta),
            origin=f"{path.name}#{index}",
            container=container,
            package_index=index,
        )
        for index, package in enumerate(container.packages)
    ]


def _resolve_packages(sources: Sequence[PackageSource]) -> list[PackageSource]:
    """Collapse superseded packages so the result is a valid v1 container.

    NUP v1 allows one base package per container (SPEC.md 3.1.2) and one update
    per version.  Add-ons are keyed by ``(title_id, ordinal)`` because a single
    title ID may ship several add-on packages; a collision between two folder
    sources is re-indexed, and only a container-vs-folder pair is treated as a
    replacement.  Later inputs win, which is what makes
    ``old.nup + newer-update-folder`` a re-bundle.
    """
    bases: list[PackageSource] = []
    updates: dict[int, PackageSource] = {}
    addons: dict[tuple[int, int], PackageSource] = {}
    others: list[PackageSource] = []
    for source in sources:
        if source.kind == KIND_BASE:
            bases.append(source)
        elif source.kind == KIND_UPDATE:
            current = updates.get(source.title_id)
            if current is None or source.version >= current.version:
                updates[source.title_id] = source
        elif source.kind in (KIND_ADDON, KIND_ADDON_UPDATE):
            # Key on the add-on content index as well as the title ID: one title
            # ID may legitimately ship several add-on packages
            # (``dlc/<title-id>/<index>/``), and collapsing them lost content.
            key = (source.title_id, source.ordinal)
            previous = addons.get(key)
            if previous is None:
                addons[key] = source
            elif previous.from_container != source.from_container:
                # An add-on inherited from an existing container being replaced
                # by a folder (or the reverse) is the re-bundle path.
                addons[key] = source
            else:
                # Two independent sources for the same title and index - think
                # "DLC Vol 1/<tid>/" and "DLC Vol 2/<tid>/".  Give this one a
                # free index and keep both; overwriting dropped a user's files
                # without a word.
                ordinal = source.ordinal
                while (source.title_id, ordinal) in addons:
                    ordinal += 1
                source.ordinal = ordinal
                addons[(source.title_id, ordinal)] = source
        else:
            others.append(source)
    if len(bases) > 1:
        raise NupError(
            "more than one base package was supplied ("
            + ", ".join(source.origin or str(source.root) for source in bases)
            + "); a NUP v1 container holds at most one base. Use --kind "
            "update / --kind addon to classify a folder explicitly."
        )
    return [*bases, *updates.values(), *addons.values(), *others]


def _find_npdm(root: Path) -> Path | None:
    for candidate in (root / EXEFS / "main.npdm", root / "main.npdm"):
        if candidate.is_file():
            return candidate
    return None


def _build_id_from_main(root: Path) -> bytes | None:
    """TLV 0x0007 payload: bytes 0x40..0x60 of the package's ``main``."""
    main = root / EXEFS / "main"
    if not main.is_file() or main.stat().st_size < 0x60:
        return None
    with main.open("rb") as handle:
        handle.seek(0x40)
        data = handle.read(0x20)
    if len(data) != 0x20 or not any(data):
        return None
    return data


def _order_sources(sources: list[PackageSource]) -> None:
    """Base first, then updates by version, then add-ons by title ID."""
    sources.sort(
        key=lambda source: (
            source.kind,
            source.version if source.kind == KIND_UPDATE else 0,
            source.title_id,
            source.origin or (str(source.root) if source.root else ""),
        )
    )


# ---------------------------------------------------------------------------
# writer: planning and emission
# ---------------------------------------------------------------------------


def _plan_package(source: PackageSource) -> _PackagePlan:
    root = _DirPlan(name="")
    if source.container is not None:
        package = source.container.packages[source.package_index]
        for parts, record in source.container.package_files(package):
            _insert_file(
                root,
                parts,
                _FilePlan(
                    parts=parts,
                    record=record,
                    container=source.container,
                    size=record.size,
                    sha256=record.sha256,
                ),
            )
    else:
        if source.root is None:  # pragma: no cover - defensive
            raise NupError("package source has neither a folder nor a container")
        for parts, path in _iter_source_files(source.root, source.exclude):
            _insert_file(root, parts, _FilePlan(parts=parts, source=path))
    return _PackagePlan(source=source, root=root)


def _insert_file(root: _DirPlan, parts: Sequence[str], entry: _FilePlan) -> None:
    """Insert one file into the plan, refusing what the reader would reject.

    Packing must not produce a container that this very module then refuses to
    open, so every name is checked against the reader's rules here as well.
    """
    if len(parts) - 1 > _MAX_TREE_DEPTH:
        raise NupError(
            f"{'/'.join(parts)!r} is nested {len(parts) - 1} levels deep; a NUP "
            f"tree is limited to {_MAX_TREE_DEPTH} directory levels"
        )
    for part in parts:
        if _safe_parts(part, where="packing") != (part,):
            raise NupError(
                f"file name {'/'.join(parts)!r} cannot be represented in a NUP: "
                f"a node name may not contain '/', '\\' or ':'"
            )
    directory = root
    for part in parts[:-1]:
        directory = directory.dirs.setdefault(part, _DirPlan(name=part))
    if parts[-1] in directory.files:
        raise NupError(f"duplicate file name {'/'.join(parts)} in a package tree")
    directory.files[parts[-1]] = entry


def _iter_plan_files(node: _DirPlan) -> Iterator[_FilePlan]:
    for name in sorted(node.files):
        yield node.files[name]
    for name in sorted(node.dirs):
        yield from _iter_plan_files(node.dirs[name])


def _count_dirs(node: _DirPlan) -> int:
    return 1 + sum(_count_dirs(child) for child in node.dirs.values())


def _assign_indices(node: _DirPlan, counter: int) -> int:
    node.index = counter
    counter += 1
    for name in sorted(node.dirs):
        counter = _assign_indices(node.dirs[name], counter)
    return counter


def _intern_dir_names(node: _DirPlan, pool: "_StringPool") -> None:
    pool.add(node.name)
    for name in sorted(node.dirs):
        _intern_dir_names(node.dirs[name], pool)


def _intern_file_names(node: _DirPlan, pool: "_StringPool") -> None:
    for name in sorted(node.files):
        pool.add(name)
    for name in sorted(node.dirs):
        _intern_file_names(node.dirs[name], pool)


class _StringPool:
    """Interns UTF-8 names so each distinct name is stored once."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._offsets: dict[bytes, int] = {}

    def add(self, text: str) -> tuple[int, int]:
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            # Linux file names are bytes; a dump folder may hold one that is not
            # valid UTF-8.  NUP stores names as UTF-8, so refuse it clearly
            # instead of escaping a UnicodeEncodeError traceback.
            raise NupError(
                f"file name {text!r} is not valid UTF-8 ({exc.reason}); NUP "
                f"stores names as UTF-8 - rename the file before packing"
            ) from exc
        offset = self._offsets.get(encoded)
        if offset is None:
            offset = len(self._data)
            self._offsets[encoded] = offset
            self._data += encoded
        return offset, len(encoded)

    def build(self) -> bytes:
        return bytes(self._data)


def _emit_dir_records(node: _DirPlan, out: bytearray, pool: _StringPool) -> None:
    offset, length = pool.add(node.name)
    out.extend(
        struct.pack("<IIIIQ", offset, length, node.first_child, node.child_count, 0)
    )
    for name in sorted(node.dirs):
        _emit_dir_records(node.dirs[name], out, pool)


def _emit_file_records(node: _DirPlan, out: bytearray, pool: _StringPool) -> None:
    for name in sorted(node.files):
        entry = node.files[name]
        offset, length = pool.add(name)
        out.extend(
            struct.pack(
                "<IIQQQ",
                offset,
                length,
                entry.size,
                entry.extent_offset,
                entry.extent_size,
            )
        )
        out.extend(entry.sha256)
        out.extend(struct.pack("<BBH", entry.codec, 0, 0))
        out.extend(b"\x00" * 12)
    for name in sorted(node.dirs):
        _emit_file_records(node.dirs[name], out, pool)


def _emit_children(node: _DirPlan, out: list[int], dir_total: int) -> None:
    """Assign contiguous child slots for *node*, sorted bytewise by name."""
    node.first_child = len(out)
    ordered = sorted(
        set(node.dirs) | set(node.files), key=lambda name: name.encode("utf-8")
    )
    for name in ordered:
        if name in node.dirs:
            out.append(node.dirs[name].index)
        else:
            out.append(dir_total + node.files[name].index)
    node.child_count = len(ordered)
    for name in sorted(node.dirs):
        _emit_children(node.dirs[name], out, dir_total)


def _choose_codec(
    node: _FilePlan, *, compression: str, memory_cap: int
) -> tuple[int, bytes | None]:
    if compression == "raw" or node.size == 0 or node.size > memory_cap:
        return CODEC_RAW, None
    if _zstd is None:
        return CODEC_RAW, None
    data = node.source.read_bytes()
    compressed = _zstd.ZstdCompressor(level=3).compress(data)
    if len(compressed) >= node.size:
        return CODEC_RAW, None
    return CODEC_ZSTD, compressed


def _serialize_meta(
    source: PackageSource, *, source_name: str | None
) -> bytes:
    meta = dict(source.meta)
    if source_name and TLV_SOURCE_NAME not in meta:
        meta[TLV_SOURCE_NAME] = source_name.encode("utf-8")
    out = bytearray()
    for tlv in sorted(meta):
        payload = meta[tlv]
        if not payload:
            continue
        out += struct.pack("<HHI", tlv, 0, len(payload))
        out += payload
    return bytes(out)


def _container_uuid(plans: Sequence[_PackagePlan]) -> bytes:
    digest = hashlib.sha256()
    for plan in plans:
        digest.update(
            struct.pack(
                "<QIB", plan.source.title_id, plan.source.version, plan.source.kind
            )
        )
    return digest.digest()[:16]


def _write_plan_node(handle, node: _FilePlan) -> None:
    if node.payload is not None:
        handle.write(node.payload)
        return
    if node.record is not None:
        _copy_extent(handle, node)
        return
    remaining = node.size
    with node.source.open("rb") as source:
        while remaining:
            block = source.read(min(_CHUNK, remaining))
            if not block:
                raise NupError(f"{node.source} shrank while packing")
            handle.write(block)
            remaining -= len(block)


def _copy_extent(handle, node: _FilePlan) -> None:
    """Copy a stored extent from a source container, verifying raw content."""
    if node.container is None or node.record is None:  # pragma: no cover - defensive
        raise NupError("extent copy requested without a source container")
    remaining = node.record.extent_size
    digest = hashlib.sha256()
    with node.container.path.open("rb") as source:
        source.seek(node.record.extent_offset)
        while remaining:
            block = source.read(min(_CHUNK, remaining))
            if not block:
                raise NupError(
                    f"{'/'.join(node.parts)}: source extent in "
                    f"{node.container.path.name} is truncated"
                )
            if node.codec == CODEC_RAW:
                digest.update(block)
            handle.write(block)
            remaining -= len(block)
    if node.codec == CODEC_RAW and digest.digest() != node.sha256:
        raise NupError(
            f"{'/'.join(node.parts)}: SHA-256 mismatch while copying from "
            f"{node.container.path.name}"
        )


def _pad_handle(handle, offset: int) -> None:
    position = handle.tell()
    if position < offset:
        handle.write(b"\x00" * (offset - position))


def build_nup(
    sources: Sequence[PackageSource],
    out_path: str | os.PathLike[str],
    *,
    compression: str = "raw",
    memory_cap: int = 256 << 20,
    source_name: str | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Pack *sources* into a NUP file and return its path.

    ``compression`` is ``"raw"`` (default), ``"zstd"``, or ``"auto"``.  Files
    larger than *memory_cap* are stored raw even when zstd is requested, since
    the writer would otherwise buffer them; the codec is a per-file field, so
    the result stays conformant.
    """
    if not sources:
        raise NupError("no packages to pack")
    if compression not in ("raw", "zstd", "auto"):
        raise NupError(f"unknown compression {compression!r}")
    if compression == "zstd":
        _require_zstd()

    # Producer conformance (SPEC.md 3.1.2): a v1 container holds at most one
    # base package.  collect_packages(replace=True) already collapses them, so
    # this only fires for hand-built source lists.
    bases = [source for source in sources if source.kind == KIND_BASE]
    if len(bases) > 1:
        raise NupError(
            "a NUP v1 container holds at most one base package, but "
            f"{len(bases)} were supplied"
        )

    # Two packages that would occupy the same slot are a caller error.  Silently
    # collapsing them used to discard a user-supplied package without a word.
    slots: dict[tuple[int, int, int], PackageSource] = {}
    for source in sources:
        slot = (
            source.kind,
            source.title_id,
            source.version if source.kind == KIND_UPDATE else source.ordinal,
        )
        previous = slots.get(slot)
        if previous is not None:
            raise NupError(
                f"{source.origin or source.root} and "
                f"{previous.origin or previous.root} would both be "
                f"{KIND_NAMES.get(source.kind, source.kind)} packages for title "
                f"0x{source.title_id:016x} at the same slot; give them distinct "
                f"versions or add-on indices"
            )
        slots[slot] = source

    plans = [_plan_package(source) for source in sources]
    ordered: list[_FilePlan] = []
    for plan in plans:
        ordered.extend(_iter_plan_files(plan.root))
    if not ordered:
        raise NupError("the given folders contain no files to pack")

    pending = [node for node in ordered if node.record is None]
    total = len(pending)
    for position, node in enumerate(pending, start=1):
        node.sha256, node.size = _hash_file(node.source)
        if progress and (position % 32 == 0 or position == total):
            progress(position, total, "Hashing source files")

    # Codec choice and cross-package dedup keyed by content identity.  Files
    # inherited from an existing container keep their stored extent and are
    # copied verbatim instead of being recompressed.
    canonical: dict[tuple[bytes, int, int], _FilePlan] = {}
    for node in ordered:
        if node.record is not None:
            node.codec = node.record.codec
            node.payload = None
            if node.codec == CODEC_ZSTD:
                _require_zstd()
            node.extent_size = node.record.extent_size
        else:
            node.codec, node.payload = _choose_codec(
                node, compression=compression, memory_cap=memory_cap
            )
            node.extent_size = (
                len(node.payload) if node.payload is not None else node.size
            )
        key = (node.sha256, node.size, node.codec)
        existing = canonical.get(key)
        if existing is not None and existing.extent_size != node.extent_size:
            existing = None
        if existing is None:
            canonical[key] = node
        else:
            node.canonical = existing
            node.extent_size = existing.extent_size

    # Directory and file indices.
    counter = 0
    for plan in plans:
        counter = _assign_indices(plan.root, counter)
    dir_count = counter
    for index, node in enumerate(ordered):
        node.index = index

    # Intern every name in emission order, so the pool is complete before the
    # TOC size (and therefore blob_offset) can be computed.
    pool = _StringPool()
    for plan in plans:
        _intern_dir_names(plan.root, pool)
    for plan in plans:
        _intern_file_names(plan.root, pool)
    pool_bytes = pool.build()

    file_count = len(ordered)
    package_count = len(plans)
    dirs_offset = TOC_PREFIX_SIZE + PACKAGE_RECORD_SIZE * package_count
    files_offset = dirs_offset + DIR_RECORD_SIZE * dir_count
    children_offset = files_offset + FILE_RECORD_SIZE * file_count
    child_slot_count = sum(
        len(node.dirs) + len(node.files)
        for plan in plans
        for node in _walk_plan_dirs(plan.root)
    )
    toc_size_unpadded = children_offset + 4 * child_slot_count

    toc_size = _align(toc_size_unpadded + len(pool_bytes))
    blob_offset = _align(SUPERBLOCK_SIZE + toc_size)

    # Extents, in discovery order, at absolute 16-byte-aligned offsets.
    cursor = blob_offset
    for node in ordered:
        if node.canonical is not None or node.extent_size == 0:
            continue
        cursor = _align(cursor)
        node.extent_offset = cursor
        cursor += node.extent_size
    for node in ordered:
        if node.canonical is not None:
            node.extent_offset = node.canonical.extent_offset
        elif node.extent_size == 0:
            node.extent_offset = 0
    blob_end = cursor

    # Child slot arrays.
    children: list[int] = []
    for plan in plans:
        _emit_children(plan.root, children, dir_count)
    if len(children) != (toc_size_unpadded - children_offset) // 4:
        raise NupError(
            f"internal layout error: {len(children)} child slots vs "
            f"{(toc_size_unpadded - children_offset) // 4} reserved"
        )

    # Record arrays.
    dirs_blob = bytearray()
    files_blob = bytearray()
    for plan in plans:
        _emit_dir_records(plan.root, dirs_blob, pool)
    for plan in plans:
        _emit_file_records(plan.root, files_blob, pool)
    if len(dirs_blob) != DIR_RECORD_SIZE * dir_count:
        raise NupError("internal layout error: directory record count mismatch")
    if len(files_blob) != FILE_RECORD_SIZE * file_count:
        raise NupError("internal layout error: file record count mismatch")

    # Package meta blocks follow the blob, in package order.
    meta_blobs = [
        _serialize_meta(plan.source, source_name=source_name) for plan in plans
    ]
    meta_offsets: list[int] = []
    cursor = blob_end
    for blob in meta_blobs:
        meta_offsets.append(cursor if blob else 0)
        cursor += len(blob)
    file_size = cursor

    standalone = not any(plan.source.kind == KIND_BASE for plan in plans)

    toc = bytearray(toc_size)
    struct.pack_into("<II", toc, 0x00, TOC_MAGIC, package_count)
    struct.pack_into("<QQ", toc, 0x08, dir_count, file_count)
    struct.pack_into("<QQ", toc, 0x18, toc_size_unpadded, len(pool_bytes))
    struct.pack_into("<Q", toc, 0x28, 0)
    for index, plan in enumerate(plans):
        base = TOC_PREFIX_SIZE + index * PACKAGE_RECORD_SIZE
        source = plan.source
        flags = 0
        if standalone and source.kind == KIND_UPDATE:
            flags |= FLAG_STANDALONE_UPDATE
        exefs_dir = (
            plan.root.dirs[EXEFS].index if EXEFS in plan.root.dirs else NO_DIR
        )
        romfs_dir = (
            plan.root.dirs[ROMFS].index if ROMFS in plan.root.dirs else NO_DIR
        )
        struct.pack_into(
            "<QIIII",
            toc,
            base,
            source.title_id,
            plan.root.index,
            exefs_dir,
            romfs_dir,
            source.version,
        )
        toc[base + 0x18] = source.kind
        toc[base + 0x19] = flags
        struct.pack_into(
            "<QI", toc, base + 0x20, meta_offsets[index], len(meta_blobs[index])
        )
    toc[dirs_offset : dirs_offset + len(dirs_blob)] = dirs_blob
    toc[files_offset : files_offset + len(files_blob)] = files_blob
    toc[children_offset : children_offset + 4 * len(children)] = struct.pack(
        f"<{len(children)}I", *children
    )
    toc[toc_size_unpadded : toc_size_unpadded + len(pool_bytes)] = pool_bytes

    superblock = bytearray(SUPERBLOCK_SIZE)
    superblock[0:4] = NUP_MAGIC
    struct.pack_into("<HH", superblock, 0x04, SUPERBLOCK_SIZE, 1)
    struct.pack_into("<QQ", superblock, 0x08, SUPERBLOCK_SIZE, toc_size)
    superblock[0x18:0x38] = _sha256_bytes(bytes(toc))
    struct.pack_into("<QQ", superblock, 0x38, blob_offset, file_size)
    struct.pack_into("<II", superblock, 0x48, package_count, 0)
    superblock[0x50:0x60] = _container_uuid(plans)

    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    try:
        with partial.open("wb") as handle:
            handle.write(superblock)
            handle.write(toc)
            _pad_handle(handle, blob_offset)
            for node in ordered:
                if node.canonical is not None or node.extent_size == 0:
                    continue
                _pad_handle(handle, node.extent_offset)
                if handle.tell() != node.extent_offset:
                    raise NupError(
                        f"internal layout error: {node.parts} expected offset "
                        f"0x{node.extent_offset:x}, handle at 0x{handle.tell():x}"
                    )
                _write_plan_node(handle, node)
            _pad_handle(handle, blob_end)
            for blob in meta_blobs:
                handle.write(blob)
            handle.truncate(file_size)
            if progress:
                progress(total, total, "Writing container")
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return destination


def _walk_plan_dirs(node: _DirPlan) -> Iterator[_DirPlan]:
    yield node
    for name in sorted(node.dirs):
        yield from _walk_plan_dirs(node.dirs[name])


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


def extract_nup(
    container: NupContainer,
    out_dir: str | os.PathLike[str],
    *,
    layout: str = "tree",
    selected: Iterable[int] | None = None,
    verify: bool = True,
    include_meta: bool = True,
    progress: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
    """Write a NUP back to disk as a folder tree.

    ``layout="tree"`` produces the canonical, re-packable shape::

        <out>/exefs/...                          base package
        <out>/romfs/...
        <out>/meta/...                           base metadata, when present
        <out>/updates/<decimal-version>/...
        <out>/dlc/<title-id>/<index>/...

    ``layout="split"`` writes one folder per package instead.
    """
    if layout not in ("tree", "split"):
        raise NupError(f"unknown layout {layout!r}")
    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)

    chosen = (
        list(range(len(container.packages))) if selected is None else list(selected)
    )
    for index in chosen:
        if index < 0 or index >= len(container.packages):
            raise NupError(f"package index {index} is out of range")

    total = sum(len(container.package_files(container.packages[i])) for i in chosen) or 1
    done = 0
    written: list[Path] = []
    addon_ordinals: dict[int, int] = {}
    update_ordinals: dict[tuple[int, int], int] = {}

    for index in chosen:
        package = container.packages[index]
        if layout == "tree":
            root = _tree_root_for(destination, package, update_ordinals, addon_ordinals)
        else:
            root = destination / _split_dirname(package)
        root.mkdir(parents=True, exist_ok=True)

        for parts, node in container.package_files(package):
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                container.copy_to(node, handle, verify=verify)
            written.append(target)
            done += 1
            if progress:
                progress(done, total, f"Extracting {parts[-1]}")

        if include_meta:
            written.extend(_write_meta(root, package))

    return written


def _tree_root_for(
    destination: Path,
    package: NupPackage,
    update_ordinals: dict[tuple[int, int], int],
    addon_ordinals: dict[int, int],
) -> Path:
    if package.kind == KIND_BASE:
        return destination
    if package.kind == KIND_UPDATE:
        key = (package.title_id, package.version)
        ordinal = update_ordinals.get(key, 0)
        update_ordinals[key] = ordinal + 1
        name = str(package.version) if ordinal == 0 else f"{package.version}-{ordinal}"
        if package.standalone_update:
            name = f"{name}-standalone"
        return destination / UPDATES_DIR / name
    if package.kind in (KIND_ADDON, KIND_ADDON_UPDATE):
        ordinal = addon_ordinals.get(package.title_id, 0)
        addon_ordinals[package.title_id] = ordinal + 1
        return destination / DLC_DIR / package.title_id_hex / str(ordinal)
    label = KIND_NAMES.get(package.kind, f"kind{package.kind}")
    return destination / label / package.title_id_hex


def _split_dirname(package: NupPackage) -> str:
    stem = f"{package.kind_name}-{package.title_id_hex}"
    if package.kind == KIND_UPDATE:
        stem += f"-{package.version}"
    return stem


def _write_meta(root: Path, package: NupPackage) -> list[Path]:
    """Persist package metadata as a ``meta/`` folder so packing is lossless."""
    if not package.meta and not package.version:
        return []
    meta_dir = root / META_DIR
    meta_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def emit(name: str, payload: bytes) -> None:
        target = meta_dir / name
        target.write_bytes(payload)
        written.append(target)

    if package.version:
        emit("version.txt", str(package.version).encode("ascii"))

    text_map = {
        TLV_TITLE_NAME: "title.txt",
        TLV_PUBLISHER: "publisher.txt",
        TLV_DISPLAY_VERSION: "display-version.txt",
        TLV_SOURCE_NAME: "source.txt",
    }
    for tlv, filename in text_map.items():
        payload = package.meta.get(tlv)
        if payload is not None:
            emit(filename, payload)
    icon = package.meta.get(TLV_ICON)
    if icon:
        emit("icon.png" if icon.startswith(b"\x89PNG") else "icon.jpg", icon)
    if TLV_NACP in package.meta:
        emit("nacp.bin", package.meta[TLV_NACP])
    if TLV_BUILD_ID in package.meta:
        emit("build-id.bin", package.meta[TLV_BUILD_ID])
    handled = {*text_map, TLV_ICON, TLV_NACP, TLV_BUILD_ID, TLV_NPDM}
    for tlv in sorted(package.meta):
        if tlv in handled:
            continue
        emit(f"tlv-0x{tlv:04x}.bin", package.meta[tlv])
    return written
