"""Register the bootstrap command without coupling to the main CLI."""
import secrets
import typer
from plumbline.auth.service import bootstrap
from plumbline.config.settings import Settings
from plumbline.persistence.models import session_for


def register(app):
    @app.command("bootstrap-admin")
    def bootstrap_admin(username: str = typer.Option(..., prompt=True)):
        """Create the first administrator and print a one-time generated password."""
        password = secrets.token_urlsafe(24)
        try:
            with session_for(Settings().database_url) as db:
                bootstrap(db, username, password)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"One-time password: {password}")
        typer.echo("Change this password at first sign-in.")
