import { Badge, Button, Card } from '../ui/primitives';
import { Icon } from '../ui/Icon';
import { ShotArt } from '../ui/ShotArt';
import { ArtifactPreview } from './Assistant';
import type { ArtifactStatus, ConnectionVM, CreationVM, ShotVM } from './types';

function ViewHeading({ title, sub }: { title: string; sub: string }) {
  return (
    <div className="sg-viewhead">
      <h2 className="sg-display sg-wshead__title">{title}</h2>
      <p className="sg-wshead__sub">{sub}</p>
    </div>
  );
}

const STATUS_COPY: Record<ArtifactStatus, { label: string; tone: 'peach' | 'violet' | 'muted' }> = {
  awaiting_approval: { label: 'Awaiting your approval', tone: 'peach' },
  draft_ready: { label: 'Draft ready', tone: 'violet' },
  preparing: { label: 'Preparing', tone: 'muted' },
};

export function CreationsView({ items, onPreview }: { items: CreationVM[]; onPreview: (id: string, opener: HTMLElement) => void }) {
  const pending = items.filter((c) => c.status === 'awaiting_approval');
  const rest = items.filter((c) => c.status !== 'awaiting_approval');
  const card = (c: CreationVM) => (
    <Card key={c.id} className="sg-creation">
      <div className="sg-creation__head">
        <Badge tone={STATUS_COPY[c.status].tone}>{STATUS_COPY[c.status].label}</Badge>
        <Badge tone="muted">Sample</Badge>
      </div>
      <h3>{c.artifact.title}</h3>
      <p className="sg-creation__kind">{c.artifact.kindLabel}</p>
      <ArtifactPreview artifact={c.artifact} compact />
      <p className="sg-creation__src">From: {c.artifact.sourceTitle}</p>
      <Button size="sm" icon="file" onClick={(e) => onPreview(c.id, e.currentTarget)} disabled={c.status === 'preparing'}>
        {c.status === 'preparing' ? 'Preparing…' : 'Open preview'}
      </Button>
    </Card>
  );
  return (
    <section aria-labelledby="creations-h">
      <ViewHeading title="Creations" sub="Drafts the assistant prepared. Nothing is saved anywhere until you approve." />
      <h3 id="creations-h" className="sg-sectionlabel">
        Pending approvals ({pending.length})
      </h3>
      <div className="sg-gallery sg-gallery--wide">{pending.length ? pending.map(card) : <p className="sg-muted">Nothing is waiting for approval.</p>}</div>
      <h3 className="sg-sectionlabel">Generated drafts</h3>
      <div className="sg-gallery sg-gallery--wide">{rest.map(card)}</div>
    </section>
  );
}

export function CryptView({
  shots,
  onRestore,
  note,
}: {
  shots: ShotVM[];
  onRestore: (id: string) => void;
  note: string;
}) {
  return (
    <section aria-label="The Crypt">
      <ViewHeading title="The Crypt" sub="Archived screenshots rest here. Restore any of them at any time." />
      <p className="sg-note">
        <Icon name="info" size={15} /> {note}
      </p>
      {shots.length === 0 ? (
        <div className="sg-empty" role="status">
          <Icon name="crypt" size={28} />
          <p>The Crypt is empty. Buried screenshots will appear here.</p>
        </div>
      ) : (
        <div className="sg-gallery">
          {shots.map((s) => (
            <article key={s.id} className="sg-shot ui-card sg-shot--crypt" aria-label={s.title}>
              <div className="sg-shot__thumb">
                <ShotArt kind={s.art} src={s.imageSrc} alt={`Preview of ${s.title}`} />
              </div>
              <div className="sg-shot__meta">
                <h3>{s.title}</h3>
                <p>
                  {s.savedLabel} · {s.sizeLabel}
                </p>
              </div>
              <div className="sg-shot__foot">
                <Button size="sm" icon="restore" onClick={() => onRestore(s.id)} aria-label={`Restore ${s.title}`}>
                  Restore
                </Button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export function ConnectionsView({ items }: { items: ConnectionVM[] }) {
  return (
    <section aria-label="Connections">
      <ViewHeading title="Connections" sub="Where approved drafts can go. No external app is connected yet." />
      <div className="sg-connections">
        {items.map((c) => (
          <Card key={c.id} className="sg-conn">
            <span className="sg-conn__logo" aria-hidden="true">{c.name.slice(0, 1)}</span>
            <div className="sg-conn__body">
              <h3>{c.name}</h3>
              <p>{c.description}</p>
            </div>
            <Badge tone="muted">{c.status === 'not_connected' ? 'Not connected' : 'Coming later'}</Badge>
            <Button size="sm" disabled aria-label={`Connect ${c.name} (unavailable in demo)`}>
              <Icon name="plug" size={15} /> Connect
            </Button>
          </Card>
        ))}
        <Card className="sg-conn sg-conn--ai">
          <span className="sg-conn__logo" aria-hidden="true">✦</span>
          <div className="sg-conn__body">
            <h3>AI provider</h3>
            <p>Demo AI mode. No live AI calls are made and no screenshot content leaves your browser.</p>
          </div>
          <Badge tone="violet">Demo mode</Badge>
        </Card>
      </div>
    </section>
  );
}
