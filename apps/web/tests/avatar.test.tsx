/** Avatar: renders the picture when a source is given and falls back to
 * initials when there is none or the image fails to load. */

import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Avatar, initialsOf } from "@/components/avatar";
import { avatarErrorMessage, avatarProps, avatarSrc, identityAvatarProps, validateAvatarFile } from "@/lib/media";

afterEach(cleanup);

describe("Avatar", () => {
  it("renders initials when there is no picture", () => {
    const { container } = render(<Avatar name="Ada Lovelace" />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.getAttribute("data-avatar")).toBe("initials");
    expect(root.textContent).toBe("AL");
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders the image with a size matching the avatar size", () => {
    const { container } = render(<Avatar name="Ada Lovelace" size="xl" src="/api/v1/workspaces/ws/media/asset" />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toBe("/api/v1/workspaces/ws/media/asset?size=256");
    expect((container.firstElementChild as HTMLElement).getAttribute("data-avatar")).toBe("image");
  });

  it("falls back to initials when the image fails to load", () => {
    const { container } = render(<Avatar name="Ada Lovelace" src="/api/v1/workspaces/ws/media/asset" />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    fireEvent.error(img as HTMLImageElement);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toBe("AL");
  });

  it("exposes a label only when asked", () => {
    const { container } = render(<Avatar name="Ada" label="Ada's avatar" src="/m/a" />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.getAttribute("role")).toBe("img");
    expect(root.getAttribute("aria-label")).toBe("Ada's avatar");
  });
});

describe("Avatar shapes", () => {
  it("renders the brand-cube shape when set and there is no image", () => {
    const { container } = render(<Avatar name="Bisby" shape="jay" color="#7371fc" />);
    const root = container.firstElementChild as HTMLElement;
    expect(root.getAttribute("data-avatar")).toBe("shape");
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute("data-shape")).toBe("jay");
    // 4 cubes × (3 faces + 1 top edge stroke)
    expect(container.querySelectorAll("polygon")).toHaveLength(16);
  });

  it("an uploaded or generated picture always wins over the shape", () => {
    const { container } = render(
      <Avatar name="Bisby" shape="jay" color="#7371fc" src="/api/v1/workspaces/ws/media/asset" />,
    );
    expect((container.firstElementChild as HTMLElement).getAttribute("data-avatar")).toBe("image");
  });

  it("falls back to initials for unknown shapes or a missing color", () => {
    const { container: unknownShape } = render(<Avatar name="Bisby" shape="hexagon" color="#7371fc" />);
    expect((unknownShape.firstElementChild as HTMLElement).getAttribute("data-avatar")).toBe("initials");
    const { container: noColor } = render(<Avatar name="Bisby" shape="jay" />);
    expect((noColor.firstElementChild as HTMLElement).getAttribute("data-avatar")).toBe("initials");
  });

  it("avatarProps and identityAvatarProps spread map entries and payloads", () => {
    expect(avatarProps({ url: "/m/a", shape: "jay", color: "#7371fc" })).toEqual({
      src: "/m/a",
      shape: "jay",
      color: "#7371fc",
    });
    expect(avatarProps(null)).toEqual({ src: null, shape: null, color: null });
    expect(identityAvatarProps({ avatar_shape: "quad", avatar_color: "#3ecf8e" })).toEqual({
      src: null,
      shape: "quad",
      color: "#3ecf8e",
    });
  });
});

describe("media helpers", () => {
  it("appends the size and keeps existing query strings", () => {
    expect(avatarSrc("/m/a", 64)).toBe("/m/a?size=64");
    expect(avatarSrc("/m/a?v=1", 128)).toBe("/m/a?v=1&size=128");
    expect(avatarSrc(null, 64)).toBeNull();
  });

  it("rejects the wrong type or size before upload", () => {
    expect(validateAvatarFile({ type: "image/gif", size: 10, name: "a.gif" })).toMatch(/PNG, JPEG, or WebP/);
    expect(validateAvatarFile({ type: "image/png", size: 9 * 1024 * 1024, name: "a.png" })).toMatch(/8 MB/);
    expect(validateAvatarFile({ type: "image/webp", size: 1024, name: "a.webp" })).toBeNull();
  });

  it("translates API rejection codes into plain language", () => {
    expect(avatarErrorMessage("animated", "x")).toMatch(/Animated/);
    expect(avatarErrorMessage("image_generation_unsupported", "x")).toMatch(/Models/);
    expect(avatarErrorMessage("unknown_code", "fallback text")).toBe("fallback text");
  });

  it("initialsOf handles single and multi-word names", () => {
    expect(initialsOf("Scout")).toBe("SC");
    expect(initialsOf("")).toBe("?");
  });
});
