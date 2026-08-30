/** Jhin brand mark: the isometric "Pastel Skies" J from the landing page,
 * rendered as four stacked cubes. Pure SVG, no animation dependency; the
 * geometry and palette match Jhin-Landing/components/LogoMark.tsx. */

const HW = 15.1; // half width of the top diamond
const DH = 8.8; // half height of the top diamond
const DF = 17.6; // full height of the top diamond
const SH = 18.7; // side face height

function faces(cx: number, y: number) {
  return {
    top: `${cx},${y} ${cx + HW},${y + DH} ${cx},${y + DF} ${cx - HW},${y + DH}`,
    left: `${cx - HW},${y + DH} ${cx},${y + DF} ${cx},${y + DF + SH} ${cx - HW},${y + DH + SH}`,
    right: `${cx + HW},${y + DH} ${cx},${y + DF} ${cx},${y + DF + SH} ${cx + HW},${y + DH + SH}`,
  };
}

// Render order: back column bottom→top, foot cube in front.
const CUBES = [
  { cx: 58.6, y: 46.3, top: "#ece2fa", right: "#a594f9", left: "#7371fc" },
  { cx: 58.6, y: 27.6, top: "#f0e8fb", right: "#cdc1ff", left: "#7d71f8" },
  { cx: 58.6, y: 8.9, top: "#f5efff", right: "#e5d9f2", left: "#8a7bf5" },
  { cx: 43, y: 55.4, top: "#f0e9fd", right: "#b7a8fc", left: "#7371fc" },
];

export function LogoMark({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="25.5 6 50.5 87.5"
      className={className}
      role="img"
      aria-label="Jhin"
      style={{ overflow: "visible" }}
    >
      {CUBES.map((c, i) => {
        const f = faces(c.cx, c.y);
        return (
          <g key={i}>
            <polygon points={f.top} fill={c.top} />
            <polygon points={f.left} fill={c.left} />
            <polygon points={f.right} fill={c.right} />
            <polygon
              points={f.top}
              fill="none"
              stroke="rgba(90,88,232,0.28)"
              strokeWidth="0.6"
              strokeLinejoin="round"
            />
          </g>
        );
      })}
    </svg>
  );
}

/** Mark + wordmark lockup used in the shell and auth screens. */
export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <LogoMark className="h-8 w-auto" />
      <span className="font-display text-lg font-bold tracking-tight text-ink">Jhin</span>
    </span>
  );
}
