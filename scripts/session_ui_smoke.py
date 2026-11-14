#!/usr/bin/env python3
"""Synthetic session journey through the actual API and built frontend."""
import secrets
import asyncio
from concurrent.futures import ThreadPoolExecutor
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from plumbline.api import main
from plumbline.auth.service import bootstrap
from plumbline.config.settings import Settings
from plumbline.persistence.migrations import migrate
from plumbline.persistence.models import session_for, init_engine_and_tables
from plumbline.jobs.worker import run_once


def run():
    temporary = Path(tempfile.mkdtemp(prefix='plumbline-session-ui-'))
    url = f"sqlite:///{temporary / 'test.db'}"
    migrate(url)
    initial = secrets.token_urlsafe(24)
    replacement = secrets.token_urlsafe(24)
    with session_for(url) as db:
        bootstrap(db, 'synthetic-admin', initial)
    main.app.dependency_overrides[main.get_settings] = lambda: Settings(database_url=url, run_env='ci')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    address = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(main.app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started, 'API did not start'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 1366, 'height': 768})
            violations = []
            page.on('pageerror', lambda error: print(f'Browser error: {error}'))
            page.set_default_timeout(10000)
            page.on('console', lambda message: violations.append(message.text) if 'Content Security Policy' in message.text else None)
            page.goto(address + '/ui/', wait_until='networkidle')
            page.get_by_role('textbox', name='Username').fill('synthetic-admin')
            page.get_by_label('Password', exact=False).fill(initial)
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.get_by_label('Current password', exact=False).fill(initial)
            page.get_by_label('New password', exact=False).fill(replacement)
            page.get_by_role('button', name='Change password', exact=True).click()
            page.get_by_role('textbox', name='Username').fill('synthetic-admin')
            page.get_by_label('Password', exact=False).fill(replacement)
            page.get_by_role('button', name='Sign in', exact=True).click()
            page.get_by_role('heading', name='Runs', exact=True).wait_for()
            page.get_by_role('link', name='Connections', exact=True).click()
            page.get_by_role('textbox', name='Provider', exact=True).click()
            page.get_by_role('option', name='Synthetic', exact=True).click()
            page.get_by_role('textbox', name='Name', exact=True).fill('Synthetic smoke')
            with page.expect_response(lambda response: response.url.endswith('/connections') and response.request.method == 'POST') as created:
                page.get_by_role('button', name='Add connection', exact=True).click()
            assert created.value.status == 201
            page.get_by_role('link', name='Runs', exact=True).click()
            page.get_by_role('textbox', name='Connection', exact=True).click()
            page.get_by_role('option', name='Synthetic smoke (synthetic)', exact=True).click()
            page.get_by_role('textbox', name='Project selection', exact=True).click()
            page.get_by_role('option', name='All projects', exact=True).click()
            page.get_by_label('Write report files', exact=True).uncheck()
            with page.expect_response(lambda response: response.url.endswith('/v1/runs') and response.request.method == 'POST') as queued:
                page.get_by_role('button', name='Start run', exact=True).click()
            assert queued.value.status == 202
            with ThreadPoolExecutor(max_workers=1) as executor:
                assert executor.submit(lambda: asyncio.run(run_once(init_engine_and_tables(url), Settings(database_url=url, run_env='ci')))).result(timeout=30)
            page.get_by_role('button', name='Refresh', exact=True).click()
            page.get_by_text('blocked_incomplete_collection', exact=False).wait_for()
            assert not violations, violations
            print('PASS real API + frontend: sign-in, forced change, saved synthetic connection, queue, worker, incomplete assessment, CSP')
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        main.app.dependency_overrides.clear()
        print(f'Temporary removal target: {temporary}')
        shutil.rmtree(temporary)


if __name__ == '__main__':
    run()
