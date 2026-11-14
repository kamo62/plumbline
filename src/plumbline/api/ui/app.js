'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const keyHeaders = () => $('api-key').value ? {'X-API-Key': $('api-key').value} : {};
async function request(path, options = {}) {
    const response = await fetch(path, {...options, headers: {...keyHeaders(), ...options.headers}});
    if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status})`); }
    return response;
}
async function download(path, name) {
    try { const response = await request(path); const url = URL.createObjectURL(await response.blob()); const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    catch (error) { $('artifacts').textContent = error.message; }
}
async function health() {
    try { const r = await request('/healthz'); const data = await r.json(); $('health').textContent = `Database: ${data.database}`; }
    catch (e) { $('health').textContent = `Health check: ${e.message}`; }
}
async function projects() {
    try { const data = await (await request('/v1/projects')).json(); $('projects').innerHTML = data.items.map(x => `<option value="${esc(x)}"></option>`).join(''); } catch { $('projects').innerHTML = ''; }
}
$('load-projects').onclick = async () => { try { const params = new URLSearchParams(); if ($('org').value.trim()) params.set('org', $('org').value.trim()); const data = await (await request(`/v1/projects/live?${params}`)).json(); $('projects').innerHTML = data.items.map(x => `<option value="${esc(x)}"></option>`).join(''); $('project-message').textContent = `Loaded ${data.items.length} projects.`; } catch (error) { $('project-message').textContent = error.message; } };
$('all-projects').onchange = () => { $('project-field').hidden = $('all-projects').checked; };
$('add-project').onclick = () => { const name = $('project').value.trim(); if (name) { $('selected-projects').value = [$('selected-projects').value.trim(), name].filter(Boolean).join('\n'); $('project').value = ''; } };
$('run-form').onsubmit = async e => {
    e.preventDefault(); const selected = [...new Set([...$('selected-projects').value.split('\n'), $('project').value].map(x => x.trim()).filter(Boolean))];
    if (!$('all-projects').checked && !selected.length) { $('run-message').textContent = 'Select at least one project.'; return; }
    const body = {org: $('org').value.trim() || null, all_projects: $('all-projects').checked, projects: $('all-projects').checked ? null : selected, run_label: $('run-label').value.trim() || `Collection ${new Date().toISOString()}`, write_files: $('write-files').checked, write_db: $('write-db').checked, db_strict: $('write-db').checked, include_maturity: $('maturity').checked};
    try { const data = await (await request('/v1/runs', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json(); $('run-message').textContent = `Run queued: ${data.id}`; await runs(); }
    catch (error) { $('run-message').textContent = error.message; }
};
const duration = run => { const seconds = run.duration_seconds ?? (run.status === 'running' && run.started_at ? Math.max(0, (Date.now() - Date.parse(run.started_at))/1000) : null); return seconds == null ? '—' : `${Math.floor(seconds/60)}m ${Math.floor(seconds%60)}s`; };
async function runs() {
    try { const data = await (await request('/v1/runs?limit=50')).json(); const filtered = data.filter(r => (!$('status-filter').value || r.status === $('status-filter').value) && (r.run_label || '').toLowerCase().includes($('search').value.toLowerCase()));
        $('runs').innerHTML = filtered.length ? `<table><thead><tr><th>Status</th><th>Label</th><th>Started</th><th>Duration</th><th>Actions</th></tr></thead><tbody>${filtered.map(r => `<tr><td>${esc(r.status)}${r.completeness?.complete === false ? " (partial evidence; assessment blocked)" : ""}</td><td>${esc(r.run_label || r.id)}</td><td>${esc(r.started_at ? new Date(r.started_at).toLocaleString() : '—')}</td><td>${duration(r)}</td><td><button data-action="files" data-id="${esc(r.id)}">Files</button> ${['running','queued'].includes(r.status) ? `<button data-action="cancel" data-id="${esc(r.id)}">Cancel</button>` : ''}</td></tr>`).join('')}</tbody></table>` : '<p>No runs found.</p>';
    } catch (e) { $('runs').textContent = e.message; }
}
$('runs').onclick = async e => { const button = e.target.closest('button[data-action]'); if (!button) return; const id = encodeURIComponent(button.dataset.id); try { if (button.dataset.action === 'cancel') { await request(`/v1/runs/${id}/cancel`, {method:'POST'}); await runs(); return; } const data = await (await request(`/v1/runs/${id}/artifacts`)).json(); $('artifacts').innerHTML = `<h3>Run files</h3><button data-url="/v1/runs/${id}/artifacts.zip" data-name="artifacts.zip">Download ZIP</button><ul>${data.files.map(f => `<li><button data-url="${esc(f.url)}" data-name="${esc(f.name.split('/').pop())}">${esc(f.name)}</button> (${f.size_bytes} bytes)</li>`).join('')}</ul>`; } catch (error) { $('artifacts').textContent = error.message; } };
$('artifacts').onclick = e => { const button = e.target.closest('button[data-url]'); if (button) download(button.dataset.url, button.dataset.name); };
$('refresh').onclick = runs; $('status-filter').onchange = runs; $('search').oninput = runs;
setInterval(() => { if ($('auto-refresh').checked) runs(); }, 10000);
let exemptionData = {};
async function exemptions() { try { exemptionData = (await (await request('/v1/exemptions')).json()).exemptions; $('exemptions').innerHTML = Object.keys(exemptionData).length ? `<ul>${Object.entries(exemptionData).map(([name, rule]) => `<li>${esc(name)}: ${esc(rule.reason)} <button data-edit="${esc(name)}">Edit</button> <button data-delete="${esc(name)}">Delete</button></li>`).join('')}</ul>` : '<p>No exemptions configured.</p>'; } catch (e) { $('exemptions').textContent = e.message; } }
$('ex-new').onclick = () => $('exemption-form').reset();
$('exemptions').onclick = async e => { const edit = e.target.dataset.edit, del = e.target.dataset.delete; if (edit) { const r = exemptionData[edit]; $('ex-project').value = edit; $('ex-metrics').value = r.exempt_metrics.join(', '); $('ex-dimensions').value = r.exempt_dimensions.join(', '); $('ex-owner').value = r.owner; $('ex-reason').value = r.reason; $('ex-effective').value = r.effective_date; $('ex-expiration').value = r.expiration_date || ''; } if (del && confirm(`Delete exemption for ${del}?`)) { try { await request(`/v1/exemptions/${encodeURIComponent(del)}`, {method:'DELETE'}); await exemptions(); } catch (error) { $('ex-message').textContent = error.message; } } };
$('exemption-form').onsubmit = async e => { e.preventDefault(); const split = value => value.split(',').map(x => x.trim()).filter(Boolean); const metrics = split($('ex-metrics').value), dimensions = split($('ex-dimensions').value); const old = exemptionData[$('ex-project').value] || {}; const repositories = old.repositories || []; const level = repositories.length ? 'mixed' : metrics.length && dimensions.length ? 'mixed' : dimensions.length ? 'dimension' : 'metric'; const body = {level, exempt_metrics: metrics, exempt_dimensions: dimensions, reason: $('ex-reason').value, owner: $('ex-owner').value, effective_date: $('ex-effective').value, expiration_date: $('ex-expiration').value || null}; try { await request(`/v1/exemptions/${encodeURIComponent($('ex-project').value)}`, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); $('ex-message').textContent = 'Exemption saved.'; await exemptions(); } catch (error) { $('ex-message').textContent = error.message; } };
health(); projects(); runs(); exemptions();
