"""Tests for the NUP (NX Package Format v1) container.

Containers are built from the module's own writers and then hand-patched byte
for byte, the same way ``test_formats.py`` assembles adversarial containers
from the real builders instead of static fixtures.
"""

import hashlib
import os
import struct
from pathlib import Path

import pytest

from sak_modern.core import nup

TID = 0x0100000000010000
ADDON_TID = 0x0100000000011000

#: NCM version 458752 == 0x00070000 -> "7.0.0".
UPDATE_VERSION = 458752
NEW_UPDATE_VERSION = 200


# ---------------------------------------------------------------------------
# synthetic inputs
# ---------------------------------------------------------------------------


def _npdm(program_id: int, *, magic: bytes = b"META", at: int = 0x00) -> bytes:
    """Minimal META-shaped NPDM carrying *program_id* at ``ACI0 + at``."""
    blob = bytearray(0x200)
    blob[0:4] = magic
    struct.pack_into("<I", blob, 0x70, 0x80)  # ACI0 offset
    struct.pack_into("<Q", blob, 0x80 + at, program_id)
    return bytes(blob)


def _write(root: Path, files: dict[str, bytes]) -> Path:
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return root


def _game_files() -> dict[str, bytes]:
    """One base package with nested RomFS, dedup material and artefacts."""
    return {
        "exefs/main": bytes(range(256)) * 2,
        "exefs/main.npdm": _npdm(TID),
        "exefs/.listing": b"main\nmain.npdm\n",
        "romfs/hello.txt": b"hello romfs\n",
        "romfs/empty.bin": b"",
        "romfs/data/a.bin": b"identical payload\n",
        "romfs/nested/deep/b.bin": b"identical payload\n",
        "romfs/.listing": b"hello.txt\n",
        "meta/title.txt": b"Synthetic Game",
    }


