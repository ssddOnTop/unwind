"""Explicit launcher-only secrets loading. Importing this module loads nothing."""
import os
from pathlib import Path
import stat


def load_runtime_environment(root: Path) -> None:
    path = root / ".env"
    if not path.exists():
        return
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("The runtime secrets file must be a regular mode-0600 file.")
    from dotenv import load_dotenv
    load_dotenv(path, override=False, verbose=False)
    # Tracing must never forward financial tokens or normalized private context.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
