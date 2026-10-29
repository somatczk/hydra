import { act } from 'react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import {
  advance,
  buttonByText,
  click,
  dialogPolyfill,
  flush,
  inputByLabel,
  mount,
  routeFetch,
  typeInto,
  type Mounted,
  type Mutations,
  type Routes,
} from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));

const { default: ModelsPage } = await import('./page');

/** A rejected fetchApi answer: the thrown ApiError carries the shared envelope. */
function refusal(status: number, code: string, message: string): ApiError {
  return new ApiError(status, JSON.stringify({ detail: { code, message, state: 'invalid' } }));
}

/** A rejected raw-fetch answer (the upload path reads the body itself). */
function envelope(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ detail: { code, message, state: 'invalid' } }), { status });
}

const STRATEGIES = [
  { id: 'saw', name: 'Saw', primary_timeframe: '1h' },
  { id: 'momentum', name: 'Momentum', primary_timeframe: '1h' },
];

function model(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: '00000000-0000-0000-0000-000000000001',
    name: 'alpha',
    version: 2,
    stage: 'Staging',
    model_type: 'xgboost',
    metrics: {
      folds: 5,
      oos_accuracy: 0.512,
      oos_base_rate: 0.498,
      oos_mean_signed_return: 0.00042,
    },
    feature_spec: { spec_version: 3 },
    drift: 0.15,
    drift_status: 'low',
    artifact_file: 'abc123.onnx',
    artifact_sha256: '0'.repeat(64),
    trained_at: '2026-10-28T12:00:00+00:00',
    created_by: 'job:ml-abc12345',
    training_job_id: 'ml-abc12345',
    archived_at: null,
    file_size: 2048,
    file_name: 'abc123.onnx',
    ...over,
  };
}

let page: Mounted | null = null;
let uploads: Array<{ url: string; init: RequestInit }>;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeAll(dialogPolyfill);

