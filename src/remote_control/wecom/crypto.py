"""WeCom message encryption/decryption and signature verification.

Implements the WeCom callback encryption protocol:
- Signature verification: SHA1(sort(token, timestamp, nonce, encrypt))
- Message decryption: AES-CBC with PKCS#7 padding
- Message encryption: AES-CBC with PKCS#7 padding + random prefix
"""

import base64
import hashlib

import struct
import xml.etree.ElementTree as ET
from typing import NamedTuple

from Crypto.Cipher import AES


class DecryptedMessage(NamedTuple):
    content: str
    corp_id: str


def verify_signature(token: str, timestamp: str, nonce: str, encrypt: str, signature: str) -> bool:
    """Verify WeCom callback signature."""
    parts = sorted([token, timestamp, nonce, encrypt])
    computed = hashlib.sha1("".join(parts).encode()).hexdigest()
    return computed == signature


def decrypt_message(encoding_aes_key: str, encrypted: str) -> DecryptedMessage:
    """Decrypt a WeCom encrypted message.

    The AES key is derived from EncodingAESKey (base64-decoded, 32 bytes).
    IV is the first 16 bytes of the AES key.
    Decrypted layout: 16 bytes random + 4 bytes msg_len (big-endian) + msg + corp_id
    """
    aes_key = base64.b64decode(encoding_aes_key + "=")
    iv = aes_key[:16]
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(base64.b64decode(encrypted))
    # Remove PKCS#7 padding
    pad_len = decrypted[-1]
    decrypted = decrypted[:-pad_len]
    # Parse: 16 random bytes + 4 bytes length + content + corp_id
    msg_len = struct.unpack(">I", decrypted[16:20])[0]
    content = decrypted[20 : 20 + msg_len].decode("utf-8")
    corp_id = decrypted[20 + msg_len :].decode("utf-8")
    return DecryptedMessage(content=content, corp_id=corp_id)


def encrypt_message(encoding_aes_key: str, corp_id: str, content: str) -> str:
    """Encrypt a reply message for WeCom.

    Layout: 16 bytes random + 4 bytes msg_len (big-endian) + msg + corp_id + PKCS#7 padding
    """
    import os

    aes_key = base64.b64decode(encoding_aes_key + "=")
    iv = aes_key[:16]
    content_bytes = content.encode("utf-8")
    corp_id_bytes = corp_id.encode("utf-8")
    random_bytes = os.urandom(16)
    msg_len = struct.pack(">I", len(content_bytes))
    plaintext = random_bytes + msg_len + content_bytes + corp_id_bytes
    # PKCS#7 padding to AES block size (32 bytes for WeCom)
    block_size = 32
    pad_len = block_size - (len(plaintext) % block_size)
    plaintext += bytes([pad_len]) * pad_len
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(plaintext)
    return base64.b64encode(encrypted).decode("utf-8")


def parse_message_xml(xml_text: str) -> dict[str, str]:
    """Parse WeCom callback XML body, extracting key fields."""
    root = ET.fromstring(xml_text)
    result = {}
    for child in root:
        if child.text:
            result[child.tag] = child.text
    return result


def make_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Generate a WeCom callback signature."""
    parts = sorted([token, timestamp, nonce, encrypt])
    return hashlib.sha1("".join(parts).encode()).hexdigest()
