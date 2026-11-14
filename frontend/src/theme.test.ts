import { describe, expect, it } from 'vitest';
import { contrast, themes, validateTheme } from './theme';
import { ApiError, outcome, parseRuns, request } from './api';
import { vi } from 'vitest';
it('keeps recovered repository failures visible and tolerates older runs', () => {
  const [run, old] = parseRuns([{ id: 'new', attempt: 2, max_attempts: 3, recovery_reason: 'Lease expired', repositories: { 'project:repo': { project_name: 'Project', repository_name: 'Repository', status: 'failed', errors: [{ scope: 'coverage' }] }, malformed: null } }, { id: 'old' }]);
  expect(run.repositories).toEqual([{ project: 'Project', repository: 'Repository', status: 'failed', failures: 1 }]);
  expect([run.attempt, run.max_attempts, run.recovery_reason]).toEqual([2, 3, 'Lease expired']);
  expect(old.repositories).toEqual([]);
  expect(old.attempt).toBeNull();
});
describe('themes', () => { it('validates every built-in theme', () => { Object.values(themes).forEach(theme => expect(validateTheme(theme)).toEqual(theme)); }); it('rejects inaccessible accent and malformed tokens', () => { expect(() => validateTheme({ ...themes.light, tokens: { ...themes.light.tokens, accent: '#eeeeee' } })).toThrow(/contrast/); expect(() => validateTheme({ name: 'bad', tokens: {} })).toThrow(); expect(contrast('#ffffff','#000000')).toBe(21); }); });
describe('API responses', () => { it('preserves partial and unknown outcomes', () => { const rows = parseRuns([{ id:'1', completeness:{complete:false} }, { id:'2' }]); expect(rows.map(outcome)).toEqual(['Partial','Unknown']); expect(() => parseRuns({})).toThrow(ApiError); }); it('surfaces access errors', async () => { const fetcher = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({detail:'Invalid API key'}), {status:403})); await expect(request('/v1/runs')).rejects.toThrow('Invalid API key'); expect(fetcher.mock.calls[0][1]?.credentials).toBe('same-origin'); fetcher.mockRestore(); }); });
