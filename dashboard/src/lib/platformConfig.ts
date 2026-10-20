export const TRADING_MODE_OPTIONS = [
  { value: 'paper', label: 'Paper Trading' },
  { value: 'live', label: 'Live Trading' },
] as const;

export const CLEAR_WEBHOOK_SECRET_WARNING =
  'Clear the webhook secret?\n\nThe signal webhook is disabled without a secret: it rejects ' +
  'every signal until a new secret is saved.';

