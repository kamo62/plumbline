# Local operations during the Plumbline migration

The package and CLI are `plumbline`; application configuration uses `PLUMBLINE_`.
The default database is `sqlite:///plumbline.db`. Set `PLUMBLINE_DATABASE_URL` for
PostgreSQL. Do not point development checks at a database containing important data.

## First administrator and worker

```sh
plumbline migrate
plumbline bootstrap-admin --username YOUR_ADMIN_NAME_HERE
python -m uvicorn plumbline.api.main:app --host 127.0.0.1 --port 8000
# In a separate process using the same configuration:
plumbline worker
```

Bootstrap displays a generated password once. Sign in at `/ui/`, change the
password, then sign in again. Password changes revoke sessions and tokens.
Bootstrap refuses an existing account database. Migration refuses unsupported
legacy schemas without converting or deleting their contents. Back them up and
choose a fresh database; automatic legacy-data conversion is unavailable.

The API queues runs in the database. A worker must be running to collect them.
Worker lease renewal, bounded recovery and progress reporting remain under
integration; consult the current acceptance record before using sensitive sources.
Local/CI cookies permit HTTP by default; dev/prod cookies require HTTPS. Set
`PLUMBLINE_COOKIE_SECURE` explicitly when needed. Forwarded headers do not decide
cookie security. The application uses session CSRF protection and revocable bearer
tokens. The former shared API key no longer grants access.

## Connection keys and rotation

ADO credentials are encrypted using a Fernet key supplied through
`PLUMBLINE_ENCRYPTION_KEY`. Generate a key with the installed `cryptography` package
and place it directly into your protected service secret configuration. Never
commit the key, put it in a URL, or include it in screenshots or logs. Connection
responses omit encrypted and plaintext credentials.

Before rotating, stop API, worker and CLI writers. Back up the database and retain
a separately secured copy of its current key. Securely generate a new key, then:

```sh
plumbline rotate-connection-key --old-key-file /protected/old-key --new-key-file /protected/new-key
```

Alternatively supply `PLUMBLINE_OLD_ENCRYPTION_KEY` and
`PLUMBLINE_NEW_ENCRYPTION_KEY` through the environment. Rotation re-encrypts all
credentials in one transaction; any decryption failure rolls it back. After
success, update `PLUMBLINE_ENCRYPTION_KEY` and restart the services. Keep each
backup paired with its matching key. Lost keys cannot recover encrypted credentials;
restore the matching key or replace the credentials.

## Retention and evidence

The export cleanup script requires a migrated database and only removes expired,
owned terminal-run directories with no stored evidence. Active runs, unknown
ownership and stored snapshots are protected. Use `--dry-run` first. Until explicit
baseline expiry is implemented, protection is deliberately conservative.

SQLite and synthetic checks are local evidence. PostgreSQL locking, Docker images,
live ADO, Entra and production deployment require separate verification.
