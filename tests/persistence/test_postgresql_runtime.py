"""Runtime checks require the explicit disposable CI database, never the app URL."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from plumbline.api.models import CollectionConfig
from plumbline.config.settings import Settings
from plumbline.jobs import queue
from plumbline.persistence.migrations import HEAD, migrate
from plumbline.persistence.models import init_engine_and_tables


def test_postgresql_migrations_and_competing_claims():
    value = os.environ.get('PLUMBLINE_TEST_DATABASE_URL')
    if not value:
        pytest.skip('Disposable PostgreSQL runtime is unavailable; CI supplies it')
    target = make_url(value)
    assert target.get_backend_name() == 'postgresql'
    assert target.host in {'localhost', '127.0.0.1'} and target.database == 'plumbline_tests'
    schema = 'test_' + uuid4().hex
    control = create_engine(target)
    with control.begin() as connection:
        connection.execute(CreateSchema(schema))
    scoped = target.update_query_dict({'options': f'-csearch_path={schema}'})
    url = scoped.render_as_string(hide_password=False)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            assert list(pool.map(migrate, [url] * 3)) == [HEAD] * 3
        engine = init_engine_and_tables(url)
        run_id = queue.enqueue(engine, CollectionConfig(project='synthetic', write_files=False), Settings(database_url=url))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: queue.claim(engine), range(2)))
        owners = [claim for claim in claims if claim]
        assert len(owners) == 1 and owners[0][0] == run_id
        assert queue.finish(engine, run_id, owners[0][1], 'succeeded')
    finally:
        print(f'Disposable PostgreSQL schema removal target: {schema}')
        with control.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        control.dispose()
