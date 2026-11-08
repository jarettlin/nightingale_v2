"""Field-level encryption for message content.

The database stores ciphertext. Only this process holds the key, so a leaked
database dump, a Supabase dashboard user, or a misconfigured RLS policy still
can't read what patients wrote. Supabase also encrypts the disk; this layer
protects against people who can query the database, not just steal its disk.

Key: MESSAGE_ENCRYPTION_KEY in .env, generated with
    .venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os

from cryptography.fernet import Fernet, InvalidToken


class EncryptionUnavailable(Exception):
    pass


class Cipher:
    def __init__(self, key: str | bytes):
        self._fernet = Fernet(key)

    @classmethod
    def from_env(cls) -> "Cipher":
        key = os.environ.get("MESSAGE_ENCRYPTION_KEY")
        if not key:
            raise EncryptionUnavailable("MESSAGE_ENCRYPTION_KEY is not set")
        return cls(key)

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise EncryptionUnavailable("ciphertext could not be decrypted") from exc


def ephemeral_cipher() -> Cipher:
    """Fresh random key for tests and the demo."""
    return Cipher(Fernet.generate_key())
