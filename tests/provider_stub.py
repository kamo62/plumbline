"""Inject controlled collection outcomes at the provider boundary in runner tests."""
from datetime import datetime, timezone

from plumbline.providers.contract import Evidence, EvidenceState, Identity, Record, Snapshot


def inject(monkeypatch, collect):
    from plumbline import providers
    from plumbline.collection import projection

    class Provider:
        def __init__(self, client, connection, **kwargs):
            self.connection = connection

        async def collect(self, projects, window):
            raw = await collect(projects, window)
            now = datetime.now(timezone.utc)
            field = Evidence({'raw': raw, 'id': 'p', 'name': 'test'}, EvidenceState.OBSERVED, 'test', now, fresh_as_of=True)
            return Snapshot(self.connection, window, frozenset(),
                            projects=[Record(Identity(self.connection, 'p'), {'project': field})])

    monkeypatch.setattr(providers, 'ADOProvider', Provider)
    monkeypatch.setattr(projection, 'project_snapshot', lambda snapshot: snapshot.projects[0].fields['project'].value['raw'])
