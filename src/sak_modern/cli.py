from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core.bundle import build_nsp, build_xci, estimate_xci, make_plan
from .core.errors import SakError
from .core.extract import extract_packages, package_label
from .core.keys import KeyStore
from .core.model import group_contents, load_container
from .core.nup import (
    KIND_ADDON,
    KIND_ADDON_UPDATE,
    KIND_BASE,
    KIND_NAMES,
    KIND_UPDATE,
    NupError,
    TLV_NAMES,
    build_nup,
    collect_packages,
    extract_nup,
    is_nup,
    open_nup,
    verify_nup,
)
from .core.verify import verify_container


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def nup_uuid_text(uuid: bytes) -> str:
    digits = uuid.hex()
    if len(digits) != 32:
        return digits
    return "-".join(
        (digits[:8], digits[8:12], digits[12:16], digits[16:20], digits[20:])
    )


def nup_facts(c) -> str:
    return (
        f"header v{c.header_version}  toc={c.toc_size} B  blob=0x{c.blob_offset:x}  "
        f"size={human_size(c.file_size)}  uuid={nup_uuid_text(c.uuid)}"
    )


def nup_meta_label(summary) -> str:
    if not summary.meta_types:
        return "-"
    return ",".join(TLV_NAMES.get(t, f"0x{t:04x}") for t in summary.meta_types)


def print_nup_inspect(c) -> None:
    print(f"\nNUP: {c.path.name}  ({human_size(c.file_size)})")
    print(f"  {nup_facts(c)}")
    for s in c.summaries():
        print(
            f"  {s.index:3d}  {s.title_id:016x}  {s.kind_name:12}  "
            f"{s.display_version:11}  {s.file_count:5d}  "
            f"{human_size(s.total_size):>12}  {s.title_name or '-'}  "
            f"[{nup_meta_label(s)}]"
        )
    for w in c.warnings:
        print(f"  ! {w}")


def report_nup_verify(path, *, check_hashes: bool) -> bool:
    c = open_nup(path)
    print(f"\n{c.path.name}")
    print(f"  {nup_facts(c)}")
    print(f"  {c.package_count} package(s), {len(c.files)} file(s)")
    problems = verify_nup(c, check_hashes=check_hashes)
    for w in c.warnings:
        print(f"  ! {w}")
    for problem in problems:
        print(f"  ERROR: {problem}")
    print("  OK" if not problems else "  FAILED")
    return not problems


def selected_nup_indices(c, selection) -> list[int]:
    chosen = list(range(c.package_count)) if not selection else sorted(set(selection))
    for index in chosen:
        if index < 0 or index >= c.package_count:
            raise NupError(f"package index {index} is out of range")
    return chosen


def nup_layout_roots(c, layout: str, chosen) -> list[tuple[Path, int, int]]:
    """``(relative root, file count, total size)`` per selected package.

    Mirrors the layout :func:`extract_nup` documents and writes, so ``--list``
    can report destinations without touching the filesystem.
    """
    roots: list[tuple[Path, int, int]] = []
    addon_ordinals: dict[int, int] = {}
    update_ordinals: dict[tuple[int, int], int] = {}
    for index in chosen:
        package = c.packages[index]
        if layout == "split":
            name = f"{package.kind_name}-{package.title_id_hex}"
            if package.kind == KIND_UPDATE:
                name += f"-{package.version}"
            rel = Path(name)
        elif package.kind == KIND_BASE:
            rel = Path(".")
        elif package.kind == KIND_UPDATE:
            key = (package.title_id, package.version)
            ordinal = update_ordinals.get(key, 0)
            update_ordinals[key] = ordinal + 1
            name = (
                str(package.version)
                if ordinal == 0
                else f"{package.version}-{ordinal}"
            )
            if package.standalone_update:
                name = f"{name}-standalone"
            rel = Path("updates") / name
        elif package.kind in (KIND_ADDON, KIND_ADDON_UPDATE):
            ordinal = addon_ordinals.get(package.title_id, 0)
            addon_ordinals[package.title_id] = ordinal + 1
            rel = Path("dlc") / package.title_id_hex / str(ordinal)
        else:
            label = KIND_NAMES.get(package.kind, f"kind{package.kind}")
            rel = Path(label) / package.title_id_hex
        files = c.package_files(package)
        roots.append((rel, len(files), sum(node.size for _, node in files)))
    return roots


