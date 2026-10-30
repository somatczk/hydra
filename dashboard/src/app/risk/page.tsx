'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { actorLabel, type RiskEvent } from '@/components/activity/ActivityLog';
import { BreakerTable } from '@/components/risk/BreakerTable';
import { LimitsSheet } from '@/components/risk/LimitsSheet';
import { StrategyLimitsSheet } from '@/components/risk/StrategyLimitsSheet';
import { KillSwitchControl } from '@/components/shell/KillSwitchControl';
import { OutcomePanel } from '@/components/shell/OutcomePanel';
import { useShellStatus } from '@/components/shell/ShellStatusProvider';
import { Button } from '@/components/ui/Button';
import { DataCard } from '@/components/ui/DataCard';
import { DataStateBadge, DataStateErrorCard } from '@/components/ui/DataStateBadge';
import { Select } from '@/components/ui/Select';
import { TypedConfirm } from '@/components/ui/TypedConfirm';
import { usePoll } from '@/hooks/usePoll';
import { fetchApi, connectWebSocket } from '@/lib/api';
import { parseErrorDetail } from '@/lib/apiState';
import { plural, sessionName, stoppedHolders, type CircuitBreaker, type Restrictions } from '@/lib/derive';
import { duration, money, pct, utc } from '@/lib/format';
import { classifyRiskWsMessage, type RiskHeartbeat } from '@/lib/riskWs';
import { debounce } from '@/lib/sessionWs';
import {
  breakerResetOutcome,
  ledgerResetOutcome,
  limitsOutcome,
  releaseOutcome,
  strategyOverrideOutcome,
  type KillSwitchResponse,
  type Outcome,
  type Result,
} from '@/lib/outcomes';
import {
  type RiskConfigResponse,
  type RiskLimitsUpdate,
  type StrategyOverrideRow,
  type StrategyOverrideValues,
} from '@/lib/riskLimits';

interface RiskStatus {
  current_drawdown: number;
  max_drawdown_limit: number;
  daily_loss: number | null;
  daily_loss_limit: number | null;
  equity: number | null;
  peak_equity: number | null;
  day_start_equity: number | null;
  consecutive_losses: number;
  max_consecutive_losses: number;
  circuit_breakers: CircuitBreaker[] | null;
  restrictions: Restrictions | null;
}

interface VarEstimate {
  var_95: number;
  var_99: number;
  cvar_95: number;
  sample_days: number;
}

interface StrategyOption {
  id: string;
  name: string;
}

const MIN_VAR_SAMPLE_DAYS = 5;
/** A burst of risk events collapses into one refetch, 500 ms after the last tick. */
const RISK_EVENT_REFETCH_DEBOUNCE_MS = 500;
const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

async function settle<T>(p: Promise<T>): Promise<Result<T>> {
  try {
    return { ok: await p };
  } catch (err) {
    return { err };
  }
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 text-sm">
      <dt className="text-ink-2">{label}</dt>
      <dd className="font-mono text-ink-1">{children}</dd>
    </div>
  );
}

const usdt = (v: number | null | undefined) => (money(v) === null ? '—' : `${money(v)} USDT`);

type Dialog = 'release' | 'ledger' | number | null;

