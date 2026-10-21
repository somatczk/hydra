'use client';

import { ConfirmDialog } from '@/components/shell/ConfirmDialog';
import { plural, sessionName, type PositionItem, type SessionResponse } from '@/lib/derive';
import { money, qty } from '@/lib/format';
import { baseAsset } from '@/lib/outcomes';

interface StopSessionDialogProps {
  session: SessionResponse;
  /** Null when the session's detail couldn't be read. */
  positions: PositionItem[] | null;
  open: boolean;
  onClose: () => void;
  onStop: () => Promise<void>;
}

/** C-STOP-1: names what the stop closes before it is sent. */
export function StopSessionDialog({ session, positions, open, onClose, onStop }: StopSessionDialogProps) {
  const held = positions?.length ?? 0;
  const worth = (positions ?? []).reduce((sum, p) => sum + p.quantity * p.avg_entry_price + p.unrealized_pnl, 0);
  return (
    <ConfirmDialog
      open={open}
      destructive={held > 0}
      title={`Stop ${sessionName(session)}?`}
      primaryLabel={held > 0 ? `Stop and close ${plural(held, 'position')}` : 'Stop session'}
      secondaryLabel="Keep running"
      pendingLabel="Stopping…"
      pendingHint="Closing this won't cancel the stop."
      onClose={onClose}
      onConfirm={async () => {
        await onStop();
        onClose();
      }}
    >
      {positions === null ? (
        <p>It closes whatever this session holds at market (paper), then stops. Open positions: unknown.</p>
      ) : held > 0 ? (
        <p>
          It closes <b>{plural(held, 'open position')}</b> ({positions.map((p) => `${qty(p.quantity)} ${baseAsset(p.symbol)}`).join(', ')},
          about {money(worth)} USDT at the last closed bar&apos;s price) at market (paper), then stops. The strategy stays saved.
        </p>
      ) : (
        <p>It holds no open positions. It stops trading; the strategy stays saved.</p>
      )}
    </ConfirmDialog>
  );
}