def print_pack_plan(sources, *, compression: str | None = None) -> None:
    suffix = f", compression={compression}" if compression else ""
    print(f"Pack plan ({len(sources)} package(s){suffix})")
    for source in sources:
        where = source.origin or (str(source.root) if source.root else "?")
        print(
            f"  {source.kind_name:12} {source.title_id:016x}  v{source.version}  "
            f"{where}"
        )


def write_nup_output(
    sources, out_path, *, compression: str, source_name: str | None, dry_run: bool
) -> int:
    out = Path(out_path).expanduser()
    if dry_run:
        print(f"Would write: {out}")
        return 0
    path = build_nup(sources, out, compression=compression, source_name=source_name)
    print(f"Wrote: {path} ({human_size(path.stat().st_size)})")
    return 0


def title_id_arg(text: str) -> int:
    digits = text.strip()
    if digits[:2].lower() == "0x":
        digits = digits[2:]
    try:
        value = int(digits, 16)
    except ValueError:
        value = -1
    if value <= 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise argparse.ArgumentTypeError(
            f"invalid title ID {text!r} (expected 16 hex digits, optionally "
            f"0x-prefixed)"
        )
    return value


def cmd_inspect(args) -> int:
    others: list[str] = []
    for path in args.files:
        if is_nup(path):
            print_nup_inspect(open_nup(path))
        else:
            others.append(path)
    if not others:
        return 0
    ks = KeyStore.load(args.keys)
    for path in others:
        c = load_container(path, ks)
        print(f"\n{c.kind.upper()}: {c.path.name}  ({human_size(c.path.stat().st_size)})")
        for e in c.ncas:
            n = e.nca
            assert n is not None
            kind = e.kind.upper()
            print(
                f"  {kind:6} {e.name:42} {human_size(e.size):>12}  "
                f"title={n.title_id_hex} type={n.content_type_name} keygen={n.key_generation}"
            )
        for w in c.warnings:
            print(f"  ! {w}")
    bases, updates, dlcs = group_contents([load_container(p, ks) for p in others])
    if bases:
        print("\nTITLE SUMMARY")
        for label, groups in (("BASE", bases), ("UPDATE", updates), ("DLC", dlcs)):
            for g in sorted(groups, key=lambda x: x.title_id):
                print(f"  {label:6} {g.title_id:016X}  v{g.version}  {human_size(g.total_size)}")
    return 0


def cmd_bundle(args) -> int:
    ks = KeyStore.load(args.keys)
    paths = [args.base, *args.add]
    plan = make_plan(paths, ks)
    print(f"Base:   {plan.base.title_id:016X}  ({len(plan.base.entries)} NCAs)")
    if plan.update:
        print(f"Update: {plan.update.title_id:016X}  v{plan.update.version}  ({len(plan.update.entries)} NCAs)")
    for d in plan.dlcs:
        print(f"DLC:    {d.title_id:016X}  v{d.version}  ({len(d.entries)} NCAs)")
    for w in plan.warnings:
        print(f"! {w}")
    out_dir = Path(args.out).expanduser()
    builder = build_xci if args.format == "xci" else build_nsp
    out = builder(plan, ks, out_dir, name=args.name, dry_run=args.dry_run)
    if args.dry_run:
        if args.format == "xci":
            total, card_byte, capacity = estimate_xci(plan, ks)
            print(f"Would write: {out} ({human_size(total)}, card 0x{card_byte:02x}, "
                  f"{capacity / (1 << 30):.0f} GiB)")
        else:
            print(f"Would write: {out}")
    else:
        print(f"Wrote: {out} ({human_size(out.stat().st_size)})")
    return 0


def cmd_extract(args) -> int:
    ks = KeyStore.load(args.keys)
    from .core.model import group_contents, load_container
    container = load_container(args.file, ks)
    bases, updates, dlcs = group_contents([container])
    groups = [*bases, *updates, *dlcs]
    print(f"{Path(args.file).name}: {len(groups)} packages")
    for g in groups:
        print(f"  {package_label(g):12} {g.title_id:016X}  {human_size(g.total_size):>12}  {len(g.entries)} NCAs")
    if args.list:
        return 0
    out = extract_packages(args.file, args.out, ks, base_format=args.base_format)
    for item in out:
        print(f"  -> {item.path} ({human_size(item.path.stat().st_size)})")
    return 0


