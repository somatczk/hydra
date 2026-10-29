import { describe, expect, it, afterEach } from 'vitest';
import { mount, type Mounted } from '@/test/pageHarness';
import {
  ProvenanceBadge,
  ProvenancePanel,
  provenanceMismatch,
  provenanceWarnings,
  type DataProvenance,
} from './DataProvenance';

const PROV: DataProvenance = {
  exchange: 'binance',
  symbol: 'BTCUSDT',
  timeframe: '1h',
  bar_count: 300,
  first_ts: '2026-01-01T00:00:00+00:00',
  last_ts: '2026-01-13T11:00:00+00:00',
  gap_count: 0,
  requested_start: '2026-01-01T00:00:00+00:00',
  requested_end: '2026-01-13T11:59:59.999999+00:00',
  data_source: 'db',
};

let page: Mounted | null = null;

afterEach(() => {
  page?.unmount();
  page = null;
});

describe('provenanceWarnings', () => {
  it('is empty for full, gap-free coverage', () => {
    expect(provenanceWarnings(PROV)).toEqual([]);
  });

  it('flags gaps and a range narrower than requested', () => {
    expect(
      provenanceWarnings({
        ...PROV,
        gap_count: 2,
        requested_start: '2025-12-01T00:00:00+00:00',
        requested_end: '2026-01-31T23:59:59.999999+00:00',
      }),
    ).toEqual([
      '2 gaps in the stored bars',
      'Data starts at 2026-01-01 00:00 UTC, after the requested start',
      'Data ends at 2026-01-13 11:00 UTC, before the requested end',
    ]);
  });
});

describe('provenanceMismatch', () => {
  it('names each differing field', () => {
    expect(provenanceMismatch(PROV, { ...PROV, exchange: 'kraken', timeframe: '4h' })).toEqual([
      'exchange: binance vs kraken',
      'timeframe: 1h vs 4h',
    ]);
  });

  it('ignores runs without provenance', () => {
    expect(provenanceMismatch(PROV, null)).toEqual([]);
  });
});

describe('ProvenanceBadge', () => {
  it('shows DB for results run on stored bars', async () => {
    page = await mount(<ProvenanceBadge provenance={PROV} />);
    const badge = page.container.querySelector('[data-state="db"]');
    expect(badge?.textContent).toBe('DB');
    expect(badge?.getAttribute('title')).toBe('300 stored bars, binance BTCUSDT 1h');
  });

  it('shows UNKNOWN when no provenance was recorded', async () => {
    page = await mount(<ProvenanceBadge provenance={null} />);
    expect(page.container.querySelector('[role="status"]')?.textContent).toBe('UNKNOWN');
  });
});

describe('ProvenancePanel', () => {
  it('lists the exact bars', async () => {
    page = await mount(<ProvenancePanel provenance={PROV} />);
    const text = page.container.textContent ?? '';
    expect(text).toContain('binance BTCUSDT 1h');
    expect(text).toContain('300');
    expect(text).toContain('2026-01-01 00:00 UTC');
    expect(text).toContain('2026-01-13 11:00 UTC');
  });

  it('warns loudly when provenance is missing', async () => {
    page = await mount(<ProvenancePanel provenance={undefined} />);
    expect(page.container.textContent).toContain('No data provenance recorded');
  });

  it('renders Hydra and Config rows only when the job carried them', async () => {
    page = await mount(
      <ProvenancePanel provenance={{ ...PROV, hydra_version: '2.4.0', config_hash: 'aabbccddeeff0099' }} />,
    );
    const text = page.container.textContent ?? '';
    expect(text).toContain('Hydra 2.4.0');
    expect(text).toContain('Config aabbccddeeff…');
    const version = Array.from(page.container.querySelectorAll('span[title]')).find((s) =>
      s.textContent?.startsWith('Hydra '),
    );
    expect(version?.getAttribute('title')).toBe('2.4.0');
    const hash = Array.from(page.container.querySelectorAll('span[title]')).find((s) =>
      s.textContent?.startsWith('Config '),
    );
    expect(hash?.getAttribute('title')).toBe('aabbccddeeff0099');
  });

  it('names each model version the job served, its stage and whether it was pinned', async () => {
    page = await mount(
      <ProvenancePanel
        provenance={{
          ...PROV,
          ml_models: [
            { id: 'id-alpha-2', name: 'alpha', version: 2, stage: 'staging', pinned: true },
            { id: 'id-gate-5', name: 'gate', version: 5, stage: 'production', pinned: false },
          ],
        }}
      />,
    );
    const rows = Array.from(page.container.querySelectorAll('ul[aria-label="Models served"] li'));
    expect(rows.map((r) => r.textContent)).toEqual([
      'Model alpha v2StagingPinned for this run',
      'Model gate v5Production',
    ]);
    expect(rows[0].getAttribute('title')).toBe('id-alpha-2');
  });

  it('lists no models for a run that served none', async () => {
    page = await mount(<ProvenancePanel provenance={{ ...PROV, ml_models: [] }} />);
    expect(page.container.querySelector('ul[aria-label="Models served"]')).toBeNull();
  });

  it('hides both rows when neither field is present, never an em dash', async () => {
    page = await mount(<ProvenancePanel provenance={PROV} />);
    const text = page.container.textContent ?? '';
    expect(text).not.toContain('Hydra');
    expect(text).not.toContain('Config');

    page?.unmount();
    page = await mount(
      <ProvenancePanel provenance={{ ...PROV, hydra_version: null, config_hash: null }} />,
    );
    expect(page.container.textContent).not.toContain('Hydra');
    expect(page.container.textContent).not.toContain('Config');
  });
});
