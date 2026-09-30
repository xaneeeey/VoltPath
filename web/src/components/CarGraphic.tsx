// Top-down EV silhouette (original artwork). The floor-pack fills with state of charge,
// the charge port glows while charging and headlights light up while driving.
import { socColor } from '../ui'

export function CarGraphic({ soc = 0, status = 'PARKED', className }: {
  soc?: number; status?: string; className?: string
}) {
  const fill = Math.max(0, Math.min(100, soc))
  const packTop = 118
  const packH = 196
  const level = packTop + packH * (1 - fill / 100)
  const driving = status === 'DRIVING'
  const charging = status === 'CHARGING'
  return (
    <svg viewBox="0 0 220 440" className={className} role="img" aria-label={`Vehicle, ${Math.round(soc)}% charged, ${status.toLowerCase()}`}>
      <defs>
        <linearGradient id="body" x1="0" x2="1">
          <stop offset="0" stopColor="var(--car-edge)" />
          <stop offset="0.18" stopColor="var(--car-body)" />
          <stop offset="0.82" stopColor="var(--car-body)" />
          <stop offset="1" stopColor="var(--car-edge)" />
        </linearGradient>
        <clipPath id="pack"><rect x="62" y={packTop} width="96" height={packH} rx="14" /></clipPath>
        <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6" /></filter>
      </defs>

      {/* shadow */}
      <ellipse cx="110" cy="225" rx="92" ry="205" fill="black" opacity="0.18" filter="url(#glow)" />
      {/* wheels */}
      {[[26, 78], [174, 78], [26, 318], [174, 318]].map(([x, y]) => (
        <rect key={`${x}${y}`} x={x - 10} y={y} width="20" height="58" rx="7" fill="var(--car-glass)" />
      ))}
      {/* body */}
      <path d="M110 14 C 160 14 186 34 190 82 L 194 180 L 194 300 L 190 372 C 186 410 160 426 110 426 C 60 426 34 410 30 372 L 26 300 L 26 180 L 30 82 C 34 34 60 14 110 14 Z"
        fill="url(#body)" stroke="var(--car-edge)" strokeWidth="1.5" />
      {/* mirrors */}
      <path d="M28 132 l-14 -4 q-4 8 2 12 l12 2 z" fill="var(--car-body)" stroke="var(--car-edge)" />
      <path d="M192 132 l14 -4 q4 8 -2 12 l-12 2 z" fill="var(--car-body)" stroke="var(--car-edge)" />
      {/* windshield, glass roof, rear window */}
      <path d="M52 120 Q110 92 168 120 L160 170 Q110 158 60 170 Z" fill="var(--car-glass)" opacity="0.92" />
      <path d="M62 178 Q110 168 158 178 L158 300 Q110 308 62 300 Z" fill="var(--car-glass)" opacity="0.55" />
      <path d="M60 312 Q110 322 160 312 L166 352 Q110 368 54 352 Z" fill="var(--car-glass)" opacity="0.9" />

      {/* battery pack under the floor (visible through the glass roof) */}
      <g clipPath="url(#pack)" opacity="0.9">
        <rect x="62" y={packTop} width="96" height={packH} fill="var(--sunken)" opacity="0.35" />
        <rect x="62" y={level} width="96" height={packTop + packH - level} fill={socColor(soc)} opacity="0.38">
          <animate attributeName="opacity" values="0.3;0.45;0.3" dur="2.4s" repeatCount={charging ? 'indefinite' : '0'} />
        </rect>
        {Array.from({ length: 7 }).map((_, i) => (
          <line key={i} x1="62" x2="158" y1={packTop + (i + 1) * (packH / 8)} y2={packTop + (i + 1) * (packH / 8)}
            stroke="var(--car-edge)" strokeWidth="0.6" opacity="0.5" />
        ))}
      </g>
      <rect x="62" y={packTop} width="96" height={packH} rx="14" fill="none" stroke="var(--car-edge)" strokeDasharray="3 4" opacity="0.6" />

      {/* headlights / tail lights */}
      <path d="M50 44 Q70 30 96 28" stroke={driving ? '#ffffff' : 'var(--car-edge)'} strokeWidth="3" fill="none" strokeLinecap="round" opacity={driving ? 1 : 0.6} />
      <path d="M170 44 Q150 30 124 28" stroke={driving ? '#ffffff' : 'var(--car-edge)'} strokeWidth="3" fill="none" strokeLinecap="round" opacity={driving ? 1 : 0.6} />
      <path d="M44 402 Q110 420 176 402" stroke="var(--bad)" strokeWidth="3" fill="none" strokeLinecap="round" opacity={driving ? 0.95 : 0.45} />
      {driving && (
        <g opacity="0.25" filter="url(#glow)">
          <path d="M60 30 L20 -10 L90 -10 Z" fill="#fff" /><path d="M160 30 L200 -10 L130 -10 Z" fill="#fff" />
        </g>
      )}
      {/* charge port (rear left) */}
      <circle cx="30" cy="352" r="4" fill={charging ? 'var(--ok)' : 'var(--car-edge)'} />
      {charging && (
        <>
          <circle cx="30" cy="352" r="10" fill="var(--ok)" opacity="0.5" filter="url(#glow)" />
          <path d="M30 352 C 0 352 -4 390 -30 400" stroke="var(--ok)" strokeWidth="3" fill="none" opacity="0.8" />
        </>
      )}
    </svg>
  )
}
