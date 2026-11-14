"""Register connection key rotation without exposing keys in arguments."""
import os
from pathlib import Path

import typer

from plumbline.config.settings import Settings
from plumbline.connections.service import CredentialError, rotate
from plumbline.persistence.models import session_for


def _key(env_name, path):
    if path:
        try:
            return Path(path).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise CredentialError("Cannot read key file") from None
    return os.environ.get(env_name, "")


def register(app: typer.Typer) -> None:
    @app.command("rotate-connection-key")
    def rotate_connection_key(
        old_key_file: str = typer.Option(None, "--old-key-file", help="File containing the old key"),
        new_key_file: str = typer.Option(None, "--new-key-file", help="File containing the new key"),
    ):
        """Rotate credentials using PLUMBLINE_OLD_ENCRYPTION_KEY and PLUMBLINE_NEW_ENCRYPTION_KEY or files."""
        try:
            old = _key("PLUMBLINE_OLD_ENCRYPTION_KEY", old_key_file)
            new = _key("PLUMBLINE_NEW_ENCRYPTION_KEY", new_key_file)
            with session_for(Settings().database_url) as db:
                count = rotate(db, old, new)
        except CredentialError:
            typer.echo("Key rotation failed; no connections changed.", err=True)
            raise typer.Exit(1) from None
        typer.echo(f"Rotated credentials for {count} connection(s).")
