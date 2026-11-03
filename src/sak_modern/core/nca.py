from __future__ import annotations

from dataclasses import dataclass

from .crypto import (
    NCA_HEADER_SIZE,
    decrypt_nca_header,
    encrypt_nca_header,
    encrypt_title_key,
)
from .errors import FormatError, KeyError
from .keys import KeyStore

CONTENT_TYPES = {
    0: "Program",
    1: "Meta",
    2: "Control",
    3: "Manual",
    4: "Data",
    5: "PublicData",
}


@dataclass
class NcaHeader:
    encrypted: bytes
    plain: bytes
    magic: str
    distribution_type: int
    content_type: int
    crypto_type: int
    key_index: int
    key_generation: int
    nca_size: int
    title_id: int
    rights_id: bytes
    key_area: bytes

    @property
    def content_type_name(self) -> str:
        return CONTENT_TYPES.get(self.content_type, f"Type{self.content_type}")

    @property
    def has_rights(self) -> bool:
        return any(self.rights_id)

    @property
    def title_id_hex(self) -> str:
        return f"{self.title_id:016X}"

    @property
    def rights_id_hex(self) -> str:
        return self.rights_id.hex()

    @property
    def is_update(self) -> bool:
        return (self.title_id & 0x800) != 0 and (self.title_id & 0xFFF) == 0x800

    @property
    def is_dlc(self) -> bool:
        low = self.title_id & 0xFFF
        return self.content_type == 5 or low not in (0x000, 0x800)

    @property
    def base_title_id(self) -> int:
        # Base, update (…A800), and DLC (…Bxxx) share the same upper 48 bits.
        return self.title_id & 0xFFFFFFFFFFFF0000


def parse_nca_header(encrypted: bytes, ks: KeyStore) -> NcaHeader:
    if len(encrypted) < NCA_HEADER_SIZE:
        raise FormatError("NCA header is truncated")
    plain = decrypt_nca_header(ks.require("header_key"), encrypted[:NCA_HEADER_SIZE])
    magic = plain[0x200:0x204]
    if magic not in (b"NCA3", b"NCA2", b"NCA0"):
        raise FormatError(f"Not a valid NCA header (magic {magic!r})")
    key_generation = max(plain[0x206], plain[0x220])
    return NcaHeader(
        encrypted=encrypted[:NCA_HEADER_SIZE],
        plain=plain,
        magic=magic.decode("ascii", "replace"),
        distribution_type=plain[0x204],
        content_type=plain[0x205],
        crypto_type=plain[0x206],
        key_index=plain[0x207],
        key_generation=key_generation,
        nca_size=int.from_bytes(plain[0x208:0x210], "little"),
        title_id=int.from_bytes(plain[0x210:0x218], "little"),
        rights_id=plain[0x230:0x240],
        key_area=plain[0x300:0x340],
    )


def rewrite_nca_for_xci(header: NcaHeader, ks: KeyStore, title_key: bytes | None) -> bytes:
    """Return a new encrypted 0xC00 header suitable for a ticketless secure partition.

    This mirrors the long-standing NSC_BUILDER/SAK behavior: the rights ID is
    cleared and the decrypted title key is wrapped into all four key-area slots.
    Section data is untouched, because it is encrypted with that same title key.
    """
    plain = bytearray(header.plain)
    if header.has_rights:
        if title_key is None:
            raise KeyError(
                f"NCA {header.title_id_hex} is title-key encrypted but no matching ticket was found."
            )
        revision = max(header.key_generation - 1, 0)
        kak = ks.key_area_key(revision, header.key_index)
        wrapped = encrypt_title_key(kak, title_key)
        plain[0x230:0x240] = b"\x00" * 16
        for slot in range(4):
            start = 0x300 + slot * 16
            plain[start:start + 16] = wrapped
    return encrypt_nca_header(ks.require("header_key"), bytes(plain))


def read_nca_header_at(fh, offset: int, ks: KeyStore) -> NcaHeader:
    fh.seek(offset)
    data = fh.read(NCA_HEADER_SIZE)
    if len(data) != NCA_HEADER_SIZE:
        raise FormatError(f"Short read at NCA offset {offset:#x}")
    return parse_nca_header(data, ks)
