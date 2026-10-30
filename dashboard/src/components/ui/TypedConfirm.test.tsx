import { act } from 'react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { buttonByText, click, dialogPolyfill, inputByLabel, mount, typeInto, type Mounted } from '@/test/pageHarness';
import { TypedConfirm } from './TypedConfirm';

let m: Mounted | null = null;
beforeAll(dialogPolyfill);
afterEach(() => {
  m?.unmount();
  m = null;
});

const render = (open: boolean, onConfirm = vi.fn(() => Promise.resolve())) => (
  <TypedConfirm open={open} title="Reset?" token="RESET" primaryLabel="Do it" secondaryLabel="Cancel" pendingLabel="Doing…" onConfirm={onConfirm} onClose={() => {}}>
    <p>Facts</p>
  </TypedConfirm>
);

const renderToken = (token: string) => (
  <TypedConfirm open title="Remove?" token={token} primaryLabel="Do it" secondaryLabel="Cancel" pendingLabel="Doing…" onConfirm={() => Promise.resolve()} onClose={() => {}}>
    <p>Facts</p>
  </TypedConfirm>
);

const blur = (el: HTMLElement) => act(async () => el.dispatchEvent(new FocusEvent('focusout', { bubbles: true })));

describe('TypedConfirm', () => {
  it('matches the token case-sensitively and trims the reason before counting it', async () => {
    const onConfirm = vi.fn(() => Promise.resolve());
    m = await mount(render(true, onConfirm));
    const primary = buttonByText(m.container, 'Do it');
    await typeInto(inputByLabel(m.container, 'Type RESET to confirm'), 'reset');
    await typeInto(inputByLabel(m.container, 'Reason'), 'good reason');
    expect(primary.disabled).toBe(true);
    await typeInto(inputByLabel(m.container, 'Type RESET to confirm'), 'RESET');
    await typeInto(inputByLabel(m.container, 'Reason'), ' a ');
    expect(primary.disabled).toBe(true);
    await typeInto(inputByLabel(m.container, 'Reason'), ' abc ');
    expect(primary.disabled).toBe(false);
    await click(primary);
    expect(onConfirm).toHaveBeenCalledWith('abc');
  });

  it('rejects a reason over 500 characters', async () => {
    m = await mount(render(true));
    await typeInto(inputByLabel(m.container, 'Type RESET to confirm'), 'RESET');
    await typeInto(inputByLabel(m.container, 'Reason'), 'x'.repeat(501));
    expect(buttonByText(m.container, 'Do it').disabled).toBe(true);
  });

  it('shows field errors on blur, not while typing', async () => {
    m = await mount(render(true));
    const field = inputByLabel(m.container, 'Type RESET to confirm');
    await typeInto(field, 'RES');
    expect(m.container.textContent).not.toContain('Type RESET in capitals to confirm.');
    await blur(field);
    expect(m.container.textContent).toContain('Type RESET in capitals to confirm.');
  });

  it('asks for an exact match when the token is not capitals, such as a strategy id', async () => {
    m = await mount(renderToken('saw'));
    const field = inputByLabel(m.container, 'Type saw to confirm');
    await typeInto(field, 'SAW');
    expect(m.container.textContent).not.toContain('Type saw exactly to confirm.');
    await blur(field);
    expect(m.container.textContent).toContain('Type saw exactly to confirm.');
  });

  it('sets the field attributes that stop autofill and autocorrect', async () => {
    m = await mount(render(true));
    const field = inputByLabel(m.container, 'Type RESET to confirm');
    expect(field.getAttribute('autocomplete')).toBe('off');
    expect(field.getAttribute('autocapitalize')).toBe('characters');
    expect(field.getAttribute('spellcheck')).toBe('false');
  });
});
