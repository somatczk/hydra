/**
 * Lightweight console logger with level filtering.
 *
 * Reads NEXT_PUBLIC_LOG_LEVEL env var; defaults to 'info' in dev, 'warn' in prod.
 * Usage: logger.info('API', 'GET /api/foo -> 200 (42ms)')
 */

type LogLevel = 'debug' | 'info' | 'warn' | 'error';

const LEVELS: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

function getThreshold(): number {
  if (typeof process !== 'undefined' && process.env?.NEXT_PUBLIC_LOG_LEVEL) {
    const env = process.env.NEXT_PUBLIC_LOG_LEVEL.toLowerCase() as LogLevel;
    if (env in LEVELS) return LEVELS[env];
  }
  const isProd =
    typeof process !== 'undefined' &&
    process.env?.NODE_ENV === 'production';
  return isProd ? LEVELS.warn : LEVELS.info;
}

const threshold = getThreshold();

function log(level: LogLevel, tag: string, message: string, data?: unknown) {
  if (LEVELS[level] < threshold) return;
  const prefix = `[${tag}]`;
  if (data !== undefined) {
    console[level](prefix, message, data);
  } else {
    console[level](prefix, message);
  }
}

export const logger = {
  debug: (tag: string, message: string, data?: unknown) =>
    log('debug', tag, message, data),
  info: (tag: string, message: string, data?: unknown) =>
    log('info', tag, message, data),
  warn: (tag: string, message: string, data?: unknown) =>
    log('warn', tag, message, data),
  error: (tag: string, message: string, data?: unknown) =>
    log('error', tag, message, data),
};
