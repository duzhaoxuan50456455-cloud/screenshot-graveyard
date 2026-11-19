import { useEffect, useRef, type ReactNode } from 'react';
import { Icon } from '../ui/Icon';

/** Right-hand modal drawer. Escape closes, focus is trapped, and focus returns to the opener. */
export function Drawer({
  title,
  onClose,
  opener,
  children,
  footer,
}: {
  title: string;
  onClose: () => void;
  opener: HTMLElement | null;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
    return () => {
      if (opener && opener.isConnected) opener.focus();
    };
  }, [opener]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key !== 'Tab') return;
    const f = Array.from(
      ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input, select, [tabindex]:not([tabindex="-1"])') ?? [],
    );
    if (f.length === 0) return;
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="sg-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="sg-drawer" role="dialog" aria-modal="true" aria-label={title} onKeyDown={onKeyDown}>
        <div className="sg-drawer__head">
          <h2 className="sg-display">{title}</h2>
          <button ref={closeRef} type="button" className="sg-icon-btn" aria-label="Close details" onClick={onClose}>
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="sg-drawer__body">{children}</div>
        {footer && <div className="sg-drawer__foot">{footer}</div>}
      </div>
    </div>
  );
}
