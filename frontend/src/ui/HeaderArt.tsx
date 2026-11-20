/** Moonlit graveyard backdrop for the page header. Purely decorative SVG. */
export function HeaderArt() {
  return (
    <svg
      className="hdr-art"
      viewBox="0 0 1440 120"
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <radialGradient id="moonGlow" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#e9d8ff" stopOpacity=".55" />
          <stop offset="1" stopColor="#e9d8ff" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="sky" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#1b1030" />
          <stop offset=".55" stopColor="#2a1646" />
          <stop offset="1" stopColor="#4a2a70" />
        </linearGradient>
        <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
          <stop offset=".6" stopColor="#0f0a17" stopOpacity="0" />
          <stop offset="1" stopColor="#0f0a17" stopOpacity=".9" />
        </linearGradient>
      </defs>
      <rect width="1440" height="120" fill="url(#sky)" />
      <circle cx="1180" cy="52" r="120" fill="url(#moonGlow)" />
      <circle cx="1180" cy="52" r="44" fill="#eadcff" />
      <circle cx="1166" cy="40" r="8" fill="#cdb8ee" opacity=".7" />
      <circle cx="1196" cy="66" r="11" fill="#cdb8ee" opacity=".55" />
      <circle cx="1190" cy="34" r="4" fill="#cdb8ee" opacity=".6" />
      {/* clouds */}
      <path d="M1040 60q40-14 90-4t120 2 70 14H1010q-8-8 30-12Z" fill="#6e4aa3" opacity=".35" />
      {/* stars */}
      {[
        [620, 22], [700, 40], [820, 16], [900, 50], [980, 26], [1320, 20], [1370, 62], [540, 60],
      ].map(([x, y]) => (
        <circle key={`${x}-${y}`} cx={x} cy={y} r="1.4" fill="#f1e6ff" opacity=".8" />
      ))}
      {/* bats */}
      <path d="M1010 40q8-8 14 0 6-8 14 0-7-2-14 6-7-8-14-6Z" fill="#150c24" />
      <path d="M1290 30q7-7 12 0 5-7 12 0-6-2-12 5-6-7-12-5Z" fill="#150c24" />
      <path d="M1100 92q6-6 10 0 4-6 10 0-5-2-10 4-5-6-10-4Z" fill="#150c24" />
      {/* cross + tombs */}
      <g fill="#140b22">
        <path d="M1285 34h6v14h14v6h-14v28h-6V54h-14v-6h14V34Z" />
        <path d="M1240 120V86q0-14 14-14t14 14v34Z" />
        <path d="M1330 120V90q0-12 12-12t12 12v30Z" />
        <path d="M1390 120V84q0-14 14-14t14 14v36Z" />
        <path d="M30 120V88q0-12 12-12t12 12v32Z" />
        <path d="M90 120V94q0-10 10-10t10 10v26Z" />
        <path d="M0 108q120-14 260 4t300-4 320 6 360-8v22H0Z" />
        <path d="M60 78l3-12 3 12h-6Zm10 0v-9m-8 9v-7" />
      </g>
      <rect width="1440" height="120" fill="url(#fade)" />
    </svg>
  );
}

/** Smaller corner scene used behind the Workspace heading (candle, jack-o-lantern, tomb). */
export function WorkspaceArt() {
  return (
    <svg className="ws-art" viewBox="0 0 300 100" aria-hidden="true" focusable="false">
      <defs>
        <radialGradient id="candleGlow" cx=".5" cy=".5" r=".5">
          <stop offset="0" stopColor="#ffb877" stopOpacity=".55" />
          <stop offset="1" stopColor="#ffb877" stopOpacity="0" />
        </radialGradient>
      </defs>
      <path d="M200 100V40q0-22 22-22t22 22v60Z" fill="#3a2a52" stroke="#6b4aa8" strokeWidth="1.5" />
      <path d="M214 54h16M222 46v16" stroke="#6b4aa8" strokeWidth="2" />
      <circle cx="150" cy="40" r="40" fill="url(#candleGlow)" />
      <rect x="146" y="38" width="9" height="26" rx="2" fill="#efe3ff" />
      <path d="M150.500 24c4 5 4 9 0 12-4-3-4-7 0-12Z" fill="#ffb877" />
      <path d="M70 100q0-24 28-24t28 24Z" fill="#2a1b3d" />
      <ellipse cx="98" cy="82" rx="22" ry="17" fill="#ff9a3c" />
      <path d="M88 76l5 6h-10l5-6Zm20 0 5 6h-10l5-6Zm-18 10q8 7 16 0" fill="#2a1146" />
      <path d="M98 66v-6" stroke="#6bc17a" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
