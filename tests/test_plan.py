from pathlib import Path

from sak_modern.core.bundle import plan_from_loaded
from sak_modern.core.model import ContentEntry, LoadedContainer, NcaHeader


def nca(tid: int, ctype: int = 0) -> NcaHeader:
    return NcaHeader(
        encrypted=b"", plain=b"", magic="NCA3", distribution_type=0,
        content_type=ctype, crypto_type=2, key_index=0, key_generation=8,
        nca_size=1, title_id=tid, rights_id=b"\x00" * 16, key_area=b"\x00" * 64,
    )


def entry(path: Path, tid: int, kind: str, version: int, ctype: int = 0, name: str | None = None) -> ContentEntry:
    e = ContentEntry(name or f"{tid:016x}.nca", 1, path, 0, nca(tid, ctype), version)
    e.package_kind = kind
    e.package_title_id = tid
    e.package_group = tid & 0xFFFFFFFFFFFF0000
    e.package_id = f"{path}:{kind}"
    return e


def container(path: Path, entries: list[ContentEntry]) -> LoadedContainer:
    return LoadedContainer(path, "nsp", entries, [], [])


def test_new_update_replaces_old_update():
    base_path = Path("/tmp/base.nsp")
    old_path = Path("/tmp/old-update.nsp")
    new_path = Path("/tmp/new-update.nsp")
    base = entry(base_path, 0x0100000000000000, "base", 0)
    old = entry(old_path, 0x0100000000000800, "update", 100)
    new = entry(new_path, 0x0100000000000800, "update", 200)
    plan = plan_from_loaded(
        [container(base_path, [base]), container(old_path, [old]), container(new_path, [new])],
        preferred=[container(new_path, [new])],
    )
    assert plan.base.entries == [base]
    assert plan.update is not None
    assert plan.update.version == 200
    assert plan.update.entries == [new]


def test_dlc_update_replaces_same_dlc_only():
    base_path = Path("/tmp/base.nsp")
    dlc_old = Path("/tmp/dlc-old.nsp")
    dlc_new = Path("/tmp/dlc-new.nsp")
    base = entry(base_path, 0x0100000000000000, "base", 0)
    old = entry(dlc_old, 0x0100000000001001, "dlc", 0, ctype=5)
    new = entry(dlc_new, 0x0100000000001001, "dlc", 5, ctype=5)
    plan = plan_from_loaded(
        [container(base_path, [base]), container(dlc_old, [old]), container(dlc_new, [new])],
        preferred=[container(dlc_new, [new])],
    )
    assert len(plan.dlcs) == 1
    assert plan.dlcs[0].entries == [new]


def test_cnmt_first_package_boundaries():
    from sak_modern.core.model import assign_packages
    p = Path("/tmp/merged.xci")
    base = [
        entry(p, 0x0100000000000000, "base", 0, name="base.nca"),
        entry(p, 0x0100000000000000, "base", 0, ctype=1, name="base.cnmt.nca"),
        entry(p, 0x0100000000000800, "update", 0, ctype=1, name="upd.cnmt.nca"),
        entry(p, 0x0100000000000000, "update", 0, name="upd.nca"),
        entry(p, 0x0100000000001001, "dlc", 0, ctype=5, name="dlc.nca"),
        entry(p, 0x0100000000001001, "dlc", 0, ctype=1, name="dlc.cnmt.nca"),
    ]
    for e in base:
        e.package_kind = None
        e.package_title_id = None
        e.package_group = None
        e.package_id = None
    assign_packages(base)
    kinds = [e.package_kind for e in base]
    assert kinds == ["base", "base", "update", "update", "dlc", "dlc"]


def test_merged_container_does_not_give_update_version_to_dlc(tmp_path: Path):
    from sak_modern.core.model import _scope_versions
    p = tmp_path / "Merged [0100000000000000] [v65536] (1G+1U+1D).xci"
    base = entry(p, 0x0100000000000000, "base", 65536, name="base.nca")
    upd = entry(p, 0x0100000000000800, "update", 65536, ctype=1, name="upd.cnmt.nca")
    dlc = entry(p, 0x0100000000001001, "dlc", 65536, ctype=5, name="dlc.nca")
    for e in (base, upd, dlc):
        e.package_id = f"{p}:{e.name}"
    _scope_versions([base, upd, dlc], p)
    assert base.version == 65536
    assert dlc.version == 0
    assert upd.version == 0


def test_package_labels_and_filenames():
    from sak_modern.core.extract import package_filename, package_label
    from sak_modern.core.model import ContentGroup
    p = Path("/tmp/Game [0100000000000000] [v393216] (1G+1U+4D).xci")
    base = ContentGroup(0x0100000000000000, "base", 0x0100000000000000, 0,
                        [entry(p, 0x0100000000000000, "base", 0, name="base.nca")])
    upd = ContentGroup(0x0100000000000000, "update", 0x0100000000000800, 393216,
                       [entry(p, 0x0100000000000800, "update", 393216, ctype=1, name="upd.cnmt.nca")])
    dlc = ContentGroup(0x0100000000000000, "dlc", 0x0100000000001007, 0,
                       [entry(p, 0x0100000000001007, "dlc", 0, ctype=5, name="dlc.nca")])
    assert package_label(base) == "BASE"
    assert package_label(upd) == "UPD v393216"
    assert package_label(dlc) == "DLC 7"
    assert "[BASE].xci" in package_filename(p, base, "xci")
    assert "[UPD v393216].nsp" in package_filename(p, upd, "nsp")
    assert "[DLC 7].nsp" in package_filename(p, dlc, "nsp")
