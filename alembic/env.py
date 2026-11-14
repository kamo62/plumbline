"""The checkout's pre-Plumbline Alembic history cannot upgrade the new schema."""
raise RuntimeError(
    "Legacy Alembic migrations are unsupported. Use 'plumbline migrate' for a fresh "
    "database. Back up legacy data and migrate it separately; it is never reset automatically."
)
