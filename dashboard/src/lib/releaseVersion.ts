/**
 * The hydra release version from pyproject.toml `[project] version`, the single source of truth
 * (dashboard/package.json has its own, unrelated version).
 */
export function readReleaseVersion(pyprojectText: string): string {
  let inProject = false;
  for (const raw of pyprojectText.split(/\r?\n/)) {
    const line = raw.trim();
    const table = /^\[([^\]]+)\]$/.exec(line);
    if (table) {
      inProject = table[1].trim() === 'project';
      continue;
    }
    const version = inProject ? /^version\s*=\s*"([^"]+)"/.exec(line) : null;
    if (version) return version[1];
  }
  throw new Error('pyproject.toml has no [project] version');
}
