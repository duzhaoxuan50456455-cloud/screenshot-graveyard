import type { SVGProps } from 'react';

/** Small stroke icon set (24px grid, drawn for this project). Decorative by default. */
const PATHS = {
  layers: 'M12 3 3 8l9 5 9-5-9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5',
  sparkles:
    'M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3Zm7 11 .8 2.2L22 17l-2.2.8L19 20l-.8-2.2L16 17l2.2-.8L19 14ZM5 15l.6 1.4L7 17l-1.4.6L5 19l-.6-1.4L3 17l1.4-.6L5 15Z',
  crypt: 'M7 21V9a5 5 0 0 1 10 0v12M4 21h16M12 7v6M9.5 9.5h5',
  link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 11 18.7l1-1',
  upload: 'M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5 4 4 0 0 1 17.5 18M12 12v8m0-8-3 3m3-3 3 3',
  grid: 'M4 4h6v6H4V4Zm10 0h6v6h-6V4ZM4 14h6v6H4v-6Zm10 0h6v6h-6v-6Z',
  tag: 'M3 12V4h8l10 10-8 8L3 12Zm5-5.5h.01',
  calendar: 'M4 6h16v14H4V6Zm0 5h16M8 3v4m8-4v4',
  heart: 'M12 20s-8-4.6-8-10.5A4.5 4.5 0 0 1 12 7a4.5 4.5 0 0 1 8 2.5C20 15.4 12 20 12 20Z',
  dots: 'M5 12h.01M12 12h.01M19 12h.01',
  chevronDown: 'm6 9 6 6 6-6',
  arrowRight: 'M5 12h14m-6-6 6 6-6 6',
  expand: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  bell: 'M6 16V11a6 6 0 1 1 12 0v5l2 2H4l2-2Zm4 4a2 2 0 0 0 4 0',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0',
  close: 'M6 6l12 12M18 6 6 18',
  check: 'm5 12.5 4.5 4.5L19 7.5',
  file: 'M7 3h7l5 5v13H7V3Zm7 0v5h5M10 13h6M10 17h6',
  shield: 'M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3Z',
  restore: 'M4 12a8 8 0 1 0 2.5-5.8M4 4v4h4',
  box: 'M3 8l9-5 9 5v8l-9 5-9-5V8Zm0 0 9 5 9-5M12 13v8',
  clock: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-13v5l3 2',
  loader: 'M12 3a9 9 0 1 0 9 9',
  help: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm-2.5-11.5a2.6 2.6 0 1 1 3.6 2.4c-.7.4-1.1.9-1.1 1.6M12 16.5h.01',
  info: 'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-10v5m0-8h.01',
  plug: 'M9 3v5m6-5v5M6 8h12v3a6 6 0 0 1-12 0V8Zm6 9v4',
  list: 'M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01',
  image: 'M4 5h16v14H4V5Zm0 11 5-5 4 4 3-3 4 4M9 9.5h.01',
} as const;

export type IconName = keyof typeof PATHS;

interface IconProps extends Omit<SVGProps<SVGSVGElement>, 'name'> {
  name: IconName;
  size?: number;
  label?: string;
}

export function Icon({ name, size = 18, label, ...rest }: IconProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
      {...rest}
    >
      <path d={PATHS[name]} />
    </svg>
  );
}
