import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type ReactNode,
} from 'react';
import { Icon, type IconName } from './Icon';

type ButtonVariant = 'primary' | 'secondary' | 'ghost';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  icon?: IconName;
  trailingIcon?: IconName;
  size?: 'sm' | 'md';
}

/** The one button primitive. `primary` is the warm peach CTA; use once per region. */
export function Button({
  variant = 'secondary',
  icon,
  trailingIcon,
  size = 'md',
  className = '',
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button type={type} className={`ui-btn ui-btn--${variant} ui-btn--${size} ${className}`} {...rest}>
      {icon && <Icon name={icon} size={size === 'sm' ? 15 : 17} />}
      {children}
      {trailingIcon && <Icon name={trailingIcon} size={size === 'sm' ? 15 : 17} />}
    </button>
  );
}

export function Card({
  className = '',
  selected = false,
  ...rest
}: HTMLAttributes<HTMLDivElement> & { selected?: boolean }) {
  return <div className={`ui-card ${selected ? 'ui-card--selected' : ''} ${className}`} {...rest} />;
}

type BadgeTone = 'violet' | 'peach' | 'green' | 'muted';

export function Badge({
  tone = 'violet',
  children,
  className = '',
}: {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}) {
  return <span className={`ui-badge ui-badge--${tone} ${className}`}>{children}</span>;
}

interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  className?: string;
}

/** Real <input type="checkbox"> with a custom skin so keyboard and screen readers keep working. */
export function Checkbox({ checked, onChange, label, className = '' }: CheckboxProps) {
  return (
    <label className={`ui-check ${className}`}>
      <input
        type="checkbox"
        checked={checked}
        aria-label={label}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="ui-check__box" aria-hidden="true">
        {checked && <Icon name="check" size={13} strokeWidth={3} />}
      </span>
    </label>
  );
}

interface SelectFieldProps {
  icon?: IconName;
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  prefix?: string;
}

/** Native <select> wrapped in the pill skin. Native keeps keyboard and mobile pickers working. */
export function SelectField({ icon, label, value, onChange, options, prefix }: SelectFieldProps) {
  const current = options.find((o) => o.value === value);
  return (
    <label className="ui-select">
      {icon && <Icon name={icon} size={16} />}
      <span className="ui-select__text">
        {prefix}
        {current?.label}
      </span>
      <Icon name="chevronDown" size={15} />
      <select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

interface MenuButtonProps {
  label: string;
  icon?: IconName;
  items: { id: string; label: string }[];
  onSelect: (id: string) => void;
  /** Open upward (used by the floating selection bar). */
  placement?: 'down' | 'up';
  className?: string;
}

/** Disclosure menu: button + list of choices. Escape closes, arrows move, focus returns to the button. */
export function MenuButton({ label, icon, items, onSelect, placement = 'down', className = '' }: MenuButtonProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    rootRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    return () => document.removeEventListener('mousedown', onDoc);
  }, [open]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) return;
    const els = Array.from(rootRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === 'Escape') {
      e.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      els[(i + 1) % els.length]?.focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      els[(i - 1 + els.length) % els.length]?.focus();
    } else if (e.key === 'Tab') {
      setOpen(false);
    }
  };

  return (
    <div className={`ui-menu ${className}`} ref={rootRef} onKeyDown={onKeyDown}>
      <button
        ref={triggerRef}
        type="button"
        className="ui-btn ui-btn--secondary ui-btn--md"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        {icon && <Icon name={icon} size={16} />}
        {label}
        <Icon name="chevronDown" size={15} />
      </button>
      {open && (
        <ul id={id} role="menu" className={`ui-menu__list ui-menu__list--${placement}`}>
          {items.map((item) => (
            <li key={item.id} role="none">
              <button
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={() => {
                  setOpen(false);
                  triggerRef.current?.focus();
                  onSelect(item.id);
                }}
              >
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export type StepState = 'done' | 'current' | 'todo';

export function Stepper({ steps }: { steps: { label: string; state: StepState }[] }) {
  return (
    <ol className="ui-stepper" aria-label="Progress: Prepare, Review, Approve">
      {steps.map((s) => (
        <li
          key={s.label}
          className={`ui-stepper__step ui-stepper__step--${s.state}`}
          aria-current={s.state === 'current' ? 'step' : undefined}
        >
          <span className="ui-stepper__dot" aria-hidden="true">
            {s.state === 'done' && <Icon name="check" size={12} strokeWidth={3} />}
          </span>
          <span>{s.label}</span>
        </li>
      ))}
    </ol>
  );
}