export default function RiskPage() {
  const shell = useShellStatus();
  const { live, sessions, now } = shell;
  const status = usePoll<RiskStatus>('/api/risk/status?source=paper', 10_000);
  const events = usePoll<RiskEvent[]>('/api/risk/events?limit=500', 30_000);
  const config = usePoll<RiskConfigResponse>('/api/risk/config', 30_000);
  const strategies = usePoll<StrategyOption[]>('/api/strategies', 30_000);
  const varEstimate = usePoll<VarEstimate>('/api/risk/var?source=paper', 30_000);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [reloadFailed, setReloadFailed] = useState<Outcome | null>(null);
  const retry = useRef<(() => Promise<unknown>) | null>(null);
  const dialogRef = useRef<Dialog>(null);

  const st = status.data;
  const [push, setPush] = useState(false);
  const [beat, setBeat] = useState<{ at: number; data: RiskHeartbeat } | null>(null);
  const refetchStatus = status.refetch;
  const { refetch: refetchEvents } = events;
  const refetchSessions = shell.sessions.refetch;

  useEffect(() => {
    const refetchOnEvent = debounce(() => {
      void refetchStatus();
      void refetchEvents();
      // the shell's session list (a breaker may have stopped sessions); the kill-switch
      // query is deliberately not here — it stays sourced from the live-status poll
      void refetchSessions();
    }, RISK_EVENT_REFETCH_DEBOUNCE_MS);
    const ws = connectWebSocket('/ws/risk', (msg) => {
      const classified = classifyRiskWsMessage(msg);
      if (classified.action === 'refetch') refetchOnEvent.trigger();
      else if (classified.action === 'heartbeat') setBeat({ at: Date.now(), data: classified.beat });
    });
    if (ws) {
      ws.onopen = () => setPush(true);
      ws.onclose = () => setPush(false);
      ws.onerror = () => setPush(false);
    }
    return () => {
      refetchOnEvent.cancel();
      ws?.close();
      setPush(false);
    };
  }, [refetchStatus, refetchEvents, refetchSessions]);

  /** The fresher of the pushed heartbeat and the last poll drives the ledger cards. */
  const hb =
    beat !== null && (status.fetchedAt === null || beat.at >= status.fetchedAt) ? beat.data : null;
  const equity = hb ? hb.equity : st?.equity ?? null;
  const peakEquity = hb ? hb.peak_equity : st?.peak_equity ?? null;
  const dayStartEquity = hb ? hb.day_start_equity : st?.day_start_equity ?? null;
  const currentDrawdown = hb ? hb.current_drawdown : st?.current_drawdown ?? null;
  const dailyLoss = hb ? hb.daily_loss : st?.daily_loss ?? null;
  const lossStreak = hb ? hb.consecutive_losses : st?.consecutive_losses ?? null;

  const ks = live.data?.kill_switch_state ?? (live.state === 'loading' ? 'loading' : 'unknown');
  const activeTier = st?.restrictions?.active_tier ?? live.data?.circuit_breaker_restrictions?.active_tier ?? 0;
  const lastKs = events.data?.find((e) => e.kind === 'kill_switch_on' || e.kind === 'kill_switch_off');
  const onSince = lastKs?.kind === 'kill_switch_on' ? lastKs : undefined;
  const resetTier = typeof dialog === 'number' ? dialog : 0;
  const holders = stoppedHolders(sessions.data ?? []);

  const prevKs = useRef(ks);
  useEffect(() => {
    if (prevKs.current !== ks) void refetchEvents();
    prevKs.current = ks;
  }, [ks, refetchEvents]);
  const show = (d: Dialog) => {
    dialogRef.current = d;
    setDialog(d);
  };

  const refresh = () => Promise.all([shell.refetchAll(), status.refetch(), events.refetch()]);

  /** Runs a typed-confirm request: success and "tier no longer active" close the dialog; any other failure keeps it open with the typed values. */
  const attempt = async <T,>(req: Promise<T>, toOutcome: (r: Result<T>) => Outcome) => {
    const started = dialogRef.current;
    const result = await settle(req);
    const o = toOutcome(result);
    const keep = 'err' in result && parseErrorDetail(result.err)?.code !== 'tier_not_active';
    // A dialog closed mid-request has no error line to show, so the failure goes to the page panel.
    if (keep && dialogRef.current === started) {
      setDialogError([o.title, ...o.lines].join(' '));
    } else {
      show(null);
      setOutcome(o);
    }
    void refresh();
  };

  const open = (d: Dialog) => {
    setDialogError(null);
    show(d);
  };

  const saveLimits = async (update: RiskLimitsUpdate): Promise<boolean> => {
    retry.current = () => saveLimits(update);
    const result = await settle(fetchApi('/api/risk/config', { method: 'PUT', ...json(update) }));
    const o = limitsOutcome(result);
    if ('err' in result && parseErrorDetail(result.err)?.code === 'risk_reload_failed') setReloadFailed(o);
    else {
      if ('ok' in result) setReloadFailed(null);
      setOutcome(o);
    }
    void Promise.all([config.refetch(), events.refetch()]);
    return 'ok' in result;
  };

  const strategyOptions = (() => {
    const byId = new Map<string, string>();
    for (const s of strategies.data ?? []) byId.set(s.id, s.name);
    for (const o of config.data?.overrides ?? []) if (!byId.has(o.strategy_id)) byId.set(o.strategy_id, o.strategy_id);
    return [...byId].map(([id, name]) => ({ id, name }));
  })();
  const [strategyId, setStrategyId] = useState<string>('');
  const selected = strategyOptions.find((s) => s.id === (strategyId || strategyOptions[0]?.id)) ?? null;
  const selectedId = selected?.id ?? '';
  const storedOverride: StrategyOverrideRow | undefined = config.data?.overrides.find(
    (o) => o.strategy_id === selectedId,
  );
  const stored: StrategyOverrideValues = storedOverride ?? {
    max_position_pct: null,
    max_risk_per_trade: null,
    max_concurrent_positions: null,
  };

  const saveStrategyOverride = async (update: StrategyOverrideValues): Promise<boolean> => {
    const id = selectedId;
    retry.current = () => saveStrategyOverride(update);
    const result = await settle(fetchApi(`/api/risk/config/${id}`, { method: 'PUT', ...json(update) }));
    setOutcome(strategyOverrideOutcome(result, id, 'save'));
    void Promise.all([config.refetch(), events.refetch()]);
    return 'ok' in result;
  };

  const clearStrategyOverride = async (reason: string): Promise<boolean> => {
    const id = selectedId;
    retry.current = () => clearStrategyOverride(reason);
    const result = await settle(fetchApi(`/api/risk/config/${id}`, { method: 'DELETE', ...json({ reason }) }));
    setOutcome(strategyOverrideOutcome(result, id, 'clear'));
    void Promise.all([config.refetch(), events.refetch()]);
    return 'ok' in result;
  };

  return (
    <div className="flex flex-col gap-4">
      <OutcomePanel
        outcome={outcome}
        onAction={() => void retry.current?.()}
        onDismiss={() => {
          retry.current = null;
          setOutcome(null);
        }}
      />

      <section id="kill-switch" aria-labelledby="ks-h" className="scroll-mt-20">
        <DataCard>
          <h2 id="ks-h" className="text-sm font-semibold text-ink-1">
            Kill switch
          </h2>
          {ks === 'active' ? (
            <div className="mt-2 flex flex-col gap-2 text-sm">
              <p className="font-semibold text-danger">On. New orders are blocked.</p>
              <p className="text-ink-2">
                {onSince
                  ? `On since ${utc(onSince.ts, now)} (${duration(now - new Date(onSince.ts ?? now).getTime())}) · by ${actorLabel(onSince.actor)} · via Hydra`
                  : 'On since: not in recent events'}
              </p>
              {holders.length > 0 && (
                <p>
                  Still held in {plural(holders.length, 'stopped session')}: {holders.map(sessionName).join(', ')}. The
                  kill switch won&apos;t close these; stop them individually.
                </p>
              )}
              <div>
                <Button variant="secondary" size="sm" aria-haspopup="dialog" onClick={() => open('release')}>
                  Release…
                </Button>
              </div>
            </div>
          ) : (
            <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
              <p>{ks === 'inactive' ? 'Off' : ks === 'unknown' ? 'State unknown. Release is hidden until it is known.' : 'Checking…'}</p>
              <KillSwitchControl />
            </div>
          )}
        </DataCard>
      </section>

      <section id="breakers" aria-labelledby="cb-h" className="scroll-mt-20">
        <DataCard>
          <h2 id="cb-h" className="text-sm font-semibold text-ink-1">
            Breakers
          </h2>
          {st === null ? (
            <div className="mt-2">
              {status.state === 'loading' ? <p className="text-sm text-ink-2">Loading…</p> : <DataStateErrorCard error={status.error} fallback="Failed to load risk status" onRetry={() => void status.refetch()} />}
            </div>
          ) : (
            <>
              <p className="mt-2 text-sm text-ink-1">
                Drawdown {pct(st.current_drawdown)} of {pct(st.max_drawdown_limit)} limit
              </p>
              {st.restrictions?.daily_halt_until && <p className="text-sm text-danger">Daily loss halt until {utc(st.restrictions.daily_halt_until, now)}</p>}
              {st.restrictions?.consecutive_pause_until && (
                <p className="text-sm text-danger">Loss-streak pause until {utc(st.restrictions.consecutive_pause_until, now)}</p>
              )}
              {st.circuit_breakers === null ? (
                <div className="mt-2 flex items-center gap-2 text-sm text-ink-2">
                  <DataStateBadge state="unavailable" reason="risk service not running" />
                  The engine&apos;s risk service is not available, so no tier status is shown.
                </div>
              ) : (
                <BreakerTable breakers={st.circuit_breakers} activeTier={activeTier} now={now} onReset={open} />
              )}
            </>
          )}
        </DataCard>
      </section>

      <DataCard title="Today's loss">
        {st === null && hb === null ? (
          <DataStateBadge state="unknown" />
        ) : (
          <dl className="flex flex-col gap-1">
            <Row label="Daily loss">
              {dailyLoss === null ? (
                <DataStateBadge state="unknown" />
              ) : (
                `${money(dailyLoss)} of ${money(st?.daily_loss_limit) ?? '—'} USDT`
              )}
            </Row>
            <Row label="Loss streak">
              {lossStreak ?? '—'} of {st?.max_consecutive_losses ?? '—'}
            </Row>
          </dl>
        )}
      </DataCard>

      <section id="ledger" aria-labelledby="ledger-h" className="scroll-mt-20">
        <DataCard>
          <h2 id="ledger-h" className="text-sm font-semibold text-ink-1">
            Paper ledger
          </h2>
          {st === null && hb === null ? (
            <div className="mt-2">
              <DataStateBadge state="unknown" />
            </div>
          ) : (
            <>
              <dl className="mt-2 flex flex-col gap-1">
                <Row label="Equity">{usdt(equity)}</Row>
                <Row label="Peak">{usdt(peakEquity)}</Row>
                <Row label="Day start">{usdt(dayStartEquity)}</Row>
                <Row label="Drawdown">{currentDrawdown === null ? '—' : pct(currentDrawdown)}</Row>
              </dl>
              <p className="mt-2 text-sm text-ink-2">
                {push ? 'Push connected' : 'Polling every 10 s'}
              </p>
              <Button className="mt-4" variant="secondary" size="sm" aria-haspopup="dialog" onClick={() => open('ledger')}>
                Reset paper ledger…
              </Button>
            </>
          )}
        </DataCard>
      </section>

      <section id="limits" aria-labelledby="limits-h" className="scroll-mt-20">
        <DataCard>
          <h2 id="limits-h" className="mb-3 text-sm font-semibold text-ink-1">
            Limits
          </h2>
          {reloadFailed && (
            <div role="alert" aria-label="Limits not in force" className="mb-3 rounded border border-danger p-3 text-sm">
              <p className="font-semibold text-ink-1">{reloadFailed.title}</p>
              {reloadFailed.lines.map((l) => (
                <p key={l} className="text-ink-1">
                  {l}
                </p>
              ))}
              <Button className="mt-2" variant="secondary" size="sm" onClick={() => void retry.current?.()}>
                Try again
              </Button>
            </div>
          )}
          {config.data ? (
            <LimitsSheet cfg={config.data} onSave={saveLimits} />
          ) : config.state === 'loading' ? (
            <p className="text-sm text-ink-2">Loading…</p>
          ) : (
            <DataStateErrorCard error={config.error} fallback="Failed to load risk limits" onRetry={() => void config.refetch()} />
          )}
        </DataCard>
      </section>

      <section id="strategy-limits" aria-labelledby="strategy-limits-h" className="scroll-mt-20">
        <DataCard>
          <h2 id="strategy-limits-h" className="mb-3 text-sm font-semibold text-ink-1">
            Per-strategy limits
          </h2>
          <p className="mb-3 text-sm text-ink-2">
            A strategy can tighten three of the global limits for its own entries: position size, risk per trade
            and concurrent positions. Everything else stays account-level.
          </p>
          {config.data ? (
            <div className="flex flex-col gap-3">
              {strategyOptions.length > 0 ? (
                <Select
                  label="Strategy"
                  options={strategyOptions.map((s) => ({ value: s.id, label: s.name }))}
                  value={selectedId}
                  onChange={(e) => setStrategyId(e.target.value)}
                />
              ) : (
                <p className="text-sm text-ink-2">No strategies yet.</p>
              )}
              {selected ? (
                <StrategyLimitsSheet
                  strategyId={selectedId}
                  cfg={config.data}
                  stored={stored}
                  hasStoredRow={storedOverride !== undefined}
                  onSave={saveStrategyOverride}
                  onClear={clearStrategyOverride}
                />
              ) : (
                <DataStateErrorCard
                  error={strategies.error}
                  fallback="Failed to load strategies"
                  onRetry={() => void strategies.refetch()}
                />
              )}
            </div>
          ) : config.state === 'loading' ? (
            <p className="text-sm text-ink-2">Loading…</p>
          ) : (
            <DataStateErrorCard error={config.error} fallback="Failed to load risk limits" onRetry={() => void config.refetch()} />
          )}
        </DataCard>
      </section>

      <DataCard title="Value at risk">
        {varEstimate.data ? (
          varEstimate.data.sample_days < MIN_VAR_SAMPLE_DAYS ? (
            <div className="flex items-center gap-2 text-sm text-ink-2">
              <DataStateBadge state="no_data" />
              Needs {MIN_VAR_SAMPLE_DAYS} days of paper P&amp;L; have {varEstimate.data.sample_days}.
            </div>
          ) : (
            <dl className="flex flex-col gap-1">
              <Row label="VaR 95%">{usdt(varEstimate.data.var_95)}</Row>
              <Row label="VaR 99%">{usdt(varEstimate.data.var_99)}</Row>
              <Row label="CVaR 95%">{usdt(varEstimate.data.cvar_95)}</Row>
            </dl>
          )
        ) : varEstimate.state === 'loading' ? (
          <p className="text-sm text-ink-2">Loading…</p>
        ) : (
          <DataStateErrorCard error={varEstimate.error} fallback="VaR could not be computed" onRetry={() => void varEstimate.refetch()} />
        )}
      </DataCard>

      <section id="activity" aria-labelledby="activity-h" className="scroll-mt-20">
        <DataCard>
          <h2 id="activity-h" className="text-sm font-semibold text-ink-1">
            Recent risk events
          </h2>
          <p className="mt-2 text-sm text-ink-2">
            The full trail — Claude&apos;s actions, approvals and every risk event — lives on{' '}
            <Link href="/activity" className="text-accent hover:underline">
              Activity ›
            </Link>
          </p>
        </DataCard>
      </section>

      <TypedConfirm
        open={dialog === 'release'}
        title="Release the kill switch?"
        token="RELEASE"
        primaryLabel="Release kill switch"
        secondaryLabel="Keep it on"
        pendingLabel="Releasing…"
        error={dialog === 'release' ? dialogError : null}
        onClose={() => show(null)}
        onConfirm={(reason) =>
          attempt(fetchApi<KillSwitchResponse>('/api/trading/kill-switch', { method: 'DELETE', ...json({ confirm: 'RELEASE', reason }) }), (r) => releaseOutcome(r))
        }
      >
        <p>
          Releasing lets sessions start and open positions again. It doesn&apos;t restart stopped sessions.
          {activeTier > 0 && ` Breaker tier ${activeTier} is still active and still blocks entries; reset it separately.`}
        </p>
        <p>{onSince ? `On since ${utc(onSince.ts, now)}.` : 'On since: not in recent events.'}</p>
      </TypedConfirm>

      <TypedConfirm
        open={typeof dialog === 'number'}
        title={`Reset breaker tier ${resetTier}?`}
        token={String(resetTier)}
        primaryLabel={`Reset tier ${resetTier}`}
        secondaryLabel="Keep it"
        pendingLabel="Resetting…"
        error={typeof dialog === 'number' ? dialogError : null}
        onClose={() => show(null)}
        onConfirm={(reason) =>
          attempt(
            fetchApi<CircuitBreaker[]>(`/api/risk/circuit-breakers/${resetTier}/reset`, { method: 'POST', ...json({ reason, confirm_tier: resetTier }) }),
            (r) => breakerResetOutcome(resetTier, r, ks === 'active'),
          )
        }
      >
        <p>
          Resetting rebases the high-water mark to current equity ({usdt(st?.equity)}). Drawdown restarts from 0.00%. It doesn&apos;t release the
          kill switch.
        </p>
      </TypedConfirm>

      <TypedConfirm
        open={dialog === 'ledger'}
        title="Reset the paper ledger?"
        token="RESET"
        primaryLabel="Reset paper ledger"
        secondaryLabel="Cancel"
        pendingLabel="Resetting…"
        error={dialog === 'ledger' ? dialogError : null}
        onClose={() => show(null)}
        onConfirm={(reason) =>
          attempt(fetchApi<RiskStatus>('/api/risk/paper-account/reset', { method: 'POST', ...json({ confirm: 'RESET', reason }) }), (r) => ledgerResetOutcome(r))
        }
      >
        <p>
          This rebases the paper ledger to current equity ({usdt(st?.equity)}): peak, day start and carried P&amp;L. Drawdown and today&apos;s loss restart
          from 0. Trades and history are kept. Breaker tiers, halts and pauses don&apos;t change; reset those separately.
        </p>
      </TypedConfirm>
    </div>
  );
}