def cmd_verify(args) -> int:
    ks = None
    failed = False
    for path in args.files:
        if is_nup(path):
            failed = not report_nup_verify(path, check_hashes=True) or failed
            continue
        if ks is None:
            ks = KeyStore.load(args.keys)
        rep = verify_container(path, ks)
        print(f"\n{rep.path.name}")
        for d in rep.details:
            print(f"  {d}")
        for w in rep.warnings:
            print(f"  ! {w}")
        for e in rep.errors:
            print(f"  ERROR: {e}")
        print("  OK" if rep.ok else "  FAILED")
        failed = failed or not rep.ok
    return 1 if failed else 0


def cmd_nup_inspect(args) -> int:
    for path in args.files:
        print_nup_inspect(open_nup(path))
    return 0


def cmd_nup_pack(args) -> int:
    sources = collect_packages(
        args.inputs,
        kind=args.kind,
        version=args.version,
        title_id=args.title_id,
        replace=not args.keep_all,
    )
    print_pack_plan(sources, compression=args.compression)
    return write_nup_output(
        sources,
        args.out,
        compression=args.compression,
        source_name=args.source_name,
        dry_run=args.dry_run,
    )


def cmd_nup_unpack(args) -> int:
    c = open_nup(args.file)
    chosen = selected_nup_indices(c, args.package)
    summaries = c.summaries()
    print(f"{c.path.name}: {len(chosen)} package(s)")
    for index in chosen:
        s = summaries[index]
        print(
            f"  {s.kind_name:12} {s.title_id:016x}  v{s.version}  "
            f"{human_size(s.total_size):>12}  {s.file_count} file(s)"
        )
    destination = Path(args.out).expanduser()
    if args.list:
        for rel, count, size in nup_layout_roots(c, args.layout, chosen):
            target = destination if rel == Path(".") else destination / rel
            print(f"  would write {target} ({count} file(s), {human_size(size)})")
        return 0
    written = extract_nup(
        c,
        destination,
        layout=args.layout,
        selected=chosen,
        verify=not args.no_verify,
    )
    print(f"Wrote {len(written)} file(s) to {destination}")
    return 0


def cmd_nup_verify(args) -> int:
    failed = False
    for path in args.files:
        if not report_nup_verify(path, check_hashes=not args.no_hashes):
            failed = True
    return 1 if failed else 0


