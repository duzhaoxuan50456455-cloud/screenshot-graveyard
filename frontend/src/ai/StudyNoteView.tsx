import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getStudyNote,
  listStudyNoteVersions,
  saveStudyNoteEdit,
  studyNoteExportUrl,
  type StudyNoteContent,
  type StudyNoteVersion,
  type VersionInfo,
} from '../api/ai';
import { imageUrl } from '../api/client';
import { describeRequestFailure } from './messages';

const toLines = (text: string): string[] =>
  text.split(/\r?\n/).filter((line) => line.trim() !== '');
const fromLines = (items: string[]): string => items.join('\n');

interface Draft {
  topic: string;
  mainConcepts: string;
  code: string[];
  explanations: string;
  pitfalls: string;
  uncertain: string;
}
const toDraft = (c: StudyNoteContent): Draft => ({
  topic: c.topic,
  mainConcepts: fromLines(c.main_concepts),
  code: [...c.extracted_code],
  explanations: fromLines(c.code_explanations),
  pitfalls: fromLines(c.pitfalls),
  uncertain: fromLines(c.uncertain_content),
});

function originLabel(origin: string): string {
  return origin === 'manual_edit' ? 'edited by you' : 'AI-generated';
}

interface Props {
  artifactId: number;
  onVersionsChanged?: () => void;
}

