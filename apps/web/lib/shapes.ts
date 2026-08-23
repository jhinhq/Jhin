/**
 * Free brand-cube avatars: the 8 tetris-ish shapes, the 12-color Pastel
 * Skies palette, and the pure color/geometry math used to draw them. The
 * cubes reuse the exact isometric geometry of the Jhin logo mark
 * (components/brand/logo-mark.tsx); the fixed lists mirror what the API
 * validates (jhin_domain.AVATAR_SHAPES / AVATAR_COLORS). No React here so
 * everything is unit-testable.
 */

// Cube geometry, identical to the logo mark's faces().
const HW = 15.1; // half width of the top diamond
const DH = 8.8; // half height of the top diamond
const DF = 17.6; // full height of the top diamond
const SH = 18.7; // side face height

// Offset from one column of cubes to the next one toward the viewer,
// matching the logo's foot cube (43, 55.4) relative to its spine (58.6, 46.3).
const STEP_X = 15.6;
const STEP_Y = 9.1;

/** One cube in a shape: `c` steps toward the viewer (front-left), `l` levels up. */
export interface CubePosition {
  c: number;
  l: number;
}

export interface ShapeSpec {
  id: string;
  label: string;
  cubes: CubePosition[];
}

/** The 8 free shapes. Ids mirror the API's fixed list. */
export const AVATAR_SHAPES: ShapeSpec[] = [
  { id: "cube", label: "Cube", cubes: [{ c: 0, l: 0 }] },
  {
    id: "column",
    label: "Column",
    cubes: [
      { c: 0, l: 0 },
      { c: 0, l: 1 },
      { c: 0, l: 2 },
    ],
  },
  {
    // The logo itself: a three-cube spine with a foot cube in front.
    id: "jay",
    label: "Jay",
    cubes: [
      { c: 1, l: 0 },
      { c: 1, l: 1 },
      { c: 1, l: 2 },
      { c: 0, l: 0 },
    ],
  },
  {
    id: "ell",
    label: "Ell",
    cubes: [
      { c: 0, l: 0 },
      { c: 1, l: 0 },
      { c: 1, l: 1 },
    ],
  },
  {
    id: "ess",
    label: "Ess",
    cubes: [
      { c: 0, l: 0 },
      { c: 0, l: 1 },
      { c: 1, l: 1 },
      { c: 1, l: 2 },
    ],
  },
  {
    id: "tee",
    label: "Tee",
    cubes: [
      { c: 0, l: 0 },
      { c: 1, l: 0 },
      { c: 2, l: 0 },
      { c: 1, l: 1 },
    ],
  },
  {
    id: "quad",
    label: "Quad",
    cubes: [
      { c: 0, l: 0 },
      { c: 1, l: 0 },
      { c: 0, l: 1 },
      { c: 1, l: 1 },
    ],
  },
  {
    id: "step",
    label: "Step",
    cubes: [
      { c: 0, l: 0 },
      { c: 1, l: 1 },
      { c: 2, l: 2 },
    ],
  },
];

export interface PaletteColor {
  hex: string;
  label: string;
}

/** The 12 palette colors (brand hues plus Pastel Skies tints). */
export const AVATAR_PALETTE: PaletteColor[] = [
  { hex: "#7371fc", label: "Slate blue" },
  { hex: "#a594f9", label: "Periwinkle" },
  { hex: "#cdc1ff", label: "Lavender" },
  { hex: "#5a58e8", label: "Indigo" },
  { hex: "#2e7558", label: "Sage" },
  { hex: "#316f98", label: "Sky" },
  { hex: "#c96f43", label: "Peach" },
  { hex: "#985b08", label: "Amber" },
  { hex: "#b44351", label: "Coral" },
  { hex: "#221e38", label: "Charcoal" },
  { hex: "#8f89b3", label: "Silver" },
  { hex: "#3ecf8e", label: "Mint" },
];

export function shapeById(id: string | null | undefined): ShapeSpec | null {
  return AVATAR_SHAPES.find((shape) => shape.id === id) ?? null;
}

/* ------------------------------------------------------------------ */
/* Color math                                                          */
/* ------------------------------------------------------------------ */

