import { Button, MenuButton, Stepper } from '../ui/primitives';
import { Ghost } from '../ui/Ghost';
import { Icon } from '../ui/Icon';
import type { ActionId, ActivityVM, ActivityStatus, ArtifactVM, AssistantSuggestionVM } from './types';

export type AssistantStage = 'prepare' | 'review';

const ACTIVITY_ICON: Record<ActivityStatus, { icon: 'check' | 'loader' | 'help' | 'file'; cls: string }> = {
  done: { icon: 'check', cls: 'done' },
  ready: { icon: 'check', cls: 'done' },
  preparing: { icon: 'loader', cls: 'busy' },
  decision: { icon: 'help', cls: 'ask' },
};

export function ArtifactPreview({ artifact, compact = false }: { artifact: ArtifactVM; compact?: boolean }) {
  return (
    <div className={`sg-artifact ${compact ? 'sg-artifact--compact' : ''}`}>
      <div className="sg-artifact__paper">
        <div className="sg-artifact__title">
          <h4>{artifact.title}</h4>
          <span className="sg-artifact__app" title={artifact.destination} aria-hidden="true">
            {artifact.destination.slice(0, 1)}
          </span>
        </div>
        {artifact.sections.map((s) => (
          <div key={s.heading}>
            <p className="sg-artifact__h">▸ {s.heading}</p>
            {s.lines.length > 0 && (
              <ul>
                {s.lines.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export interface AssistantPanelProps {
  collapsed: boolean;
  onToggleCollapsed: () => void;
  suggestion: AssistantSuggestionVM | null;
  stage: AssistantStage;
  onReview: () => void;
  onBack: () => void;
  onDismiss: () => void;
  onChooseAction: (id: ActionId) => void;
  extraChoices: { id: string; label: string }[];
  onExtraChoice: (id: string) => void;
  notice: string | null;
  activity: ActivityVM[];
  onSeeAll: () => void;
  onSafeBurial: () => void;
  onResurrection: () => void;
  canBury: boolean;
  canResurrect: boolean;
  archiveNote: string;
}

export function AssistantPanel(p: AssistantPanelProps) {
  const s = p.suggestion;
  if (p.collapsed) {
    return (
      <aside className="sg-assistant sg-assistant--collapsed ui-card" aria-label="Quiet Assistant (collapsed)">
        <button
          type="button"
          className="sg-assistant__expand"
          aria-expanded="false"
          aria-controls="sg-assistant-body"
          onClick={p.onToggleCollapsed}
        >
          <Ghost size={34} />
          <span className="sg-assistant__expand-text">
            <b>Quiet Assistant</b>
            <small>{s ? '1 suggestion waiting' : 'Nothing to suggest'}</small>
          </span>
          <Icon name="chevronDown" size={18} />
        </button>
        {s && <span className="sg-assistant__pip" aria-hidden="true" />}
      </aside>
    );
  }
  const alts = s?.alternatives.filter((a) => a.id !== 'checklist') ?? [];
  const dest = s?.action.destination;
  return (
    <aside className="sg-assistant ui-card" aria-label="Quiet Assistant" id="sg-assistant-body">
      <div className="sg-assistant__head">
        <Ghost size={44} />
        <div>
          <h2 className="sg-display sg-assistant__title">Quiet Assistant</h2>
          <p>A gentle nudge to turn screenshots into progress.</p>
        </div>
        <button type="button" className="sg-icon-btn sg-icon-btn--sm" aria-expanded="true" aria-label="Collapse assistant" onClick={p.onToggleCollapsed}>
          <Icon name="chevronDown" size={16} style={{ transform: 'rotate(180deg)' }} />
        </button>
      </div>

      {s ? (
        <>
          <section className="sg-suggest" aria-label="Current suggestion">
            <div className="sg-suggest__msg">
              <Icon name="sparkles" size={18} />
              <p>{s.action.message}</p>
            </div>
            {dest && (
              <div className="sg-dest" aria-label={`Destination preview: ${dest.app}, ${dest.path}`}>
                <span className="sg-dest__logo" aria-hidden="true">{dest.app.slice(0, 1)}</span>
                <span className="sg-dest__text">
                  <b>{dest.app}</b>
                  <small>{dest.path}</small>
                </span>
                <span className="sg-dest__status">{dest.connected ? 'Connected' : 'Not connected'}</span>
              </div>
            )}
            <button type="button" className="sg-suggest__dismiss" onClick={p.onDismiss}>
              Not now
            </button>
          </section>

          <div className="sg-alt" role="group" aria-label="Other actions">
            <span className="sg-alt__label">Other actions:</span>
            <div className="sg-alt__row">
              {alts.slice(0, 2).map((a) => (
                <Button key={a.id} size="sm" icon={a.id === 'add_calendar' ? 'calendar' : a.id === 'make_notes' ? 'sparkles' : 'heart'} onClick={() => p.onChooseAction(a.id)}>
                  {a.label}
                </Button>
              ))}
              <MenuButton label="Something else" icon="dots" items={p.extraChoices} onSelect={p.onExtraChoice} className="sg-alt__menu" />
            </div>
          </div>
          {p.notice && (
            <p className="sg-notice" role="status">
              <Icon name="info" size={15} /> {p.notice}
            </p>
          )}

          <Stepper
            steps={[
              { label: 'Prepare', state: p.stage === 'prepare' ? 'current' : 'done' },
              { label: 'Review', state: p.stage === 'review' ? 'current' : 'todo' },
              { label: 'Approve', state: 'todo' },
            ]}
          />

          <section className="sg-prepared" aria-label="Prepared artifact">
            <p className="sg-prepared__label">
              <Icon name="file" size={15} /> Prepared: {s.artifact.kindLabel}
              <span className="sg-demo-flag sg-demo-flag--sm">Sample</span>
            </p>
            <ArtifactPreview artifact={s.artifact} />
            {p.stage === 'prepare' ? (
              <Button variant="primary" trailingIcon="arrowRight" className="sg-prepared__cta" onClick={p.onReview}>
                Review draft
              </Button>
            ) : (
              <div className="sg-prepared__pair">
                <Button onClick={p.onBack}>Back</Button>
                <Button variant="primary" disabled aria-describedby="sg-approve-why" className="sg-prepared__cta">
                  Approve
                </Button>
              </div>
            )}
            <p className="sg-prepared__note" id="sg-approve-why">
              <Icon name="info" size={14} />
              {p.stage === 'prepare'
                ? 'Demo preview only. Nothing is written to any app automatically.'
                : `Approving is disabled in demo mode. ${dest ? `${dest.app} is not connected.` : 'Nothing is saved.'}`}
            </p>
          </section>
        </>
      ) : (
        <p className="sg-suggest--none">
          Nothing to suggest right now. Select screenshots, or I will speak up when I notice something.
        </p>
      )}

      <section className="sg-activity" aria-label="Recent Activity">
        <div className="sg-activity__head">
          <h3>Recent Activity</h3>
          <button type="button" className="sg-link" onClick={p.onSeeAll}>
            See all
          </button>
        </div>
        <ul>
          {p.activity.map((a) => {
            const ic = ACTIVITY_ICON[a.status];
            return (
              <li key={a.id}>
                <span className={`sg-activity__ic sg-activity__ic--${ic.cls}`} aria-hidden="true">
                  <Icon name={ic.icon} size={13} strokeWidth={2.6} />
                </span>
                <span className="sg-activity__label">{a.label}</span>
                <span className="sg-activity__detail">{a.detail}</span>
                <span className="sg-activity__when">{a.when}</span>
              </li>
            );
          })}
        </ul>
        <p className="sg-activity__demo">Demo activity, not from your account.</p>
      </section>

      <div className="sg-assistant__foot">
        <Button icon="box" onClick={p.onSafeBurial} disabled={!p.canBury}>
          Safe Burial
        </Button>
        <Button icon="restore" onClick={p.onResurrection} disabled={!p.canResurrect}>
          Resurrection
        </Button>
        <p>{p.archiveNote}</p>
      </div>
    </aside>
  );
}
