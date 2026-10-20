import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';

import packageJson from '../../package.json';
import { readReleaseVersion } from './releaseVersion';

const repoRoot = path.resolve(process.cwd(), '..');
const read = (name: string) => readFileSync(path.join(repoRoot, name), 'utf8');

function lockedVersion(): string {
  const match = /\[\[package\]\]\nname = "hydra"\nversion = "([^"]+)"/.exec(read('uv.lock'));
  if (!match) throw new Error('uv.lock has no hydra package');
  return match[1];
}

describe('readReleaseVersion', () => {
  it("reads the repo's pyproject version, which uv.lock records too", () => {
    const version = readReleaseVersion(read('pyproject.toml'));
    expect(version).toMatch(/^\d+\.\d+\.\d+$/);
    expect(version).toBe(lockedVersion());
  });

  it('reads only the [project] table', () => {
    const text = '[tool.poetry]\nversion = "9.9.9"\n\n[project]\nname = "hydra"\nversion = "2.3.0"\n';
    expect(readReleaseVersion(text)).toBe('2.3.0');
  });

  it('throws when [project] has no version', () => {
    expect(() => readReleaseVersion('[project]\nname = "hydra"\n[tool.x]\nversion = "1.0.0"\n')).toThrow(
      'no [project] version',
    );
  });

  it('is what next.config.ts exposes to the sidebar, not the package.json version', async () => {
    vi.resetModules();
    const { default: config } = await import('../../next.config');
    expect(config.env?.APP_VERSION).toBe(lockedVersion());
    expect(lockedVersion()).not.toBe(packageJson.version);
  });
});