def _game_tree(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    payloads = _game_files()
    return _write(tmp_path / "base", payloads), payloads


def _merged_tree(tmp_path: Path) -> Path:
    """Base + one update + one add-on, in the merged dump layout."""
    return _write(
        tmp_path / "merged-game",
        {
            "exefs/main": b"base main" * 16,
            "exefs/main.npdm": _npdm(TID),
            "romfs/base.txt": b"base romfs\n",
            "updates/458752/exefs/main": b"update main" * 16,
            # An update NPDM still carries the base program ID; the update's
            # own title ID is derived from it by the folder discovery code.
            "updates/458752/exefs/main.npdm": _npdm(TID),
            "updates/458752/romfs/patch.txt": b"update romfs\n",
            f"dlc/{ADDON_TID:016x}/0/exefs/main": b"addon main" * 16,
            f"dlc/{ADDON_TID:016x}/0/exefs/main.npdm": _npdm(TID),
            f"dlc/{ADDON_TID:016x}/0/romfs/dlc.txt": b"addon romfs\n",
        },
    )


def _tiny_nup(tmp_path: Path) -> Path:
    root = _write(
        tmp_path / "tiny-base",
        {
            "exefs/main": b"tiny-main" * 32,
            "exefs/main.npdm": _npdm(TID),
            "romfs/a.txt": b"alpha",
        },
    )
    return nup.build_nup(nup.collect_packages([root]), tmp_path / "tiny.nup")


def _files_by_path(container: nup.NupContainer, package: nup.NupPackage) -> dict[str, nup.NupFile]:
    return {"/".join(parts): node for parts, node in container.package_files(package)}


# ---------------------------------------------------------------------------
# byte-level patching helpers
# ---------------------------------------------------------------------------


def _mutate_copy(source: Path, destination: Path, edit) -> Path:
    data = bytearray(source.read_bytes())
    edit(data)
    destination.write_bytes(data)
    return destination


def _dir_counts(toc: bytes) -> tuple[int, int]:
    return struct.unpack_from("<QQ", toc, 0x08)


def _toc_offsets(toc: bytes) -> tuple[int, int]:
    """Offsets of the dir and file record arrays inside the TOC."""
    _magic, package_count = struct.unpack_from("<II", toc, 0x00)
    dir_count, file_count = _dir_counts(toc)
    dirs_offset = nup.TOC_PREFIX_SIZE + nup.PACKAGE_RECORD_SIZE * package_count
    files_offset = dirs_offset + nup.DIR_RECORD_SIZE * dir_count
    return dirs_offset, files_offset


def _edit_toc(data: bytearray, edit) -> None:
    """Patch the TOC and recompute the superblock digest over the new bytes."""
    toc_offset, toc_size = struct.unpack_from("<QQ", data, 0x08)
    toc = bytearray(data[toc_offset : toc_offset + toc_size])
    edit(toc)
    data[toc_offset : toc_offset + toc_size] = toc
    data[0x18:0x38] = hashlib.sha256(bytes(toc)).digest()


def _file_record_offset(toc: bytes, index: int) -> int:
    _dirs_offset, files_offset = _toc_offsets(toc)
    return files_offset + index * nup.FILE_RECORD_SIZE


# ---------------------------------------------------------------------------
# 1. reader validation
# ---------------------------------------------------------------------------


def test_reader_rejects_not_a_nup(tmp_path: Path):
    good = _tiny_nup(tmp_path)

    def break_magic(data: bytearray) -> None:
        data[0:4] = b"NUX\x01"

    bad = _mutate_copy(good, tmp_path / "wrong-magic.nup", break_magic)
    with pytest.raises(nup.NupError, match="not a NUP container"):
        nup.open_nup(bad)
    # The untouched container still opens, so the patch is what was rejected.
    assert nup.open_nup(good).package_count == 1


def test_reader_rejects_short_file(tmp_path: Path):
    short = tmp_path / "short.nup"
    short.write_bytes(nup.NUP_MAGIC + b"\x00" * 60)  # 0x40 bytes
    with pytest.raises(nup.NupError, match="needs 128"):
        nup.open_nup(short)


def test_reader_rejects_tampered_toc(tmp_path: Path):
    good = _tiny_nup(tmp_path)

    def flip(data: bytearray) -> None:
        toc_offset, _toc_size = struct.unpack_from("<QQ", data, 0x08)
        data[toc_offset + 0x60] ^= 0xFF  # deliberately not re-signed

    bad = _mutate_copy(good, tmp_path / "tampered.nup", flip)
    with pytest.raises(nup.NupError, match="TOC SHA-256 mismatch"):
        nup.open_nup(bad)


def test_reader_rejects_wrong_declared_file_size(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    declared = good.stat().st_size + 0x10
    bad = _mutate_copy(
        good,
        tmp_path / "declared-size.nup",
        lambda data: struct.pack_into("<Q", data, 0x40, declared),
    )
    with pytest.raises(nup.NupError, match="declares file_size"):
        nup.open_nup(bad)


def test_reader_rejects_misaligned_blob_offset(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    (blob_offset,) = struct.unpack_from("<Q", good.read_bytes(), 0x38)
    bad = _mutate_copy(
        good,
        tmp_path / "misaligned.nup",
        lambda data: struct.pack_into("<Q", data, 0x38, blob_offset + 1),
    )
    with pytest.raises(nup.NupError, match="not 16-byte aligned"):
        nup.open_nup(bad)


def test_reader_rejects_blob_offset_overlapping_toc(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    (header_size,) = struct.unpack_from("<H", good.read_bytes(), 0x04)
    bad = _mutate_copy(
        good,
        tmp_path / "blob-overlap.nup",
        lambda data: struct.pack_into("<Q", data, 0x38, header_size),
    )
    with pytest.raises(nup.NupError, match="overlaps the TOC"):
        nup.open_nup(bad)


def test_reader_rejects_toc_offset_mismatch(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    bad = _mutate_copy(
        good,
        tmp_path / "toc-offset.nup",
        lambda data: struct.pack_into("<Q", data, 0x08, 0x90),
    )
    with pytest.raises(nup.NupError, match="must equal header_size"):
        nup.open_nup(bad)


def test_reader_rejects_zero_package_count(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    bad = _mutate_copy(
        good,
        tmp_path / "no-packages.nup",
        lambda data: struct.pack_into("<I", data, 0x48, 0),
    )
    with pytest.raises(nup.NupError, match="package_count must be at least 1"):
        nup.open_nup(bad)


def test_reader_rejects_superblock_flags(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    bad = _mutate_copy(
        good,
        tmp_path / "flags.nup",
        lambda data: struct.pack_into("<I", data, 0x4C, 1),
    )
    with pytest.raises(nup.NupError, match="unknown superblock flags"):
        nup.open_nup(bad)


def test_reader_rejects_unknown_file_codec(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    index = next(node.index for node in nup.open_nup(good).files if node.name == "a.txt")

    def set_codec(toc: bytearray) -> None:
        toc[_file_record_offset(toc, index) + 0x40] = 3

    bad = _mutate_copy(
        good, tmp_path / "codec.nup", lambda data: _edit_toc(data, set_codec)
    )
    with pytest.raises(nup.NupError, match="unknown codec 3"):
        nup.open_nup(bad)


def test_reader_rejects_extent_past_eof(tmp_path: Path):
    good = _tiny_nup(tmp_path)
    total = good.stat().st_size
    index = next(node.index for node in nup.open_nup(good).files if node.name == "a.txt")

    def grow_extent(toc: bytearray) -> None:
        struct.pack_into("<Q", toc, _file_record_offset(toc, index) + 0x18, total)

    bad = _mutate_copy(
        good, tmp_path / "extent-eof.nup", lambda data: _edit_toc(data, grow_extent)
    )
    with pytest.raises(nup.NupError, match="runs past the end of the file"):
        nup.open_nup(bad)


def test_reader_rejects_reserved_package_kind(tmp_path: Path):
    good = _tiny_nup(tmp_path)

    def set_kind(toc: bytearray) -> None:
        toc[nup.TOC_PREFIX_SIZE + 0x18] = 5

    bad = _mutate_copy(
        good, tmp_path / "kind.nup", lambda data: _edit_toc(data, set_kind)
    )
    with pytest.raises(nup.NupError, match="reserved kind 5"):
        nup.open_nup(bad)


# ---------------------------------------------------------------------------
# 2. happy-path round trip
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_tree_and_dedups(tmp_path: Path):
    root, payloads = _game_tree(tmp_path)
    path = nup.build_nup(nup.collect_packages([root]), tmp_path / "game.nup")

    assert nup.is_nup(path)
    container = nup.open_nup(path)
    assert container.package_count == 1
    package = container.primary
    assert package.kind == nup.KIND_BASE
    assert package.kind_name == "base"
    assert package.title_id == TID
    assert package.title_name == "Synthetic Game"
    assert package.exefs_dir != nup.NO_DIR
    assert package.romfs_dir != nup.NO_DIR

    files = _files_by_path(container, package)
    expected = {
        relative: payload
        for relative, payload in payloads.items()
        if not relative.startswith("meta/") and not relative.endswith(".listing")
    }
    assert set(files) == set(expected)
    assert "exefs/.listing" not in files
    assert "romfs/.listing" not in files
    assert "meta/title.txt" not in files

    first = files["romfs/data/a.bin"]
    second = files["romfs/nested/deep/b.bin"]
    assert first.sha256 == second.sha256
    assert first.extent_size > 0
    assert first.extent_offset == second.extent_offset
    assert first.extent_size == second.extent_size

    empty = files["romfs/empty.bin"]
    assert empty.size == 0
    assert empty.extent_size == 0
    assert empty.extent_offset == 0
    assert container.read(empty) == b""

    for relative, payload in expected.items():
        assert container.read(files[relative]) == payload
    assert nup.verify_nup(container) == []


# ---------------------------------------------------------------------------
# 3. extraction and determinism
# ---------------------------------------------------------------------------


def test_extract_writes_canonical_tree_and_repacks_identically(tmp_path: Path):
    root, payloads = _game_tree(tmp_path)
    first = nup.build_nup(nup.collect_packages([root]), tmp_path / "first.nup")
    container = nup.open_nup(first)

    out = tmp_path / "extracted"
    written = nup.extract_nup(container, out)

    assert (out / "exefs" / "main").read_bytes() == payloads["exefs/main"]
    assert (out / "exefs" / "main.npdm").read_bytes() == payloads["exefs/main.npdm"]
    assert (out / "romfs" / "hello.txt").read_bytes() == payloads["romfs/hello.txt"]
    assert (out / "romfs" / "empty.bin").read_bytes() == b""
    assert (out / "romfs" / "nested" / "deep" / "b.bin").read_bytes() == payloads[
        "romfs/nested/deep/b.bin"
    ]
    assert (out / "meta" / "title.txt").read_bytes() == payloads["meta/title.txt"]
    assert not list(out.rglob(".listing"))

    written_relative = {path.relative_to(out).as_posix() for path in written}
    assert {
        "exefs/main",
        "exefs/main.npdm",
        "romfs/nested/deep/b.bin",
        "romfs/empty.bin",
        "meta/title.txt",
    } <= written_relative

    second = nup.build_nup(nup.collect_packages([out]), tmp_path / "second.nup")
    assert second.read_bytes() == first.read_bytes()


# ---------------------------------------------------------------------------
# 4. merged base + update + add-on tree
# ---------------------------------------------------------------------------


def test_merged_tree_yields_three_packages(tmp_path: Path):
    root = _merged_tree(tmp_path)
    path = nup.build_nup(nup.collect_packages([root]), tmp_path / "merged.nup")
    container = nup.open_nup(path)

    assert container.package_count == 3
    assert [package.kind for package in container.packages] == [
        nup.KIND_BASE,
        nup.KIND_UPDATE,
        nup.KIND_ADDON,
    ]
    base, update, addon = container.packages
    assert base.title_id == TID
    # The update NPDM exposes the base program ID, and the add-on's title ID
    # comes from its ``dlc/<title-id>`` wrapper folder.
    assert update.title_id == TID
    assert addon.title_id == ADDON_TID
    assert update.version == UPDATE_VERSION

    base_paths = {"/".join(parts) for parts, _ in container.package_files(base)}
    assert base_paths == {"exefs/main", "exefs/main.npdm", "romfs/base.txt"}
    assert not any(path.startswith(("updates/", "dlc/")) for path in base_paths)
    update_paths = {"/".join(parts) for parts, _ in container.package_files(update)}
    assert update_paths == {"exefs/main", "exefs/main.npdm", "romfs/patch.txt"}
    addon_paths = {"/".join(parts) for parts, _ in container.package_files(addon)}
    assert addon_paths == {"exefs/main", "exefs/main.npdm", "romfs/dlc.txt"}

    summaries = container.summaries()
    assert [summary.kind for summary in summaries] == [
        nup.KIND_BASE,
        nup.KIND_UPDATE,
        nup.KIND_ADDON,
    ]
    assert summaries[1].version == UPDATE_VERSION
    assert summaries[1].display_version == "7.0.0"
    assert summaries[1].kind_name == "update"
    assert summaries[2].display_version == "0.0.0"
    assert summaries[0].file_count == 3
    assert nup.verify_nup(container) == []


# ---------------------------------------------------------------------------
# 5. standalone update flag
# ---------------------------------------------------------------------------


def test_standalone_update_flag(tmp_path: Path):
    update_only = _write(
        tmp_path / "updates-only",
        {
            "updates/458752/exefs/main": b"update main" * 16,
            "updates/458752/exefs/main.npdm": _npdm(TID),
            "updates/458752/romfs/patch.txt": b"update romfs\n",
        },
    )
    sources = nup.collect_packages([update_only])
    assert len(sources) == 1
    assert sources[0].kind == nup.KIND_UPDATE

    standalone_path = nup.build_nup(sources, tmp_path / "update-only.nup")
    standalone = nup.open_nup(standalone_path)
    assert standalone.package_count == 1
    assert standalone.packages[0].kind == nup.KIND_UPDATE
    assert standalone.packages[0].version == UPDATE_VERSION
    assert standalone.packages[0].standalone_update

    merged_root = _merged_tree(tmp_path)
    merged = nup.open_nup(
        nup.build_nup(nup.collect_packages([merged_root]), tmp_path / "merged.nup")
    )
    by_kind = {package.kind: package for package in merged.packages}
    assert not by_kind[nup.KIND_UPDATE].standalone_update
    assert not by_kind[nup.KIND_BASE].standalone_update


# ---------------------------------------------------------------------------
# 6. re-bundling an existing container with a newer update
# ---------------------------------------------------------------------------


def _rebundle_inputs(tmp_path: Path) -> tuple[Path, Path]:
    """Old container (base v42, update v100, addon v7) plus a newer update."""
    base_root = _write(
        tmp_path / "base",
        {
            "exefs/main": b"base main" * 8,
            "exefs/main.npdm": _npdm(TID),
            "romfs/base.txt": b"base\n",
        },
    )
    old_update = _write(
        tmp_path / "update-old",
        {
            "exefs/main": b"old update" * 8,
            "exefs/main.npdm": _npdm(TID),
            "romfs/old.txt": b"old\n",
        },
    )
    addon_root = _write(
        tmp_path / "addon",
        {
            "exefs/main": b"addon main" * 8,
            "exefs/main.npdm": _npdm(TID),
            "romfs/addon.txt": b"addon\n",
        },
    )
    sources = [
        nup.collect_packages([base_root], version=42)[0],
        nup.collect_packages([old_update], kind="update", version=100)[0],
        nup.collect_packages(
            [addon_root], kind="addon", version=7, title_id=ADDON_TID
        )[0],
    ]
    old_container = nup.build_nup(sources, tmp_path / "old.nup")
    newer_update = _write(
        tmp_path / "update-new",
        {
            "exefs/main": b"new update" * 8,
            "exefs/main.npdm": _npdm(TID),
            "romfs/new.txt": b"new\n",
        },
    )
    return old_container, newer_update


def test_rebundle_replaces_older_update(tmp_path: Path):
    old_container, newer_update = _rebundle_inputs(tmp_path)

    sources = nup.collect_packages(
        [old_container, newer_update], version=NEW_UPDATE_VERSION
    )
    assert [source.kind for source in sources] == [
        nup.KIND_BASE,
        nup.KIND_UPDATE,
        nup.KIND_ADDON,
    ]
    updates = [source for source in sources if source.kind == nup.KIND_UPDATE]
    assert len(updates) == 1
    assert updates[0].version == NEW_UPDATE_VERSION
    # An explicit version= override must not touch inherited packages.
    assert sources[0].version == 42
    assert sources[2].version == 7
    assert sources[2].title_id == ADDON_TID

    rebuilt = nup.open_nup(nup.build_nup(sources, tmp_path / "rebuilt.nup"))
    assert rebuilt.package_count == 3
    assert [package.kind for package in rebuilt.packages] == [
        nup.KIND_BASE,
        nup.KIND_UPDATE,
        nup.KIND_ADDON,
    ]
    base, update, addon = rebuilt.packages
    assert base.version == 42
    assert update.version == NEW_UPDATE_VERSION
    assert addon.version == 7
    assert addon.title_id == ADDON_TID
    update_paths = {"/".join(parts) for parts, _ in rebuilt.package_files(update)}
    assert update_paths == {"exefs/main", "exefs/main.npdm", "romfs/new.txt"}
    addon_paths = {"/".join(parts) for parts, _ in rebuilt.package_files(addon)}
    assert addon_paths == {"exefs/main", "exefs/main.npdm", "romfs/addon.txt"}
    assert nup.verify_nup(rebuilt) == []


def test_rebundle_keeps_both_updates_without_replace(tmp_path: Path):
    old_container, newer_update = _rebundle_inputs(tmp_path)

    sources = nup.collect_packages(
        [old_container, newer_update], version=NEW_UPDATE_VERSION, replace=False
    )
    versions = sorted(
        source.version for source in sources if source.kind == nup.KIND_UPDATE
    )
    assert versions == [100, NEW_UPDATE_VERSION]

    kept = nup.open_nup(nup.build_nup(sources, tmp_path / "kept.nup"))
    assert kept.package_count == 4
    assert [package.kind for package in kept.packages] == [
        nup.KIND_BASE,
        nup.KIND_UPDATE,
        nup.KIND_UPDATE,
        nup.KIND_ADDON,
    ]
    assert [package.version for package in kept.packages] == [
        42,
        100,
        NEW_UPDATE_VERSION,
        7,
    ]


# ---------------------------------------------------------------------------
# 7. split extraction layout
# ---------------------------------------------------------------------------


def test_split_layout_writes_one_folder_per_package(tmp_path: Path):
    root = _merged_tree(tmp_path)
    container = nup.open_nup(
        nup.build_nup(nup.collect_packages([root]), tmp_path / "merged.nup")
    )
    out = tmp_path / "split"
    nup.extract_nup(container, out, layout="split")

    expected = sorted(
        [
            f"base-{TID:016x}",
            f"update-{TID:016x}-{UPDATE_VERSION}",
            f"addon-{ADDON_TID:016x}",
        ]
    )
    assert sorted(entry.name for entry in out.iterdir()) == expected
    assert (out / f"base-{TID:016x}" / "exefs" / "main").is_file()
    assert not (out / f"base-{TID:016x}" / "updates").exists()
    assert (
        out / f"update-{TID:016x}-{UPDATE_VERSION}" / "romfs" / "patch.txt"
    ).is_file()
    assert (out / f"addon-{ADDON_TID:016x}" / "romfs" / "dlc.txt").is_file()


# ---------------------------------------------------------------------------
# 8. NPDM program ID extraction
# ---------------------------------------------------------------------------


def test_npdm_program_id_reads_both_switch_layouts():
    assert nup.npdm_program_id(_npdm(TID)) == TID
    assert nup.npdm_program_id(_npdm(TID, at=0x10)) == TID


def test_npdm_program_id_returns_zero_for_unusable_metadata():
    assert nup.npdm_program_id(None) == 0
    assert nup.npdm_program_id(_npdm(TID, magic=b"NACP")) == 0
    # Truncated right after the ACI0 offset, so both candidates overshoot.
    assert nup.npdm_program_id(_npdm(TID)[:0x84]) == 0
    # Right top byte, but the low 12 bits of a title ID must be zero.
    assert nup.npdm_program_id(_npdm(0x040074A01BF12FFF)) == 0


# ---------------------------------------------------------------------------
# 9. path traversal defence
# ---------------------------------------------------------------------------


def test_extract_never_follows_traversal_name(tmp_path: Path):
    root = _write(
        tmp_path / "victim",
        {
            "exefs/main": b"victim main" * 16,
            "exefs/main.npdm": _npdm(TID),
            "romfs/placeholder-directory/keep.txt": b"keep\n",
        },
    )
    clean = nup.build_nup(nup.collect_packages([root]), tmp_path / "clean.nup")
    evil = tmp_path / "evil.nup"
    evil.write_bytes(clean.read_bytes())

    # Rewrite the directory name inside the string pool and shrink name_len,
    # then re-sign the TOC so only the name itself is adversarial.
    data = bytearray(evil.read_bytes())
    toc_offset, toc_size = struct.unpack_from("<QQ", data, 0x08)
    toc = bytearray(data[toc_offset : toc_offset + toc_size])
    dirs_offset, _files_offset = _toc_offsets(toc)
    dir_count, _file_count = _dir_counts(toc)
    (pool_offset,) = struct.unpack_from("<Q", toc, 0x18)
    evil_name = b"../../escape"
    for index in range(dir_count):
        record = dirs_offset + index * nup.DIR_RECORD_SIZE
        name_offset, name_length = struct.unpack_from("<II", toc, record)
        start = pool_offset + name_offset
        stored = bytes(toc[start : start + name_length])
        if stored != b"placeholder-directory":
            continue
        assert len(evil_name) <= name_length
        toc[start : start + len(evil_name)] = evil_name
        struct.pack_into("<I", toc, record + 0x04, len(evil_name))
        break
    else:  # pragma: no cover - the fixture always has that directory
        raise AssertionError("placeholder-directory not found")
    data[toc_offset : toc_offset + toc_size] = toc
    data[0x18:0x38] = hashlib.sha256(bytes(toc)).digest()
    evil.write_bytes(data)
    assert evil_name in evil.read_bytes()

    # The reader validates names while walking the tree, so it already refuses
    # the hostile container before extraction can start.
    with pytest.raises(nup.NupError, match="escapes the output tree"):
        nup.open_nup(evil)

    # extract_nup carries its own guard.  open_nup never yields a container
    # holding this name, so mutate the parsed record to exercise it directly.
    container = nup.open_nup(clean)
    victim = next(
        node
        for _parts, node in container.package_dirs(container.primary)
        if node.name == "placeholder-directory"
    )
    victim.name = "../../escape"
    target = tmp_path / "extract-target"
    with pytest.raises(nup.NupError, match="escapes the output tree"):
        nup.extract_nup(container, target)
    assert not any(path.is_file() for path in target.rglob("*"))
    assert not (tmp_path.parent / "escape").exists()


def test_safe_parts_rejects_unsafe_names():
    assert nup._safe_parts("romfs/data", where="test") == ("romfs", "data")
    assert nup._safe_parts("a//b/./c", where="test") == ("a", "b", "c")
    for bad in ("..", "a/../b", "a\\..\\b"):
        with pytest.raises(nup.NupError, match="escapes the output tree"):
            nup._safe_parts(bad, where="test")
    for bad in ("C:", "C:evil", "a/b:stream"):
        with pytest.raises(nup.NupError, match="drive or stream"):
            nup._safe_parts(bad, where="test")
    with pytest.raises(nup.NupError, match="NUL byte"):
        nup._safe_parts("a\x00b", where="test")
    with pytest.raises(nup.NupError, match="empty node name"):
        nup._safe_parts("", where="test")


# ---------------------------------------------------------------------------
# 10. verification catches corruption
# ---------------------------------------------------------------------------


def test_verify_reports_extent_corruption(tmp_path: Path):
    root, _payloads = _game_tree(tmp_path)
    path = nup.build_nup(nup.collect_packages([root]), tmp_path / "game.nup")
    container = nup.open_nup(path)
    node = _files_by_path(container, container.primary)["romfs/hello.txt"]

    data = bytearray(path.read_bytes())
    data[node.extent_offset] ^= 0xFF
    corrupted = tmp_path / "corrupted.nup"
    corrupted.write_bytes(data)

    damaged = nup.open_nup(corrupted)
    problems = nup.verify_nup(damaged)
    assert len(problems) == 1
    assert "romfs/hello.txt" in problems[0]
    assert "SHA-256 mismatch" in problems[0]
    assert nup.verify_nup(damaged, check_hashes=False) == []

    damaged_node = _files_by_path(damaged, damaged.primary)["romfs/hello.txt"]
    with pytest.raises(nup.NupError, match="SHA-256 mismatch"):
        damaged.read(damaged_node)


# ---------------------------------------------------------------------------
# 11. zstd codec
# ---------------------------------------------------------------------------


def test_zstd_codec_roundtrip(tmp_path: Path):
    pytest.importorskip("zstandard")
    payload = b"compress-me-" * 4096  # 48 KiB, far smaller once compressed
    root = _write(
        tmp_path / "base",
        {
            "exefs/main": b"main" * 32,
            "exefs/main.npdm": _npdm(TID),
            "romfs/repeat.bin": payload,
        },
    )
    path = nup.build_nup(
        nup.collect_packages([root]), tmp_path / "zstd.nup", compression="zstd"
    )
    container = nup.open_nup(path)
    node = _files_by_path(container, container.primary)["romfs/repeat.bin"]

    assert nup.CODEC_ZSTD == 1
    assert node.codec == nup.CODEC_ZSTD
    assert node.is_compressed
    assert node.size == len(payload)
    assert 0 < node.extent_size < node.size
    assert container.read(node) == payload
    assert nup.verify_nup(container) == []

    # The persisted record byte, not just the parsed attribute.
    data = path.read_bytes()
    toc_offset, _toc_size = struct.unpack_from("<QQ", data, 0x08)
    toc = data[toc_offset:]
    record = toc_offset + _file_record_offset(toc, node.index)
    assert data[record + 0x40] == 1


# ---------------------------------------------------------------------------
# regression tests for two defects found while reviewing the module
# ---------------------------------------------------------------------------

_BASE_TID = 0x0100000000010000
_ADDON_TID = 0x0100A0B0C0D00001


def test_reader_rejects_empty_extent_with_nonzero_offset(tmp_path: Path):
    """SPEC.md 3.6: zero-length files must use extent_offset 0.

    The original check only rejected a non-zero offset when ``size`` was also
    non-zero, so a record with ``size == 0`` and ``extent_offset == 0x100`` was
    accepted.
    """
    root = _write(
        tmp_path / "empty-offset",
        {
            "exefs/main": b"tiny-main" * 32,
            "exefs/main.npdm": _npdm(_BASE_TID),
            "romfs/empty.bin": b"",
        },
    )
    source = nup.build_nup(nup.collect_packages([root]), tmp_path / "empty-offset.nup")

    toc_offset, toc_size = struct.unpack_from("<QQ", source.read_bytes(), 0x08)
    toc = source.read_bytes()[toc_offset : toc_offset + toc_size]
    empty_record = None
    for index in range(_dir_counts(toc)[1]):
        base = _file_record_offset(toc, index)
        size, _extent_offset, extent_size = struct.unpack_from("<QQQ", toc, base + 0x08)
        if size == 0 and extent_size == 0:
            empty_record = base
            break
    assert empty_record is not None, "fixture must contain one zero-length file"

    def edit(data: bytearray) -> None:
        _edit_toc(
            data,
            lambda inner: struct.pack_into("<Q", inner, empty_record + 0x10, 0x100),
        )

    corrupt = _mutate_copy(source, tmp_path / "empty-offset-bad.nup", edit)
    with pytest.raises(nup.NupError, match="zero-length files"):
        nup.open_nup(corrupt)


def test_wrapped_title_id_is_addon_only_without_exefs(tmp_path: Path):
    """A base dump wrapped in its own 16-hex title ID must stay a base.

    DLC dumps are ``<title-id>/<index>/romfs`` with no ``exefs``; a base dump is
    sometimes wrapped as ``<title-id>/exefs``.  Classifying on the hex name alone
    turned the latter into an add-on package.
    """
    wrapped = _write(
        tmp_path / "wrapped" / f"{_BASE_TID:016x}",
        {
            "exefs/main": b"m" * 64,
            "exefs/main.npdm": _npdm(_BASE_TID),
            "romfs/a.txt": b"a",
        },
    )
    sources = nup.collect_packages([wrapped.parent])
    assert len(sources) == 1
    assert sources[0].kind == nup.KIND_BASE
    assert sources[0].title_id == _BASE_TID

    dlc = _write(
        tmp_path / "dlc-root" / f"{_ADDON_TID:016x}" / "0",
        {"romfs/only.txt": b"addon"},
    )
    addon_sources = nup.collect_packages([dlc.parent.parent])
    assert len(addon_sources) == 1
    assert addon_sources[0].kind == nup.KIND_ADDON
    assert addon_sources[0].title_id == _ADDON_TID


def test_parent_folder_with_several_dumps_yields_all_packages(tmp_path: Path):
    """The everyday ".../Game Name/{BASE, Update, DLC}" folder must pack whole.

    A single-folder file picker cannot select three siblings, so pointing at the
    folder that *contains* them has to contribute one package per child.
    """
    collection = tmp_path / "collection"
    _write(
        collection / "Game Switch 2 NSP BASE GAME",
        {
            "exefs/main": b"b" * 64,
            "exefs/main.npdm": _npdm(_BASE_TID),
            "romfs/a.txt": b"a",
        },
    )
    _write(
        collection / "Game Switch 2 NSP Update 2.1.11" / "589824",
        {
            "exefs/main": b"u" * 64,
            "exefs/main.npdm": _npdm(_BASE_TID),
            "romfs/p.txt": b"p",
        },
    )
    _write(
        collection / "Game Switch 2 NSP DLC" / f"{_ADDON_TID:016x}" / "0",
        {"romfs/d.bin": b"d"},
    )

    sources = nup.collect_packages([collection])
    assert sorted(source.kind_name for source in sources) == ["addon", "base", "update"]

    update = next(s for s in sources if s.kind == nup.KIND_UPDATE)
    assert update.version == 589824
    # "2.1.11" from the folder name is the marketing version, 589824 the NCM one.
    assert update.meta[nup.TLV_DISPLAY_VERSION] == b"2.1.11"

    addon = next(s for s in sources if s.kind == nup.KIND_ADDON)
    assert addon.title_id == _ADDON_TID

    out = nup.build_nup(sources, tmp_path / "collection.nup")
    container = nup.open_nup(out)
    assert [summary.kind_name for summary in container.summaries()] == [
        "base",
        "update",
        "addon",
    ]
    assert nup.verify_nup(container) == []


# ---------------------------------------------------------------------------
# 12. hardening regressions (found by independent adversarial review)
# ---------------------------------------------------------------------------


def test_reader_bounds_toc_size_before_reading(tmp_path: Path):
    """A huge toc_size must raise NupError, not drive a MemoryError."""
    data = bytearray(_tiny_nup(tmp_path).read_bytes())
    struct.pack_into("<Q", data, 0x10, 1 << 40)   # toc_size
    struct.pack_into("<Q", data, 0x38, 1 << 41)   # blob_offset (16-aligned)
    bad = tmp_path / "huge-toc.nup"
    bad.write_bytes(bytes(data))
    with pytest.raises(nup.NupError, match="TOC|blob_offset"):
        nup.open_nup(bad)


def test_reader_rejects_string_pool_before_child_array(tmp_path: Path):
    """string_pool_off < children_offset must be an error, not a warning.

    ``(string_pool_off - children_offset) % 4`` passes for a negative multiple
    of four, so the slot count went negative and struct raised ``struct.error``.
    """

    def edit(toc: bytearray) -> None:
        _dirs, files = _toc_offsets(toc)
        _dc, fc = _dir_counts(toc)
        children_offset = files + fc * nup.FILE_RECORD_SIZE
        struct.pack_into("<QQ", toc, 0x18, children_offset - 4, 8)

    bad = _mutate_copy(
        _tiny_nup(tmp_path),
        tmp_path / "neg-slots.nup",
        lambda data: _edit_toc(data, edit),
    )
    with pytest.raises(nup.NupError, match="string_pool_off|children"):
        nup.open_nup(bad)


def test_reader_rejects_zero_header_version(tmp_path: Path):
    """header_version starts at 1; 0 was previously accepted untouched."""

    def edit(data: bytearray) -> None:
        struct.pack_into("<H", data, 0x06, 0)

    bad = _mutate_copy(_tiny_nup(tmp_path), tmp_path / "hv0.nup", edit)
    with pytest.raises(nup.NupError, match="header_version"):
        nup.open_nup(bad)


def test_reader_bounds_tree_depth(tmp_path: Path):
    """A 1000-deep chain must fail as NupError, not RecursionError."""
    depth = 1000
    pool = bytearray()
    offsets: dict[bytes, int] = {}

    def string(text: str) -> tuple[int, int]:
        raw = text.encode()
        if raw not in offsets:
            offsets[raw] = len(pool)
            pool.extend(raw)
        return offsets[raw], len(raw)

    dirs = [("", 0, 1)] + [("d", i, 1) for i in range(1, depth - 1)] + [("d", 0, 0)]
    children = list(range(1, depth))
    names = [string(name) for name, _first, _count in dirs]
    dir_count, file_count, package_count = len(dirs), 0, 1
    dirs_offset = nup.TOC_PREFIX_SIZE + nup.PACKAGE_RECORD_SIZE
    children_offset = dirs_offset + nup.DIR_RECORD_SIZE * dir_count
    unpadded = children_offset + 4 * len(children)
    toc_size = nup._align(unpadded + len(pool))
    blob_offset = nup._align(nup.SUPERBLOCK_SIZE + toc_size)
    toc = bytearray(toc_size)
    struct.pack_into("<II", toc, 0x00, nup.TOC_MAGIC, package_count)
    struct.pack_into("<QQ", toc, 0x08, dir_count, file_count)
    struct.pack_into("<QQ", toc, 0x18, unpadded, len(pool))
    for index, ((offset, length), (_n, first, count)) in enumerate(zip(names, dirs)):
        struct.pack_into(
            "<IIIIQ", toc, dirs_offset + nup.DIR_RECORD_SIZE * index,
            offset, length, first, count, 0,
        )
    for index, node_id in enumerate(children):
        struct.pack_into("<I", toc, children_offset + 4 * index, node_id)
    toc[unpadded : unpadded + len(pool)] = pool
    struct.pack_into("<QIIII", toc, nup.TOC_PREFIX_SIZE, TID, 0, 0, nup.NO_DIR, 0)
    superblock = bytearray(nup.SUPERBLOCK_SIZE)
    superblock[0:4] = nup.NUP_MAGIC
    struct.pack_into("<HH", superblock, 0x04, nup.SUPERBLOCK_SIZE, 1)
    struct.pack_into("<QQ", superblock, 0x08, nup.SUPERBLOCK_SIZE, toc_size)
    superblock[0x18:0x38] = hashlib.sha256(bytes(toc)).digest()
    struct.pack_into("<QQ", superblock, 0x38, blob_offset, blob_offset)
    struct.pack_into("<II", superblock, 0x48, package_count, 0)
    deep = tmp_path / "deep.nup"
    deep.write_bytes(
        bytes(superblock) + bytes(toc)
        + b"\x00" * (blob_offset - nup.SUPERBLOCK_SIZE - toc_size)
    )
    with pytest.raises(nup.NupError, match="nesting|depth"):
        nup.open_nup(deep)


def test_verify_reports_tampered_zero_length_hash(tmp_path: Path):
    """sha256 of empty content is fixed; a wrong record must be reported."""
    root = _write(
        tmp_path / "zero",
        {"exefs/main.npdm": _npdm(TID), "romfs/empty.bin": b""},
    )
    source = nup.build_nup(nup.collect_packages([root]), tmp_path / "zero.nup")

    def edit(toc: bytearray) -> None:
        for index in range(_dir_counts(toc)[1]):
            base = _file_record_offset(toc, index)
            size, _offset, extent_size = struct.unpack_from("<QQQ", toc, base + 0x08)
            if size == 0 and extent_size == 0:
                toc[base + 0x20 : base + 0x40] = b"\x11" * 32
                return
        raise AssertionError("fixture must contain a zero-length file")

    bad = _mutate_copy(
        source, tmp_path / "zero-bad.nup", lambda data: _edit_toc(data, edit)
    )
    problems = nup.verify_nup(nup.open_nup(bad))
    assert any("zero-length" in problem for problem in problems), problems


def test_duplicate_addon_indices_are_all_kept(tmp_path: Path):
    """``dlc/<title-id>/0`` and ``/1`` are distinct packages, not duplicates.

    Collapsing them by title ID discarded one add-on's files with exit 0.
    """
    root = _write(
        tmp_path / "multi-index",
        {
            "exefs/main": b"base" * 16,
            "exefs/main.npdm": _npdm(TID),
            "romfs/base.bin": b"BASE",
            f"dlc/{ADDON_TID:016x}/0/romfs/idx0.bin": b"IDX0",
            f"dlc/{ADDON_TID:016x}/1/romfs/idx1.bin": b"IDX1",
        },
    )
    sources = nup.collect_packages([root])
    addons = [source for source in sources if source.kind == nup.KIND_ADDON]
    assert len(addons) == 2, [source.origin for source in addons]
    assert sorted(source.ordinal for source in addons) == [0, 1]

    out = nup.build_nup(sources, tmp_path / "multi.nup")
    blob = out.read_bytes()
    assert b"IDX0" in blob and b"IDX1" in blob, "an add-on input was discarded"

    # and it survives a folder -> NUP -> folder -> NUP cycle
    back = tmp_path / "multi-back"
    nup.extract_nup(nup.open_nup(out), back)
    assert (back / "dlc" / f"{ADDON_TID:016x}" / "0" / "romfs" / "idx0.bin").is_file()
    assert (back / "dlc" / f"{ADDON_TID:016x}" / "1" / "romfs" / "idx1.bin").is_file()


def test_pack_reports_non_utf8_file_name(tmp_path: Path):
    """A dump folder may hold a name that is not valid UTF-8."""
    root = tmp_path / "weird"
    (root / "exefs").mkdir(parents=True)
    (root / "romfs").mkdir()
    (root / "exefs" / "main.npdm").write_bytes(_npdm(TID))
    with open(os.fsencode(root / "romfs") + b"/bad\xff.bin", "wb") as handle:
        handle.write(b"x")
    with pytest.raises(nup.NupError, match="UTF-8"):
        nup.build_nup(
            nup.collect_packages([root], kind="base"), tmp_path / "weird.nup"
        )


def test_partial_extent_overlap_is_reported(tmp_path: Path):
    """Two records claiming overlapping (not identical) bytes is corruption."""
    root = _write(
        tmp_path / "overlap",
        {
            "exefs/main.npdm": _npdm(TID),
            "romfs/big.bin": b"B" * 64,
            "romfs/small.bin": b"S" * 8,
        },
    )
    source = nup.build_nup(nup.collect_packages([root]), tmp_path / "overlap.nup")

    def edit(toc: bytearray) -> None:
        records = []
        for index in range(_dir_counts(toc)[1]):
            base = _file_record_offset(toc, index)
            size, offset, extent_size = struct.unpack_from("<QQQ", toc, base + 0x08)
            if size in (64, 8):
                records.append((size, base, offset, extent_size))
        big = next(item for item in records if item[0] == 64)
        small = next(item for item in records if item[0] == 8)
        assert big[3] > 16
        struct.pack_into("<Q", toc, small[1] + 0x10, big[2] + 16)

    bad = _mutate_copy(
        source, tmp_path / "overlap-bad.nup", lambda data: _edit_toc(data, edit)
    )
    problems = nup.verify_nup(nup.open_nup(bad), check_hashes=False)
    assert any("overlaps" in problem for problem in problems), problems


def test_explicit_version_beats_name_derived_display_version(tmp_path: Path):
    """``--version`` must not be shadowed by a version parsed from the folder name."""
    root = _write(
        tmp_path / "Game Update 2.1.11",
        {"exefs/main.npdm": _npdm(TID), "romfs/p.bin": b"p"},
    )
    sources = nup.collect_packages([root], kind="update", version=99)
    assert sources[0].version == 99
    assert nup.TLV_DISPLAY_VERSION not in sources[0].meta

    out = nup.build_nup(sources, tmp_path / "override.nup")
    container = nup.open_nup(out)
    assert container.primary.display_version == nup._decode_version(99)


def test_pack_rejects_name_the_reader_would_refuse(tmp_path: Path):
    """Packing must not emit a container this module then refuses to open.

    The reader refuses ':' (Windows drive/stream separator) and '\\', so the
    writer has to refuse them too instead of trapping its own output.
    """
    root = _write(tmp_path / "colon", {"exefs/main.npdm": _npdm(TID)})
    (root / "romfs").mkdir()
    (root / "romfs" / "a:b.txt").write_bytes(b"colon")
    with pytest.raises(nup.NupError, match=":"):
        nup.build_nup(
            nup.collect_packages([root], kind="base"), tmp_path / "colon.nup"
        )

    other = _write(tmp_path / "backslash", {"exefs/main.npdm": _npdm(TID)})
    (other / "romfs").mkdir()
    (other / "romfs" / "a\\b.txt").write_bytes(b"backslash")
    with pytest.raises(nup.NupError, match="represent"):
        nup.build_nup(
            nup.collect_packages([other], kind="base"), tmp_path / "backslash.nup"
        )


def test_multi_index_addons_roundtrip_byte_identical(tmp_path: Path):
    """Two add-on indices must survive folder -> NUP -> folder -> NUP untouched."""
    root = _write(
        tmp_path / "multi",
        {
            "exefs/main.npdm": _npdm(TID),
            f"dlc/{ADDON_TID:016x}/0/romfs/idx0.bin": b"IDX0-unique",
            f"dlc/{ADDON_TID:016x}/1/romfs/idx1.bin": b"IDX1-unique",
        },
    )
    first = nup.build_nup(nup.collect_packages([root]), tmp_path / "first.nup")
    back = tmp_path / "back"
    nup.extract_nup(nup.open_nup(first), back)
    second = nup.build_nup(nup.collect_packages([back]), tmp_path / "second.nup")
    assert second.read_bytes() == first.read_bytes()
    addons = [p for p in nup.open_nup(second).packages if p.kind == nup.KIND_ADDON]
    assert len(addons) == 2


def test_pack_rejects_over_deep_nesting(tmp_path: Path):
    """A folder deeper than the reader's cap must fail at pack time, not open time."""
    deep = tmp_path / "deep" / "exefs"
    deep.mkdir(parents=True)
    (deep / "main.npdm").write_bytes(_npdm(TID))
    current = tmp_path / "deep" / "romfs"
    for _ in range(nup._MAX_TREE_DEPTH + 4):
        current = current / "d"
    current.mkdir(parents=True)
    (current / "leaf.bin").write_bytes(b"x")
    with pytest.raises(nup.NupError, match="nested|levels"):
        nup.build_nup(
            nup.collect_packages([tmp_path / "deep"], kind="base"),
            tmp_path / "deep.nup",
        )


def test_two_addon_folders_for_one_title_are_both_kept(tmp_path: Path):
    """Two independent DLC folders for one title must not overwrite each other.

    Only ``dlc/<title-id>/<index>`` discovery carries an index.  Wrapper folders
    and separate command-line inputs do not, so both arrived at ordinal 0 and
    ``_resolve_packages`` overwrote one with exit status 0.  Both are now kept by
    re-indexing the second; only a container-vs-folder pair is a replacement.
    """
    tid = f"{ADDON_TID:016x}"
    first = _write(tmp_path / "DLC Vol 1" / tid, {"romfs/vol1.bin": b"VOL1-unique"})
    second = _write(tmp_path / "DLC Vol 2" / tid, {"romfs/vol2.bin": b"VOL2-unique"})

    sources = nup.collect_packages([first, second])
    addons = [source for source in sources if source.kind == nup.KIND_ADDON]
    assert len(addons) == 2, [source.origin for source in addons]
    assert sorted(source.ordinal for source in addons) == [0, 1]

    out = nup.build_nup(sources, tmp_path / "both.nup")
    blob = out.read_bytes()
    assert b"VOL1-unique" in blob and b"VOL2-unique" in blob, "an add-on was discarded"
    assert nup.verify_nup(nup.open_nup(out)) == []


def test_addon_replacement_from_a_container_still_works(tmp_path: Path):
    """The re-bundle path must still replace an add-on inherited from a .nup."""
    tid = f"{ADDON_TID:016x}"
    root = _write(
        tmp_path / "initial",
        {
            "exefs/main.npdm": _npdm(TID),
            f"dlc/{tid}/0/romfs/old.bin": b"OLD-DLC",
        },
    )
    original = nup.build_nup(nup.collect_packages([root]), tmp_path / "original.nup")

    newer = _write(tmp_path / "newer" / tid, {"romfs/new.bin": b"NEW-DLC"})
    merged = nup.build_nup(
        nup.collect_packages([original, newer]), tmp_path / "merged.nup"
    )
    blob = merged.read_bytes()
    assert b"NEW-DLC" in blob
    assert b"OLD-DLC" not in blob, "a container's add-on should be replaced"
    addons = [p for p in nup.open_nup(merged).packages if p.kind == nup.KIND_ADDON]
    assert len(addons) == 1
