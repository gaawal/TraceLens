declare global {
  interface Window {
    __TRACELENS_CONFIG__?: {
      version?: string;
    };
  }
}

function normalizeVersion(value?: string): string {
  const text = String(value ?? '').trim();
  if (!text) return 'v0.0.1';
  return text.startsWith('v') ? text : `v${text}`;
}

export const APP_VERSION = normalizeVersion(window.__TRACELENS_CONFIG__?.version);
