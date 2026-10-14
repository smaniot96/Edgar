/**
 * EdgarLogo — self-contained inline-SVG wordmark for Edgar.
 *
 * Art direction:
 *   - Wordmark "EDGAR" in pale gold Georgia; letters sit on equal centred slots.
 *   - The A is a VOLCANO: a basalt cone cut like a serif A (thin left leg, thick right leg),
 *     its counter glowing with magma, a lava flow for the crossbar, rivulets down both faces
 *     and an erupting crater with flying embers and a smoke plume.
 *   - A side-profile dragon in flight arcs above: horned head with brow, teeth and glowing eye,
 *     arched neck, throat plates, four clawed legs, a curled tail and layered bat wings.
 *     Copper-lit oxblood hide, ivory horns and curved, ribbed wing membranes
 *     so it reads on the dark background.
 *
 * Variants: "hero" (landing splash) and "mark" (compact nav). Animation (ember flicker + smoke
 * drift) lives in index.css and is disabled under prefers-reduced-motion.
 */

import { useId } from "react";

import { cn } from "../lib/cn";

type EdgarLogoVariant = "hero" | "mark";

// Equal slot centres so the wordmark reads evenly; the A slot (348) is the volcano.
const SLOT = { E: 84, D: 172, G: 260, R: 436 } as const;
const BASE_Y = 208; // local baseline; wordmark groups are translated below the dragon

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
  const uid = useId().replace(/:/g, "");
  const id = (name: string) => `${name}-${uid}`;
  const url = (name: string) => `url(#${id(name)})`;

  return (
    <svg
      viewBox="0 0 520 310"
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
        <linearGradient id={id("letter")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#fdf6e3" />
          <stop offset="100%" stopColor="#d9c79a" />
        </linearGradient>
        <linearGradient
          id={id("hide")}
          gradientUnits="userSpaceOnUse"
          x1="250"
          y1="50"
          x2="270"
          y2="175"
        >
          <stop stopColor="#27191a" />
          <stop offset=".48" stopColor="#48231f" />
          <stop offset="1" stopColor="#a85b34" />
        </linearGradient>
        <linearGradient
          id={id("membrane")}
          gradientUnits="userSpaceOnUse"
          x1="145"
          y1="8"
          x2="185"
          y2="143"
        >
          <stop stopColor="#74382b" />
          <stop offset=".48" stopColor="#4c221d" />
          <stop offset="1" stopColor="#211719" />
        </linearGradient>
        <linearGradient
          id={id("far-wing")}
          gradientUnits="userSpaceOnUse"
          x1="260"
          y1="12"
          x2="310"
          y2="116"
        >
          <stop stopColor="#462621" />
          <stop offset="1" stopColor="#211719" />
        </linearGradient>
        <linearGradient
          id={id("bone")}
          gradientUnits="userSpaceOnUse"
          x1="240"
          y1="20"
          x2="300"
          y2="155"
        >
          <stop stopColor="#bf8552" />
          <stop offset=".5" stopColor="#98633e" />
          <stop offset="1" stopColor="#f0bf75" />
        </linearGradient>
        <linearGradient id={id("horn")} x1="0" y1="0" x2="1" y2="1">
          <stop stopColor="#e8d5a8" />
          <stop offset=".6" stopColor="#ad8757" />
          <stop offset="1" stopColor="#654630" />
        </linearGradient>
        <linearGradient id={id("belly")} x1="0" y1="0" x2="0" y2="1">
          <stop stopColor="#8d583a" />
          <stop offset="1" stopColor="#e7ad65" />
        </linearGradient>
        <filter
          id={id("eye-glow")}
          x="-100%"
          y="-150%"
          width="300%"
          height="400%"
        >
          <feGaussianBlur stdDeviation="1.6" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <linearGradient id={id("rock")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#3a2a22" />
          <stop offset="35%" stopColor="#6e5a4a" />
          <stop offset="100%" stopColor="#8a7560" />
        </linearGradient>
        <radialGradient id={id("magma")} cx="50%" cy="70%" r="70%">
          <stop offset="0%" stopColor="#fff1b8" />
          <stop offset="40%" stopColor="#ffb02e" />
          <stop offset="80%" stopColor="#e2441b" />
          <stop offset="100%" stopColor="#7a1410" />
        </radialGradient>
        <linearGradient id={id("lava")} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#ffe08a" />
          <stop offset="55%" stopColor="#ff7a1f" />
          <stop offset="100%" stopColor="#b3200f" />
        </linearGradient>
        <radialGradient id={id("smoke")} cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#8d8f96" stopOpacity="0.55" />
          <stop offset="100%" stopColor="#5d6068" stopOpacity="0" />
        </radialGradient>
        <filter id={id("glow")} x="-100%" y="-100%" width="300%" height="300%">
          <feGaussianBlur stdDeviation="3" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* A short plume stays below the dragon and above the crater. */}
      <g className="edgar-smoke" fill={url("smoke")}>
        <circle cx="348" cy="188" r="7" />
        <circle cx="351" cy="181" r="8" />
        <circle cx="356" cy="176" r="7" />
      </g>

      {/* Hand-drawn flying dragon; layered wings and four articulated legs. */}
      <g
        className="edgar-dragon"
        transform="translate(44 7) scale(0.92)"
        strokeLinejoin="round"
        strokeLinecap="round"
      >
        {/* The distant wing has its own shoulder and a narrower, raised fan. */}
        <path
          d="M285 117 Q294 87 283 61 L270 28 L273 8 L263 22 Q249 10 228 3 Q245 30 233 49 Q229 63 213 69 Q241 75 236 96 Q264 83 267 108 Z"
          fill={url("far-wing")}
          stroke="#98633e"
          strokeWidth="1.15"
        />
        <path
          d="M283 114 Q294 87 283 61 L265 24 M265 24 Q246 12 228 3 M265 24 Q252 56 213 69 M265 24 Q274 76 236 96"
          fill="none"
          stroke="#b08050"
          strokeWidth="1.1"
          opacity=".75"
        />
        {/* A tapered S-curve, with a bladed tip. */}
        <path
          d="M202 125 C166 120 144 141 119 152 C88 166 53 156 49 129 C46 108 66 103 80 117 C67 111 56 117 58 130 C62 149 90 151 116 140 C145 123 167 116 201 120 Z"
          fill={url("hide")}
          stroke="#bf8552"
          strokeWidth="1.1"
        />
        <path
          d="M80 117 L73 104 Q89 108 96 115 L87 118 L88 130 Z"
          fill={url("bone")}
        />
        <path
          d="M59 139 Q76 160 117 146 Q161 123 193 126"
          fill="none"
          stroke="#df9b59"
          strokeWidth="1.1"
        />
        {/* Far legs, darker to separate them from the visible pair. */}
        <path
          d="M207 133 Q218 147 200 152 L189 158 L196 165 L185 162 L182 167 L179 160 L175 155 L187 147 L190 137 Z"
          fill="#35201e"
          stroke="#98633e"
          strokeWidth="1"
        />
        <path
          d="M310 128 Q326 135 328 145 L346 143 L353 149 L345 147 L341 152 L334 150 L319 151 Q308 143 301 138 Z"
          fill="#35201e"
          stroke="#98633e"
          strokeWidth="1"
        />
        {/* Torso, arched neck and the skull are one continuous silhouette. */}
        <path
          d="M175 126 C208 106 244 101 270 108 C291 115 308 105 317 84 C325 62 339 49 357 52 C372 53 381 55 388 61 L401 62 L404 69 L426 74 L435 82 L429 88 L407 87 L397 91 L426 99 L422 105 L401 109 L383 103 C368 110 357 132 335 141 C315 149 294 143 279 138 C257 152 222 155 191 144 C180 140 172 133 175 126 Z"
          fill={url("hide")}
          stroke="#c58b53"
          strokeWidth="1.35"
        />
        <path
          d="M184 128 C215 114 244 112 271 118 C291 126 317 112 325 93 C332 75 337 64 350 62 L354 73 C344 83 339 100 326 113 C310 130 295 137 277 132 Q237 145 202 140 Z"
          fill="#170f13"
          opacity=".28"
        />
        {/* The illuminated throat plates continue into the abdomen. */}
        <path
          d="M378 100 C362 112 353 130 333 136 C312 143 294 137 277 133 Q249 148 213 144 Q250 158 279 141 C301 149 320 151 339 142 C360 132 368 113 383 103 Z"
          fill={url("belly")}
        />
        <path
          d="M368 110 L375 114 M361 118 L367 123 M354 125 L359 131 M345 132 L350 138 M334 136 L338 143 M322 139 L323 146 M309 140 L308 146 M296 137 L292 143 M279 137 L282 144 M265 142 L268 148 M252 146 L254 151 M238 146 L240 151"
          fill="none"
          stroke="#613929"
          strokeWidth="1.15"
        />
        {/* Near hind leg: muscular haunch, bent hock, three hooked talons. */}
        <path
          d="M216 132 C236 132 248 144 238 156 L222 166 L220 171 L233 171 L239 178 L229 175 L234 184 L222 178 L222 184 L214 179 L201 179 L187 172 Q201 171 207 159 C198 156 195 148 199 140 Z"
          fill={url("hide")}
          stroke="#c58b53"
          strokeWidth="1.1"
        />
        <path
          d="M213 139 Q234 137 232 150 Q229 155 216 158 M209 165 L214 170 L228 172"
          fill="none"
          stroke="#df9b59"
          strokeWidth="1.2"
        />
        <path
          d="M233 173 L239 178 L230 175 M228 179 L234 184 L223 179 M220 180 L222 184 L215 180"
          fill="#e2c698"
        />
        {/* Near foreleg, with a defined elbow and grasping claws. */}
        <path
          d="M303 120 C320 124 327 137 314 147 L306 153 L319 164 L337 160 L347 165 L338 166 L348 172 L335 169 L341 178 L327 171 L319 174 L306 164 C291 157 287 147 295 133 Z"
          fill={url("hide")}
          stroke="#c58b53"
          strokeWidth="1.1"
        />
        <path
          d="M308 129 Q320 136 306 147 M299 153 L315 169 L330 166"
          fill="none"
          stroke="#e0a062"
          strokeWidth="1.2"
        />
        <path
          d="M340 162 L347 165 L339 165 M340 168 L348 172 L338 170 M335 173 L341 178 L330 173"
          fill="#e2c698"
        />
        {/* Uneven crest plates follow the neck instead of floating over it. */}
        <path
          d="M326 68 L315 54 L334 59 L324 44 L342 53 L337 37 L354 52 M319 81 L307 71 L322 72 M313 94 L298 88 L316 85 M304 108 L289 100 L310 99 M171 126 L162 117 L180 121 M149 133 L142 126 L158 128 M128 144 L122 137 L135 139 M108 154 L105 147 L115 150"
          fill={url("bone")}
          stroke="#98633e"
          strokeWidth=".55"
        />
        {/* Near wing: an articulated arm and five long fingers, with curved sails between. */}
        <path
          d="M268 120 C256 99 247 78 237 58 L224 21 L230 7 L219 18 C184 5 148 -1 103 3 C122 16 117 33 96 38 C77 43 62 32 28 33 C46 41 50 56 42 69 L34 81 C52 73 72 90 65 111 C91 100 111 117 125 141 C139 117 163 111 190 124 C211 111 237 114 268 120 Z"
          fill={url("membrane")}
          stroke="#bf8552"
          strokeWidth="1.35"
        />
        {/* Broad root shadow and subtle folds give the wing volume. */}
        <path
          d="M223 23 Q213 80 190 124 Q229 109 268 120 Q250 101 236 59 Z"
          fill="#170f12"
          opacity=".4"
        />
        <g fill="none" stroke="#c27848" strokeWidth=".7" opacity=".4">
          <path d="M213 27 Q167 15 119 9 M201 33 Q157 27 114 38 M201 44 Q135 41 48 36 M189 56 Q111 62 45 76 M190 75 Q120 90 73 105 M204 90 Q160 115 132 130" />
          <path d="M119 16 Q144 24 144 35 M53 46 Q75 50 72 66 M78 88 Q100 88 105 103 M139 119 Q159 101 176 101" />
        </g>
        <path
          d="M266 118 C254 99 245 77 234 58 L221 21"
          fill="none"
          stroke="#211519"
          strokeWidth="7"
        />
        <path
          d="M266 118 C254 99 245 77 234 58 L221 21"
          fill="none"
          stroke={url("bone")}
          strokeWidth="3.4"
        />
        <g fill="none" stroke={url("bone")} strokeWidth="1.65">
          <path d="M221 21 Q164 5 103 3" />
          <path d="M221 21 Q125 19 28 33" />
          <path d="M221 21 Q123 42 34 81" />
          <path d="M221 21 Q153 63 65 111" />
          <path d="M221 21 Q177 92 125 141" />
        </g>
        {/* Wing shoulder and overlapping chest armour. */}
        <path
          d="M252 112 Q270 103 285 115 L303 123 Q283 119 279 138 L269 141 Q266 124 252 112 Z"
          fill={url("hide")}
          stroke="#c58b53"
          strokeWidth="1.1"
        />
        <path
          d="M270 113 Q277 116 277 127 M288 120 L282 125 L282 132 M301 111 L308 120 L318 119 M313 105 L321 113 L330 110 M322 97 L330 104 L339 100 M329 87 L337 93 L345 86 M337 76 L345 82 L351 74"
          fill="none"
          stroke="#b7794c"
          strokeWidth="1.1"
          opacity=".8"
        />
        {/* Swept horns, ear fin, cheek armour and a sharp, reptilian muzzle. */}
        <path
          d="M374 62 C361 43 347 31 330 24 C349 43 351 54 365 70 Z"
          fill={url("horn")}
          stroke="#684832"
          strokeWidth=".8"
        />
        <path
          d="M366 64 C352 48 333 39 313 38 C334 48 345 59 356 73 Z"
          fill={url("horn")}
          stroke="#684832"
          strokeWidth=".8"
        />
        <path
          d="M361 74 L341 66 L349 80 L337 83 L353 91 L344 101 L367 96 L376 82 Z"
          fill={url("hide")}
          stroke="#c58b53"
          strokeWidth="1.1"
        />
        <path
          d="M349 76 L365 84 L349 86 M356 92 L366 87"
          fill="none"
          stroke="#dfa465"
          strokeWidth="1"
        />
        <path d="M372 67 L388 64 L405 71 L394 76 L379 73 Z" fill="#815032" />
        <path
          d="M375 67 L388 64 L403 70"
          fill="none"
          stroke="#d4a06b"
          strokeWidth="1.2"
        />
        <path
          d="M375 78 L385 77 L396 88 L385 99 L369 94 L363 85 Z"
          fill="#442522"
          stroke="#bd824e"
          strokeWidth=".9"
        />
        <path
          d="M379 81 L387 84 L386 92 L376 95 M398 79 L412 81 L425 79 M399 102 L413 103 L421 101"
          fill="none"
          stroke="#dea263"
          strokeWidth="1.1"
        />
        <path d="M397 90 L427 89 L430 96 L422 101 L407 98 Z" fill="#140e11" />
        <path
          d="M407 89 L409 97 L412 89 M419 89 L421 95 L424 89 M417 100 L419 96 L422 101"
          fill="#eddbb1"
        />
        <path
          d="M385 74 L398 73 L392 78 L386 78 Z"
          className="edgar-ember"
          fill="#ffcd70"
          filter={url("eye-glow")}
        />
        <path d="M392 74 L391 77" stroke="#291414" strokeWidth="1.4" />
        <path d="M424 80 L428 81" stroke="#140e11" strokeWidth="2" />
        <path
          d="M365 64 L360 58 M371 59 L368 54 M358 66 L352 61"
          stroke="#654630"
          strokeWidth="1"
        />
      </g>

      {/* ---- Wordmark: E D G _ R (the A is the volcano below) ---- */}
      <g
        className="edgar-letters"
        transform="translate(0 72)"
        fill={url("letter")}
        fontFamily="Georgia, 'Times New Roman', serif"
        fontWeight={700}
        fontSize="120"
        textAnchor="middle"
      >
        <text x={SLOT.E} y={BASE_Y}>
          E
        </text>
        <text x={SLOT.D} y={BASE_Y}>
          D
        </text>
        <text x={SLOT.G} y={BASE_Y}>
          G
        </text>
        <text x={SLOT.R} y={BASE_Y}>
          R
        </text>
      </g>

      {/* ---- The volcano "A" (centred on 348) ---- */}
      <g className="edgar-volcano" transform="translate(0 72)">
        {/* Magma glowing in the A's counter. */}
        <path
          d="M348,146 L361,178 L333,178 Z"
          fill={url("magma")}
          filter={url("glow")}
        />
        {/* Basalt cone cut as a serif A (evenodd punches the counter). */}
        <path
          fillRule="evenodd"
          fill={url("rock")}
          stroke="#d9c79a"
          strokeOpacity="0.6"
          strokeWidth="1.2"
          d={`M341,121 Q344,125 348,124 Q352,125 355,121 L392,201 Q395,206 401,${BASE_Y} L367,${BASE_Y} Q371,206 371,202 L365.5,188 L327,188 L320,202 Q320,206 324,${BASE_Y} L296,${BASE_Y} Q302,206 304,201 Z M348,146 L361,178 L333,178 Z`}
        />
        {/* Rock strata. */}
        <path
          d="M314,180 L322,178 M309,192 L318,190 M376,176 L384,179 M380,188 L390,192"
          stroke="#3a2a22"
          strokeWidth="1.2"
          strokeOpacity="0.6"
        />
        {/* Lava flow as the crossbar. */}
        <path
          d="M331,178 C 338,176 354,180 362,178 L 366,188 C 356,190 338,186 327,188 Z"
          fill={url("lava")}
          filter={url("glow")}
        />
        {/* Rivulets running down both faces. */}
        <path
          d="M344,124 C 341,138 334,152 328,168 C 324,180 318,190 312,198"
          fill="none"
          stroke={url("lava")}
          strokeWidth="2"
          filter={url("glow")}
        />
        <path
          d="M353,125 C 358,142 368,160 376,176 C 381,188 385,196 389,204"
          fill="none"
          stroke={url("lava")}
          strokeWidth="2.4"
          filter={url("glow")}
        />
        {/* Crater glow and eruption. */}
        <ellipse
          className="edgar-ember"
          cx="348"
          cy="122"
          rx="9"
          ry="3"
          fill={url("magma")}
          filter={url("glow")}
        />
        <path
          d="M343.5,121 C 344.5,112 346,106 348,99 C 350,106 351.5,112 352.5,121 Z"
          fill={url("lava")}
          filter={url("glow")}
        />
        <g className="edgar-ember" fill="#ffb02e" filter={url("glow")}>
          <circle cx="341" cy="106" r="1.8" />
          <circle cx="356" cy="102" r="1.6" />
          <circle cx="347" cy="93" r="1.4" />
          <circle cx="361" cy="111" r="1.2" />
          <circle cx="335" cy="113" r="1.1" />
        </g>
      </g>
    </svg>
  );
}

export default EdgarLogo;
