'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { describeApiError } from '@/lib/apiState';
import { fetchApi } from '@/lib/api';
import type { SessionResponse } from '@/lib/derive';
import { startOutcome, type Outcome, type StartSessionRequest } from '@/lib/outcomes';
import { settle } from './useOutcomeSlot';

interface StrategyOption {
  id: string;
  name: string;
}

interface StrategyDetail extends StrategyOption {
  symbol: string;
  timeframes: { primary: string };
}

const DECIMAL = /^\d+(\.\d{1,8})?$/;

interface StartSessionDialogProps {
  /** Preselected from a Strategies row or a backtest result; otherwise the dialog asks. */
  strategyId?: string;
  onClose: () => void;
}

/** F3: start a paper session. Mount it only while open. */
export function StartSessionDialog({ strategyId, onClose }: StartSessionDialogProps) {
  const router = useRouter();
  const { live, setOutcome } = useShellStatus();
  const [options, setOptions] = useState<StrategyOption[] | null>(null);
  const [picked, setPicked] = useState(strategyId ?? '');
  const [detail, setDetail] = useState<StrategyDetail | Error | null>(null);
  const [capital, setCapital] = useState('');
  const [failure, setFailure] = useState<Outcome | null>(null);

  useEffect(() => {
    if (strategyId) return;
    let alive = true;
    fetchApi<StrategyOption[]>('/api/strategies')
      .then((list) => alive && setOptions(list))
      .catch((e: unknown) => alive && setDetail(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive = false;
    };
  }, [strategyId]);

  useEffect(() => {
    if (!picked) return;
    let alive = true;
    setDetail(null);
    fetchApi<StrategyDetail>(`/api/strategies/${picked}`)
      .then((d) => alive && setDetail(d))
      .catch((e: unknown) => alive && setDetail(e instanceof Error ? e : new Error(String(e))));
    return () => {
      alive = false;
    };
  }, [picked]);

  const killSwitchOn = live.data?.kill_switch_state === 'active';
  const tier = live.data?.circuit_breaker_restrictions?.active_tier ?? 0;
  const capitalError = capital !== '' && !(DECIMAL.test(capital) && Number(capital) > 0) ? 'Enter an amount above 0 with up to 8 decimals.' : null;
  const known = detail !== null && !(detail instanceof Error) ? detail : null;

  const start = async () => {
    const req: StartSessionRequest = { strategy_id: picked, trading_mode: 'paper', ...(capital ? { paper_capital: capital } : {}) };
    const result = await settle(fetchApi<SessionResponse>('/api/trading/sessions', { method: 'POST', body: JSON.stringify(req) }));
    const outcome = startOutcome(req, result);
    if (outcome.navigate) {
      setOutcome(outcome);
      onClose();
      router.push(outcome.navigate);
    } else {
      setFailure(outcome);
    }
  };

  return (
    <ConfirmDialog
      open
      title="Start a paper session"
      primaryLabel="Start paper session"
      secondaryLabel="Cancel"
      pendingLabel="Starting…"
      pendingHint="Closing this won't cancel the start."
      primaryDisabled={!picked || killSwitchOn || capitalError !== null}
      onClose={onClose}
      onConfirm={start}
    >
      {!strategyId && (
        <Select
          label="Strategy"
          placeholder={options ? 'Choose a strategy' : 'Loading strategies…'}
          options={(options ?? []).map((o) => ({ value: o.id, label: o.name }))}
          value={picked}
          onChange={(e) => setPicked(e.target.value)}
        />
      )}
      {picked && (
        <p>
          {known
            ? `${known.name} · Binance spot · ${known.symbol} · ${known.timeframes.primary} · PAPER`
            : detail instanceof Error
              ? `Strategy details couldn't be read: ${describeApiError(detail, 'unknown error')}`
              : 'Reading the strategy…'}
        </p>
      )}
      <Input
        label="Paper capital for this session (USDT)"
        inputMode="decimal"
        autoComplete="off"
        value={capital}
        onChange={(e) => setCapital(e.target.value.trim())}
        error={capitalError ?? undefined}
        hint="Leave empty to use all unallocated paper capital."
      />
      {killSwitchOn && (
        <p role="alert" className="text-danger">
          The kill switch is on. Release it on Risk before starting a session.{' '}
          <Link href="/risk#kill-switch" className="underline">
            Release on Risk ›
          </Link>
        </p>
      )}
      {tier >= 2 && (
        <p className="text-caution">Breaker tier {tier} is active. The session will start but can&apos;t open entries until it clears.</p>
      )}
      {failure && (
        <div role="alert" className="text-caution">
          <p className="font-semibold">{failure.title}</p>
          {failure.lines.map((l) => (
            <p key={l}>{l}</p>
          ))}
          {failure.actions.map((a) =>
            a.href ? (
              <Link key={a.label} href={a.href} className="underline">
                {a.label}
              </Link>
            ) : null,
          )}
        </div>
      )}
    </ConfirmDialog>
  );
}
