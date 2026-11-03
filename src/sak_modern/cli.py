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
from .core.verify import verify_container


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def cmd_inspect(args) -> int:
    ks = KeyStore.load(args.keys)
    for path in args.files:
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
    bases, updates, dlcs = group_contents([load_container(p, ks) for p in args.files])
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
    ks = KeyStore.load(args.keys)
    failed = False
    for path in args.files:
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
