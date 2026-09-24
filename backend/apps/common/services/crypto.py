from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipherError(ValueError):
    pass


def _default_key() -> str:
    from django.conf import settings

    return settings.TRACELENS_CREDENTIAL_KEY


class CredentialCipher:
    def __init__(self, key: str | bytes | None = None) -> None:
        raw_key = key or _default_key()
        if isinstance(raw_key, str):
            raw_key = raw_key.encode("ascii")
        self._fernet = Fernet(raw_key)

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise CredentialCipherError("凭据无法解密，请检查加密密钥是否发生变化。") from exc
