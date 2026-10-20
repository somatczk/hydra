export const TRADING_MODE_OPTIONS = [
  { value: 'paper', label: 'Paper Trading' },
  { value: 'live', label: 'Live Trading' },
] as const;

export const CLEAR_WEBHOOK_SECRET_WARNING =
  'Clear the webhook secret?\n\nThe signal webhook will accept payloads without a secret, ' +
  'so anyone who can reach it can send trade signals to running sessions, including live ones.';

