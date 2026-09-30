import { BatteryMedium, Bell, CarFront, LogOut, Moon, ShieldCheck, Sparkles, Sun, Truck, X, Zap } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, auth, liveSocketUrl, setUnauthorizedHandler, type Alert, type Me, type Station, type Summary } from './api'
import { Login } from './components/Login'
import { MapView } from './components/MapView'
import { AlertsPanel } from './panels/AlertsPanel'
import { AuditPanel } from './panels/AuditPanel'
import { BatteryPanel } from './panels/BatteryPanel'
import { ChargingPanel } from './panels/ChargingPanel'
import { CopilotPanel } from './panels/CopilotPanel'
import { FleetPanel } from './panels/FleetPanel'
import { VehiclePanel } from './panels/VehiclePanel'
import { cx, fmt } from './ui'

export type Tab = 'fleet' | 'vehicle' | 'charging' | 'battery' | 'alerts' | 'copilot' | 'audit'
export type Ctx = {
  me: Me
  can: (p: string) => boolean
  summary: Summary | null
  selectedVin: string | null
  select: (vin: string | null, tab?: Tab) => void
  setTab: (t: Tab) => void
  setSelectedPos: (p: [number, number] | null) => void
  showRoute: (route: [number, number][] | null, highlight?: Station[]) => void
  liveAlerts: Alert[]
}

function useTheme() {
  const [theme, setTheme] = useState<'dark' | 'light'>(() =>
    document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark')
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try { localStorage.setItem('vp-theme', theme) } catch { /* storage unavailable */ }
  }, [theme])
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))] as const
}

function useClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 15000); return () => clearInterval(t) }, [])
  return now.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [booting, setBooting] = useState(!!auth.token)
  const [theme, toggleTheme] = useTheme()

  const loadMe = useCallback(async () => {
    try { setMe(await api<Me>('/v1/auth/me')) } catch { setMe(null) } finally { setBooting(false) }
  }, [])
  useEffect(() => { setUnauthorizedHandler(() => setMe(null)); if (auth.token) loadMe() }, [loadMe])

  if (booting) return <div className="grid h-full place-items-center text-muted">Loading…</div>
  if (!me) return <Login onDone={loadMe} theme={theme} toggleTheme={toggleTheme} />
  return <Shell me={me} theme={theme} toggleTheme={toggleTheme} onLogout={() => { auth.set(null); setMe(null) }} />
}

