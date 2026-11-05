"""Read-only ringing signalling. Never sends connect, release or activate.
Wire format reconstructed from the vendor Android client: LE16 frames,
MD5 challenge response and PBKDF2-SHA1/AES-CBC after registration.
"""

import asyncio
import hashlib
import ipaddress
import struct
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class SignalCipher:
    def __init__(self, private_key, salt):
        derived = hashlib.pbkdf2_hmac("sha1", private_key.encode(), salt.encode(), 1024, 32)
        self.key, self.iv = derived[:16], derived[16:]

    def encode(self, data):
        pad = padding.PKCS7(128).padder()
        data = pad.update(data) + pad.finalize()
        enc = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return enc.update(data) + enc.finalize()

    def decode(self, data):
        dec = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        raw = dec.update(data) + dec.finalize()
        pad = padding.PKCS7(128).unpadder()
        return pad.update(raw) + pad.finalize()


async def public_address(host, port):
    if (
        not isinstance(host, str)
        or len(host) > 253
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise ValueError("Invalid signalling endpoint")
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=__import__("socket").SOCK_STREAM
    )
    for _, _, _, _, address in addresses:
        if ipaddress.ip_address(address[0]).is_global:
            return address[0]
    raise ValueError("Non-public signalling endpoint")


async def observe_call(host, port, push_token, access_key, adapter, call_id, on_end, on_ready):
    """Observe the caller hanging up; TCP closure alone is not a hang-up."""
    address = await public_address(host, port)
    async with asyncio.timeout(8):
        reader, writer = await asyncio.open_connection(address, port)
    cipher = None

    async def read():
        size = struct.unpack("<H", await reader.readexactly(2))[0]
        if not 0 < size <= 8192:
            raise ValueError("Invalid signalling frame")
        body = await reader.readexactly(size)
        return (cipher.decode(body) if cipher else body).decode().split()

    async def send(words):
        if words[0] not in ("register", "alert", "heartbeat"):
            raise ValueError("Read-only signalling command rejected")
        if any(
            not isinstance(w, str) or not w or len(w) > 4096 or any(c.isspace() for c in w)
            for w in words
        ):
            raise ValueError("Invalid signalling word")
        body = " ".join(words).encode()
        if cipher:
            body = cipher.encode(body)
        writer.write(struct.pack("<H", len(body)) + body)
        await writer.drain()

    try:
        async with asyncio.timeout(8):
            challenge = await read()
            if len(challenge) != 2 or challenge[0] != "authorize":
                raise ValueError("Invalid signalling challenge")
            salt = challenge[1]
            digest = hashlib.md5((salt + ":" + access_key).encode()).hexdigest()
            await send(["register", push_token, digest])
            reply = await read()
            if len(reply) < 2 or reply[0] != "registered" or reply[1] != push_token:
                raise ValueError("Signalling registration rejected")
            cipher = SignalCipher(access_key, salt)
            await send(["alert", adapter, call_id])
        while True:
            async with asyncio.timeout(30):
                words = await read()
            if not words:
                continue
            if words[0] == "alert" and words[1:3] == [adapter, call_id]:
                on_ready()
            elif words[0] == "release":
                # Some releases include adapter/call; reject mismatched releases.
                if len(words) >= 3 and words[1:3] != [adapter, call_id]:
                    continue
                on_end()
                return
            elif words[0] == "heartbeat":
                await send(words)
            # In particular, never answer a connect or send audio.
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
