import { Moon, Sun, Zap } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { login } from '../api'
import { Button, cx } from '../ui'
import { CarGraphic } from './CarGraphic'

const DEMO = [
  { email: 'fleetmanager@acme.test', label: 'Fleet manager', hint: 'full access · Acme' },
  { email: 'analyst@acme.test', label: 'Analyst', hint: 'masked locations' },
  { email: 'viewer@acme.test', label: 'Viewer', hint: 'read-only' },
  { email: 'admin@acme.test', label: 'Admin', hint: 'audit · erasure' },
  { email: 'fleetmanager@zenith.test', label: 'Other tenant', hint: 'Zenith Rentals' },
]

export function Login({ onDone, theme, toggleTheme }: { onDone: () => void; theme: string; toggleTheme: () => void }) {
  const [email, setEmail] = useState(DEMO[0].email)
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try { await login(email, password); onDone() } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  return (
    <div className="relative grid min-h-full lg:grid-cols-[1.1fr_1fr]">
      <button onClick={toggleTheme} aria-label="Toggle theme" className="absolute right-4 top-4 z-10 grid h-9 w-9 place-items-center rounded-full text-muted hover:bg-raised hover:text-fg">
        {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
      </button>
      <div className="relative hidden items-center justify-center overflow-hidden bg-sunken lg:flex">
        <div className="absolute inset-0 opacity-60" style={{ background: 'radial-gradient(60% 50% at 50% 55%, var(--accent-soft), transparent)' }} />
        <CarGraphic soc={78} status="CHARGING" className="relative h-[70vh] max-h-[640px] -rotate-90" />
        <div className="absolute bottom-10 left-10">
          <div className="text-[64px] font-extralight leading-none tracking-tight tabular">78<span className="text-2xl text-muted">%</span></div>
          <div className="label mt-2">100,000 vehicles · live</div>
        </div>
      </div>
      <div className="flex items-center justify-center px-6 py-16">
        <form onSubmit={submit} className="w-full max-w-sm">
          <div className="mb-10 flex items-center gap-2">
            <Zap size={18} className="text-accent" fill="currentColor" />
            <span className="text-sm font-semibold tracking-[0.36em]">VOLTPATH</span>
          </div>
          <h1 className="text-3xl font-light tracking-tight">Sign in</h1>
          <p className="mt-2 text-sm text-muted">EV charging and battery intelligence for connected fleets.</p>
          <label className="label mt-8 block" htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required
            className="mt-2 h-11 w-full rounded-lg border border-line bg-raised px-3 text-sm outline-none focus:border-accent" />
          <label className="label mt-4 block" htmlFor="password">Password</label>
          <input id="password" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required
            className="mt-2 h-11 w-full rounded-lg border border-line bg-raised px-3 text-sm outline-none focus:border-accent" />
          {error && <div role="alert" className="mt-3 text-sm text-bad">{error}</div>}
          <Button type="submit" disabled={busy} className="mt-6 w-full">{busy ? 'Signing in…' : 'Sign in'}</Button>

          <div className="mt-10">
            <div className="label">Seeded demo accounts</div>
            <div className="mt-3 flex flex-wrap gap-2">
              {DEMO.map((d) => (
                <button type="button" key={d.email} onClick={() => setEmail(d.email)} title={d.hint}
                  className={cx('rounded-full border px-3 py-1.5 text-xs transition-colors',
                    email === d.email ? 'border-fg text-fg' : 'border-line text-muted hover:text-fg')}>
                  {d.label}
                </button>
              ))}
            </div>
            <p className="mt-3 text-xs text-faint">Test password for seeded users is set by SEED_PASSWORD in .env.example.</p>
          </div>
        </form>
      </div>
    </div>
  )
}
