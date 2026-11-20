import { useRef, useState, type DragEvent, type ReactNode } from 'react';
import { Button, Checkbox, MenuButton, SelectField } from '../ui/primitives';
import { Icon } from '../ui/Icon';
import { ShotArt } from '../ui/ShotArt';
import { WorkspaceArt } from '../ui/HeaderArt';
import type { ActionDef, ActionId, ShotVM } from './types';

/* ---------------- Upload drop zone ---------------- */

export function UploadDropZone({
  onFiles,
  hint,
  disabled = false,
  filterByMime = true,
}: {
  onFiles: (files: File[]) => void;
  hint: string;
  disabled?: boolean;
  filterByMime?: boolean;
}) {
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const take = (list: FileList | null) => {
    if (disabled) return;
    const files = Array.from(list ?? []).filter((f) => !filterByMime || f.type.startsWith('image/'));
    if (files.length) onFiles(files);
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    take(e.dataTransfer.files);
  };
  return (
    <div
      className={`sg-drop ${over ? 'sg-drop--over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      data-testid="drop-zone"
    >
      <div className="sg-drop__row">
        <Icon name="upload" size={26} />
        <strong>Upload screenshots</strong>
        <span className="sg-drop__or">or drag &amp; drop here</span>
        <Button className="sg-drop__browse" disabled={disabled} onClick={() => inputRef.current?.click()}>
          Browse
        </Button>
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          multiple
          disabled={disabled}
          className="sg-sr-only"
          tabIndex={-1}
          aria-label="Choose screenshot files"
          onChange={(e) => {
            take(e.target.files);
            e.target.value = '';
          }}
        />
      </div>
      <p className="sg-drop__hint">{hint}</p>
    </div>
  );
}

/* ---------------- Filter / sort controls ---------------- */

import { type FilterState } from './filters';

export function FilterBar({
  value,
  onChange,
  suggestionOptions,
}: {
  value: FilterState;
  onChange: (next: FilterState) => void;
  suggestionOptions: { value: string; label: string }[];
}) {
  const set = (k: keyof FilterState) => (v: string) => onChange({ ...value, [k]: v });
  return (
    <div className="sg-filters" role="group" aria-label="Filter and sort screenshots">
      <SelectField
        icon="grid"
        label="Screenshot type"
        value={value.topic}
        onChange={set('topic')}
        options={[
          { value: 'all', label: 'All screenshots' },
          { value: 'lecture', label: 'Lectures' },
          { value: 'event', label: 'Events' },
          { value: 'shopping', label: 'Shopping' },
          { value: 'chat', label: 'Chats' },
          { value: 'other', label: 'Other' },
        ]}
      />
      <SelectField icon="tag" label="Suggestion" value={value.suggestion} onChange={set('suggestion')} options={suggestionOptions} />
      <SelectField
        icon="calendar"
        label="Time range"
        value={value.time}
        onChange={set('time')}
        options={[
          { value: 'all', label: 'All time' },
          { value: '1', label: 'Today' },
          { value: '3', label: 'Past 3 days' },
          { value: '7', label: 'Past week' },
        ]}
      />
      <span className="sg-filters__spacer" />
      <SelectField
        label="Sort order"
        prefix="Sort: "
        value={value.sort}
        onChange={set('sort')}
        options={[
          { value: 'newest', label: 'Newest' },
          { value: 'oldest', label: 'Oldest' },
          { value: 'name', label: 'Name' },
        ]}
      />
    </div>
  );
}

/* ---------------- Cards + gallery ---------------- */

export function ScreenshotCard({
  shot,
  selected,
  actionLabel,
  onToggle,
  onOpen,
  extra,
}: {
  shot: ShotVM;
  selected: boolean;
  actionLabel?: string;
  onToggle?: () => void;
  onOpen: (opener: HTMLElement) => void;
  /** Optional card-level action row (e.g. Resurrection in The Crypt). */
  extra?: ReactNode;
}) {
  const icon = shot.suggestedAction === 'add_calendar' ? 'calendar' : shot.suggestedAction === 'make_notes' ? 'sparkles' : 'heart';
  return (
    <article className={`sg-shot ui-card ${selected ? 'ui-card--selected' : ''}`} aria-label={shot.title}>
      <div className="sg-shot__thumb">
        <ShotArt kind={shot.art} src={shot.imageSrc} alt={`Preview of ${shot.title}`} />
        {onToggle && (
          <Checkbox className="sg-shot__check" checked={selected} onChange={onToggle} label={`Select ${shot.title}`} />
        )}
        <button
          type="button"
          className="sg-shot__expand"
          aria-label={`Open details for ${shot.title}`}
          onClick={(e) => onOpen(e.currentTarget)}
        >
          <Icon name="expand" size={14} />
        </button>
      </div>
      <div className="sg-shot__meta">
        <h3>{shot.title}</h3>
        <p>
          {shot.savedLabel} · {shot.sizeLabel}
        </p>
      </div>
      <div className="sg-shot__foot">
        {actionLabel ? (
          <span className="sg-chip">
            <Icon name={icon} size={14} />
            Suggested: <b>{actionLabel}</b>
          </span>
        ) : (
          <span className="sg-chip sg-chip--quiet">
            {shot.statusLabel ?? (shot.local ? 'Local preview, not uploaded' : 'No suggestion')}
          </span>
        )}
        <button type="button" className="sg-link" onClick={(e) => onOpen(e.currentTarget)} aria-label={`View ${shot.title}`}>
          View <Icon name="arrowRight" size={14} />
        </button>
      </div>
      {extra}
    </article>
  );
}

export function ScreenshotGallery({
  shots,
  selectedIds = [],
  actions,
  onToggle,
  onOpen,
  emptyMessage = 'No screenshots match these filters.',
  renderExtra,
}: {
  shots: ShotVM[];
  selectedIds?: string[];
  actions?: Record<ActionId, ActionDef>;
  onToggle?: (id: string) => void;
  onOpen: (id: string, opener: HTMLElement) => void;
  emptyMessage?: string;
  renderExtra?: (shot: ShotVM) => ReactNode;
}) {
  if (shots.length === 0) {
    return (
      <div className="sg-empty" role="status">
        <Icon name="image" size={28} />
        <p>{emptyMessage}</p>
      </div>
    );
  }
  return (
    <div className="sg-gallery">
      {shots.map((s) => (
        <ScreenshotCard
          key={s.id}
          shot={s}
          selected={selectedIds.includes(s.id)}
          actionLabel={s.suggestedAction ? actions?.[s.suggestedAction].label : undefined}
          onToggle={onToggle ? () => onToggle(s.id) : undefined}
          onOpen={(el) => onOpen(s.id, el)}
          extra={renderExtra?.(s)}
        />
      ))}
    </div>
  );
}

/* ---------------- Floating contextual selection bar ---------------- */

export function SelectionBar({
  count,
  onPrepare,
  onChoose,
  onClear,
  choices,
}: {
  count: number;
  onPrepare: () => void;
  onChoose: (id: string) => void;
  onClear: () => void;
  choices: { id: string; label: string }[];
}) {
  return (
    <div className="sg-selbar" role="region" aria-label="Selection actions">
      <span className="sg-selbar__count" role="status">
        <span className="sg-selbar__tick" aria-hidden="true">
          <Icon name="check" size={13} strokeWidth={3} />
        </span>
        {count} selected
      </span>
      <Button variant="primary" icon="sparkles" onClick={onPrepare}>
        Prepare for Review
      </Button>
      <MenuButton label="Something else" items={choices} onSelect={onChoose} placement="up" />
      <button type="button" className="sg-selbar__close" aria-label="Clear selection" onClick={onClear}>
        <Icon name="close" size={18} />
      </button>
    </div>
  );
}

export function WorkspaceHeading({ demo }: { demo: boolean }) {
  return (
    <div className="sg-wshead">
      <div className="sg-wshead__top">
        <h2 className="sg-display sg-wshead__title">Workspace</h2>
        {demo && <span className="sg-demo-flag">Demo data</span>}
      </div>
      <p className="sg-wshead__sub">Turn your screenshots into something done.</p>
      <WorkspaceArt />
    </div>
  );
}