function Shell({ me, theme, toggleTheme, onLogout }: { me: Me; theme: 'dark' | 'light'; toggleTheme: () => void; onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>('fleet')
  const [summary, setSummary] = useState<Summary | null>(null)
  const [selectedVin, setSelectedVin] = useState<string | null>(null)
  const [selectedPos, setSelectedPos] = useState<[number, number] | null>(null)
  const [route, setRoute] = useState<[number, number][] | null>(null)
  const [highlight, setHighlight] = useState<Station[]>([])
  const [liveAlerts, setLiveAlerts] = useState<Alert[]>([])
  const [toasts, setToasts] = useState<Alert[]>([])
  const [connected, setConnected] = useState(false)
  const clock = useClock()
  const perms = useMemo(() => new Set(me.permissions), [me])
  const can = useCallback((p: string) => perms.has(p), [perms])

  // live channel: KPIs every 2 s + alerts as they fire (reconnects with backoff)
  useEffect(() => {
    let ws: WebSocket | null = null
    let stop = false
    let delay = 1000
    const connect = () => {
      ws = new WebSocket(liveSocketUrl())
      ws.onopen = () => { setConnected(true); delay = 1000 }
      ws.onclose = () => { setConnected(false); if (!stop) setTimeout(connect, (delay = Math.min(delay * 2, 15000))) }
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data)
        if (msg.kind === 'kpi') setSummary(msg.data)
        if (msg.kind === 'alert') {
          const a: Alert = { ...msg.data, type_code: msg.data.type }
          setLiveAlerts((xs) => [a, ...xs].slice(0, 200))
          if (a.type_code !== 'RAPID_SOH_DROP') {
            setToasts((xs) => [a, ...xs].slice(0, 3))
            setTimeout(() => setToasts((xs) => xs.filter((x) => x.id !== a.id)), 6500)
          }
        }
      }
    }
    connect()
    return () => { stop = true; ws?.close() }
  }, [])

  const select = useCallback((vin: string | null, next?: Tab) => {
    setSelectedVin(vin)
    setRoute(null)
    setHighlight([])
    if (!vin) setSelectedPos(null)
    setTab(next ?? (vin ? 'vehicle' : 'fleet'))
  }, [])
  const showRoute = useCallback((r: [number, number][] | null, h: Station[] = []) => { setRoute(r); setHighlight(h) }, [])

  const ctx: Ctx = { me, can, summary, selectedVin, select, setTab, setSelectedPos, showRoute, liveAlerts }
  const openAlerts = summary ? Object.entries(summary.open_alerts).filter(([k]) => k !== 'RAPID_SOH_DROP').reduce((a, [, v]) => a + v, 0) : 0

  const dock: { id: Tab; label: string; icon: typeof Truck; show: boolean; badge?: number; disabled?: boolean }[] = [
    { id: 'fleet', label: 'Fleet', icon: Truck, show: true },
    { id: 'vehicle', label: 'Vehicle', icon: CarFront, show: true, disabled: !selectedVin },
    { id: 'charging', label: 'Charging', icon: Zap, show: can('charging:plan') },
    { id: 'battery', label: 'Battery', icon: BatteryMedium, show: can('analytics:read') },
    { id: 'alerts', label: 'Alerts', icon: Bell, show: can('alert:read'), badge: openAlerts },
    { id: 'copilot', label: 'Copilot', icon: Sparkles, show: can('copilot:use') },
    { id: 'audit', label: 'Audit', icon: ShieldCheck, show: can('audit:read') },
  ]

  return (
    <div className="flex h-full flex-col bg-bg">
      {/* ---------------------------------------------------------------- top bar */}
      <header className="flex h-12 shrink-0 items-center justify-between border-b border-line bg-panel px-4">
        <div className="flex items-center gap-2">
          <Zap size={16} className="text-accent" fill="currentColor" />
          <span className="text-[13px] font-semibold tracking-[0.32em]">VOLTPATH</span>
          <span className="ml-3 hidden text-xs text-muted sm:inline">{me.tenant}</span>
        </div>
        <div className="flex items-center gap-4 text-sm">
          <span className="hidden items-center gap-1.5 text-xs text-muted md:flex">
            <span className={cx('h-1.5 w-1.5 rounded-full', connected ? 'live-dot bg-ok' : 'bg-faint')} />
            {connected ? 'LIVE' : 'RECONNECTING'}
          </span>
          <span className="tabular font-medium">{clock}</span>
          <span className="hidden text-xs text-muted sm:inline">Bengaluru</span>
          <button onClick={toggleTheme} aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            className="grid h-8 w-8 place-items-center rounded-full text-muted hover:bg-raised hover:text-fg">
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <div className="hidden text-right leading-tight sm:block">
            <div className="text-xs font-medium">{me.name}</div>
            <div className="text-[10px] uppercase tracking-wider text-muted">{me.role.replace('_', ' ')}</div>
          </div>
          <button onClick={onLogout} aria-label="Sign out" className="grid h-8 w-8 place-items-center rounded-full text-muted hover:bg-raised hover:text-fg">
            <LogOut size={15} />
          </button>
        </div>
      </header>

      {/* ---------------------------------------------------------------- main */}
      <main className="relative flex min-h-0 flex-1 flex-col lg:flex-row">
        <section className="relative order-2 min-h-0 flex-1 overflow-y-auto scroll-thin border-line bg-panel lg:order-1 lg:w-[440px] lg:flex-none lg:border-r">
          <div key={tab} className="anim-sheet p-5 pb-28 lg:pb-8">
            {tab === 'fleet' && <FleetPanel ctx={ctx} />}
            {tab === 'vehicle' && (selectedVin ? <VehiclePanel ctx={ctx} vin={selectedVin} /> : <FleetPanel ctx={ctx} />)}
            {tab === 'charging' && <ChargingPanel ctx={ctx} />}
            {tab === 'battery' && <BatteryPanel ctx={ctx} />}
            {tab === 'alerts' && <AlertsPanel ctx={ctx} />}
            {tab === 'copilot' && <CopilotPanel ctx={ctx} />}
            {tab === 'audit' && <AuditPanel />}
          </div>
        </section>
        <section className="relative order-1 h-[42vh] shrink-0 lg:order-2 lg:h-auto lg:flex-1">
          <MapView theme={theme} selectedVin={selectedVin} selectedPos={selectedPos} route={route} highlight={highlight} onSelectVin={(v) => select(v)} />
          {summary && (
            <div className="pointer-events-none absolute left-4 top-4 hidden gap-2 md:flex">
              {[['Online', fmt.n(summary.online)], ['Driving', fmt.n(summary.driving)], ['Charging', fmt.n(summary.charging)], ['Avg SoC', `${fmt.n(summary.avg_soc)}%`]].map(([k, v]) => (
                <div key={k} className="rounded-xl border border-line bg-panel/85 px-3 py-2 backdrop-blur">
                  <div className="label !text-[9px]">{k}</div>
                  <div className="tabular text-base font-light">{v}</div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* notifications, car-screen style */}
        <div className="pointer-events-none fixed left-1/2 top-14 z-50 flex w-[min(92vw,420px)] -translate-x-1/2 flex-col gap-2" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} style={{ animation: 'slide-down 260ms cubic-bezier(.2,.8,.2,1)' }}
              className="pointer-events-auto flex items-start gap-3 rounded-2xl border border-line bg-panel/95 p-3 pr-2 shadow-[var(--shadow)] backdrop-blur">
              <span className={cx('mt-1 h-2 w-2 shrink-0 rounded-full', t.type_code.includes('LOW_SOC') ? 'bg-warn' : 'bg-bad')} />
              <button className="min-w-0 flex-1 text-left" onClick={() => { select(t.vin); setToasts((xs) => xs.filter((x) => x.id !== t.id)) }}>
                <div className="text-xs font-semibold">{t.type_code.replaceAll('_', ' ')}</div>
                <div className="truncate text-xs text-muted">{t.vin} · {t.message}</div>
              </button>
              <button aria-label="Dismiss" onClick={() => setToasts((xs) => xs.filter((x) => x.id !== t.id))} className="p-1 text-muted hover:text-fg"><X size={14} /></button>
            </div>
          ))}
        </div>
      </main>

      {/* ---------------------------------------------------------------- dock */}
      <nav className="fixed inset-x-0 bottom-0 z-40 flex h-[68px] shrink-0 items-center justify-center gap-1 border-t border-line bg-panel/95 px-2 backdrop-blur lg:static lg:bg-panel"
        aria-label="Main">
        {dock.filter((d) => d.show).map((d) => {
          const Icon = d.icon
          const active = tab === d.id
          return (
            <button key={d.id} onClick={() => !d.disabled && setTab(d.id)} disabled={d.disabled} aria-current={active ? 'page' : undefined}
              className={cx('relative flex h-14 w-[64px] flex-col items-center justify-center gap-1 rounded-xl transition-colors sm:w-[76px]',
                active ? 'text-fg' : 'text-muted hover:text-fg', d.disabled && 'opacity-30')}>
              <Icon size={22} strokeWidth={active ? 2 : 1.6} />
              <span className="text-[10px] font-medium tracking-wide">{d.label}</span>
              {active && <span className="absolute bottom-0.5 h-0.5 w-5 rounded-full bg-accent" />}
              {!!d.badge && d.badge > 0 && (
                <span className="absolute right-2.5 top-1.5 min-w-4 rounded-full bg-bad px-1 text-[9px] font-semibold leading-4 text-white">
                  {d.badge > 99 ? '99+' : d.badge}
                </span>
              )}
            </button>
          )
        })}
      </nav>
    </div>
  )
}
