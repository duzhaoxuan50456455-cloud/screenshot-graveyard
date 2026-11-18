import { useRef, useState } from 'react';
import { editAnalysis, type Analysis } from '../api/ai';
import { describeRequestFailure } from './messages';

interface Props {
  screenshotId: number;
  /** Display name used in accessible labels. */
  name: string;
  /** The Epitaph as the server currently stores it. */
  epitaph: string;
  /** Called with the server's response (the source of truth) after a save. */
  onSaved: (analysis: Analysis) => void;
}

/**
 * Inline Epitaph editor for a screenshot card (Req 5.5).
 *
 * It shows what is really stored, edits it only through the existing append-only
 * edit endpoint (a new version; earlier versions are kept), and never starts AI
 * processing. The displayed value changes only after the server confirms a save.
 */
export function EpitaphEditor({ screenshotId, name, epitaph, onSaved }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(epitaph);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const fieldId = `epitaph-edit-${screenshotId}`;

  const open = () => {
    setDraft(epitaph); // always start from the real stored value
    setMessage(null);
    setEditing(true);
  };

  const save = async () => {
    if (savingRef.current) return; // duplicate-click prevention
    const text = draft.trim();
    if (text === '') return;
    if (text === epitaph) {
      setMessage({ kind: 'info', text: 'Nothing changed.' });
      return;
    }
    savingRef.current = true;
    setSaving(true);
    try {
      const res = await editAnalysis(screenshotId, { epitaphText: text });
      if (!res.ok) {
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
        return;
      }
      setEditing(false);
      setMessage({ kind: 'info', text: 'Epitaph saved as a new version. Earlier versions are kept.' });
      onSaved(res.data);
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <div className="gy-epitaph">
      {!editing ? (
        <>
          <button
            type="button"
            className="gy-btn"
            onClick={open}
            aria-label={`Edit Epitaph of ${name}`}
          >
            Edit Epitaph
          </button>
          {message && (
            <p
              className={`gy__notice gy__notice--${message.kind}`}
              role={message.kind === 'error' ? 'alert' : 'status'}
            >
              {message.text}
            </p>
          )}
        </>
      ) : (
        <form
          className="ai-edit"
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <label htmlFor={fieldId}>Epitaph for {name}</label>
          <textarea
            id={fieldId}
            rows={3}
            maxLength={500}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          {message && (
            <p
              className={`gy__notice gy__notice--${message.kind}`}
              role={message.kind === 'error' ? 'alert' : 'status'}
            >
              {message.text}
            </p>
          )}
          <div className="gy-card__actions">
            <button type="submit" className="gy-btn gy-btn--revive" disabled={saving || draft.trim() === ''}>
              {saving ? 'Saving…' : 'Save Epitaph'}
            </button>
            <button type="button" className="gy-btn" onClick={() => setEditing(false)} disabled={saving}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
