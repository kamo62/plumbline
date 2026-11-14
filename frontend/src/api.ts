import type { paths } from './api.generated';
// The current OpenAPI response schema is unspecified; validate the actual response at the boundary.
type RunsOperation = paths['/v1/runs']['get'];
export type RunsResponse = RunsOperation['responses'][200]['content']['application/json'];
export type RepositoryProgress = { project: string; repository: string; status: string; failures: number };
export type Run = { repositories: RepositoryProgress[]; attempt: number | null; max_attempts: number | null; recovery_reason: string | null; id: string; status: string | null; started_at: string | null; finished_at: string | null; duration_seconds: number | null; run_label: string | null; completeness: { complete?: boolean } | null; assessment_status: string | null };
export class ApiError extends Error { constructor(message: string, public status?: number) { super(message); } }
export async function request(path: string, options: RequestInit = {}): Promise<unknown> {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers);
  if (options.body) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = document.cookie.split('; ').find(item => item.startsWith('plumbline_csrf='))?.slice('plumbline_csrf='.length);
    if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf));
  }
  let response: Response;
  try { response = await fetch(path, { ...options, headers, credentials: 'same-origin' }); }
  catch (error) { if (error instanceof DOMException && error.name === 'AbortError') throw error; throw new ApiError('Cannot reach the API. Check the service and try again.'); }
  if (!response.ok) {
    let detail = ''; try { const body: unknown = await response.json(); if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') detail = body.detail; } catch { /* no JSON error body */ }
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('plumbline-session-expired'));
    const prefix = response.status === 401 ? 'Sign-in required. ' : response.status === 403 ? 'Access denied. ' : '';
    throw new ApiError(prefix + (detail || `Request failed (HTTP ${response.status}).`), response.status);
  }
  try { return await response.json(); } catch { throw new ApiError('The API returned an invalid response.'); }
}
export type Role = 'viewer' | 'admin';
export type User = { id: string; username: string; role: Role; force_change: boolean };
export function parseUser(value: unknown): User {
  if (!value || typeof value !== 'object') throw new ApiError('Invalid account response.');
  const u = value as Record<string, unknown>;
  if (typeof u.id !== 'string' || typeof u.username !== 'string' || (u.role !== 'admin' && u.role !== 'viewer') || typeof u.force_change !== 'boolean') throw new ApiError('Invalid account response.');
  return { id: u.id, username: u.username, role: u.role, force_change: u.force_change };
}
export function list<T>(value: unknown): T[] { if (!Array.isArray(value)) throw new ApiError('Invalid list response.'); return value as T[]; }
export const json = (method: string, body?: unknown): RequestInit => ({ method, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export function parseRuns(value: unknown): Run[] {
  if (!Array.isArray(value)) throw new ApiError('The API returned an invalid runs list.');
  return value.map((row: unknown) => {
    if (!row || typeof row !== 'object' || !('id' in row) || typeof row.id !== 'string') throw new ApiError('The API returned an invalid run.');
    const r = row as Record<string, unknown>;
    const str = (v: unknown) => typeof v === 'string' ? v : null;
    const repositories: RepositoryProgress[] = [];
    if (r.repositories && typeof r.repositories === 'object' && !Array.isArray(r.repositories)) {
      for (const [key, item] of Object.entries(r.repositories)) {
        if (!item || typeof item !== 'object') continue;
        const progress = item as Record<string, unknown>;
        repositories.push({ project: str(progress.project_name) || str(progress.project_id) || 'Unknown project', repository: str(progress.repository_name) || str(progress.repository_id) || key, status: str(progress.status) || 'Unknown', failures: Array.isArray(progress.errors) ? progress.errors.length : 0 });
      }
    }
    return { repositories, attempt: typeof r.attempt === 'number' ? r.attempt : null, max_attempts: typeof r.max_attempts === 'number' ? r.max_attempts : null, recovery_reason: str(r.recovery_reason), id: r.id as string, status: str(r.status), started_at: str(r.started_at), finished_at: str(r.finished_at), duration_seconds: typeof r.duration_seconds === 'number' ? r.duration_seconds : null, run_label: str(r.run_label), completeness: r.completeness && typeof r.completeness === 'object' ? r.completeness as Run['completeness'] : null, assessment_status: str(r.assessment_status) };
  });
}
export function outcome(run: Run) { if (run.completeness?.complete === false) return 'Partial'; if (run.completeness?.complete === true) return 'Complete'; return 'Unknown'; }
