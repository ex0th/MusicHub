"""Verschlüsselung der OAuth-Tokens (Fernet: AES-128-CBC + HMAC-SHA256, mit Zeitstempel).

Tokens liegen nur verschlüsselt in der Datenbank. Der Schlüssel kommt ausschließlich aus
``MUSICHUB_TOKEN_ENCRYPTION_KEY``; mehrere Schlüssel (kommagetrennt) erlauben Rotation:
Der erste verschlüsselt, alle werden zum Entschlüsseln probiert.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from musichub.core.config import Settings


class TokenCipherError(Exception):
    """Schlüssel fehlt, ist ungültig oder passt nicht zum gespeicherten Wert."""


class TokenCipher:
    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise TokenCipherError("Kein Schlüssel für die Token-Verschlüsselung gesetzt.")
        try:
            self._fernet = MultiFernet([Fernet(k.strip().encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise TokenCipherError(
                "MUSICHUB_TOKEN_ENCRYPTION_KEY ist ungültig "
                "(erwartet: 32 Byte, URL-sicheres Base64)."
            ) from exc

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenCipher:
        raw = (
            settings.token_encryption_key.get_secret_value()
            if settings.token_encryption_key
            else ""
        )
        return cls([k for k in raw.split(",") if k.strip()])

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise TokenCipherError(
                "Gespeichertes Token lässt sich nicht entschlüsseln (Schlüssel geändert?)."
            ) from exc

    def encrypt_optional(self, value: str | None) -> str | None:
        return self.encrypt(value) if value else None

    def decrypt_optional(self, value: str | None) -> str | None:
        return self.decrypt(value) if value else None


def cipher_available(settings: Settings) -> bool:
    try:
        TokenCipher.from_settings(settings)
    except TokenCipherError:
        return False
    return True
