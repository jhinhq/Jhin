/** Pure SVG rendering of a free brand-cube avatar: a tetris-ish stack of the
 * logo mark's isometric cubes, tinted from one palette color. Decorative by
 * default (aria-hidden); pair it with a visible name, or pass a `title`. */

import {
  AVATAR_SHAPES,
  cubeFaces,
  cubeOrigin,
  faceColors,
  orderedCubes,
  shapeById,
  shapeViewBox,
} from "@/lib/shapes";

export function ShapeAvatar({
  shape,
  color,
  className = "",
  title,
}: {
  /** One of the fixed shape ids (unknown ids fall back to a single cube). */
  shape: string;
  /** Palette hex like `#7371fc`. */
  color: string;
  className?: string;
  /** Accessible label; omitted = decorative. */
  title?: string;
}) {
  const spec = shapeById(shape) ?? AVATAR_SHAPES[0];
  const palette = faceColors(color);
  const box = shapeViewBox(spec);
  return (
    <svg
      viewBox={`${box.x} ${box.y} ${box.width} ${box.height}`}
      className={className}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
      data-shape={spec.id}
      style={{ overflow: "visible" }}
    >
      {orderedCubes(spec).map((cube, index) => {
        const { cx, y } = cubeOrigin(cube);
        const faces = cubeFaces(cx, y);
        return (
          <g key={index}>
            <polygon points={faces.top} fill={palette.top} />
            <polygon points={faces.left} fill={palette.left} />
            <polygon points={faces.right} fill={palette.right} />
            <polygon
              points={faces.top}
              fill="none"
              stroke={palette.edge}
              strokeOpacity={0.28}
              strokeWidth={0.6}
              strokeLinejoin="round"
            />
          </g>
        );
      })}
    </svg>
  );
}
