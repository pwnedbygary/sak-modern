from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .bundle import BundlePlan, build_nsp, build_xci
from .keys import KeyStore
from .model import ContentGroup, group_contents, load_container

Progress = Callable[[int, int, str], None]


@dataclass
class ExtractedPackage:
    group: ContentGroup
    label: str
    path: Path
    format: str


def package_label(group: ContentGroup) -> str:
    if group.kind == "base":
        return "BASE"
    if group.kind == "update":
        return f"UPD v{group.version}" if group.version else "UPD"
    dlc_no = group.title_id & 0xFFF
    return f"DLC {dlc_no}"


def package_filename(source: Path, group: ContentGroup, fmt: str, index: int = 1) -> str:
    stem = source.stem
    stem = re.sub(r"\s*\[[0-9A-Fa-f]{16}\].*$", "", stem)
    stem = re.sub(r"\s*\[v\d+\].*$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s*\([^)]*\)\s*$", "", stem)
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .") or "game"
    label = package_label(group)
    suffix = f" ({index})" if index > 1 else ""
    return f"{stem} [{group.title_id:016X}] [{label}]{suffix}.{fmt}"


def extract_packages(
    source: str | Path,
    out_dir: str | Path,
    ks: KeyStore,
    *,
    base_format: str = "source",
    selected: Iterable[str] | None = None,
    progress: Progress | None = None,
) -> list[ExtractedPackage]:
    """Split a merged container into one file per title package.

    The base package keeps the source format by default (XCI -> XCI, NSP -> NSP).
    Updates and DLC are emitted as NSPs so their tickets and rights IDs survive.
    """
    src = Path(source)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    container = load_container(src, ks)
    bases, updates, dlcs = group_contents([container])
    groups: list[ContentGroup] = [*bases, *updates, *dlcs]
    # A merged container often stores its version in the filename while the
    # per-package version is unavailable. Use that for an update label.
    from .model import version_from_name
    file_version = version_from_name(src)
    for g in updates:
        if g.version == 0 and file_version:
            g.version = file_version
    wanted = set(selected) if selected is not None else None
    if wanted is not None:
        groups = [g for g in groups if g.package_id in wanted or f"{g.title_id:016X}" in wanted]

    total = sum(g.total_size for g in groups) or 1
    done = 0
    results: list[ExtractedPackage] = []
    used_names: dict[str, int] = {}
    for group in groups:
        if wanted is not None and group.package_id not in wanted and f"{group.title_id:016X}" not in wanted:
            continue
        if group.kind != "base":
            fmt = "nsp"
        elif base_format == "source":
            fmt = "xci" if src.suffix.lower() == ".xci" else "nsp"
        elif base_format in ("xci", "nsp"):
            fmt = base_format
        else:
            raise ValueError(f"Unsupported base format: {base_format}")
        base_name = package_filename(src, group, fmt)
        used_names[base_name] = used_names.get(base_name, 0) + 1
        name = base_name if used_names[base_name] == 1 else package_filename(src, group, fmt, used_names[base_name])
        plan = BundlePlan(base=group, update=None, dlcs=[], containers=[container], warnings=[])
        builder = build_xci if fmt == "xci" else build_nsp
        path = builder(plan, ks, out, name=name)
        results.append(ExtractedPackage(group, package_label(group), path, fmt))
        done += group.total_size
        if progress:
            progress(done, total, f"Extracting {package_label(group)}")
    return results