beforeEach(() => {
  uploads = [];
  calls = [];
  toast.mockReset();
  fetchApi.mockReset();
  fetchApi.mockImplementation(routeFetch(
    { '/api/models': [model()], '/api/backtest/strategies': STRATEGIES },
    calls,
  ));
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function open(routes: Routes = {}, mutations: Mutations = {}): Promise<HTMLElement> {
  fetchApi.mockImplementation(routeFetch({
    '/api/models': [model()],
    '/api/backtest/strategies': STRATEGIES,
    ...routes,
  }, calls, mutations));
  page = await mount(<ModelsPage />);
  await flush();
  return page.container;
}

const dlg = (c: HTMLElement, title: string): HTMLElement => {
  const d = Array.from(c.querySelectorAll('dialog')).find((x) => x.querySelector('h2')?.textContent === title);
  if (!d) throw new Error(`no dialog ${title}`);
  return d;
};

const sent = (method: string, path: string) =>
  calls.filter((c) => c.path === path && c.init?.method === method).map((c) => JSON.parse(String(c.init?.body)));

function selectByLabel(container: HTMLElement, label: string): HTMLSelectElement {
  const el = Array.from(container.querySelectorAll('label')).find((l) => l.textContent === label);
  if (!el) throw new Error(`no label ${label}`);
  const sel = document.getElementById(el.htmlFor);
  if (!(sel instanceof HTMLSelectElement)) throw new Error(`no select for ${label}`);
  return sel;
}

async function choose(select: HTMLSelectElement, value: string): Promise<void> {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  await act(async () => {
    setter?.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

function stubUpload(...responses: Response[]): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit) => {
      uploads.push({ url, init });
      return responses.shift() ?? new Response('{}', { status: 201 });
    }),
  );
}

async function chooseFile(container: HTMLElement): Promise<void> {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File([new Uint8Array([1, 2, 3])], 'alpha.onnx');
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await flush();
}

describe('ModelsPage registry table', () => {
  it('renders the version list from the registry shapes', async () => {
    const c = await open();

    const text = c.textContent ?? '';
    expect(text).toContain('alpha');
    expect(text).toContain('v2');
    expect(text).toContain('Staging');
    expect(text).toContain('job:ml-abc12345');
    expect(text).toContain('ml-abc12345');
    expect(text).toContain('51.20%');
    expect(text).toContain('49.80%');
    expect(text).toContain('+0.04%');
    expect(text).toContain('0.150');
    expect(text).toContain('Low');
  });

  it('shows N/A where the honest fold metrics are absent, never a placeholder', async () => {
    const c = await open({
      '/api/models': [model({
        metrics: {},
        drift: null,
        drift_status: 'unknown',
        created_by: 'upload:dev',
        training_job_id: null,
      })],
    });

    const text = c.textContent ?? '';
    expect(text).toContain('N/A');
    expect(text).not.toContain('NaN');
    expect(text).not.toContain('undefined');
    expect(text).toContain('Unknown');
    expect(text).toContain('upload:dev');
    expect(text).not.toContain('job:');
  });

  it('colours the drift badge at the 0.2/0.25 bands the backend serves', async () => {
    const c = await open({
      '/api/models': [
        model({ id: '00000000-0000-0000-0000-000000000001', drift: 0.15, drift_status: 'low' }),
        model({ id: '00000000-0000-0000-0000-000000000002', name: 'beta', drift: 0.22, drift_status: 'moderate' }),
        model({ id: '00000000-0000-0000-0000-000000000003', name: 'gamma', drift: 0.31, drift_status: 'high' }),
        model({ id: '00000000-0000-0000-0000-000000000004', name: 'delta', drift: null, drift_status: 'unknown' }),
      ],
    });

    const text = c.textContent ?? '';
    expect(text).toContain('0.150');
    expect(text).toContain('Low');
    expect(text).toContain('0.220');
    expect(text).toContain('Moderate');
    expect(text).toContain('0.310');
    expect(text).toContain('High');
    expect(text).toContain('Unknown');
  });

  it('refuses to promote the version already in Production', async () => {
    const c = await open({ '/api/models': [model({ stage: 'Production' })] });

    const promote = buttonByText(c, 'Promote');
    expect(promote.disabled).toBe(true);
    expect(buttonByText(c, 'Rollback').disabled).toBe(false);
  });

  it('says models are consumed and promotion stays human, with one h1', async () => {
    const c = await open();

    expect(c.querySelectorAll('h1')).toHaveLength(1);
    const note = c.querySelector('[role="note"]')?.textContent ?? '';
    expect(note).toContain('Production models are consumed');
    expect(note).toContain('Promoting, rolling back and retraining are human actions');
    expect(note).toContain('pages you when it is high');
  });
});

describe('ModelsPage promote and rollback', () => {
  it('promotes only after PROMOTE is typed exactly and a reason is given, then sends both', async () => {
    const c = await open({}, { 'POST /api/models/00000000-0000-0000-0000-000000000001/promote': { version: 2 } });

    await click(buttonByText(c, 'Promote'));
    const d = dlg(c, 'Promote alpha v2 to Production?');
    const primary = buttonByText(d, 'Promote to Production');
    expect(primary.disabled).toBe(true);

    await typeInto(inputByLabel(d, 'Type PROMOTE to confirm'), 'promote');
    await typeInto(inputByLabel(d, 'Reason'), 'challenger won OOS');
    expect(primary.disabled).toBe(true);

    await typeInto(inputByLabel(d, 'Type PROMOTE to confirm'), 'PROMOTE');
    expect(primary.disabled).toBe(false);
    await click(primary);
    await flush();

    expect(sent('POST', '/api/models/00000000-0000-0000-0000-000000000001/promote'))
      .toEqual([{ reason: 'challenger won OOS' }]);
    expect(toast).toHaveBeenCalledWith('success', 'alpha v2 promoted to Production');
  });

  it('rolls back only after ROLLBACK is typed and a reason is given', async () => {
    const c = await open({}, {
      'POST /api/models/00000000-0000-0000-0000-000000000001/rollback': { restored_version: 1 },
    });

    await click(buttonByText(c, 'Rollback'));
    const d = dlg(c, 'Roll back alpha?');
    const primary = buttonByText(d, 'Roll back');

    await typeInto(inputByLabel(d, 'Type ROLLBACK to confirm'), 'ROLLBACK');
    await typeInto(inputByLabel(d, 'Reason'), 'v2 drifted');
    expect(primary.disabled).toBe(false);
    await click(primary);
    await flush();

    expect(sent('POST', '/api/models/00000000-0000-0000-0000-000000000001/rollback'))
      .toEqual([{ reason: 'v2 drifted' }]);
    expect(toast).toHaveBeenCalledWith('success', 'alpha rolled back');
  });

  it('shows the registry refusal inside the dialog and keeps it open', async () => {
    const c = await open({}, {
      'POST /api/models/00000000-0000-0000-0000-000000000001/rollback': refusal(
        409, 'rollback_refused', "model 'alpha' has no archived version to roll back to",
      ),
    });

    await click(buttonByText(c, 'Rollback'));
    const d = dlg(c, 'Roll back alpha?');
    await typeInto(inputByLabel(d, 'Type ROLLBACK to confirm'), 'ROLLBACK');
    await typeInto(inputByLabel(d, 'Reason'), 'v2 drifted');
    await click(buttonByText(d, 'Roll back'));
    await flush();

    expect(toast).not.toHaveBeenCalledWith('success', expect.anything());
    expect(d.textContent).toContain("model 'alpha' has no archived version to roll back to");
    expect(d.hasAttribute('open')).toBe(true);
  });
});

describe('ModelsPage retrain', () => {
  it('queues the ml_train job with the strategy and symbol, then surfaces its status', async () => {
    let polls = 0;
    const c = await open({
      '/api/models/retrain/ml-new12345': () => ({
        task_id: 'ml-new12345',
        status: polls === 0 ? 'running' : 'completed',
        progress: polls === 0 ? 40 : 100,
        error: null,
        result: polls === 0 ? null : { name: 'alpha', version: 3, stage: 'staging' },
        metrics: polls === 0 ? null : { oos_accuracy: 0.53, oos_base_rate: 0.5, oos_mean_signed_return: 0.0007 },
        provenance: null,
      }),
    }, {
      'POST /api/models/alpha/retrain': { task_id: 'ml-new12345', status: 'queued' },
    });

    await click(buttonByText(c, 'Retrain'));
    const d = dlg(c, 'Retrain alpha');
    expect(buttonByText(d, 'Queue retrain').disabled).toBe(true);

    await choose(selectByLabel(c, 'Strategy'), 'saw');
    await typeInto(inputByLabel(c, 'Symbol'), 'BTCUSDT');
    expect(buttonByText(d, 'Queue retrain').disabled).toBe(false);

    // fake timers before the submit: the poll interval must arm on the fake clock
    vi.useFakeTimers();
    await click(buttonByText(d, 'Queue retrain'));
    await flush();

    expect(sent('POST', '/api/models/alpha/retrain')).toEqual([
      { strategy_id: 'saw', symbol: 'BTCUSDT' },
    ]);
    expect(toast).toHaveBeenCalledWith('success', 'Retrain of alpha queued');

    // the status panel polls until the job is terminal
    expect(c.textContent).toContain('running');
    expect(c.textContent).toContain('ml-new12345');
    polls = 1;
    await advance(2000);

    expect(c.textContent).toContain('completed');
    expect(c.textContent).toContain('registered alpha v3');
    expect(c.textContent).toContain('53.00%');
  });

  it('surfaces the 422 of a symbol outside the universe inside the dialog', async () => {
    const c = await open({}, {
      'POST /api/models/alpha/retrain': refusal(
        422, 'symbol_not_in_universe', 'NOPEUSDT is not in the current top-100',
      ),
    });

    await click(buttonByText(c, 'Retrain'));
    await choose(selectByLabel(c, 'Strategy'), 'saw');
    await typeInto(inputByLabel(c, 'Symbol'), 'NOPEUSDT');
    await click(buttonByText(dlg(c, 'Retrain alpha'), 'Queue retrain'));
    await flush();

    const d = dlg(c, 'Retrain alpha');
    expect(d.textContent).toContain('NOPEUSDT is not in the current top-100');
    expect(d.hasAttribute('open')).toBe(true);
  });

  it('loads a version training job status when its job id is clicked', async () => {
    const c = await open({
      '/api/models/retrain/ml-abc12345': {
        task_id: 'ml-abc12345',
        status: 'completed',
        progress: 100,
        error: null,
        result: { name: 'alpha', version: 2, stage: 'staging' },
        metrics: { oos_accuracy: 0.512 },
        provenance: null,
      },
    });

    await click(c.querySelector('button[aria-label="Show the training job of alpha v2"]') as HTMLButtonElement);
    await flush();

    expect(c.textContent).toContain('Training job');
    expect(c.textContent).toContain('completed');
    expect(c.textContent).toContain('registered alpha v2');
  });
});

describe('ModelsPage upload', () => {
  it('sends the CSRF header with the multipart upload', async () => {
    stubUpload(new Response('{}', { status: 201 }));
    const c = await open();
    await chooseFile(c);

    expect(uploads).toHaveLength(1);
    expect(uploads[0].url).toBe('/api/models/upload');
    expect(uploads[0].init.headers).toEqual({ 'x-hydra-request': '1' });
    expect(toast).toHaveBeenCalledWith('success', 'Model "alpha.onnx" uploaded as the next Staging version');
  });

  it('shows the server message, never [object Object]', async () => {
    stubUpload(envelope(413, 'model_too_large', 'Model files are limited to 200 MiB'));
    const c = await open();
    await chooseFile(c);

    expect(toast).toHaveBeenCalledWith('error', 'Model files are limited to 200 MiB');
    expect(JSON.stringify(toast.mock.calls)).not.toContain('[object Object]');
  });
});
