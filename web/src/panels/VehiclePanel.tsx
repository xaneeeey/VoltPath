import { ArrowLeft, MapPin, Navigation, Zap } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from 'recharts'
import type { Ctx } from '../App'
import { api, type Reachable, type VehicleDetail } from '../api'
import { CarGraphic } from '../components/CarGraphic'
import { Button, Card, Label, Pill, Spinner, Stat, cx, fmt, socColor } from '../ui'

export function VehiclePanel({ ctx, vin }: { ctx: Ctx; vin: string }) {
  const [d, setD] = useState<VehicleDetail | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [series, setSeries] = useState<{ ts: string; soc: number }[]>([])
  const [reach, setReach] = useState<Reachable | null>(null)
  const [reachBusy, setReachBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const v = await api<VehicleDetail>(`/v1/vehicles/${vin}`)
      setD(v)
      if (v.state) ctx.setSelectedPos([v.state.lon, v.state.lat])
    } catch (e) { setErr((e as Error).message) }
  }, [vin, ctx])

  useEffect(() => { setD(null); setReach(null); load(); const t = setInterval(load, 4000); return () => clearInterval(t) }, [vin]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!ctx.can('telemetry:read')) return
    api<{ points: { ts: string; soc: number }[] }>(`/v1/vehicles/${vin}/telemetry?hours=6`).then((r) => setSeries(r.points)).catch(() => {})
  }, [vin, ctx])

  async function findChargers() {
    setReachBusy(true)
    try {
      const r = await api<Reachable>(`/v1/vehicles/${vin}/reachable-chargers?k=4&explain=true`)
      setReach(r)
      ctx.showRoute(r.chargers[0]?.route ?? null, r.chargers)
    } catch (e) { setErr((e as Error).message) } finally { setReachBusy(false) }
  }

  if (err && !d) return <div className="text-sm text-bad">{err}</div>
  if (!d) return <div className="flex justify-center py-16"><Spinner /></div>
  const st = d.state
  const tone = st?.status === 'CHARGING' ? 'ok' : st?.status === 'DRIVING' ? 'accent' : 'muted'

  return (
    <div>
      <button onClick={() => ctx.select(null)} className="mb-3 flex items-center gap-1 text-xs text-muted hover:text-fg"><ArrowLeft size={14} /> Fleet</button>
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-xl font-medium tracking-tight">{d.vehicle.model}</h2>
          <div className="mt-0.5 font-mono text-xs text-muted">{vin}</div>
          <div className="text-xs text-muted">{d.vehicle.registration} · {d.vehicle.fleet}</div>
        </div>
        {st && <Pill tone={tone as 'ok'}>{st.status.toLowerCase()}</Pill>}
      </div>

      <div className="mt-2 grid grid-cols-[1fr_auto] items-center gap-2">
        <div className="min-w-0">
          {st ? (
            <>
              <div className="text-[64px] font-extralight leading-none tracking-tight tabular" style={{ color: socColor(st.soc) }}>
                {Math.round(st.soc)}<span className="text-xl text-muted">%</span>
              </div>
              {d.range && (
                <div className="mt-4">
                  <div className="text-3xl font-light tabular">{fmt.n(d.range.range_km)} <span className="text-base text-muted">km</span></div>
                  <div className="mt-1 text-[11px] leading-snug text-muted">
                    {d.range.ml_km != null ? 'ML range estimate' : 'range estimate'}<br />rated {fmt.n(d.range.baseline_km)} km
                  </div>
                </div>
              )}
              {st.status === 'CHARGING' && (
                <div className="mt-4"><span className="text-2xl font-light tabular text-ok">{fmt.n(st.power, 1)}</span> <span className="label">kW</span></div>
              )}
            </>
          ) : <div className="text-sm text-muted">No live telemetry yet.</div>}
        </div>
        <CarGraphic soc={st?.soc ?? 0} status={st?.status} className="h-[280px] w-[140px]" />
      </div>

      {st && (
        <div className="grid grid-cols-3 gap-4 border-y border-line py-4">
          <Stat label="Health" value={fmt.n(st.soh, 1)} unit="%" />
          <Stat label="Pack temp" value={fmt.n(st.temp, 0)} unit="°C" tone={st.temp > 50 ? 'bad' : undefined} />
          <Stat label="Speed" value={fmt.n(st.speed)} unit="km/h" />
          <Stat label="Odometer" value={fmt.n(st.odo)} unit="km" />
          <Stat label="Power" value={fmt.n(st.power, 1)} unit="kW" />
          <Stat label="Updated" value={<span className="text-base">{fmt.ago(st.ts)}</span>} />
        </div>
      )}
      {st?.location_masked && <div className="mt-2 flex items-center gap-1 text-xs text-muted"><MapPin size={12} /> Location shown at ~5 km precision for your role.</div>}

      <div className="mt-5 flex gap-2">
        {ctx.can('charging:plan') && <Button onClick={findChargers} disabled={reachBusy || !st} className="flex-1"><Navigation size={15} />{reachBusy ? 'Routing…' : 'Find chargers'}</Button>}
        {ctx.can('charging:plan') && <Button kind="ghost" onClick={() => ctx.setTab('charging')} className="flex-1"><Zap size={15} />Plan charge</Button>}
      </div>

      {reach && (
        <div className="mt-4 anim-sheet">
          {reach.warning && <div className="mb-2 rounded-xl bg-bad-soft px-3 py-2 text-xs text-bad">{reach.warning}</div>}
          <ul className="divide-y divide-line rounded-2xl border border-line">
            {reach.chargers.map((c, i) => (
              <li key={c.id}>
                <button onClick={() => ctx.showRoute(c.route ?? null, [c])} className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-raised">
                  <span className={cx('grid h-6 w-6 place-items-center rounded-full text-[11px] font-semibold', i === 0 ? 'bg-accent text-white' : 'bg-raised text-muted')}>{i + 1}</span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm">{c.name}</div>
                    <div className="text-xs text-muted">{c.free}/{c.connectors} free · {c.max_kw} kW{c.dc ? ' DC' : ''} · ₹{c.price_now}/kWh</div>
                  </div>
                  <div className="text-right text-xs">
                    <div className="tabular">{c.road_km} km</div>
                    <div className="text-muted">arrive {c.arrival_soc}%</div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
          {reach.search && (
            <div className="mt-2 text-[11px] text-faint">
              Road search: multi-target Dijkstra expanded {fmt.n(reach.search.multi_target_expanded)} nodes · A* {fmt.n(reach.search.astar_expanded)} vs Dijkstra {fmt.n(reach.search.dijkstra_expanded)} to #1
            </div>
          )}
        </div>
      )}

      {series.length > 2 && (
        <div className="mt-6">
          <Label>State of charge · 6 h</Label>
          <div className="mt-2 h-24">
            <ResponsiveContainer>
              <AreaChart data={series} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
                <defs><linearGradient id="soc" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="var(--accent)" stopOpacity={0.35} /><stop offset="1" stopColor="var(--accent)" stopOpacity={0} /></linearGradient></defs>
                <YAxis domain={[0, 100]} hide />
                <Tooltip contentStyle={{ background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 10, fontSize: 12 }}
                  labelFormatter={(l) => fmt.time(String(l))} formatter={(v) => [`${Number(v).toFixed(1)}%`, 'SoC']} />
                <Area type="monotone" dataKey="soc" stroke="var(--accent)" strokeWidth={1.5} fill="url(#soc)" isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {d.dtc && d.dtc.length > 0 && (
        <div className="mt-6">
          <Label>Diagnostic codes</Label>
          {d.dtc.map((x) => (
            <div key={x.code} className="mt-2 flex items-start gap-2 text-sm">
              <Pill tone={x.severity === 'critical' ? 'bad' : 'warn'}>{x.code}</Pill>
              <span className="text-muted">{x.description}</span>
            </div>
          ))}
        </div>
      )}

      {d.forecast && (
        <Card className="mt-6">
          <Label>Battery outlook</Label>
          <div className="mt-3 grid grid-cols-3 gap-3">
            <Stat label="Loss / yr" value={fmt.n(d.forecast.slope_per_year, 1)} unit="pts" tone={d.forecast.anomaly_score > 3.5 ? 'bad' : undefined} />
            <Stat label="To 80 %" value={d.forecast.days_to_80 == null ? '>10y' : fmt.n(d.forecast.days_to_80)} unit={d.forecast.days_to_80 == null ? '' : 'days'} />
            <Stat label="DC share" value={fmt.n(d.forecast.dc_fast_share * 100)} unit="%" />
          </div>
          {d.forecast.anomaly_score > 3.5 && <div className="mt-3 text-xs text-bad">Degrading {fmt.n(d.forecast.anomaly_score, 1)}σ faster than its cohort — inspect under warranty.</div>}
          {ctx.can('analytics:read') && <button onClick={() => ctx.setTab('battery')} className="mt-3 text-xs text-accent">Battery details →</button>}
        </Card>
      )}

      {d.open_alerts.length > 0 && (
        <div className="mt-6">
          <Label>Open alerts</Label>
          {d.open_alerts.map((a) => (
            <div key={a.id} className="mt-2 rounded-xl border border-line px-3 py-2">
              <div className="text-xs font-medium">{a.type_code.replaceAll('_', ' ')}</div>
              <div className="text-xs text-muted">{a.message} · {fmt.ago(a.created_at)}</div>
            </div>
          ))}
        </div>
      )}

      {d.driver && (
        <div className="mt-6 text-xs text-muted">
          Driver: <span className="text-fg">{d.driver.full_name ?? 'erased'}</span>{d.driver.phone ? ` · ${d.driver.phone}` : ''}
        </div>
      )}
      <div className="mt-6 grid grid-cols-2 gap-2 text-xs text-muted">
        <div>Pack <span className="text-fg">{d.vehicle.chemistry} {fmt.n(d.vehicle.capacity_kwh, 1)} kWh</span></div>
        <div>OEM feed <span className="text-fg">{d.vehicle.oem}</span></div>
      </div>
    </div>
  )
}
