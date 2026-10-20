import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, inputByLabel, mount, typeInto, type Mounted } from '@/test/pageHarness';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));

const { ExchangeConnectDialog } = await import('./ExchangeConnectDialog');

function envelope(status: number, code: string, message: string, state: string): ApiError {
  return new ApiError(status, JSON.stringify({ detail: { code, message, state } }));
}

let page: Mounted | null = null;

beforeEach(() => {
  fetchApi.mockReset();
});

afterEach(() => {
  page?.unmount();
  page = null;
});

async function submit(connected = false): Promise<Mounted> {
  const mounted = await mount(
    <ExchangeConnectDialog
      open
      onClose={vi.fn()}
      onUpdated={vi.fn()}
      exchange={{ id: 'binance', name: 'Binance', connected }}
    />,
  );
  if (!connected) {
    await typeInto(inputByLabel(mounted.container, 'API Key'), 'key');
    await typeInto(inputByLabel(mounted.container, 'API Secret'), 'secret');
    await click(buttonByText(mounted.container, 'Connect'));
  } else {
    await click(buttonByText(mounted.container, 'Disconnect'));
  }
  return mounted;
}

describe('ExchangeConnectDialog errors', () => {
  it.each([
    [422, 'credentials_rejected', 'Binance rejected these API keys (key, secret or permissions); nothing was stored', 'invalid'],
    [502, 'exchange_unreachable', 'Binance could not be reached to check the keys; nothing was stored', 'unavailable'],
    [503, 'database_unavailable', 'Database error while storing exchange credentials', 'unavailable'],
  ])('shows the %s %s message, never the raw JSON body', async (status, code, message, state) => {
    fetchApi.mockRejectedValue(envelope(status, code, message, state));
    page = await submit();

    expect(page.container.textContent).toContain(message);
    expect(page.container.textContent).not.toContain('{"detail"');
    expect(page.container.textContent).not.toContain(code);
  });

  it('shows the envelope message when a disconnect fails', async () => {
    fetchApi.mockRejectedValue(envelope(503, 'database_unavailable', 'Database is not available', 'unavailable'));
    page = await submit(true);

    expect(page.container.textContent).toContain('Database is not available');
    expect(page.container.textContent).not.toContain('{"detail"');
  });

  it('posts the keys to the connect endpoint', async () => {
    fetchApi.mockResolvedValue({ id: 'binance', connected: true });
    page = await submit();

    const [path, init] = fetchApi.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/api/system/exchanges/binance/connect');
    expect(JSON.parse(String(init.body))).toEqual({ api_key: 'key', api_secret: 'secret' });
  });
});
