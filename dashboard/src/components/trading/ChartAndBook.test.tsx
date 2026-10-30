import { describe, expect, it } from 'vitest';
import { tradingViewSrc } from './ChartAndBook';

describe('tradingViewSrc venue prefix (P7-F)', () => {
  it('prefixes the symbol with the session venue', () => {
    expect(decodeURIComponent(tradingViewSrc('BTCUSDT', 'dark', 'bybit'))).toContain(
      'symbol=BYBIT:BTCUSDT',
    );
    expect(decodeURIComponent(tradingViewSrc('BTCUSDT', 'dark', 'binance'))).toContain(
      'symbol=BINANCE:BTCUSDT',
    );
    expect(decodeURIComponent(tradingViewSrc('BTCUSDT', 'dark', 'binanceusdm'))).toContain(
      'symbol=BINANCE:BTCUSDT',
    );
  });

  it('falls back to the Binance feed for an unknown venue', () => {
    expect(decodeURIComponent(tradingViewSrc('ETHUSDT', 'light', 'kraken'))).toContain(
      'symbol=BINANCE:ETHUSDT',
    );
    expect(decodeURIComponent(tradingViewSrc('ETHUSDT', 'light'))).toContain(
      'symbol=BINANCE:ETHUSDT',
    );
  });
});
