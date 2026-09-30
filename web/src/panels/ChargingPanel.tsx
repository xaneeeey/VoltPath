import { CalendarCheck, Zap } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { Ctx } from '../App'
import { api, type Page, type Plan } from '../api'
import { Button, Card, Empty, Label, PanelHeader, Spinner, Stat, cx, fmt } from '../ui'

type Schedule = { id: number; vin: string; created_via: string; window_start: string; expected_cost: number; target_soc: number; status: string }

function nextSeven() {
  // default departure: next 07:00 IST, expressed for a datetime-local input in the viewer's zone
  const now = new Date()
  const ist = new Date(now.getTime() + (330 + now.getTimezoneOffset()) * 60000)
  const dep = new Date(ist)
  dep.setHours(7, 0, 0, 0)
  if (dep.getTime() - ist.getTime() < 3600_000) dep.setDate(dep.getDate() + 1)
  const utc = dep.getTime() - (330 + now.getTimezoneOffset()) * 60000
  const local = new Date(utc - now.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

export function ChargingPanel({ ctx }: { ctx: Ctx }) {
  const vin = ctx.selectedVin
  const [target, setTarget] = useState(80)
  const [depart, setDepart] = useState(nextSeven)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [booked, setBooked] = useState<string | null>(null)
  const [schedules, setSchedules] = useState<Schedule[]>([])

  const loadSchedules = () => api<Page<Schedule>>('/v1/charging/schedules?limit=8').then((d) => setSchedules(d.items)).catch(() => {})
  useEffect(() => { loadSchedules() }, [])

  async function compute() {
    if (!vin) return
    setBusy(true); setErr(null); setBooked(null)
    try {
      setPlan(await api<Plan>('/v1/charging/plan', { method: 'POST', body: JSON.stringify({ vin, target_soc: target, depart_at: new Date(depart).toISOString() }) }))
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  useEffect(() => { setPlan(null); if (vin) compute() }, [vin]) // eslint-disable-line react-hooks/exhaustive-deps

  async function book() {
    if (!vin || !plan) return
    setBusy(true)
    try {
      const r = await api<{ schedule_id: number }>('/v1/charging/schedules', { method: 'POST', body: JSON.stringify({ vin, target_soc: target, depart_at: new Date(depart).toISOString(), charger_id: plan.charger.id }) })
      setBooked(`Scheduled #${r.schedule_id}`)
      loadSchedules()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  const maxPrice = useMemo(() => Math.max(1, ...(plan?.slots.map((s) => s.price) ?? [1])), [plan])
  const maxKw = useMemo(() => Math.max(1, ...(plan?.slots.map((s) => s.kw) ?? [1])), [plan])

  return (
    <div>
      <PanelHeader title="Charging" sub={vin ? <span className="font-mono text-xs">{vin}</span> : 'Cost-optimal schedules under time-of-use tariffs'} />
      {!vin ? (
        <Card><div className="text-sm text-muted">Select a vehicle on the map or in Fleet to plan its charge.</div></Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="target">Target {target}%</label>
              <input id="target" type="range" min={20} max={100} step={5} value={target} onChange={(e) => setTarget(+e.target.value)} className="mt-3 w-full accent-[var(--accent)]" />
            </div>
            <div>
              <label className="label" htmlFor="depart">Departure</label>
              <input id="depart" type="datetime-local" value={depart} onChange={(e) => setDepart(e.target.value)}
                className="mt-2 h-9 w-full rounded-lg border border-line bg-raised px-2 text-xs outline-none" />
            </div>
          </div>
          <Button onClick={compute} disabled={busy} kind="dark" className="mt-4 w-full">{busy ? <Spinner /> : <Zap size={15} />}Optimise</Button>
          {err && <div className="mt-3 text-sm text-bad">{err}</div>}

          {plan && (
            <div className="mt-6 anim-sheet">
              <div className="flex items-end justify-between">
                <div>
                  <div className="label">Optimised cost</div>
                  <div className="text-[44px] font-extralight leading-none tabular">{fmt.inr(plan.cost)}</div>
                </div>
                <div className="text-right">
                  <div className="text-2xl font-light tabular text-ok">−{fmt.inr(plan.savings)}</div>
                  <div className="text-xs text-muted">vs {fmt.inr(plan.baseline_cost)} charging now</div>
                </div>
              </div>
              <div className="mt-4 grid grid-cols-3 gap-3 border-y border-line py-3">
                <Stat label="Energy" value={fmt.n(plan.energy_kwh, 1)} unit="kWh" />
                <Stat label="SoC" value={`${fmt.n(plan.soc_now)}→${fmt.n(plan.final_soc)}`} unit="%" />
                <Stat label="Wear cost" value={fmt.inr(plan.wear_cost)} />
              </div>
              <div className="mt-3 text-xs text-muted">{plan.charger.station} · {plan.charger.type} {plan.charger.max_kw} kW</div>
              {plan.notes.map((n) => <div key={n} className="mt-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">{n}</div>)}

              {/* tariff timeline: bar height = price, fill = planned kW */}
              <div className="mt-5">
                <div className="flex justify-between"><Label>Tariff &amp; plan</Label><span className="text-[10px] text-muted">bars: ₹/kWh · blue: charging</span></div>
                <div className="mt-2 flex h-24 items-end gap-px" role="img" aria-label="Charging slots against tariff">
                  {plan.slots.map((s) => (
                    <div key={s.start} className="relative flex-1" style={{ height: `${(s.price / maxPrice) * 100}%` }} title={`${fmt.time(s.start)} · ₹${s.price}/kWh · ${s.kw} kW`}>
                      <div className="absolute inset-0 rounded-t-sm bg-line" />
                      {s.kw > 0 && <div className="absolute inset-x-0 bottom-0 rounded-t-sm bg-accent" style={{ height: `${Math.max(12, (s.kw / maxKw) * 100)}%` }} />}
                    </div>
                  ))}
                </div>
                <div className="mt-1 flex justify-between text-[10px] text-muted tabular">
                  <span>{fmt.time(plan.slots[0].start)}</span>
                  <span>{fmt.time(plan.slots[Math.floor(plan.slots.length / 2)].start)}</span>
                  <span>{fmt.time(plan.depart_at)}</span>
                </div>
              </div>
              {ctx.can('charging:schedule') ? (
                <Button onClick={book} disabled={busy || !!booked || !plan.slots.some((s) => s.kw > 0)} className="mt-5 w-full">
                  <CalendarCheck size={15} />{booked ?? 'Schedule this charge'}
                </Button>
              ) : <div className="mt-4 text-xs text-muted">Your role can plan but not book charging.</div>}
            </div>
          )}
        </>
      )}

      <div className="mt-8">
        <Label>Recent schedules</Label>
        {schedules.length === 0 ? <Empty>No schedules yet.</Empty> : (
          <ul className="mt-2 divide-y divide-line">
            {schedules.map((s) => (
              <li key={s.id} className="flex items-center gap-3 py-2.5 text-sm">
                <button onClick={() => ctx.select(s.vin, 'charging')} className="min-w-0 flex-1 text-left">
                  <div className="truncate font-mono text-xs">{s.vin}</div>
                  <div className="text-xs text-muted">from {fmt.time(s.window_start)} · to {fmt.n(s.target_soc)}%</div>
                </button>
                <span className={cx('rounded-full px-2 py-0.5 text-[10px]', s.created_via === 'agent' ? 'bg-accent-soft text-accent' : 'bg-raised text-muted')}>{s.created_via}</span>
                <span className="tabular text-xs">{fmt.inr(s.expected_cost)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
