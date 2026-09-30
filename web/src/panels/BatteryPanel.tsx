import { useEffect, useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { Ctx } from '../App'
import { api } from '../api'
import { Card, Label, PanelHeader, Segmented, Spinner, fmt } from '../ui'

type Report = {
  distribution: { bucket: string; n: number }[]
  at_risk: { vin: string; model_code: string; soh_now: number; slope_per_year: number; days_to_80: number }[]
  anomalies: { vin: string; model_code: string; soh_now: number; slope_per_year: number; anomaly_score: number }[]
  cohorts: { model_code: string; avg_soh: number; avg_loss_per_year: number; n: number }[]
}
type VehBattery = {
  history: { day: string; soh_pct: number }[]
  forecast: { soh_now: number; slope_per_year: number; days_to_80: number | null } | null
  similar: { vin: string; model_code: string; soh_now: number; slope_per_year: number; similarity: number }[]
}
const tip = { contentStyle: { background: 'var(--panel)', border: '1px solid var(--line)', borderRadius: 10, fontSize: 12 } }

export function BatteryPanel({ ctx }: { ctx: Ctx }) {
  const [view, setView] = useState<'fleet' | 'vehicle'>(ctx.selectedVin ? 'vehicle' : 'fleet')
  const [r, setR] = useState<Report | null>(null)
  const [vb, setVb] = useState<VehBattery | null>(null)
  const [list, setList] = useState<'risk' | 'anomalies'>('anomalies')

  useEffect(() => { api<Report>('/v1/analytics/battery-report?limit=12').then(setR).catch(() => {}) }, [])
  useEffect(() => {
    setVb(null)
    if (ctx.selectedVin && view === 'vehicle') api<VehBattery>(`/v1/vehicles/${ctx.selectedVin}/battery`).then(setVb).catch(() => {})
  }, [ctx.selectedVin, view])

  const curve = useMemo(() => {
    if (!vb) return []
    const pts = vb.history.map((h) => ({ day: h.day, soh: h.soh_pct, fc: null as number | null }))
    if (vb.forecast && vb.forecast.slope_per_year > 0 && pts.length) {
      const last = new Date(pts[pts.length - 1].day)
      pts[pts.length - 1].fc = vb.forecast.soh_now
      for (let i = 1; i <= 6; i++) {
        const d = new Date(last.getTime() + i * 60 * 86400000)
        pts.push({ day: d.toISOString().slice(0, 10), soh: null as unknown as number, fc: vb.forecast.soh_now - (vb.forecast.slope_per_year * i * 60) / 365 })
      }
    }
    return pts
  }, [vb])

  return (
    <div>
      <PanelHeader title="Battery health" sub="State-of-health trends, forecasts and anomalies"
        right={ctx.selectedVin ? <Segmented value={view} onChange={setView} options={[{ value: 'fleet', label: 'Fleet' }, { value: 'vehicle', label: 'Vehicle' }]} /> : undefined} />

      {view === 'vehicle' && ctx.selectedVin ? (
        !vb ? <div className="flex justify-center py-10"><Spinner /></div> : (
          <div className="anim-sheet">
            <div className="font-mono text-xs text-muted">{ctx.selectedVin}</div>
            {vb.forecast && (
              <div className="mt-3 flex items-end gap-6">
                <div><div className="text-[48px] font-extralight leading-none tabular">{fmt.n(vb.forecast.soh_now, 1)}<span className="text-lg text-muted">%</span></div><div className="label mt-1">state of health</div></div>
                <div><div className="text-2xl font-light tabular">{fmt.n(vb.forecast.slope_per_year, 1)}</div><div className="label mt-1">pts lost / year</div></div>
                <div><div className="text-2xl font-light tabular">{vb.forecast.days_to_80 == null ? '>10y' : fmt.n(vb.forecast.days_to_80)}</div><div className="label mt-1">days to 80 %</div></div>
              </div>
            )}
            <div className="mt-5 h-48">
              <ResponsiveContainer>
                <LineChart data={curve} margin={{ top: 5, right: 5, left: -20, bottom: 0 }}>
                  <CartesianGrid stroke="var(--line)" vertical={false} />
                  <XAxis dataKey="day" tick={{ fontSize: 10, fill: 'var(--muted)' }} tickLine={false} axisLine={false} minTickGap={40} tickFormatter={(d) => String(d).slice(5)} />
                  <YAxis domain={['dataMin - 2', 'dataMax + 1']} tick={{ fontSize: 10, fill: 'var(--muted)' }} tickLine={false} axisLine={false} />
                  <Tooltip {...tip} formatter={(v) => (v == null ? '—' : `${Number(v).toFixed(2)}%`)} />
                  <ReferenceLine y={80} stroke="var(--bad)" strokeDasharray="4 4" />
                  <Line dataKey="soh" stroke="var(--fg)" dot={false} strokeWidth={1.2} isAnimationActive={false} name="SoH" connectNulls={false} />
                  <Line dataKey="fc" stroke="var(--accent)" dot={false} strokeWidth={1.5} strokeDasharray="5 4" isAnimationActive={false} name="Forecast" connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-6">
              <Label>Degrading like this one · vector search</Label>
              <ul className="mt-2 divide-y divide-line">
                {vb.similar.map((s) => (
                  <li key={s.vin}>
                    <button onClick={() => ctx.select(s.vin, 'battery')} className="flex w-full items-center gap-3 py-2 text-left text-sm hover:opacity-80">
                      <div className="min-w-0 flex-1"><div className="truncate font-mono text-xs">{s.vin}</div><div className="text-xs text-muted">{s.model_code} · SoH {fmt.n(s.soh_now, 1)}% · −{fmt.n(s.slope_per_year, 1)}/yr</div></div>
                      <span className="tabular text-xs text-muted">{(s.similarity * 100).toFixed(1)}%</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )
      ) : !r ? <div className="flex justify-center py-10"><Spinner /></div> : (
        <div className="anim-sheet">
          <Label>SoH distribution</Label>
          <div className="mt-2 h-36">
            <ResponsiveContainer>
              <BarChart data={r.distribution} margin={{ top: 5, right: 0, left: -20, bottom: 0 }}>
                <XAxis dataKey="bucket" tick={{ fontSize: 10, fill: 'var(--muted)' }} tickLine={false} axisLine={false} />
                <YAxis tick={{ fontSize: 10, fill: 'var(--muted)' }} tickLine={false} axisLine={false} />
                <Tooltip {...tip} cursor={{ fill: 'var(--raised)' }} />
                <Bar dataKey="n" fill="var(--accent)" radius={[4, 4, 0, 0]} isAnimationActive={false} name="vehicles" />
              </BarChart>
            </ResponsiveContainer>
          </div>

          <Card className="mt-5 p-0">
            <table className="w-full text-sm">
              <thead><tr className="text-left">{['Model', 'Vehicles', 'Avg SoH', 'Loss/yr'].map((h) => <th key={h} className="label px-3 py-2 font-medium">{h}</th>)}</tr></thead>
              <tbody>
                {r.cohorts.map((c) => (
                  <tr key={c.model_code} className="border-t border-line">
                    <td className="px-3 py-2 font-mono text-xs">{c.model_code}</td>
                    <td className="px-3 py-2 tabular">{fmt.n(c.n)}</td>
                    <td className="px-3 py-2 tabular">{fmt.n(c.avg_soh, 1)}%</td>
                    <td className="px-3 py-2 tabular">{fmt.n(c.avg_loss_per_year, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <div className="mt-6 flex items-center justify-between">
            <Label>{list === 'anomalies' ? 'Abnormal degradation · z > 3.5' : 'Soonest to 80 % SoH'}</Label>
            <Segmented value={list} onChange={setList} options={[{ value: 'anomalies', label: 'Anomalies' }, { value: 'risk', label: 'At risk' }]} />
          </div>
          <ul className="mt-2 divide-y divide-line">
            {(list === 'anomalies' ? r.anomalies : r.at_risk).map((a) => (
              <li key={a.vin}>
                <button onClick={() => ctx.select(a.vin, 'battery')} className="flex w-full items-center gap-3 py-2.5 text-left hover:opacity-80">
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-mono text-xs">{a.vin}</div>
                    <div className="text-xs text-muted">{a.model_code} · SoH {fmt.n(a.soh_now, 1)}% · −{fmt.n(a.slope_per_year, 1)} pts/yr</div>
                  </div>
                  <span className="tabular text-sm text-bad">
                    {'anomaly_score' in a ? `${fmt.n(a.anomaly_score, 1)}σ` : `${fmt.n((a as Report['at_risk'][0]).days_to_80)}d`}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
