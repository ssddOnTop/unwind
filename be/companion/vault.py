"""Private authenticated encryption. Ciphertext may join the authoritative transaction."""
import json
import os
import hashlib
import hmac
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class PrivateVault:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.key_path = data_dir / "plaid.key"
        self.pending_path = data_dir / "plaid-pending.vault"

    def _cipher(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.key_path.exists():
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
        if self.key_path.is_symlink() or self.key_path.stat().st_mode & 0o077:
            raise RuntimeError("Private vault key permissions are unsafe.")
        return Fernet(self.key_path.read_bytes())

    def digest(self, value: dict) -> str:
        self._cipher()
        return hmac.new(self.key_path.read_bytes(), json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()

    def seal(self, value: dict) -> str:
        return self._cipher().encrypt(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).decode()

    def open(self, ciphertext: str | None) -> dict:
        if not ciphertext:
            return {}
        if not self.key_path.exists():
            raise RuntimeError("Private vault key is missing.")
        try:
            return json.loads(self._cipher().decrypt(ciphertext.encode()))
        except (InvalidToken, ValueError):
            raise RuntimeError("Private vault cannot be opened.") from None

    def stage_exchange(self, value: dict) -> None:
        ciphertext = self.seal(value)
        temporary = self.pending_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(ciphertext)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.pending_path)

    def pending(self) -> dict:
        return self.open(self.pending_path.read_text()) if self.pending_path.exists() else {}

    def clear_pending(self):
        self.pending_path.unlink(missing_ok=True)
        self.pending_path.with_suffix(".tmp").unlink(missing_ok=True)

    def clear(self):
        self.clear_pending()
        self.key_path.unlink(missing_ok=True)
