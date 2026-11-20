/** Demo mode is frontend-only (synthetic fixtures, no API access). */
export function isDemoMode(): boolean {
  if (import.meta.env.VITE_UI_MODE === 'demo') return true;
  return typeof window !== 'undefined' && new URLSearchParams(window.location.search).get('demo') === '1';
}
