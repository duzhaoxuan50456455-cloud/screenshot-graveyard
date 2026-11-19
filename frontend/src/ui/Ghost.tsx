import { useId } from 'react';

/** Cute ghost mascot. `crown` adds the flower crown and held screenshot used in the header. */
export function Ghost({ size = 40, crown = false }: { size?: number; crown?: boolean }) {
  const gid = useId();
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset="1" stopColor="#d9ccf5" />
        </linearGradient>
      </defs>
      <path
        d="M12 54V28C12 16 21 8 32 8s20 8 20 20v26l-6.5-5-6.5 5-7-5-6.5 5-7-5-5.5 5Z"
        fill={`url(#${gid})`}
        stroke="#b79cf0"
        strokeWidth="1.5"
      />
      <ellipse cx="25" cy="30" rx="2.6" ry="3.4" fill="#2a1a45" />
      <ellipse cx="39" cy="30" rx="2.6" ry="3.4" fill="#2a1a45" />
      <circle cx="21" cy="37" r="3" fill="#f6a6c8" opacity=".6" />
      <circle cx="43" cy="37" r="3" fill="#f6a6c8" opacity=".6" />
      <path d="M29 37q3 3 6 0" fill="none" stroke="#2a1a45" strokeWidth="1.8" strokeLinecap="round" />
      {crown && (
        <g>
          <circle cx="20" cy="12" r="4" fill="#f6a6c8" />
          <circle cx="29" cy="8" r="4" fill="#ffd9a0" />
          <circle cx="38" cy="8" r="4" fill="#c9a8ff" />
          <circle cx="46" cy="12" r="4" fill="#f6a6c8" />
          <circle cx="29" cy="8" r="1.3" fill="#fff" />
          <circle cx="20" cy="12" r="1.3" fill="#fff" />
          <circle cx="46" cy="12" r="1.3" fill="#fff" />
          <circle cx="38" cy="8" r="1.3" fill="#fff" />
        </g>
      )}
    </svg>
  );
}