function parseHex(hex: string): [number, number, number] | null {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) return null;
  const value = parseInt(match[1], 16);
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

/** Mix `base` toward `toward` by `amount` (0 = base, 1 = toward). Invalid
 * input falls back to the base string unchanged. */
export function mixHex(base: string, toward: string, amount: number): string {
  const from = parseHex(base);
  const to = parseHex(toward);
  if (!from || !to) return base;
  const t = Math.min(1, Math.max(0, amount));
  const channel = (index: number) => Math.round(from[index] + (to[index] - from[index]) * t);
  const value = (channel(0) << 16) | (channel(1) << 8) | channel(2);
  return `#${value.toString(16).padStart(6, "0")}`;
}

export interface CubeFaceColors {
  top: string;
  right: string;
  left: string;
  edge: string;
}

/** Face tints derived from one base color: light top, mid right, dark left —
 * the same relationship the logo's cubes use. */
export function faceColors(base: string): CubeFaceColors {
  return {
    top: mixHex(base, "#ffffff", 0.78),
    right: mixHex(base, "#ffffff", 0.42),
    left: mixHex(base, "#000000", 0.06),
    edge: mixHex(base, "#000000", 0.3),
  };
}

/** `#rrggbb` + alpha (0..1) → `#rrggbbaa`, for soft tint backgrounds. */
export function hexWithAlpha(hex: string, alpha: number): string {
  const parsed = parseHex(hex);
  if (!parsed) return hex;
  const a = Math.round(Math.min(1, Math.max(0, alpha)) * 255);
  return `${hex.toLowerCase()}${a.toString(16).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ */
/* Geometry                                                            */
/* ------------------------------------------------------------------ */

/** Top-diamond origin of a cube at grid position (c, l). */
export function cubeOrigin(cube: CubePosition): { cx: number; y: number } {
  return { cx: cube.c * STEP_X, y: -cube.c * STEP_Y - cube.l * SH };
}

/** The three visible faces of one cube — the logo mark's exact geometry. */
export function cubeFaces(cx: number, y: number): { top: string; left: string; right: string } {
  return {
    top: `${cx},${y} ${cx + HW},${y + DH} ${cx},${y + DF} ${cx - HW},${y + DH}`,
    left: `${cx - HW},${y + DH} ${cx},${y + DF} ${cx},${y + DF + SH} ${cx - HW},${y + DH + SH}`,
    right: `${cx + HW},${y + DH} ${cx},${y + DF} ${cx},${y + DF + SH} ${cx + HW},${y + DH + SH}`,
  };
}

/** Painter's order: back columns first (descending c), bottom-up per column. */
export function orderedCubes(spec: ShapeSpec): CubePosition[] {
  return [...spec.cubes].sort((a, b) => b.c - a.c || a.l - b.l);
}

/** Tight bounding box (plus a little padding) for a shape's cubes. */
export function shapeViewBox(spec: ShapeSpec): {
  x: number;
  y: number;
  width: number;
  height: number;
} {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  for (const cube of spec.cubes) {
    const { cx, y } = cubeOrigin(cube);
    minX = Math.min(minX, cx - HW);
    maxX = Math.max(maxX, cx + HW);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y + DF + SH);
  }
  const pad = 2;
  return {
    x: minX - pad,
    y: minY - pad,
    width: maxX - minX + pad * 2,
    height: maxY - minY + pad * 2,
  };
}

/* ------------------------------------------------------------------ */
/* Defaults                                                            */
/* ------------------------------------------------------------------ */

function hashOf(name: string): number {
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return hash;
}

/** Deterministic shape + color for a name, so new agents start as colorful
 * cubes instead of initials. Different divisors decorrelate the two picks. */
export function defaultShapeFor(name: string): { shape: string; color: string } {
  const hash = hashOf(name.trim() || "Agent");
  return {
    shape: AVATAR_SHAPES[hash % AVATAR_SHAPES.length].id,
    color: AVATAR_PALETTE[Math.floor(hash / 7) % AVATAR_PALETTE.length].hex,
  };
}
