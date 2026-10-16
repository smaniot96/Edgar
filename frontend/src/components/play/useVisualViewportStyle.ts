import { useEffect, useState, type CSSProperties } from "react";

type ViewportStyle = CSSProperties & {
  "--edgar-visible-height"?: string;
  "--edgar-visible-top"?: string;
};

const EMPTY_STYLE: ViewportStyle = {};

function readViewportStyle(): ViewportStyle {
  if (typeof window === "undefined") return EMPTY_STYLE;
  const viewport = window.visualViewport;
  if (!viewport || Math.abs(viewport.scale - 1) > 0.01) return EMPTY_STYLE;
  return {
    "--edgar-visible-height": `${Math.round(viewport.height)}px`,
    "--edgar-visible-top": `${Math.round(viewport.offsetTop)}px`,
  };
}

/**
 * Tracks the unzoomed visible viewport so a mobile keyboard resize can keep the app shell
 * and its composer inside the visible area. Pinch zoom stays governed by CSS viewport units.
 */
export function useVisualViewportStyle(): ViewportStyle {
  const [style, setStyle] = useState(readViewportStyle);

  useEffect(() => {
    const viewport = window.visualViewport;
    if (!viewport) return;
    const update = () => setStyle(readViewportStyle());
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    update();
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, []);

  return style;
}
