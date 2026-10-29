'use client';

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { fetchApi } from '@/lib/api';
import { describeApiError } from '@/lib/apiState';
import { age, utc } from '@/lib/format';
import { cn } from '@/components/ui/cn';

/** `routes/market_data.py` UniverseMember. */
export interface UniverseMember {
  rank: number;
  symbol: string;
  quote_volume: number;
}

/** `routes/market_data.py` UniverseSnapshot: the stored ranking plus its provenance. */
export interface UniverseSnapshot {
  exchange_id: string;
  quote_asset: string;
  source: string;
  ranked_at: string;
  age_hours: number;
  count: number;
  symbols: UniverseMember[];
}

/** The write gate's exact normalization (`services/universe.py`): strip slashes, never case-fold. */
export function symbolInUniverse(symbol: string, universe: UniverseSnapshot): boolean {
  return universe.symbols.some((m) => m.symbol === symbol.replace(/\//g, ''));
}

/** "ranked 2 h ago" inside a day, "ranked at 25 Oct 08:00 UTC" beyond it (age() goes absolute past 24 h). */
export function rankedAtPhrase(ageHours: number, rankedAt: string): string {
  return ageHours < 24
    ? `ranked ${age(ageHours * 3_600_000) ?? rankedAt}`
    : `ranked at ${utc(rankedAt) ?? rankedAt}`;
}

const VOLUME = new Intl.NumberFormat('en-GB', { notation: 'compact', maximumFractionDigits: 1 });

interface UniversePickerProps {
  /** The owning dialog's open state: each open re-reads the stored ranking (stale-OK read, no exchange call). */
  open: boolean;
  value: string;
  onChange: (symbol: string) => void;
  /** Reports the loaded snapshot (null when the read failed) so the owner can gate its save on membership. */
  onUniverse?: (snapshot: UniverseSnapshot | null) => void;
}

/** The builder's symbol field: a searchable combobox over the current top-100 universe.
 *
 * Only members are committable — the write gate enforced at the UI layer — while an
 * unreadable universe degrades to a plain text field and leaves the refusal to the
 * engine's 422 (`symbol_not_in_universe`), which the dialog renders guided. */
export function UniversePicker({ open, value, onChange, onUniverse }: UniversePickerProps) {
  const [universe, setUniverse] = useState<UniverseSnapshot | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [listOpen, setListOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  /** The typed search; null shows every member (focus opens the full list, typing narrows it). */
  const [query, setQuery] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /** The latest callback: the owner need not keep it stable for the read to run once per open. */
  const onUniverseRef = useRef(onUniverse);
  const id = useId();
  const listId = `${id}-listbox`;
  const statusId = `${id}-status`;

  useEffect(() => {
    onUniverseRef.current = onUniverse;
  }, [onUniverse]);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setUniverse(null);
    setLoadError(null);
    setListOpen(false);
    setQuery(null);
    onUniverseRef.current?.(null);
    fetchApi<UniverseSnapshot>('/api/market/universe')
      .then((snapshot) => {
        if (!alive) return;
        setUniverse(snapshot);
        onUniverseRef.current?.(snapshot);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setLoadError(err);
        onUniverseRef.current?.(null);
      });
    return () => {
      alive = false;
    };
  }, [open]);

  const expanded = listOpen && universe !== null;
  const isMember = universe !== null && symbolInUniverse(value, universe);
  const needle = (query ?? '').replace(/\//g, '').toUpperCase();
  const filtered = useMemo(
    () =>
      universe === null
        ? []
        : query === null
          ? universe.symbols
          : universe.symbols.filter((m) => m.symbol.toUpperCase().includes(needle)),
    [universe, query, needle],
  );
  const active = filtered.length === 0 ? 0 : Math.min(highlight, filtered.length - 1);

  const select = (member: UniverseMember) => {
    onChange(member.symbol);
    setQuery(null);
    setListOpen(false);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!expanded) {
        setListOpen(true);
        setHighlight(0);
        return;
      }
      setHighlight((h) => (e.key === 'ArrowDown' ? Math.min(h + 1, filtered.length - 1) : Math.max(h - 1, 0)));
    } else if (e.key === 'Enter') {
      if (expanded && filtered[active]) {
        e.preventDefault();
        select(filtered[active]);
      }
    } else if (e.key === 'Escape' && listOpen) {
      e.preventDefault();
      e.stopPropagation();
      setListOpen(false);
      setQuery(null);
    }
  };

  const invalid = universe !== null && value !== '' && !isMember;

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-ink-2">
        Symbol
      </label>
      <div className="relative">
        <input
          ref={inputRef}
          id={id}
          role="combobox"
          aria-expanded={expanded}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={expanded && filtered.length > 0 ? `${listId}-${active}` : undefined}
          aria-describedby={statusId}
          aria-invalid={invalid ? true : undefined}
          value={value}
          onChange={(e) => {
            onChange(e.target.value.toUpperCase());
            setQuery(e.target.value);
            setListOpen(true);
            setHighlight(0);
          }}
          onFocus={() => setListOpen(true)}
          onKeyDown={onKeyDown}
          onBlur={() => {
            setListOpen(false);
            setQuery(null);
          }}
          placeholder="Search the top-100 universe"
          autoComplete="off"
          className={cn(
            'h-10 max-lg:h-11 w-full rounded border bg-canvas px-3 text-sm text-ink-1 transition-colors',
            'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent',
            invalid ? 'border-danger' : 'border-line-strong',
          )}
        />
        {expanded && (
          <ul
            role="listbox"
            id={listId}
            aria-label="Universe members"
            className="absolute z-20 mt-1 max-h-60 w-full overflow-auto rounded border border-line bg-surface p-1 shadow-lg"
          >
            {filtered.length === 0 ? (
              <li className="px-2 py-2 text-xs text-ink-2">
                No member matches &quot;{value}&quot;. Symbols outside the top-100 universe can&apos;t be saved.
              </li>
            ) : (
              filtered.map((m, i) => (
                <li
                  key={m.symbol}
                  id={`${listId}-${i}`}
                  role="option"
                  aria-selected={i === active}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    select(m);
                  }}
                  onMouseEnter={() => setHighlight(i)}
                  className={cn(
                    'flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm',
                    i === active && 'bg-hover',
                  )}
                >
                  <span className="w-9 text-right text-xs text-ink-2 tabular-nums">#{m.rank}</span>
                  <span className="font-medium text-ink-1">{m.symbol}</span>
                  <span className="ml-auto text-xs text-ink-2 tabular-nums">
                    24h {VOLUME.format(m.quote_volume)}
                  </span>
                </li>
              ))
            )}
          </ul>
        )}
      </div>
      {loadError !== null ? (
        <p id={statusId} role="status" className="text-xs text-caution">
          Universe unavailable: {describeApiError(loadError, 'unavailable')} — the engine still refuses a
          non-member symbol on save.
        </p>
      ) : universe !== null && invalid ? (
        <p id={statusId} role="alert" className="text-xs text-danger">
          {value} is not in the current top-100 universe — pick a member from the list.
        </p>
      ) : universe !== null && (
        <p id={statusId} className="text-xs text-ink-2">
          Top {universe.count} {universe.exchange_id} {universe.quote_asset} pairs by 24h quote volume ·{' '}
          {rankedAtPhrase(universe.age_hours, universe.ranked_at)}
        </p>
      )}
    </div>
  );
}
