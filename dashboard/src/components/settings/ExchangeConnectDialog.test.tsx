import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/lib/api';
import { buttonByText, click, dialogPolyfill, inputByLabel, mount, routeFetch, typeInto, type Mounted, type Mutations } from '@/test/pageHarness';
import { liveChallenge } from '@/test/fixtures';

const fetchApi = vi.fn();

vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  fetchApi: (...args: unknown[]) => fetchApi(...args),
}));
vi.mock('@/components/ui/Toast', () => ({ useToast: () => ({ toast: vi.fn() }) }));

const { ExchangeConnectDialog } = await import('./ExchangeConnectDialog');

const APPROVE_PATH = '/api/activity/approvals/3f0b9e2a-2000-4000-8000-000000000001/approve';

function envelope(status: number, code: string, message: string, state: string): ApiError {
  return new ApiError(status, JSON.stringify({ detail: { code, message, state } }));
}

let page: Mounted | null = null;
let calls: Array<{ path: string; init?: RequestInit }>;

beforeEach(() => {
  dialogPolyfill();
  fetchApi.mockReset();
  calls = [];
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
      exchange={{ id: 'binance', name: 'Binance', connected, environment: 'testnet' }}
    />,
  );
  if (!connected) {
    await typeInto(inputByLabel(mounted.container, 'API Key'), 'key');
    await typeInto(inputByLabel(mounted.container, 'API Secret'), 'secret');
    await click(buttonByText(mounted.container, 'Check and save keys'));
  } else {
    await click(buttonByText(mounted.container, 'Delete keys'));
  }
  return mounted;
}

describe('ExchangeConnectDialog errors', () => {
  it.each([
    [422, 'credentials_rejected', 'Binance testnet rejected these API keys (key, secret or permissions); nothing was stored', 'invalid'],
    [502, 'exchange_unreachable', 'Binance testnet could not be reached to check the keys; nothing was stored', 'unavailable'],
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
    fetchApi.mockResolvedValue({ id: 'binance', connected: true, message: 'stored' });
    page = await submit();

    const [path, init] = fetchApi.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/api/system/exchanges/binance/connect');
    expect(JSON.parse(String(init.body))).toEqual({ api_key: 'key', api_secret: 'secret' });
  });

  it('asks for read-only keys and tells the operator what deleting them costs', async () => {
    page = await mount(
      <ExchangeConnectDialog
        open
        onClose={vi.fn()}
        onUpdated={vi.fn()}
        exchange={{ id: 'binance', name: 'Binance', connected: false, environment: 'testnet' }}
      />,
    );
    expect(page.container.textContent).toContain('Use a read-only API key.');
    page.unmount();

    page = await mount(
      <ExchangeConnectDialog
        open
        onClose={vi.fn()}
        onUpdated={vi.fn()}
        exchange={{ id: 'binance', name: 'Binance', connected: true, environment: 'testnet' }}
      />,
    );
    const dialog = page.container.querySelector('dialog');
    expect(dialog?.textContent).toContain('Delete the stored Binance keys?');
    expect(dialog?.textContent).toContain("Paper sessions aren't affected");
    expect(document.activeElement?.textContent).toBe('Delete the stored Binance keys?');
    expect(fetchApi).not.toHaveBeenCalled();
  });

  it('deletes the keys only after the confirm button', async () => {
    fetchApi.mockResolvedValue({});
    page = await submit(true);

    const [path, init] = fetchApi.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/api/system/exchanges/binance/connect');
    expect(init.method).toBe('DELETE');
  });

  it('names the environment the keys are checked against', async () => {
    page = await mount(
      <ExchangeConnectDialog
        open
        onClose={vi.fn()}
        onUpdated={vi.fn()}
        exchange={{ id: 'binance', name: 'Binance', connected: false, environment: 'testnet' }}
      />,
    );
    const target = page.container.querySelector('[data-testid="connect-target"]')?.textContent ?? '';
    expect(target).toContain('Binance testnet');
    expect(target).toContain('mainnet keys are rejected there');
  });
});

