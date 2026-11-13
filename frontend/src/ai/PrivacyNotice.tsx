import { useEffect, useState } from 'react';
import { getProviderMode, type ProviderMode } from '../api/ai';

/**
 * General privacy warning shown BEFORE any AI analysis (Req 9.1, 9.3).
 * Distinguishes private storage from external AI processing and does not imply
 * that withdrawing consent recalls anything already sent.
 */
// This hook intentionally shares the provider-status request with both AI UI
// components; it is not a React component itself.
// eslint-disable-next-line react-refresh/only-export-components
export function useProviderMode(): ProviderMode | 'unknown' {
  const [mode, setMode] = useState<ProviderMode | 'unknown'>('unknown');
  useEffect(() => {
    void getProviderMode().then((r) => setMode(r.ok ? r.data : 'unknown'));
  }, []);
  return mode;
}

export function PrivacyNotice({
  scope,
  mode,
}: {
  scope: 'one' | 'many';
  mode: ProviderMode | 'unknown';
}) {
  const target = scope === 'one' ? 'this screenshot' : 'every selected screenshot';
  return (
    <div className="ai-privacy" role="note" aria-label="Privacy notice">
      <p className="ai-privacy__title">
        <strong>Before you Revive: provider and privacy</strong>
      </p>
      <ul className="ai-privacy__list">
        <li>
          Your {scope === 'one' ? 'screenshot is' : 'screenshots are'} stored
          privately in your workspace. Storing, archiving and restoring never
          need AI.
        </li>
        <li>
          {mode === 'fake'
            ? 'Demo mode uses a deterministic fake adapter. No image is sent to an external AI provider, and results do not interpret the screenshot content.'
            : mode === 'openai'
              ? `Authorized image content of ${target} may be sent to an external AI provider.`
              : 'Provider mode could not be confirmed. Processing may be local or external; no assumption is made.'}
        </li>
        <li>
          {mode === 'fake'
            ? 'You can still use consent to exercise the full demo workflow. The simulated result is not an understanding of your image.'
            : mode === 'openai'
              ? `Screenshots may contain private details such as names, accounts or passwords. We cannot know that before analysis, so only continue if you are comfortable sending ${scope === 'one' ? 'it' : 'them'}.`
              : 'Do not rely on this screen to decide where image content is processed while provider status is unavailable.'}
        </li>
        <li>
          You can withdraw consent at any time. That stops future AI use but
          {mode === 'openai' ? ' cannot take back anything already sent.' : ' does not override the server-side authorization check.'}
        </li>
        <li>
          AI results are suggestions and can be wrong. An Epitaph is a guess at
          why you saved something, not a fact.
        </li>
      </ul>
    </div>
  );
}
