import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadConfig() {
  vi.resetModules();
  return import('../../next.config');
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('production security headers (next.config.ts)', () => {
  it('serves a CSP that blocks third-party scripts and frames only TradingView', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    const { default: config } = await loadConfig();
    const rules = await config.headers?.();
    expect(rules).toHaveLength(1);
    const [rule] = rules ?? [];
    expect(rule.source).toBe('/:path*');
    const csp = rule.headers.find((h) => h.key === 'Content-Security-Policy')?.value ?? '';
    const directives = Object.fromEntries(
      csp.split(';').map((d) => {
        const [name, ...values] = d.trim().split(/\s+/);
        return [name, values];
      }),
    );
    expect(directives['script-src']).toEqual(["'self'", "'unsafe-inline'"]);
    expect(directives['connect-src']).toEqual(["'self'"]);
    expect(directives['frame-src']).toEqual([
      'https://s.tradingview.com',
      'https://www.tradingview.com',
    ]);
    expect(directives['frame-ancestors']).toEqual(["'none'"]);
    expect(directives['object-src']).toEqual(["'none'"]);
    expect(csp).not.toContain('s3.tradingview.com');
  });

  it('drops the dev-only /api rewrite from production builds', async () => {
    vi.stubEnv('NODE_ENV', 'production');
    const { default: config } = await loadConfig();
    expect(await config.rewrites?.()).toEqual([]);
  });

  it('keeps next dev unrestricted (fast refresh needs eval) and proxies /api', async () => {
    vi.stubEnv('NODE_ENV', 'development');
    vi.stubEnv('API_URL', '');
    const { default: config } = await loadConfig();
    expect(await config.headers?.()).toEqual([]);
    expect(await config.rewrites?.()).toEqual([
      { source: '/api/:path*', destination: 'http://localhost:8000/api/:path*' },
    ]);
  });
});
