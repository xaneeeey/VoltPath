import { ChevronRight, Search } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { Ctx } from '../App'
import { api, type LiveState, type Page } from '../api'
import { Card, Empty, Label, PanelHeader, Spinner, Stat, STATUS_COLOR, cx, fmt, socColor } from '../ui'

type Row = { vin: string; registration: string; model_code: string; fleet_id: number; state: LiveState | null }
type Fleet = { id: number; name: string; depot: string; vehicles: number }

export function FleetPanel({ ctx }: { ctx: Ctx }) {
  const s = ctx.summary
  const [q, setQ] = useState('')
  const [fleetId, setFleetId] = useState<number | null>(null)
  const [rows, setRows] = useState<Row[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [fleets, setFleets] = useState<Fleet[]>([])

  useEffect(() => { api<Fleet[]>('/v1/fleets').then(setFleets).catch(() => {}) }, [])
  useEffect(() => {
    const t = setTimeout(async () => {
      const p = new URLSearchParams({ limit: '25' })
      if (q) p.set('q', q)
      if (fleetId) p.set('fleet_id', String(fleetId))
      try {
        const d = await api<Page<Row>>(`/v1/vehicles?${p}`)
        setRows(d.items)
        setCursor(d.next_cursor)
      } catch { setRows([]) }
    }, 250)
    return () => clearTimeout(t)
  }, [q, fleetId])

  async function more() {
    if (!cursor) return
    const p = new URLSearchParams({ limit: '25', cursor })
    if (q) p.set('q', q)
    if (fleetId) p.set('fleet_id', String(fleetId))
    const d = await api<Page<Row>>(`/v1/vehicles?${p}`)
    setRows((r) => [...(r ?? []), ...d.items])
    setCursor(d.next_cursor)
  }

  const total = s ? s.driving + s.charging + s.parked + s.offline : 0
  const seg = s ? [
    { k: 'Driving', v: s.driving, c: 'var(--accent)' }, { k: 'Charging', v: s.charging, c: 'var(--ok)' },
    { k: 'Parked', v: s.parked, c: 'var(--muted)' }, { k: 'Offline', v: s.offline, c: 'var(--line)' },
  ] : []

  return (
    <div>
      <PanelHeader title="Fleet" sub={ctx.me.tenant} />
      {!s ? <div className="flex justify-center py-10"><Spinner /></div> : (
        <>
          <div className="flex items-end justify-between">
            <div>
              <div className="text-[64px] font-extralight leading-none tracking-tight tabular">{fmt.n(s.online)}</div>
              <div className="label mt-2">of {fmt.n(s.vehicles)} vehicles online</div>
            </div>
            <div className="text-right">
              <div className="text-3xl font-light tabular" style={{ color: s.avg_soc == null ? 'var(--muted)' : socColor(s.avg_soc) }}>{fmt.n(s.avg_soc)}<span className="text-base text-muted">%</span></div>
              <div className="label mt-1">average SoC</div>
            </div>
          </div>
          <div className="mt-5 flex h-1.5 overflow-hidden rounded-full bg-sunken" role="img" aria-label="Fleet status breakdown">
            {seg.map((x) => <div key={x.k} style={{ width: `${total ? (x.v / total) * 100 : 0}%`, background: x.c }} />)}
          </div>
          <div className="mt-3 grid grid-cols-4 gap-2 text-xs">
            {seg.map((x) => (
              <div key={x.k}>
                <div className="flex items-center gap-1.5 text-muted"><span className="h-1.5 w-1.5 rounded-full" style={{ background: x.c }} />{x.k}</div>
                <div className="mt-0.5 tabular text-sm">{fmt.n(x.v)}</div>
              </div>
            ))}
          </div>

          <div className="mt-6 grid grid-cols-2 gap-3">
            <Card><Stat label="Below 20 % SoC" value={fmt.n(s.low_soc)} tone={s.low_soc ? 'warn' : undefined} /></Card>
            <Card><Stat label="Charging now" value={fmt.n(s.charging_power_kw / 1000, 1)} unit="MW" tone="ok" /></Card>
            <Card><Stat label="Charging spend today" value={fmt.inr(s.today.charge_cost)} sub={`${fmt.n(s.today.energy_kwh)} kWh · ${fmt.n(s.today.sessions)} sessions`} /></Card>
            <Card><Stat label="Packs at risk" value={fmt.n(s.battery_risk.soh_below_80_within_1y)} tone="bad" sub="SoH < 80 % within a year" /></Card>
          </div>

          {Object.keys(s.open_alerts).length > 0 && (
            <div className="mt-6">
              <Label>Open alerts · 24 h</Label>
              <div className="mt-2 flex flex-wrap gap-2">
                {Object.entries(s.open_alerts).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
                  <button key={k} onClick={() => ctx.setTab('alerts')}
                    className="rounded-full border border-line px-3 py-1 text-xs text-muted hover:text-fg">
                    {k.replaceAll('_', ' ').toLowerCase()} <span className="ml-1 tabular text-fg">{v}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      <div className="mt-8">
        <Label>Vehicles</Label>
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-line bg-raised px-3">
          <Search size={15} className="text-muted" />
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search VIN or registration"
            aria-label="Search vehicles" className="h-10 flex-1 bg-transparent text-sm outline-none placeholder:text-faint" />
        </div>
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1 scroll-thin">
          <button onClick={() => setFleetId(null)} className={cx('shrink-0 rounded-full border px-3 py-1 text-xs', fleetId === null ? 'border-fg' : 'border-line text-muted')}>All</button>
          {fleets.map((f) => (
            <button key={f.id} onClick={() => setFleetId(f.id)} className={cx('shrink-0 rounded-full border px-3 py-1 text-xs', fleetId === f.id ? 'border-fg' : 'border-line text-muted')}>
              {f.name} <span className="text-faint">{fmt.n(f.vehicles)}</span>
            </button>
          ))}
        </div>
        <ul className="mt-2 divide-y divide-line">
          {rows === null && <li className="flex justify-center py-6"><Spinner /></li>}
          {rows?.length === 0 && <Empty>No vehicles match.</Empty>}
          {rows?.map((r) => (
            <li key={r.vin}>
              <button onClick={() => ctx.select(r.vin)} className="flex w-full items-center gap-3 py-3 text-left hover:opacity-80">
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: STATUS_COLOR[r.state?.status ?? 'OFFLINE'] }} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[13px]">{r.vin}</div>
                  <div className="text-xs text-muted">{r.registration} · {r.model_code}</div>
                </div>
                {r.state && <div className="tabular text-sm" style={{ color: socColor(r.state.soc) }}>{Math.round(r.state.soc)}%</div>}
                <ChevronRight size={16} className="text-faint" />
              </button>
            </li>
          ))}
        </ul>
        {cursor && <button onClick={more} className="mt-2 w-full rounded-lg border border-line py-2 text-xs text-muted hover:text-fg">Load more</button>}
      </div>
    </div>
  )
}
