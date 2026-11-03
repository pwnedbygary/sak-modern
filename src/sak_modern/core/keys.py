from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import KeyError


def parse_keys_text(text: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().split()[0] if value.strip() else ""
        if not name or not value:
            continue
        try:
            out[name] = bytes.fromhex(value)
        except ValueError:
            continue
    return out


@dataclass
class KeyStore:
    values: dict[str, bytes]
    source: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None) -> "KeyStore":
        if path is None:
            for candidate in discover_key_files():
                try:
                    return cls.load(candidate)
                except KeyError:
                    continue
            raise KeyError(
                "No prod.keys/keys.txt found. Put prod.keys next to the app, in ~/.switch, "
                "or select it in Settings."
            )
        p = Path(path).expanduser()
        if not p.is_file():
            raise KeyError(f"Keys file not found: {p}")
        ks = cls(parse_keys_text(p.read_text(encoding="utf-8", errors="replace")), p)
        ks.require("header_key")
        return ks

    def require(self, name: str) -> bytes:
        val = self.values.get(name)
        if val is None:
            raise KeyError(f"Required key '{name}' is missing from {self.source or 'the keys file'}.")
        return val

    def get(self, name: str, default: bytes | None = None) -> bytes | None:
        return self.values.get(name, default)

    def has(self, name: str) -> bool:
        return name in self.values

    def master_key(self, revision: int) -> bytes:
        return self.require(f"master_key_{revision:02x}")

    def title_kek(self, revision: int) -> bytes:
        direct = self.values.get(f"titlekek_{revision:02x}")
        if direct is not None:
            return direct
        source = self.values.get("titlekek_source")
        if source is None:
            raise KeyError(f"titlekek_{revision:02x} and titlekek_source are both missing.")
        return aes_ecb_decrypt(self.master_key(revision), source)

    def key_area_key(self, revision: int, kind: int = 0) -> bytes:
        names = ("application", "ocean", "system")
        if kind not in range(3):
            raise KeyError(f"Unsupported key-area type {kind}.")
        direct = self.values.get(f"key_area_key_{names[kind]}_{revision:02x}")
        if direct is not None:
            return direct
        source = self.values.get(f"key_area_key_{names[kind]}_source")
        if source is None:
            raise KeyError(f"key_area_key_{names[kind]}_source is missing.")
        stage1 = aes_ecb_decrypt(self.master_key(revision), self.require("aes_kek_generation_source"))
        stage2 = aes_ecb_decrypt(stage1, source)
        return aes_ecb_decrypt(stage2, self.require("aes_key_generation_source"))


def discover_key_files() -> Iterable[Path]:
    seen: set[Path] = set()
    roots = [Path.cwd(), Path(__file__).resolve().parents[3], Path.home() / ".switch"]
    for root in roots:
        for name in ("prod.keys", "keys.txt", "keys.dat"):
            p = (root / name).resolve()
            if p not in seen and p.is_file():
                seen.add(p)
                yield p
    for p in (Path.home() / ".config" / "Ryujinx" / "system" / "prod.keys",
              Path.home() / ".local" / "share" / "yuzu" / "keys" / "prod.keys"):
        p = p.resolve()
        if p not in seen and p.is_file():
            seen.add(p)
            yield p


# Late import keeps the public keys module free of a hard crypto dependency at import time.
from .crypto import aes_ecb_decrypt  # noqa: E402