export function StudyNoteView({ artifactId, onVersionsChanged }: Props) {
  const [versions, setVersions] = useState<VersionInfo[]>([]);
  const [shown, setShown] = useState<StudyNoteVersion | null>(null);
  const [load, setLoad] = useState<'loading' | 'ready' | 'error'>('loading');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);

  const refresh = useCallback(
    async (versionId?: number) => {
      const [v, n] = await Promise.all([
        listStudyNoteVersions(artifactId),
        getStudyNote(artifactId, versionId),
      ]);
      if (!v.ok || !n.ok) {
        setLoad('error');
        return;
      }
      setVersions(v.data);
      setShown(n.data);
      setLoad('ready');
    },
    [artifactId],
  );

  useEffect(() => {
    setLoad('loading');
    setEditing(false);
    setMessage(null);
    void refresh();
  }, [refresh]);

  const startEdit = () => {
    if (!shown) return;
    setDraft(toDraft(shown.content));
    setEditing(true);
    setMessage(null);
  };

  const onSave = async () => {
    if (!draft || savingRef.current) return; // duplicate-click prevention
    savingRef.current = true;
    setSaving(true);
    try {
      const content: StudyNoteContent = {
        topic: draft.topic.trim(),
        main_concepts: toLines(draft.mainConcepts),
        // Code blocks are kept EXACTLY as typed (indentation, tabs, symbols).
        extracted_code: draft.code.filter((c) => c.trim() !== ''),
        code_explanations: toLines(draft.explanations),
        pitfalls: toLines(draft.pitfalls),
        uncertain_content: toLines(draft.uncertain),
      };
      const res = await saveStudyNoteEdit(artifactId, content);
      if (!res.ok) {
        setMessage({ kind: 'error', text: describeRequestFailure(res) });
        return;
      }
      setEditing(false);
      setMessage({ kind: 'info', text: `Saved as version ${res.data.versionNumber}. Earlier versions are kept.` });
      await refresh();
      onVersionsChanged?.();
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  if (load === 'loading') return <p className="ai-hint">Loading study note…</p>;
  if (load === 'error' || !shown) {
    return (
      <p role="alert" className="gy__status gy__status--error">
        Could not load this study note.
      </p>
    );
  }
  const c = shown.content;
  const isCurrent = versions.find((v) => v.versionId === shown.versionId)?.isCurrent ?? false;

  return (
    <article className="ai-note" aria-labelledby="ai-note-h">
      <h4 id="ai-note-h">{c.topic}</h4>
      <p className="ai-hint">
        Version {shown.versionNumber} ({originLabel(shown.origin)})
        {isCurrent ? ' \u2014 current' : ' \u2014 earlier version, read only'}
      </p>

      {versions.length > 1 && (
        <nav aria-label="Study note versions">
          <ul className="ai-versions">
            {versions.map((v) => (
              <li key={v.versionId}>
                <button
                  type="button"
                  className="gy-btn"
                  aria-pressed={v.versionId === shown.versionId}
                  onClick={() => {
                    setEditing(false);
                    void refresh(v.versionId);
                  }}
                >
                  v{v.versionNumber} ({originLabel(v.origin)}){v.isCurrent ? ' \u2014 current' : ''}
                </button>
              </li>
            ))}
          </ul>
        </nav>
      )}

      {message && (
        <p
          className={`gy__notice gy__notice--${message.kind}`}
          role={message.kind === 'error' ? 'alert' : 'status'}
        >
          {message.text}
        </p>
      )}

      {!editing ? (
        <>
          <Section title="Main concepts" items={c.main_concepts} />
          <section aria-labelledby="ai-code-h">
            <h5 id="ai-code-h">Extracted code</h5>
            {c.extracted_code.length === 0 ? (
              <p className="ai-hint">None.</p>
            ) : (
              c.extracted_code.map((code, i) => (
                <pre key={i} className="ai-code">
                  <code>{code}</code>
                </pre>
              ))
            )}
          </section>
          <Section title="Code explanations" items={c.code_explanations} />
          <Section title="Important pitfalls" items={c.pitfalls} />
          <Section
            title="Uncertain/ambiguous content"
            items={c.uncertain_content}
            marker="?"
            emptyText="Nothing was flagged as uncertain."
          />

          <section aria-labelledby="ai-src-h">
            <h5 id="ai-src-h">Sources (in the order you selected them)</h5>
            <ol className="ai-sources">
              {shown.sourceScreenshotIds.map((sid) => (
                <li key={sid}>
                  <img src={imageUrl(sid)} alt={`Source screenshot #${sid}`} className="ai-sources__thumb" />
                  <a href={imageUrl(sid)} target="_blank" rel="noopener noreferrer">
                    Open original screenshot #{sid}
                  </a>
                </li>
              ))}
            </ol>
          </section>

          <div className="ai-note__actions">
            <button type="button" className="gy-btn" onClick={startEdit}>
              {isCurrent ? 'Edit this note' : `Edit from version ${shown.versionNumber}`}
            </button>
            <a
              className="gy-btn ai-linkbtn"
              href={studyNoteExportUrl(artifactId, shown.versionId)}
              download
            >
              Export version {shown.versionNumber} as Markdown
            </a>
          </div>
        </>
      ) : (
        draft && (
          <form
            className="ai-edit"
            onSubmit={(e) => {
              e.preventDefault();
              void onSave();
            }}
          >
            <label htmlFor="note-topic">Topic</label>
            <input
              id="note-topic"
              value={draft.topic}
              onChange={(e) => setDraft({ ...draft, topic: e.target.value })}
            />
            <label htmlFor="note-concepts">Main concepts (one per line)</label>
            <textarea
              id="note-concepts"
              rows={4}
              value={draft.mainConcepts}
              onChange={(e) => setDraft({ ...draft, mainConcepts: e.target.value })}
            />
            <fieldset>
              <legend>Extracted code (kept exactly as typed)</legend>
              {draft.code.map((code, i) => (
                <div key={i} className="ai-edit__code">
                  <label htmlFor={`note-code-${i}`}>Code block {i + 1}</label>
                  <textarea
                    id={`note-code-${i}`}
                    rows={5}
                    spellCheck={false}
                    className="ai-code"
                    value={code}
                    onChange={(e) =>
                      setDraft({
                        ...draft,
                        code: draft.code.map((x, j) => (j === i ? e.target.value : x)),
                      })
                    }
                  />
                  <button
                    type="button"
                    className="gy-btn"
                    onClick={() => setDraft({ ...draft, code: draft.code.filter((_, j) => j !== i) })}
                  >
                    Remove code block {i + 1}
                  </button>
                </div>
              ))}
              <button type="button" className="gy-btn" onClick={() => setDraft({ ...draft, code: [...draft.code, ''] })}>
                Add code block
              </button>
            </fieldset>
            <label htmlFor="note-expl">Code explanations (one per line)</label>
            <textarea
              id="note-expl"
              rows={3}
              value={draft.explanations}
              onChange={(e) => setDraft({ ...draft, explanations: e.target.value })}
            />
            <label htmlFor="note-pit">Important pitfalls (one per line)</label>
            <textarea
              id="note-pit"
              rows={3}
              value={draft.pitfalls}
              onChange={(e) => setDraft({ ...draft, pitfalls: e.target.value })}
            />
            <label htmlFor="note-unc">Uncertain/ambiguous content (one per line)</label>
            <textarea
              id="note-unc"
              rows={3}
              value={draft.uncertain}
              onChange={(e) => setDraft({ ...draft, uncertain: e.target.value })}
            />
            <div className="ai-note__actions">
              <button type="submit" className="gy-btn gy-btn--revive" disabled={saving || draft.topic.trim() === ''}>
                {saving ? 'Saving\u2026' : 'Save as new version'}
              </button>
              <button type="button" className="gy-btn" onClick={() => setEditing(false)} disabled={saving}>
                Cancel
              </button>
            </div>
            <p className="ai-hint">Saving adds a new version. The source screenshots and earlier versions are never changed.</p>
          </form>
        )
      )}
    </article>
  );
}

function Section({
  title,
  items,
  marker,
  emptyText = 'None.',
}: {
  title: string;
  items: string[];
  marker?: string;
  emptyText?: string;
}) {
  const hid = `ai-sec-${title.replace(/[^a-z]/gi, '').toLowerCase()}`;
  return (
    <section aria-labelledby={hid}>
      <h5 id={hid}>{title}</h5>
      {items.length === 0 ? (
        <p className="ai-hint">{emptyText}</p>
      ) : (
        <ul>
          {items.map((item, i) => (
            <li key={i}>
              {marker && <span aria-hidden="true">{marker} </span>}
              {item}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
