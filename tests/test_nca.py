from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sak_modern.core.crypto import decrypt_nca_header, encrypt_nca_header
from sak_modern.core.keys import KeyStore
from sak_modern.core.nca import parse_nca_header, rewrite_nca_for_xci
from sak_modern.core.ticket import Ticket


def fake_keystore():
    return KeyStore({
        "header_key": bytes(range(32)),
        "master_key_07": bytes([8]) * 16,
        "aes_kek_generation_source": bytes([1]) * 16,
        "key_area_key_application_source": bytes([2]) * 16,
        "aes_key_generation_source": bytes([3]) * 16,
        "titlekek_07": bytes([4]) * 16,
    })


def make_nca(keygen=8, rights=b"\x01" * 16):
    plain = bytearray(0xC00)
    plain[0x200:0x204] = b"NCA3"
    plain[0x204] = 0
    plain[0x205] = 0
    plain[0x206] = 2
    plain[0x207] = 0
    plain[0x208:0x210] = (0x100000).to_bytes(8, "little")
    plain[0x210:0x218] = (0x0100000000000000).to_bytes(8, "little")
    plain[0x220] = keygen
    plain[0x230:0x240] = rights
    return bytes(plain)


def test_rewrite_is_ticketless_and_preserves_size():
    ks = fake_keystore()
    plain = make_nca()
    enc = encrypt_nca_header(ks.require("header_key"), plain)
    header = parse_nca_header(enc, ks)
    assert header.has_rights
    new = rewrite_nca_for_xci(header, ks, b"\xAA" * 16)
    dec = decrypt_nca_header(ks.require("header_key"), new)
    assert dec[0x230:0x240] == b"\x00" * 16
    assert dec[0x204] == 0
    assert dec[0x300:0x310] == dec[0x310:0x320]
    assert dec[0x200:0x204] == b"NCA3"


def test_ticket_parse_offsets():
    t = bytearray(0x2C0)
    t[0:4] = (0x010004).to_bytes(4, "little")
    body = (4 + 0x100 + 0x3F) & ~0x3F
    t[body + 0x40:body + 0x50] = b"\x11" * 16
    t[body + 0x141] = 0
    t[body + 0x160:body + 0x170] = b"\x22" * 16
    ticket = Ticket.parse(bytes(t))
    assert ticket.encrypted_title_key == b"\x11" * 16
    assert ticket.rights_id == b"\x22" * 16
