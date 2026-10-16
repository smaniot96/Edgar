/// <reference types="node" />
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";

// Vitest does not transform CSS, so read the theme tokens from the source file.
const css = readFileSync(new URL("../index.css", import.meta.url), "utf8");
const token = (name: string) => css.match(new RegExp(`--color-${name}: (#[0-9a-f]{6})`))![1];

function luminance(hex: string) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(foreground: string, background: string) {
  const [lighter, darker] = [luminance(token(foreground)), luminance(token(background))].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

it("keeps text and link colours at WCAG AA contrast on every play surface", () => {
  for (const surface of ["canvas", "surface", "surface-sunken", "surface-raised"]) {
    for (const text of ["ink", "ink-muted", "ink-subtle", "ember-300"]) expect(ratio(text, surface), `${text} on ${surface}`).toBeGreaterThanOrEqual(4.5);
    expect(ratio("ember-400", surface), `focus ring on ${surface}`).toBeGreaterThanOrEqual(3);
  }
});

it("does not use the low-contrast dark ember shades as text on the dark theme", () => {
  const sources = import.meta.glob<string>("../**/*.tsx", { query: "?raw", import: "default", eager: true });
  const offenders = Object.entries(sources)
    .filter(([path, source]) => !path.includes(".test.") && /text-ember-[789]00/.test(source))
    .map(([path]) => path);
  expect(offenders).toEqual([]);
});
