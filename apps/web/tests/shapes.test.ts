/** Unit tests: brand-cube shape catalog, palette, and pure color math. */

import { describe, expect, it } from "vitest";
import {
  AVATAR_PALETTE,
  AVATAR_SHAPES,
  defaultShapeFor,
  faceColors,
  hexWithAlpha,
  mixHex,
  orderedCubes,
  shapeById,
  shapeViewBox,
} from "@/lib/shapes";

describe("mixHex", () => {
  it("mixes toward white and black", () => {
    expect(mixHex("#000000", "#ffffff", 0.5)).toBe("#808080");
    expect(mixHex("#7371fc", "#ffffff", 0)).toBe("#7371fc");
    expect(mixHex("#7371fc", "#ffffff", 1)).toBe("#ffffff");
    expect(mixHex("#7371fc", "#000000", 1)).toBe("#000000");
  });

  it("clamps the amount and tolerates garbage input", () => {
    expect(mixHex("#ffffff", "#000000", 2)).toBe("#000000");
    expect(mixHex("#ffffff", "#000000", -1)).toBe("#ffffff");
    expect(mixHex("nope", "#000000", 0.5)).toBe("nope");
  });
});

describe("faceColors", () => {
  it("derives a light top, mid right, and dark left from one base", () => {
    const faces = faceColors("#7371fc");
    expect(faces.top).toBe(mixHex("#7371fc", "#ffffff", 0.78));
    expect(faces.right).toBe(mixHex("#7371fc", "#ffffff", 0.42));
    expect(faces.left).toBe(mixHex("#7371fc", "#000000", 0.06));
    const value = (hex: string) => parseInt(hex.slice(1), 16);
    expect(value(faces.top)).toBeGreaterThan(value(faces.right));
    expect(value(faces.right)).toBeGreaterThan(value(faces.left));
  });
});

describe("hexWithAlpha", () => {
  it("appends the alpha byte and normalizes case", () => {
    expect(hexWithAlpha("#7371FC", 0.12)).toBe("#7371fc1f");
    expect(hexWithAlpha("bad", 0.5)).toBe("bad");
  });
});

describe("shape catalog", () => {
  it("ships the 8 fixed shapes and the 12-color palette", () => {
    expect(AVATAR_SHAPES.map((shape) => shape.id)).toEqual([
      "cube",
      "column",
      "jay",
      "ell",
      "ess",
      "tee",
      "quad",
      "step",
    ]);
    expect(AVATAR_PALETTE).toHaveLength(12);
    expect(AVATAR_PALETTE.every((color) => /^#[0-9a-f]{6}$/.test(color.hex))).toBe(true);
    expect(AVATAR_PALETTE.map((color) => color.hex)).toContain("#7371fc");
    expect(AVATAR_PALETTE.map((color) => color.hex)).toContain("#3ecf8e");
  });

  it("draws cubes back-to-front, bottom-up — the logo's paint order", () => {
    const jay = shapeById("jay");
    expect(jay).not.toBeNull();
    expect(orderedCubes(jay!)).toEqual([
      { c: 1, l: 0 },
      { c: 1, l: 1 },
      { c: 1, l: 2 },
      { c: 0, l: 0 },
    ]);
    expect(shapeById("hexagon")).toBeNull();
  });

  it("computes a tight viewBox around the cubes", () => {
    const box = shapeViewBox(shapeById("cube")!);
    expect(box.width).toBeCloseTo(15.1 * 2 + 4);
    expect(box.height).toBeCloseTo(17.6 + 18.7 + 4);
    const column = shapeViewBox(shapeById("column")!);
    expect(column.height).toBeGreaterThan(box.height);
  });

  it("defaultShapeFor is deterministic and always valid", () => {
    const picked = defaultShapeFor("Bisby");
    expect(picked).toEqual(defaultShapeFor("Bisby"));
    expect(AVATAR_SHAPES.some((shape) => shape.id === picked.shape)).toBe(true);
    expect(AVATAR_PALETTE.some((color) => color.hex === picked.color)).toBe(true);
    expect(defaultShapeFor("")).toEqual(defaultShapeFor("Agent"));
  });
});
