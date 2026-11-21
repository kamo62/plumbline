#!/usr/bin/env python3
"""Offline synthetic browser smoke for the interim UI.

Run from any directory: .venv/bin/python scripts/phase0_ui_smoke.py
Requires Playwright and its Chromium browser in the current environment. All
browser requests are intercepted; no application server or live data is used.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / 'src/plumbline/api/ui'
SIZES = [(1440, 900), (1366, 768), (1280, 800)]
HOST = 'http://synthetic.invalid'


def run(output: Path | None = None):
    files = {name: (UI / name).read_bytes() for name in ('index.html', 'app.js', 'styles.css')}
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in SIZES:
            context = browser.new_context(viewport={'width': width, 'height': height},
                                          color_scheme='light', accept_downloads=True)
            page = context.new_page()
            posts = []
            unexpected = []

            def route_request(route):
                request = route.request
                parts = urlsplit(request.url)
                if parts.hostname != 'synthetic.invalid':
                    unexpected.append(request.url)
                    route.abort()
                    return
                path = parts.path
                name = path.rsplit('/', 1)[-1]
                if path in ('/', '/ui/'):
                    name = 'index.html'
                if name in files:
                    mime = {'index.html': 'text/html', 'app.js': 'text/javascript',
                            'styles.css': 'text/css'}[name]
                    route.fulfill(status=200, content_type=mime, body=files[name])
                    return
                status = 200
                data = {}
                if path == '/healthz':
                    data = {'database': 'synthetic, disconnected'}
                elif path in ('/v1/projects', '/v1/projects/live'):
                    data = {'items': ['Alpha', 'Beta']}
                elif path == '/v1/runs' and request.method == 'POST':
                    posts.append({'body': request.post_data_json,
                                  'key': request.headers.get('x-api-key')})
                    data = {'id': 'synthetic-run'}
                elif path == '/v1/runs':
                    data = [{'id': 'synthetic-run', 'status': 'failed',
                             'run_label': 'Synthetic partial run',
                             'started_at': '2026-01-01T10:00:00Z',
                             'duration_seconds': 42,
                             'completeness': {'complete': False}}]
                elif path == '/v1/exemptions' and request.method == 'GET':
                    data = {'exemptions': {}}
                elif path.startswith('/v1/exemptions/') and request.method == 'PUT':
                    status, data = 403, {'detail': 'Synthetic save denied'}
                elif path == '/v1/runs/synthetic-run/artifacts':
                    data = {'files': [{'name': 'synthetic.txt', 'size_bytes': 19,
                                       'url': '/v1/runs/synthetic-run/artifacts/synthetic.txt'}]}
                elif path == '/v1/runs/synthetic-run/artifacts/synthetic.txt':
                    route.fulfill(status=200, content_type='text/plain', body='synthetic evidence\n')
                    return
                else:
                    # Unknown application endpoints stay offline and visible to the UI.
                    status, data = 404, {'detail': 'Synthetic endpoint unavailable'}
                route.fulfill(status=status, content_type='application/json', body=json.dumps(data))

            page.route('**/*', route_request)
            page.goto(HOST + '/ui/', wait_until='networkidle')
            page.locator('#api-key').fill('synthetic-key')
            page.locator('#all-projects').uncheck()
            page.locator('#selected-projects').fill('Alpha\nBeta\nAlpha')
            page.locator('#run-form button[type=submit]').click()
            page.get_by_text('Run queued: synthetic-run').wait_for()
            assert len(posts) == 1 and posts[0]['body']['projects'] == ['Alpha', 'Beta']
            assert posts[0]['body']['all_projects'] is False and posts[0]['key'] == 'synthetic-key'
            page.get_by_text('partial evidence; assessment blocked', exact=False).wait_for()
            page.get_by_role('button', name='Files', exact=True).click()
            page.get_by_role('button', name='Download ZIP').wait_for()
            with page.expect_download() as downloaded:
                page.get_by_role('button', name='synthetic.txt').click()
            download = downloaded.value
            assert download.suggested_filename == 'synthetic.txt'
            assert Path(download.path()).read_text() == 'synthetic evidence\n'
            page.locator('#ex-project').fill('Alpha')
            page.locator('#ex-metrics').fill('coverage')
            page.locator('#ex-owner').fill('Synthetic owner')
            page.locator('#ex-reason').fill('Synthetic reason')
            page.locator('#ex-effective').fill('2026-01-01')
            page.get_by_role('button', name='Save exemption').click()
            page.get_by_text('Synthetic save denied').wait_for()
            # Traverse the actual browser focus order, including dynamic controls.
            page.locator('body').click(position={'x': 1, 'y': 1})
            page.evaluate('document.activeElement.blur()')
            visited = set()
            for _ in range(55):
                page.keyboard.press('Tab')
                visited.add(page.evaluate('document.activeElement.id || document.activeElement.textContent.trim()'))
            required = {'api-key', 'all-projects', 'selected-projects', 'Start collection',
                        'Files', 'Download ZIP', 'Save exemption'}
            assert required <= visited, f'keyboard controls not reached: {required - visited}'
            overflow = page.evaluate('document.documentElement.scrollWidth > document.documentElement.clientWidth')
            assert not overflow, f'page horizontal overflow at {width}x{height}'
            print(f'PASS current {width}x{height}: POST/header, partial, save error, download, tab, no page overflow')
            if output is not None:
                page.screenshot(path=str(output / f'after-synthetic-{width}x{height}.png'), full_page=True)
            assert not unexpected, f'Unexpected external requests: {unexpected}'
            context.close()
        browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path, help='Optional directory for current smoke screenshots.')
    run(parser.parse_args().screenshots)
