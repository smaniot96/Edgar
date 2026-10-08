/**
 * Landing — the entry splash for Edgar.
 *
 * Full-bleed hero rendered OUTSIDE the standard app nav chrome (it is its own
 * top-level route in main.tsx), so it reads as a distinct splash. Shows the
 * EdgarLogo, a one-sentence pitch, and CTA cards into the app.
 */

import { Link } from "react-router-dom";

import EdgarLogo from "../components/EdgarLogo";
import { cn } from "../lib/cn";

type Cta = {
  to: string;
  label: string;
  blurb: string;
  primary?: boolean;
};

const CTAS: Cta[] = [
  {
    to: "/campaigns",
    label: "Enter the Campaigns",
    blurb: "Generate or upload an adventure, then play.",
    primary: true,
  },
  { to: "/characters", label: "Your Characters", blurb: "Forge and manage your heroes." },
  { to: "/settings", label: "Settings", blurb: "Your profile and display name." },
];

function CtaCard({ cta }: { cta: Cta }) {
  return (
    <Link
      to={cta.to}
      className={cn(
        "group relative flex flex-col rounded-xl border p-5 text-left transition-all duration-200",
        "hover:-translate-y-0.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember-400",
        cta.primary
          ? "border-ember-700/60 bg-gradient-to-br from-ember-950/50 to-surface hover:border-ember-500 hover:shadow-ember"
          : "border-line bg-surface-sunken/80 hover:border-ember-700/60 hover:shadow-[0_0_22px_-10px_rgba(255,107,43,0.4)]",
      )}
    >
      <span
        className={cn(
          "font-display text-xl font-semibold",
          cta.primary ? "text-ember-200" : "text-ink",
        )}
      >
        {cta.label}
      </span>
      <span className="mt-1 text-sm text-ink-muted">{cta.blurb}</span>
      <span
        className={cn(
          "mt-3 text-sm transition-transform duration-200 group-hover:translate-x-1",
          cta.primary ? "text-ember-400" : "text-ember-300",
        )}
        aria-hidden
      >
        Enter →
      </span>
    </Link>
  );
}

export default function Landing() {
  return (
    <main className="edgar-landing relative flex min-h-dvh flex-col items-center justify-center overflow-hidden bg-canvas px-4 py-12 text-ink">
      {/* Dark vignette + molten radial glow behind the logo. */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(60% 45% at 50% 32%, rgba(255,94,43,0.16), transparent 70%), radial-gradient(120% 90% at 50% 100%, rgba(0,0,0,0.7), transparent 60%)",
        }}
      />
      {/* Faint ember particles. */}
      <div aria-hidden className="edgar-embers pointer-events-none absolute inset-0">
        {EMBERS.map((e, i) => (
          <span
            key={i}
            className="edgar-ember-dot"
            style={{
              left: e.left,
              bottom: "-8px",
              animationDelay: e.delay,
              animationDuration: e.duration,
            }}
          />
        ))}
      </div>

      <div className="relative z-10 flex w-full max-w-3xl flex-col items-center text-center">
        <EdgarLogo variant="hero" className="w-full max-w-[560px]" />

        <p className="mt-6 max-w-2xl text-balance text-base text-ink/80 sm:text-lg">
          Edgar is your AI Dungeon Master — upload or generate a D&amp;D 5e campaign, create a hero,
          and play a living adventure right in your browser.
        </p>

        {/* Thin molten divider. */}
        <div
          aria-hidden
          className="mt-8 h-px w-48 max-w-full"
          style={{
            background:
              "linear-gradient(90deg, transparent, #ff6b2b 35%, #ffd166 50%, #ff6b2b 65%, transparent)",
          }}
        />

        <div className="mt-8 grid w-full gap-4 sm:grid-cols-3">
          {CTAS.map((cta) => (
            <CtaCard key={cta.to} cta={cta} />
          ))}
        </div>
      </div>
    </main>
  );
}

// Pre-computed ember positions so the particles look organic but stay static
// across renders (no per-render randomness, no layout jank).
const EMBERS = [
  { left: "8%", delay: "0s", duration: "9s" },
  { left: "20%", delay: "2.5s", duration: "11s" },
  { left: "33%", delay: "5s", duration: "8s" },
  { left: "47%", delay: "1.2s", duration: "12s" },
  { left: "61%", delay: "3.8s", duration: "10s" },
  { left: "74%", delay: "6s", duration: "9.5s" },
  { left: "88%", delay: "0.6s", duration: "11.5s" },
];
