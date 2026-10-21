import { afterEach, describe, expect, it } from 'vitest';
import { mount, type Mounted } from '@/test/pageHarness';
import { Table } from './Table';

interface Row {
  id: string;
  name: string;
  pnl: string;
  venue: string;
}

const columns = [
  { key: 'name', header: 'Name' },
  { key: 'venue', header: 'Venue', hideOnMobile: true },
  { key: 'pnl', header: 'PnL', numeric: true },
];
const data: Row[] = [{ id: 'a', name: 'Alpha', pnl: '−1.00', venue: 'Binance' }];

let page: Mounted | null = null;
afterEach(() => {
  page?.unmount();
  page = null;
});

describe('Table', () => {
  it('hides hideOnMobile columns on the phone list only, and shows every column on desktop', async () => {
    page = await mount(<Table columns={columns} data={data} keyExtractor={(r) => r.id} />);

    const card = page.container.querySelector('ul li');
    expect(card?.textContent).toContain('Alpha');
    expect(card?.textContent).not.toContain('Binance');
    const headers = Array.from(page.container.querySelectorAll('th')).map((th) => th.textContent);
    expect(headers).toEqual(['Name', 'Venue', 'PnL']);
  });

  it('puts scope="col" on every header and right-aligns numeric columns', async () => {
    page = await mount(<Table columns={columns} data={data} keyExtractor={(r) => r.id} />);

    const ths = Array.from(page.container.querySelectorAll('th'));
    expect(ths.every((th) => th.getAttribute('scope') === 'col')).toBe(true);
    expect(ths[2].className).toContain('text-right');
    expect(page.container.querySelectorAll('td')[2].className).toContain('text-right');
  });

  it('renders a non-navigating row without any button, and a navigating row as a link', async () => {
    page = await mount(<Table columns={columns} data={data} keyExtractor={(r) => r.id} />);
    expect(page.container.querySelector('button')).toBeNull();
    expect(page.container.querySelector('a')).toBeNull();
    page.unmount();

    page = await mount(
      <Table columns={columns} data={data} keyExtractor={(r) => r.id} rowHref={(r) => `/backtest/${r.id}`} />,
    );
    const links = Array.from(page.container.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(links).toEqual(['/backtest/a', '/backtest/a']);
    expect(page.container.querySelector('button')).toBeNull();
  });
});
