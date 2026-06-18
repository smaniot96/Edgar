/**
 * EdgarLogo — self-contained inline-SVG wordmark for Edgar.
 *
 * Art direction: the wordmark reads "EDGAR" where the letter A is a volcano
 * (triangular A silhouette with a glowing lava crater + smoke plume), a dragon
 * arcs above the whole word guarding it, and smoke curls around the E.
 *
 * Two variants:
 *   - "hero": large splash version for the landing page.
 *   - "mark": compact version for the top nav (sits next to nothing else).
 *
 * Animation (ember flicker + smoke drift) is driven by CSS keyframes defined in
 * index.css and is disabled under prefers-reduced-motion.
 */

import { cn } from "../lib/cn";

type EdgarLogoVariant = "hero" | "mark";

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
  // A stable id suffix keeps gradient ids unique if both variants render at once.
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
        {/* Molten lava glow for the volcano crater. */}
        <radialGradient id={`lava-${uid}`} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#fff3c4" />
          <stop offset="35%" stopColor="#ffb347" />
          <stop offset="70%" stopColor="#ff5e2b" />
          <stop offset="100%" stopColor="#b21f1f" />
        </radialGradient>
        {/* Vertical molten band suggesting the A's crossbar / lava flow. */}
        <linearGradient id={`molten-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ffd166" />
          <stop offset="50%" stopColor="#ff6b2b" />
          <stop offset="100%" stopColor="#7a1414" />
        </linearGradient>
        {/* Pale-gold letterforms. */}
        <linearGradient id={`letter-${uid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#fdf6e3" />
          <stop offset="100%" stopColor="#d9c79a" />
        </linearGradient>
        {/* Ash-grey smoke. */}
        <linearGradient id={`smoke-${uid}`} x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stopColor="#6b7077" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#9aa0a8" stopOpacity="0" />
        </linearGradient>
        {/* Dragon silhouette gradient (charcoal with an ember underglow). */}
        <linearGradient id={`dragon-${uid}`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#2a2f36" />
          <stop offset="60%" stopColor="#1a1d22" />
          <stop offset="100%" stopColor="#3a1410" />
        </linearGradient>
        <filter id={`glow-${uid}`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* ---- Dragon arcing above the wordmark ---- */}
      <g className="edgar-dragon" fill={`url(#dragon-${uid})`} stroke="#0a0c0f" strokeWidth="1.5">
        {/* Body arc + tail */}
        <path d="M70 70 C 150 18, 370 18, 450 70 C 400 50, 300 44, 260 52 C 320 60, 360 70, 372 86 C 350 74, 300 70, 262 66 C 220 70, 170 74, 150 86 C 162 70, 200 60, 260 52 C 220 44, 120 50, 70 70 Z" />
        {/* Left wing */}
        <path d="M150 60 C 110 30, 80 28, 58 40 C 86 44, 104 56, 120 76 C 100 64, 80 64, 66 72 C 92 78, 116 84, 138 78 Z" />
        {/* Right wing */}
        <path d="M370 60 C 410 30, 440 28, 462 40 C 434 44, 416 56, 400 76 C 420 64, 440 64, 454 72 C 428 78, 404 84, 382 78 Z" />
        {/* Head */}
        <path d="M450 70 C 470 64, 484 70, 486 82 C 480 80, 476 82, 474 86 C 480 88, 482 92, 478 96 C 470 94, 458 88, 452 80 Z" />
      </g>

      {/* ---- Smoke plume from the crater + wisps around the E ---- */}
      <g className="edgar-smoke" fill={`url(#smoke-${uid})`}>
        {/* Plume rising from the volcano (A) */}
        <path d="M298 120 C 286 100, 312 92, 300 72 C 294 56, 318 50, 308 34 C 320 48, 304 64, 318 78 C 330 94, 308 104, 320 122 Z" />
        {/* Wisp wrapping the E */}
        <path d="M70 150 C 44 142, 40 116, 64 110 C 50 120, 58 134, 80 132 C 64 142, 96 150, 84 168 C 76 158, 64 158, 70 150 Z" />
        <path d="M96 196 C 70 192, 64 168, 90 162 C 76 172, 86 184, 108 182 C 92 192, 116 198, 104 214 Z" />
      </g>

      {/* ---- Wordmark: E D G _ R (A is drawn as the volcano below) ---- */}
      <g
        className="edgar-letters"
        fill={`url(#letter-${uid})`}
        fontFamily="Georgia, 'Times New Roman', serif"
        fontWeight={700}
        fontSize="120"
        letterSpacing="6"
      >
        <text x="56" y="208">E</text>
        <text x="128" y="208">D</text>
        <text x="208" y="208">G</text>
        {/* gap at ~300 reserved for the volcano A */}
        <text x="408" y="208">R</text>
      </g>

      {/* ---- The volcano "A" ---- */}
      <g className="edgar-volcano">
        {/* Mountain silhouette forming the A */}
        <path
          d="M300 96 L 352 208 L 326 208 L 318 188 L 282 188 L 274 208 L 248 208 Z"
          fill="#23262c"
          stroke="#3a1410"
          strokeWidth="2"
        />
        {/* Molten crossbar band of the A */}
        <path d="M286 178 L 314 178 L 318 188 L 282 188 Z" fill={`url(#molten-${uid})`} />
        {/* Lava crater glow at the summit */}
        <ellipse
          className="edgar-ember"
          cx="300"
          cy="100"
          rx="20"
          ry="9"
          fill={`url(#lava-${uid})`}
          filter={`url(#glow-${uid})`}
        />
        {/* Lava trickle down the slope */}
        <path
          d="M300 104 C 304 130, 296 150, 302 178"
          fill="none"
          stroke={`url(#molten-${uid})`}
          strokeWidth="4"
          strokeLinecap="round"
        />
      </g>
    </svg>
  );
}

export default EdgarLogo;