def cmd_nup_merge(args) -> int:
    sources = collect_packages([args.existing, *args.inputs], replace=True)
    print_pack_plan(sources, compression=args.compression)
    return write_nup_output(
        sources,
        args.out,
        compression=args.compression,
        source_name=args.source_name,
        dry_run=args.dry_run,
    )


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    return gui_main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sak-modern",
        description="SAK Modern - bundle/convert/inspect Switch NSP and XCI files.",
    )
    p.add_argument("--version", action="version", version=f"SAK Modern {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    insp = sub.add_parser("inspect", help="show contents and title groups")
    insp.add_argument("files", nargs="+")
    insp.add_argument("--keys", help="path to prod.keys/keys.txt")
    insp.set_defaults(func=cmd_inspect)

    bun = sub.add_parser("bundle", help="bundle a base game with update/DLC")
    bun.add_argument("--base", required=True, help="base game NSP or XCI")
    bun.add_argument("--add", action="append", default=[], help="update or DLC NSP/XCI (repeatable)")
    bun.add_argument("-o", "--out", required=True, help="output directory")
    bun.add_argument("--format", choices=("xci", "nsp"), default="xci")
    bun.add_argument("--name", help="output filename")
    bun.add_argument("--keys", help="path to prod.keys/keys.txt")
    bun.add_argument("--dry-run", action="store_true", help="plan only; do not write a huge file")
    bun.set_defaults(func=cmd_bundle)

    ext = sub.add_parser("extract", help="split a merged file into base/update/DLC packages")
    ext.add_argument("file")
    ext.add_argument("-o", "--out", required=True)
    ext.add_argument("--base-format", choices=("source", "xci", "nsp"), default="source")
    ext.add_argument("--list", action="store_true", help="list packages only")
    ext.add_argument("--keys", help="path to prod.keys/keys.txt")
    ext.set_defaults(func=cmd_extract)

    ver = sub.add_parser("verify", help="verify container structure and HFS0 hashes")
    ver.add_argument("files", nargs="+")
    ver.add_argument("--keys", help="path to prod.keys/keys.txt")
    ver.set_defaults(func=cmd_verify)

    nup_p = sub.add_parser("nup", help="work with NUP (NX Package Format) containers")
    nup_sub = nup_p.add_subparsers(dest="nup_action", required=True)

    nup_insp = nup_sub.add_parser(
        "inspect", help="list NUP packages and superblock facts"
    )
    nup_insp.add_argument("files", nargs="+")
    nup_insp.set_defaults(func=cmd_nup_inspect)

    nup_pack = nup_sub.add_parser(
        "pack", help="pack folders and/or .nup files into a NUP"
    )
    nup_pack.add_argument(
        "inputs", nargs="+", help="package folder(s) and/or existing .nup file(s)"
    )
    nup_pack.add_argument("-o", "--out", required=True, help="output .nup path")
    nup_pack.add_argument(
        "--kind",
        choices=("base", "update", "addon"),
        help="package kind override for folder inputs",
    )
    nup_pack.add_argument(
        "--version", type=int, help="NCM version override for folder inputs"
    )
    nup_pack.add_argument(
        "--title-id",
        type=title_id_arg,
        metavar="0xHEX",
        help="title ID override for folder inputs",
    )
    nup_pack.add_argument(
        "--compression", choices=("raw", "zstd", "auto"), default="raw"
    )
    nup_pack.add_argument(
        "--keep-all",
        action="store_true",
        help="keep every update version instead of replacing an older update/DLC "
        "for the same title",
    )
    nup_pack.add_argument(
        "--source-name", help="producer label stored in the container meta"
    )
    nup_pack.add_argument(
        "--dry-run", action="store_true", help="plan only; do not write the file"
    )
    nup_pack.set_defaults(func=cmd_nup_pack)

    nup_unpack = nup_sub.add_parser(
        "unpack", help="write a NUP back to a folder tree"
    )
    nup_unpack.add_argument("file")
    nup_unpack.add_argument("-o", "--out", required=True, help="output directory")
    nup_unpack.add_argument(
        "--layout",
        choices=("tree", "split"),
        default="tree",
        help="tree writes the re-packable merged shape; split writes one folder "
        "per package",
    )
    nup_unpack.add_argument(
        "--package",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="package index to unpack (repeatable)",
    )
    nup_unpack.add_argument(
        "--no-verify", action="store_true", help="skip per-file SHA-256 checks"
    )
    nup_unpack.add_argument(
        "--list", action="store_true", help="list packages and destinations only"
    )
    nup_unpack.set_defaults(func=cmd_nup_unpack)

    nup_verify = nup_sub.add_parser(
        "verify", help="verify NUP structure and per-file hashes"
    )
    nup_verify.add_argument("files", nargs="+")
    nup_verify.add_argument(
        "--no-hashes", action="store_true", help="skip per-file SHA-256 checks"
    )
    nup_verify.set_defaults(func=cmd_nup_verify)

    nup_merge = nup_sub.add_parser(
        "merge",
        help="re-bundle an existing .nup with extra folders or .nup files",
        description="Merge an existing .nup with folder and/or .nup inputs. "
        "Packages inherited from the existing container keep their stored "
        "metadata, and an older update or add-on package for the same title is "
        "replaced by the newer input.",
    )
    nup_merge.add_argument("existing", help="existing .nup file to start from")
    nup_merge.add_argument(
        "inputs", nargs="*", help="folder(s) and/or .nup file(s) to merge in"
    )
    nup_merge.add_argument("-o", "--out", required=True, help="output .nup path")
    nup_merge.add_argument(
        "--compression", choices=("raw", "zstd", "auto"), default="raw"
    )
    nup_merge.add_argument(
        "--source-name", help="producer label stored in the container meta"
    )
    nup_merge.add_argument(
        "--dry-run", action="store_true", help="plan only; do not write the file"
    )
    nup_merge.set_defaults(func=cmd_nup_merge)

    g = sub.add_parser("gui", help="start the desktop UI")
    g.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SakError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