describe('the L4 delete for trade-capable keys (UI-4c)', () => {
  const l4Exchange = { id: 'binance', name: 'Binance', connected: true, environment: 'testnet' as const, tradeEnabled: true };

  function showL4(mutations: Mutations = {}) {
    fetchApi.mockImplementation(
      routeFetch({}, calls, {
        'POST /api/live/credential-delete': liveChallenge({ note: 'Enter this code once to delete the keys.' }),
        ...mutations,
      }),
    );
    return mount(<ExchangeConnectDialog open onClose={vi.fn()} onUpdated={vi.fn()} exchange={l4Exchange} />);
  }

  const primary = () => buttonByText(page?.container ?? document.body, 'Delete keys');
  const dialog = () => page?.container.querySelector('dialog') as HTMLDialogElement;

  it('asks for the typed exchange name and the one-time code, and sends nothing until both are valid', async () => {
    page = await showL4();
    const d = dialog();
    expect(d.textContent).toContain('Delete the stored Binance keys?');
    expect(d.textContent).toContain('Type BINANCE to confirm');
    expect(d.textContent).toContain('482913');
    const challenge = JSON.parse(calls.find((c) => c.path === '/api/live/credential-delete')?.init?.body as string);
    expect(challenge).toEqual({ exchange_id: 'binance', reason: 'Delete the stored Binance keys' });
    expect(primary().disabled).toBe(true);
    await typeInto(inputByLabel(page.container, 'Type BINANCE to confirm'), 'BINANCE');
    expect(primary().disabled).toBe(true);
    await typeInto(inputByLabel(page.container, 'One-time code'), '48291');
    expect(primary().disabled).toBe(true);
    await typeInto(inputByLabel(page.container, 'One-time code'), '482913');
    expect(primary().disabled).toBe(false);
    expect(calls.filter((c) => c.path === '/api/system/exchanges/binance/connect')).toHaveLength(0);
    await click(primary());
    expect(JSON.parse(calls.find((c) => c.path === APPROVE_PATH)?.init?.body as string)).toEqual({ code: '482913' });
  });

  it('keeps the typed values and stays inline on a wrong code', async () => {
    page = await showL4({
      [`POST ${APPROVE_PATH}`]: envelope(422, 'code_mismatch', "That code didn't match. Nothing changed.", 'invalid'),
    });
    await typeInto(inputByLabel(page.container, 'Type BINANCE to confirm'), 'BINANCE');
    await typeInto(inputByLabel(page.container, 'One-time code'), '000000');
    await click(primary());
    expect(page.container.textContent).toContain("That code didn't match. Nothing changed.");
    expect(inputByLabel(page.container, 'One-time code').value).toBe('000000');
    expect(inputByLabel(page.container, 'Type BINANCE to confirm').value).toBe('BINANCE');
    expect(document.activeElement).toBe(inputByLabel(page.container, 'One-time code'));
  });

  it('sends no delete at all on Enter at open', async () => {
    page = await showL4();
    expect(calls.filter((c) => c.path === APPROVE_PATH)).toHaveLength(0);
    expect(calls.filter((c) => c.path === '/api/system/exchanges/binance/connect')).toHaveLength(0);
    expect(document.activeElement?.textContent).toBe('Delete the stored Binance keys?');
  });

  it('keeps today’s L2 delete byte-identical for read-only keys', async () => {
    fetchApi.mockResolvedValue({});
    page = await mount(
      <ExchangeConnectDialog
        open
        onClose={vi.fn()}
        onUpdated={vi.fn()}
        exchange={{ id: 'binance', name: 'Binance', connected: true, environment: 'testnet' }}
      />,
    );
    const d = dialog();
    expect(d.textContent).toContain("Paper sessions aren't affected");
    expect(() => inputByLabel(d, 'One-time code')).toThrow();
    await click(buttonByText(page.container, 'Delete keys'));
    const [path, init] = fetchApi.mock.calls[0] as [string, RequestInit];
    expect(path).toBe('/api/system/exchanges/binance/connect');
    expect(init.method).toBe('DELETE');
  });
});
