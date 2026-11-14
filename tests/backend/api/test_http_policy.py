"""Offline checks of the production ADO transport policy."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from plumbline.backend.api import ado_client
from plumbline.backend.api.builds_client import BuildsClient
from plumbline.backend.api.errors import ADOPermanentError, ADOTransientError
from plumbline.backend.api.policies_client import PoliciesClient


URL = 'https://dev.azure.com/test-org/project/_apis/test'


def client_with_session(session):
    client = object.__new__(ado_client.AzureDevOpsClient)
    client.base_url = 'https://dev.azure.com/test-org'
    client._get_aiohttp_session = AsyncMock(return_value=session)
    client.stats = {'pages': 0, 'builds': 0}
    client.logger = MagicMock()
    return client


def response(status, data=None, retry_after=None):
    result = MagicMock()
    result.status = status
    result.headers = {}
    result.json = AsyncMock(return_value=data if data is not None else {'value': []})
    result.__aenter__ = AsyncMock(return_value=result)
    result.__aexit__ = AsyncMock(return_value=False)
    if status >= 400:
        headers = {'Retry-After': retry_after} if retry_after is not None else {}
        result.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(real_url=URL), (), status=status, message='synthetic', headers=headers
        )
    return result


@pytest.mark.asyncio
async def test_retry_budget_clamps_delay_and_retains_last_429(monkeypatch):
    now = [0.0]
    sleeps = []
    monkeypatch.setattr(ado_client, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(ado_client, '_REQUEST_BUDGET_SECONDS', 5.0)
    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
    monkeypatch.setattr(ado_client.asyncio, 'sleep', sleep)
    session = MagicMock()
    session.get.return_value = response(429, retry_after='3')
    with pytest.raises(ADOTransientError) as error:
        await client_with_session(session)._request_json(URL, session)
    assert sleeps == [3, 2]
    assert session.get.call_count == 2
    assert error.value.status == 429
    assert error.value.__cause__.status == 429


@pytest.mark.asyncio
async def test_retry_after_http_date(monkeypatch):
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    monkeypatch.setattr(ado_client.asyncio, 'sleep', sleep)
    session = MagicMock()
    date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5), usegmt=True)
    session.get.side_effect = [response(429, retry_after=date), response(200, {'value': [{'id': 42}]})]
    data, _ = await client_with_session(session)._request_json(URL, session)
    assert data['value'][0]['id'] == 42
    assert 3 <= sleeps[0] <= 5


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['projects', 'coverage', 'tests', 'policies'])
async def test_401_stops_and_survives_endpoint_wrappers(endpoint):
    session = MagicMock()
    session.get.return_value = response(401)
    client = client_with_session(session)
    calls = {
        'projects': client.get_projects,
        'coverage': lambda: BuildsClient(client).get_code_coverage('project', 42),
        'tests': lambda: BuildsClient(client).get_test_results('project', 42),
        'policies': lambda: PoliciesClient(client).get_policy_configurations('project'),
    }
    with pytest.raises(ADOPermanentError) as error:
        await calls[endpoint]()
    assert error.value.status == 401
    assert error.value.__cause__.status == 401
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_bad_json_does_not_retry():
    session = MagicMock()
    result = response(200)
    result.json.side_effect = json.JSONDecodeError('bad', 'x', 0)
    session.get.return_value = result
    with pytest.raises(ADOPermanentError) as error:
        await client_with_session(session)._request_json(URL, session)
    assert isinstance(error.value.__cause__, json.JSONDecodeError)
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_cancellation_exits_response_context():
    entered = asyncio.Event()
    result = response(200)
    async def pending_json():
        entered.set()
        await asyncio.Event().wait()
    result.json.side_effect = pending_json
    session = MagicMock()
    session.get.return_value = result
    task = asyncio.create_task(client_with_session(session)._request_json(URL, session))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result.__aexit__.assert_awaited_once()
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_budget_cancels_a_stalled_response(monkeypatch):
    monkeypatch.setattr(ado_client, '_REQUEST_BUDGET_SECONDS', 0.01)
    result = response(200)
    async def pending_json():
        await asyncio.Event().wait()
    result.json.side_effect = pending_json
    session = MagicMock()
    session.get.return_value = result
    with pytest.raises(ADOTransientError) as error:
        await client_with_session(session)._request_json(URL, session)
    assert isinstance(error.value.__cause__, TimeoutError)
    result.__aexit__.assert_awaited_once()
    assert session.get.call_count == 1


@pytest.mark.asyncio
async def test_build_enrichment_keeps_success_when_other_request_fails():
    session = MagicMock()
    session.get.side_effect = [
        response(200, {'count': 1, 'value': [{'id': 42}]}),
        response(401),
        response(200, {'count': 1, 'value': [{'id': 'commit-1'}]}),
    ]
    result = await BuildsClient(client_with_session(session)).get_builds('project')
    assert result[0]['timeline'] == {}
    assert result[0]['changes'] == [{'id': 'commit-1'}]
    assert 'timeline' in result[0]['enrichment_failures']
    assert session.get.call_count == 3
