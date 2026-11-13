import type { OperationStatus } from '../api/ai';

const LABELS: Record<OperationStatus, { glyph: string; text: string }> = {
  pending: { glyph: '\u25CC', text: 'Pending' },
  successful: { glyph: '\u2714', text: 'Successful' },
  failed: { glyph: '\u2716', text: 'Failed' },
  uncertain: { glyph: '?', text: 'Uncertain' },
};

/**
 * Operation status shown as a glyph AND a text label (Req 11.5): colour is only
 * decoration, never the sole carrier of meaning.
 */
export function StatusBadge({ status }: { status: OperationStatus }) {
  const { glyph, text } = LABELS[status];
  return (
    <span className={`ai-status ai-status--${status}`} data-status={status}>
      <span aria-hidden="true" className="ai-status__glyph">
        {glyph}
      </span>{' '}
      <span className="ai-status__text">Status: {text}</span>
    </span>
  );
}
