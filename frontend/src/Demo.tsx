import { useState } from 'react';
import { Button, Select } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { ApiError, list, request } from './api';
import type { components } from './api.generated';

type DemoProvider = components['schemas']['DemoProvider'];

function label(value: string) {
  if (value === 'check_runs_completed_in_window') return 'IDs of check runs completed in window';
  const words = value.replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function displayFact(value: unknown): string {
  if (value === null) return 'Unavailable';
  if (Array.isArray(value)) return value.length ? value.map(displayFact).join(', ') : 'None';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function Demo() {
  const [selected, setSelected] = useState<string | null>(null);
  const demo = useQuery({
    queryKey: ['synthetic-provider-demo'],
    queryFn: async ({ signal }) => {
      const result = await request('/v1/demo/providers', { signal });
      if (!result || typeof result !== 'object' || !('providers' in result)) {
        throw new ApiError('Invalid provider demo response.');
      }
      return list<DemoProvider>(result.providers);
    },
  });
  const provider = demo.data?.find(item => item.id === selected) || demo.data?.[0];

  return (
    <main id="main">
      <div className="breadcrumbs">Plumbline / Synthetic demo</div>
      <div className="page-heading">
        <div>
          <h1>Synthetic provider demo</h1>
          <p>Explore fictional API responses and the evidence each sample supplies.</p>
        </div>
      </div>
      {demo.isPending && <p className="message" role="status">Loading provider packs…</p>}
      {demo.isError && (
        <div className="message" role="alert">
          <p>Could not load the provider packs. {demo.error.message}</p>
          <Button variant="outline" onClick={() => void demo.refetch()}>Try again</Button>
        </div>
      )}
      {demo.isSuccess && !provider && <p className="message">No provider packs available.</p>}
      {provider && (
        <>
          <div className="demo-picker">
            <Select
              label="Provider"
              value={provider.id}
              onChange={setSelected}
              data={demo.data?.map(item => ({ value: item.id, label: item.label }))}
              allowDeselect={false}
            />
            <p>API version <strong>{provider.api_version}</strong></p>
          </div>
          <div className="demo-evidence">
            <section className="message">
              <h2>Repositories</h2>
              <ul>
                {provider.repositories.map(repo => (
                  <li key={repo.id}>{repo.name} <span className="run-id">ID {repo.id}</span></li>
                ))}
              </ul>
              <h3>Evidence window (UTC)</h3>
              <p>{provider.window.start.slice(0, 10)} to {provider.window.end_exclusive.slice(0, 10)} (exclusive)</p>
              <p>Observed {provider.window.observed_at.slice(0, 10)}</p>
            </section>
            <section className="message">
              <h2>Sample facts</h2>
              <dl>
                {Object.entries(provider.facts).map(([key, value]) => (
                  <div key={key}>
                    <dt>{label(key)}</dt>
                    <dd>{typeof value === 'number' && key.endsWith('_percent') ? `${value}%` : displayFact(value)}</dd>
                  </div>
                ))}
              </dl>
            </section>
          </div>
          <section className="demo-responses">
            <h2>Source responses</h2>
            <div className="table-wrap" tabIndex={0} aria-label="Source responses">
              <table>
                <thead><tr><th>Response</th><th>Source</th><th>HTTP status</th></tr></thead>
                <tbody>
                  {provider.responses.map(response => (
                    <tr key={response.name}>
                      <td>{response.name}</td>
                      <td>{label(response.source)}</td>
                      <td>{response.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section>
            <h2>Evidence limits</h2>
            <ul>{provider.limitations.map(limit => <li key={limit}>{label(limit)}.</li>)}</ul>
          </section>
        </>
      )}
    </main>
  );
}
