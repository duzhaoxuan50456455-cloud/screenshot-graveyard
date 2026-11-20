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

  // Listen on the document, not the dialog: when the focused control becomes
  // disabled (a pending action) the browser drops focus to <body>, and Escape
  // must still dismiss the drawer.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const f = Array.from(
        ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), [href], input, select, [tabindex]:not([tabindex="-1"])') ?? [],
      );
      if (f.length === 0) {
        e.preventDefault();
        ref.current?.focus();
        return;
      }
      const first = f[0];
      const last = f[f.length - 1];
      const active = document.activeElement;
      if (!ref.current?.contains(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  return (
    <div className="sg-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={ref} className="sg-drawer" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}>
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
