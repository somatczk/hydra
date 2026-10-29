import { act } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, routeFetch, flush, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();
const toast = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast }) }));

const { default: ModelsPage } = await import('./page');

function envelope(status: number, code: string, message: string): Response {
  return new Response(JSON.stringify({ detail: { code, message, state: 'invalid' } }), { status });
}

let page: Mounted | null = null;
let uploads: Array<{ url: string; init: RequestInit }>;

beforeEach(() => {
  uploads = [];
  toast.mockReset();
  fetchApi.mockReset();
  fetchApi.mockImplementation(routeFetch({ '/api/models': [] }, []));
});

afterEach(() => {
  page?.unmount();
  page = null;
  vi.unstubAllGlobals();
});

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
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

describe('ModelsPage upload', () => {
  it('sends the CSRF header with the multipart upload', async () => {
    stubUpload(new Response('{}', { status: 201 }));
    page = await mount(<ModelsPage />);
    await chooseFile(page.container);

    expect(uploads).toHaveLength(1);
    expect(uploads[0].url).toBe('/api/models/upload');
    expect(uploads[0].init.headers).toEqual({ 'x-hydra-request': '1' });
    expect(toast).toHaveBeenCalledWith('success', 'Model "alpha.onnx" uploaded');
  });

  it('asks before replacing an existing model, then retries with replace=true', async () => {
    const ask = vi.fn(() => true);
    vi.stubGlobal('confirm', ask);
    stubUpload(envelope(409, 'model_exists', 'exists'), new Response('{}', { status: 201 }));
    page = await mount(<ModelsPage />);
    await chooseFile(page.container);

    expect(ask).toHaveBeenCalledTimes(1);
    expect(uploads.map((u) => u.url)).toEqual([
      '/api/models/upload',
      '/api/models/upload?replace=true',
    ]);
  });

  it('keeps the existing model when the replace is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    stubUpload(envelope(409, 'model_exists', 'exists'));
    page = await mount(<ModelsPage />);
    await chooseFile(page.container);

    expect(uploads).toHaveLength(1);
  });

  it('shows the server message, never [object Object]', async () => {
    stubUpload(envelope(413, 'model_too_large', 'Model files are limited to 200 MiB'));
    page = await mount(<ModelsPage />);
    await chooseFile(page.container);

    expect(toast).toHaveBeenCalledWith('error', 'Model files are limited to 200 MiB');
    expect(JSON.stringify(toast.mock.calls)).not.toContain('[object Object]');
  });
});

describe('ModelsPage banner', () => {
  it('says models are not used for trading and has one h1', async () => {
    page = await mount(<ModelsPage />);

    expect(page.container.querySelectorAll('h1')).toHaveLength(1);
    expect(page.container.querySelector('[role="note"]')?.textContent).toBe(
      "Models aren't used for trading until a later release. Promoting a model here has no effect on sessions.",
    );
  });
});

describe('ModelsPage registry table', () => {
  it('renders the Last Trained column from the served trained_at', async () => {
    fetchApi.mockImplementation(
      routeFetch(
        {
          '/api/models': [
            {
              id: '00000000-0000-0000-0000-000000000001',
              name: 'alpha',
              version: '1',
              stage: 'Staging',
              metrics: { accuracy: null, precision: null },
              drift: null,
              drift_status: 'unknown',
              trained_at: '2026-10-28T12:00:00+00:00',
              file_size: 2048,
              file_name: 'abc123.onnx',
            },
          ],
        },
        [],
      ),
    );
    page = await mount(<ModelsPage />);
    await flush();

    expect(page.container.textContent).toContain('Last Trained');
    expect(page.container.textContent).toContain('2026-10-28T12:00:00+00:00');
  });
});
