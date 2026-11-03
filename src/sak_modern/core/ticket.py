from __future__ import annotations

import struct
from dataclasses import dataclass

from .crypto import decrypt_title_key
from .errors import FormatError, KeyError
from .keys import KeyStore

SIG_SIZES = {
    0x010000: 0x200,
    0x010001: 0x100,
    0x010002: 0x3C,
    0x010003: 0x200,
    0x010004: 0x100,
    0x010005: 0x3C,
}


@dataclass
class Ticket:
    raw: bytes
    sig_type: int
    body_offset: int
    encrypted_title_key: bytes
    rights_id: bytes
    title_key_type: int

    @classmethod
    def parse(cls, data: bytes) -> "Ticket":
        if len(data) < 0x2C0:
            raise FormatError("Ticket is too short")
        sig_type = struct.unpack_from("<I", data, 0)[0]
        sig_size = SIG_SIZES.get(sig_type)
        if sig_size is None:
            raise FormatError(f"Unknown ticket signature type 0x{sig_type:06X}")
        body_offset = (4 + sig_size + 0x3F) & ~0x3F
        if len(data) < body_offset + 0x174:
            raise FormatError("Ticket body is truncated")
        body = data[body_offset:]
        return cls(
            raw=data,
            sig_type=sig_type,
            body_offset=body_offset,
            encrypted_title_key=body[0x40:0x50],
            rights_id=body[0x160:0x170],
            title_key_type=body[0x141],
        )

    def title_key(self, ks: KeyStore, key_generation: int) -> bytes:
        revision = max(key_generation - 1, 0)
        return decrypt_title_key(ks.title_kek(revision), self.encrypted_title_key)


def ticket_map(tickets: list[Ticket]) -> dict[bytes, Ticket]:
    return {t.rights_id: t for t in tickets}
