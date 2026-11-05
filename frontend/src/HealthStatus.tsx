import { useEffect, useState } from 'react';
import { getHealth } from './api/client';

type ViewState =
  | { phase: 'checking' }
  | { phase: 'online'; status: string }
  | { phase: 'offline'; error: string };

export function HealthStatus() {
  const [state, setState] = useState<ViewState>({ phase: 'checking' });

  useEffect(() => {
    let active = true;
    getHealth().then((result) => {
      if (!active) return;
      if (result.ok && result.status === 'ok') {
        setState({ phase: 'online', status: result.status });
      } else if (result.ok) {
        // 2xx with an unexpected status value — do not report healthy.
        setState({ phase: 'offline', error: `Unexpected status: ${result.status}` });
      } else {
        setState({ phase: 'offline', error: result.error });
      }
    });
    return () => {
      active = false;
    };
  }, []);

  let label: string;
  let indicator: string;
  if (state.phase === 'checking') {
    label = 'Checking...';
    indicator = '•';
  } else if (state.phase === 'online') {
    label = 'Backend Online';
    indicator = '●';
  } else {
    label = `Backend Offline / Error (${state.error})`;
    indicator = '×';
  }

  return (
    <p
      className={`health health--${state.phase}`}
      role="status"
      aria-live="polite"
      data-phase={state.phase}
    >
      <span aria-hidden="true" className="health__indicator">
        {indicator}
      </span>{' '}
      {label}
    </p>
  );
}

export default HealthStatus;
