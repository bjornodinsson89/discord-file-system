"""Security utilities for encryption."""

from cryptography.fernet import Fernet
from typing import Optional
import config


class SecurityManager:
    """Manages encryption/decryption of sensitive data."""

    def __init__(self, fernet_key: str):
        self._fernet = Fernet(fernet_key.encode())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, encrypted: str) -> str:
        """Decrypt an encrypted string."""
        return self._fernet.decrypt(encrypted.encode()).decode()

    def encrypt_api_key(self, api_key: str) -> str:
        """Encrypt an API key (alias for encrypt)."""
        return self.encrypt(api_key)

    def decrypt_api_key(self, encrypted_key: str) -> str:
        """Decrypt an API key (alias for decrypt)."""
        return self.decrypt(encrypted_key)

    @staticmethod
    def generate_fernet_key() -> str:
        """Generate a new Fernet key for configuration."""
        return Fernet.generate_key().decode()


_security_manager: Optional[SecurityManager] = None


async def init_security() -> SecurityManager:
    """Initialize the security manager singleton."""
    global _security_manager
    _security_manager = SecurityManager(config.FERNET_KEY)
    return _security_manager


def get_security_manager() -> SecurityManager:
    """Get the security manager singleton."""
    if _security_manager is None:
        raise RuntimeError("Security not initialized. Call init_security() first.")
    return _security_manager
