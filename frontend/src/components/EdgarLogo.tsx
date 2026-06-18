/**
 * EdgarLogo — self-contained inline-SVG wordmark for Edgar.
 *
 * Art direction:
 *   - Wordmark "EDGAR" in pale gold; letters sit on equal centred slots so spacing is even.
 *   - The A is a VOLCANO: layered bottom-up green base -> rocky cone -> a glowing lava crater
 *     at the truncated summit that fumes smoke. A faint lava flow nods to the A's crossbar.
 *   - A stylised dragon (horned head, wing, spine ridges, barbed tail) arcs above as decoration.
 *   - Smoke wisps curl around the E (and a plume rises from the crater).
 *
 * Variants: "hero" (landing splash) and "mark" (compact nav). Animation (ember flicker + smoke
 * drift) lives in index.css and is disabled under prefers-reduced-motion.
 */

import { cn } from "../lib/cn";

type EdgarLogoVariant = "hero" | "mark";

// Equal slot centres so the wordmark reads evenly; the A slot (348) is the volcano.
const SLOT = { E: 84, D: 172, G: 260, A: 348, R: 436 } as const;
const BASE_Y = 208; // shared baseline for letters + volcano base

export function EdgarLogo({
  variant = "hero",
  className,
  title = "Edgar",
}: {
  variant?: EdgarLogoVariant;
  className?: string;
  title?: string;
}) {
  const isHero = variant === "hero";
  const uid = isHero ? "hero" : "mark";

  return (
    <svg
      viewBox="0 0 520 260"
      role="img"
      aria-label={title}
      className={cn(
        "edgar-logo block h-auto w-full select-none",
        isHero ? "max-w-[640px]" : "max-w-[150px]",
        className,
      )}
      xmlns="http://www.w3.org/2000/svg"
    >
      <title>{title}</title>
      <defs>
        {/* Molten lava glow for the crater. */}
        <radialGradient id={`lava-${uid}`} cx="50%" cy="45%" r="55%">
          <stop offset="0%" stopColor="#fff3c4" />
          <stop offset="35%" stopColor="#ffb347" />
          <stop offset="70%" stopColor="#ff5e2b" />
          <stop offset="100%" stopColor="#b21f1f" />
        </radialGradient>
        {/* Lava flow / crossbar. */}
        <linearGradient id={`molten-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ffd166" />
          <stop offset="50%" stopColor="#ff6b2b" />
          <stop offset="100%" stopColor="#7a1414" />
        </linearGradient>
        {/* Volcano body: rock at the top fading to green vegetation at the base. */}
        <linearGradient id={`volcano-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#4a443d" />
          <stop offset="20%" stopColor="#6b6258" />
          <stop offset="45%" stopColor="#6a6450" />
          <stop offset="62%" stopColor="#5c6b44" />
          <stop offset="80%" stopColor="#3f8240" />
          <stop offset="100%" stopColor="#2c6e33" />
        </linearGradient>
        {/* Pale-gold letterforms. */}
        <linearGradient id={`letter-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#fdf6e3" />
          <stop offset="100%" stopColor="#d9c79a" />
        </linearGradient>
        {/* Ash-grey smoke. */}
        <linearGradient id={`smoke-${uid}`} x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor="#7a808a" stopOpacity="0.6" />
          <stop offset="100%" stopColor="#aab0b8" stopOpacity="0" />
        </linearGradient>
        {/* Dragon silhouette (charcoal with an ember underglow). */}
        <linearGradient id={`dragon-${uid}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#2c313a" />
          <stop offset="55%" stopColor="#171a1f" />
          <stop offset="100%" stopColor="#3a1410" />
        </linearGradient>
        <filter id={`glow-${uid}`} x="-80%" y="-80%" width="260%" height="260%">
          <feGaussianBlur stdDeviation="3.5" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* ---- Dragon arcing above the wordmark (decoration) ---- */}
      <g className="edgar-dragon" fill={`url(#dragon-${uid})`} stroke="#0a0c0f" strokeWidth="1.2" strokeLinejoin="round">
        {/* Serpentine body (thicker crescent). */}
        <path d="M96 76 C 158 16, 362 16, 438 56 C 372 38, 166 38, 96 76 Z" />
        {/* Spine ridges along the back. */}
        <path d="M194 28 L198 14 L204 30 Z" />
        <path d="M254 22 L258 8 L264 24 Z" />
        <path d="M314 24 L318 11 L324 27 Z" />
        <path d="M372 33 L376 21 L382 36 Z" />
        {/* Large bat-wing membrane rising from the body. */}
        <path d="M292 30 C 280 6, 300 2, 322 6 C 312 12, 318 18, 330 15 C 320 23, 330 28, 342 25 C 330 33, 338 39, 350 37 C 334 45, 314 38, 304 40 C 300 34, 296 31, 292 30 Z" />
        {/* Barbed tail (left). */}
        <path d="M96 76 L72 66 L88 76 L66 86 L90 83 L80 98 L104 80 Z" />
        {/* Horned head with open jaw (right). */}
        <path d="M438 56 C 456 46, 476 50, 482 64 C 473 60, 465 62, 465 68 L 480 70 L 462 77 C 469 84, 462 91, 453 90 C 447 83, 433 69, 426 58 Z" />
        {/* Crest horns. */}
        <path d="M456 48 L466 31 L460 50 Z" />
        <path d="M446 50 L450 34 L441 51 Z" />
      </g>
      {/* Dragon eye ember. */}
      <circle className="edgar-ember" cx="457" cy="62" r="2.6" fill={`url(#lava-${uid})`} />

      {/* ---- Smoke: plume from the crater + wisps wrapping the E ---- */}
      <g className="edgar-smoke" fill={`url(#smoke-${uid})`}>
        {/* Plume rising from the volcano summit, drifting left. */}
        <path d="M346 96 C 334 76, 360 70, 346 50 C 338 36, 358 30, 346 14 C 360 30, 342 46, 356 64 C 366 80, 344 86, 356 104 Z" />
        {/* Wisps curling around the E. */}
        <path d="M70 150 C 42 142, 40 114, 66 108 C 50 120, 60 134, 84 132 C 66 142, 98 150, 84 170 C 76 158, 62 158, 70 150 Z" />
        <path d="M96 198 C 68 192, 64 166, 92 160 C 76 172, 88 184, 112 182 C 94 192, 120 200, 104 218 Z" />
      </g>

      {/* ---- Wordmark: E D G _ R (A is the volcano below). Centred glyphs => even spacing. ---- */}
      <g
        className="edgar-letters"
        fill={`url(#letter-${uid})`}
        fontFamily="Georgia, 'Times New Roman', serif"
        fontWeight={700}
        fontSize="120"
        textAnchor="middle"
      >
        <text x={SLOT.E} y={BASE_Y}>E</text>
        <text x={SLOT.D} y={BASE_Y}>D</text>
        <text x={SLOT.G} y={BASE_Y}>G</text>
        <text x={SLOT.R} y={BASE_Y}>R</text>
      </g>

      {/* ---- The volcano "A" (centred on SLOT.A = 348) ---- */}
      <g className="edgar-volcano">
        {/* Rocky/green cone (truncated triangle => reads as both a volcano and an A). */}
        <path
          d={`M334 104 L362 104 L394 ${BASE_Y} L302 ${BASE_Y} Z`}
          fill={`url(#volcano-${uid})`}
          stroke="#241f1a"
          strokeWidth="2"
        />
        {/* Lava flow down the cone (the A's "bar" + flow). */}
        <path d="M311 178 L385 178 L389 188 L307 188 Z" fill={`url(#molten-${uid})`} opacity="0.95" />
        <path
          d="M348 108 C 352 132, 343 152, 350 178"
          fill="none"
          stroke={`url(#molten-${uid})`}
          strokeWidth="4"
          strokeLinecap="round"
        />
        {/* Crater rim + glowing lava pool at the summit, fuming. */}
        <path d="M333 104 Q 348 96 363 104 Q 348 110 333 104 Z" fill="#2a241f" />
        <ellipse
          className="edgar-ember"
          cx="348"
          cy="103"
          rx="14"
          ry="5"
          fill={`url(#lava-${uid})`}
          filter={`url(#glow-${uid})`}
        />
        {/* Small lava bubble cresting the rim. */}
        <path d="M341 103 Q 348 95 355 103 Z" fill={`url(#lava-${uid})`} filter={`url(#glow-${uid})`} />
        {/* Green base highlight along the foot. */}
        <path d={`M302 ${BASE_Y} L394 ${BASE_Y} L390 ${BASE_Y - 8} C 360 ${BASE_Y - 2}, 336 ${BASE_Y - 2}, 306 ${BASE_Y - 8} Z`} fill="#357a36" opacity="0.6" />
      </g>
    </svg>
  );
}

export default EdgarLogo;
