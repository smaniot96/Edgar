import type { Config } from "tailwindcss";
import colors from "tailwindcss/colors";

/**
 * Edgar design tokens.
 *
 *   canvas            page background (#101216)
 *   surface           cards / panels; `surface-sunken` for inputs & insets, `surface-raised` for hovers
 *   line              hairline borders; `line-strong` for control borders
 *   ink               primary text; `ink-muted` secondary (≈7:1); `ink-subtle` tertiary (≥4.5:1)
 *   ember-{50..950}   amber accent (primary actions, links, focus); DEFAULT = amber-500
 *   danger / success / info / warning   status colours, each with a `-soft` tinted background
 *
 * Fonts: `font-display` (serif headings), `font-sans` (UI), `font-mono`.
 * Radii: `rounded-control` (inputs/buttons), `rounded-card` (panels).
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#101216",
        surface: {
          DEFAULT: "#1a1c20",
          sunken: "#15161a",
          raised: "#22252b",
        },
        line: {
          DEFAULT: "#2a2c30",
          strong: "#3a3d44",
        },
        ink: {
          DEFAULT: "#e6e6e6",
          muted: "#a3a9b3",
          subtle: "#8b919a",
        },
        ember: { ...colors.amber, DEFAULT: colors.amber[500] },
        danger: {
          DEFAULT: colors.red[400],
          strong: colors.red[700],
          soft: "#3a1414",
        },
        success: {
          DEFAULT: colors.green[400],
          soft: "#10331f",
        },
        info: {
          DEFAULT: colors.blue[300],
          soft: "#1f3a5f",
        },
        warning: {
          DEFAULT: colors.yellow[400],
          soft: "#3b2900",
        },
      },
      fontFamily: {
        display: [
          '"Cormorant Garamond"',
          "Cormorant",
          "Georgia",
          "Cambria",
          '"Times New Roman"',
          "serif",
        ],
      },
      fontSize: {
        // Display scale for serif headings (Cormorant runs small, so it is sized up).
        "display-sm": ["1.375rem", { lineHeight: "1.75rem", fontWeight: "600" }],
        "display-md": ["1.75rem", { lineHeight: "2.125rem", fontWeight: "600" }],
        "display-lg": ["2.25rem", { lineHeight: "2.5rem", fontWeight: "700" }],
      },
      borderRadius: {
        control: "0.375rem",
        card: "0.75rem",
      },
      boxShadow: {
        ember: "0 0 24px -8px rgba(255, 107, 43, 0.5)",
      },
    },
  },
  plugins: [],
} satisfies Config;
