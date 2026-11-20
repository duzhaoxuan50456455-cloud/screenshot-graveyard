import type { KeyboardEvent } from 'react';
import { Ghost } from '../ui/Ghost';
import { HeaderArt } from '../ui/HeaderArt';
import { Icon, type IconName } from '../ui/Icon';
import type { ViewId } from './types';

export function AppHeader({ modeLabel, demo = true }: { modeLabel: string; demo?: boolean }) {
  return (
    <header className="sg-header">
      <HeaderArt />
      <div className="sg-header__inner">
        <div className="sg-brand">
          <Ghost crown size={72} />
          <div>
            <h1 className="sg-display sg-brand__title">Screenshot Graveyard</h1>
            <p className="sg-brand__tag">Where screenshots finish their unfinished business.</p>
          </div>
        </div>
        <span className="sg-mode-pill" title={demo ? 'No live AI calls are made in this mode' : undefined}>
          <Icon name="sparkles" size={15} />
          {modeLabel}
        </span>
        <div className="sg-header__actions">
          <button type="button" className="sg-icon-btn" aria-label="Notifications (coming later)" aria-disabled="true">
            <Icon name="bell" size={19} />
            <span className="sg-icon-btn__dot" aria-hidden="true" />
          </button>
          <button type="button" className="sg-icon-btn" aria-label="Account (coming later)" aria-disabled="true">
            <Icon name="user" size={19} />
          </button>
        </div>
      </div>
    </header>
  );
}

const TABS: { id: ViewId; label: string; icon: IconName }[] = [
  { id: 'workspace', label: 'Workspace', icon: 'layers' },
  { id: 'creations', label: 'Creations', icon: 'sparkles' },
  { id: 'crypt', label: 'The Crypt', icon: 'crypt' },
  { id: 'connections', label: 'Connections', icon: 'link' },
];

export function NavTabs({
  active,
  onNavigate,
  cryptCount,
  pendingCount,
  views,
}: {
  active: ViewId;
  onNavigate: (v: ViewId) => void;
  cryptCount: number;
  pendingCount: number;
  views?: readonly ViewId[];
}) {
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return;
    const btns = Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('button'));
    const i = btns.indexOf(document.activeElement as HTMLButtonElement);
    if (i < 0) return;
    e.preventDefault();
    const next =
      e.key === 'Home' ? 0 : e.key === 'End' ? btns.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + btns.length) % btns.length;
    btns[next].focus();
  };
  return (
    <nav className="sg-nav" aria-label="Main" onKeyDown={onKeyDown}>
      {TABS.filter((t) => !views || views.includes(t.id)).map((t) => {
        const count = t.id === 'crypt' ? cryptCount : t.id === 'creations' ? pendingCount : 0;
        return (
          <button
            key={t.id}
            type="button"
            className="sg-nav__tab"
            aria-current={active === t.id ? 'page' : undefined}
            onClick={() => onNavigate(t.id)}
          >
            <Icon name={t.icon} size={18} />
            <span>{t.label}</span>
            {count > 0 && <span className="sg-nav__count" aria-label={`${count} items`}>{count}</span>}
          </button>
        );
      })}
    </nav>
  );
}
