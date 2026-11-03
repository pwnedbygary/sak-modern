from __future__ import annotations

import os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MEDIA_SIZE = 0x200
NCA_HEADER_SIZE = 0xC00


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return dec.update(data) + dec.finalize()


def aes_ecb_encrypt(key: bytes, data: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def xts_crypt(key: bytes, data: bytes, *, encrypt: bool, sector_size: int = MEDIA_SIZE,
              big_endian_tweak: bool = True) -> bytes:
    """Nintendo NCA header XTS: each media sector uses its sector index as the tweak."""
    if len(key) != 32:
        raise ValueError("AES-XTS key must be 32 bytes")
    if len(data) % sector_size:
        raise ValueError("XTS data length must be a multiple of the sector size")
    out = bytearray()
    for index in range(len(data) // sector_size):
        tweak = index.to_bytes(16, "big" if big_endian_tweak else "little")
        block = data[index * sector_size:(index + 1) * sector_size]
        cipher = Cipher(algorithms.AES(key), modes.XTS(tweak))
        ctx = cipher.encryptor() if encrypt else cipher.decryptor()
        out += ctx.update(block) + ctx.finalize()
    return bytes(out)


def decrypt_nca_header(header_key: bytes, encrypted: bytes) -> bytes:
    return xts_crypt(header_key, encrypted, encrypt=False)


def encrypt_nca_header(header_key: bytes, plain: bytes) -> bytes:
    return xts_crypt(header_key, plain, encrypt=True)


def encrypt_title_key(key_area_key: bytes, title_key: bytes) -> bytes:
    return aes_ecb_encrypt(key_area_key, title_key)


def decrypt_title_key(title_kek: bytes, encrypted_title_key: bytes) -> bytes:
    return aes_ecb_decrypt(title_kek, encrypted_title_key)


def sha256(data: bytes) -> bytes:
    import hashlib
    return hashlib.sha256(data).digest()


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
