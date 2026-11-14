"""Exercise the interim form without a live service or browser."""
import subprocess
from pathlib import Path


def test_multi_project_form_sends_one_request():
    app = Path(__file__).parents[2] / "src/plumbline/api/ui/app.js"
    script = r'''
const fs = require('fs'), vm = require('vm');
const elements = {};
const element = id => elements[id] ||= {value:'', checked:false, hidden:false, innerHTML:'', textContent:'', dataset:{}};
const requests = [];
global.document = {getElementById: element};
global.setInterval = () => {};
global.fetch = async (path, options = {}) => {
  requests.push({path, options});
  return {ok:true, json:async () => path === '/v1/runs' && options.method === 'POST' ? {id:'run-1'} : path.includes('runs?') ? [] : path.includes('projects') ? {items:[]} : path.includes('exemptions') ? {exemptions:{}} : {database:'none'}};
};
vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  element('api-key').value = 'synthetic-key';
  element('selected-projects').value = 'First\nSecond\nFirst';
  element('project').value = 'Third';
  await element('run-form').onsubmit({preventDefault(){}});
  const posts = requests.filter(r => r.path === '/v1/runs' && r.options.method === 'POST');
  const body = JSON.parse(posts[0].options.body);
  if (posts.length !== 1 || JSON.stringify(body.projects) !== '["First","Second","Third"]' || body.all_projects || posts[0].options.headers['X-API-Key'] !== 'synthetic-key') process.exit(1);
})().catch(() => process.exit(1));
'''
    subprocess.run(["node", "-e", script, str(app)], check=True)
