import type { ReactNode } from 'react'

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(' ')

export const fmt = {
  n: (v: number | null | undefined, d = 0) =>
    v == null || Number.isNaN(v) ? '—' : v.toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d }),
  inr: (v: number | null | undefined) =>
    v == null ? '—' : '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: 0 }),
  time: (iso: string | number) => {
    const d = typeof iso === 'number' ? new Date(iso * 1000) : new Date(iso)
    return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
  },
  ago: (iso: string | number) => {
    const t = typeof iso === 'number' ? iso * 1000 : Date.parse(iso)
    const s = Math.max(0, (Date.now() - t) / 1000)
    if (s < 60) return `${Math.round(s)}s ago`
    if (s < 3600) return `${Math.round(s / 60)}m ago`
    if (s < 86400) return `${Math.round(s / 3600)}h ago`
    return `${Math.round(s / 86400)}d ago`
  },
}

export function socColor(soc: number) {
  if (soc < 15) return 'var(--bad)'
  if (soc < 30) return 'var(--warn)'
  return 'var(--ok)'
}

export const STATUS_COLOR: Record<string, string> = {
  DRIVING: 'var(--accent)', CHARGING: 'var(--ok)', PARKED: 'var(--muted)', OFFLINE: 'var(--faint)',
}

export function Label({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('label', className)}>{children}</div>
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx('rounded-2xl border border-line bg-raised/60 p-4', className)}>{children}</div>
}

export function Stat({ label, value, unit, tone, sub }: {
  label: string; value: ReactNode; unit?: string; tone?: 'ok' | 'warn' | 'bad' | 'accent'; sub?: ReactNode
}) {
  const color = tone ? `var(--${tone})` : undefined
  return (
    <div className="min-w-0">
      <Label>{label}</Label>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="text-[28px] font-light leading-none tracking-tight tabular" style={{ color }}>{value}</span>
        {unit && <span className="text-sm text-muted">{unit}</span>}
      </div>
      {sub && <div className="mt-1 text-xs text-muted">{sub}</div>}
    </div>
  )
}

export function Pill({ children, tone = 'muted' }: { children: ReactNode; tone?: 'ok' | 'warn' | 'bad' | 'accent' | 'muted' }) {
  const styles: Record<string, string> = {
    ok: 'bg-ok-soft text-ok', warn: 'bg-warn-soft text-warn', bad: 'bg-bad-soft text-bad',
    accent: 'bg-accent-soft text-accent', muted: 'bg-raised text-muted',
  }
  return <span className={cx('inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium', styles[tone])}>{children}</span>
}

export function Segmented<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void
}) {
  return (
    <div className="inline-flex rounded-full border border-line bg-sunken p-0.5" role="tablist">
      {options.map((o) => (
        <button key={o.value} role="tab" aria-selected={value === o.value} onClick={() => onChange(o.value)}
          className={cx('rounded-full px-3 py-1 text-xs font-medium transition-colors',
            value === o.value ? 'bg-fg text-bg' : 'text-muted hover:text-fg')}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Button({ children, onClick, kind = 'primary', disabled, className, type = 'button' }: {
  children: ReactNode; onClick?: () => void; kind?: 'primary' | 'ghost' | 'dark'; disabled?: boolean
  className?: string; type?: 'button' | 'submit'
}) {
  const k = {
    primary: 'bg-accent text-white hover:brightness-110',
    ghost: 'border border-line text-fg hover:bg-raised',
    dark: 'bg-fg text-bg hover:opacity-90',
  }[kind]
  return (
    <button type={type} onClick={onClick} disabled={disabled}
      className={cx('inline-flex h-10 items-center justify-center gap-2 rounded-lg px-4 text-sm font-medium transition disabled:opacity-40', k, className)}>
      {children}
    </button>
  )
}

export function Spinner() {
  return <div className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-fg" aria-label="Loading" />
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-10 text-center text-sm text-muted">{children}</div>
}

export function PanelHeader({ title, sub, right }: { title: string; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-4 flex items-start justify-between gap-3">
      <div>
        <h2 className="text-xl font-medium tracking-tight">{title}</h2>
        {sub && <div className="mt-0.5 text-sm text-muted">{sub}</div>}
      </div>
      {right}
    </div>
  )
}
