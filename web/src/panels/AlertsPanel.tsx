import { Check } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import type { Ctx } from '../App'
import { api, type Alert, type Page } from '../api'
import { Empty, PanelHeader, Segmented, Spinner, cx, fmt } from '../ui'

const SEV: Record<string, 'critical' | 'warning' | 'info'> = {
  LOW_RANGE_NO_CHARGER: 'critical', BATTERY_OVERTEMP: 'critical', DTC_CRITICAL: 'critical',
  LOW_SOC: 'warning', RAPID_SOH_DROP: 'warning', CHARGER_CONGESTION: 'info', UNAPPROVED_DEPOT: 'warning',
}

export function AlertsPanel({ ctx }: { ctx: Ctx }) {
  const [filter, setFilter] = useState<'live' | 'critical' | 'battery'>('live')
  const [items, setItems] = useState<Alert[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [acked, setAcked] = useState<Set<number>>(new Set())

  useEffect(() => {
    setItems(null)
    const type = filter === 'battery' ? '&type=RAPID_SOH_DROP' : ''
    api<Page<Alert>>(`/v1/alerts?status=OPEN&limit=40${type}`).then((d) => { setItems(d.items); setCursor(d.next_cursor) }).catch(() => setItems([]))
  }, [filter])

  const merged = useMemo(() => {
    const seen = new Set<number>()
    const all = [...(filter === 'battery' ? [] : ctx.liveAlerts), ...(items ?? [])].filter((a) => (seen.has(a.id) ? false : (seen.add(a.id), true)))
    return all.filter((a) => {
      const sev = a.severity ?? SEV[a.type_code]
      if (filter === 'critical') return sev === 'critical'
      if (filter === 'live') return a.type_code !== 'RAPID_SOH_DROP'
      return true
    })
  }, [ctx.liveAlerts, items, filter])

  async function more() {
    if (!cursor) return
    const type = filter === 'battery' ? '&type=RAPID_SOH_DROP' : ''
    const d = await api<Page<Alert>>(`/v1/alerts?status=OPEN&limit=40&cursor=${cursor}${type}`)
    setItems((x) => [...(x ?? []), ...d.items]); setCursor(d.next_cursor)
  }
  async function ack(id: number) {
    await api(`/v1/alerts/${id}/ack`, { method: 'POST' })
    setAcked((s) => new Set(s).add(id))
  }

  return (
    <div>
      <PanelHeader title="Alerts" sub="Real-time rules on the telemetry stream"
        right={<Segmented value={filter} onChange={setFilter} options={[{ value: 'live', label: 'Live' }, { value: 'critical', label: 'Critical' }, { value: 'battery', label: 'Battery' }]} />} />
      {items === null ? <div className="flex justify-center py-10"><Spinner /></div> : merged.length === 0 ? <Empty>All clear.</Empty> : (
        <ul className="space-y-2">
          {merged.map((a) => {
            const sev = a.severity ?? SEV[a.type_code] ?? 'info'
            const done = acked.has(a.id)
            return (
              <li key={a.id} className={cx('rounded-2xl border border-line p-3 transition-opacity', done && 'opacity-40')}>
                <div className="flex items-start gap-3">
                  <span className={cx('mt-1.5 h-2 w-2 shrink-0 rounded-full', sev === 'critical' ? 'bg-bad' : sev === 'warning' ? 'bg-warn' : 'bg-accent')} />
                  <button onClick={() => ctx.select(a.vin)} className="min-w-0 flex-1 text-left">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold tracking-wide">{a.type_code.replaceAll('_', ' ')}</span>
                      <span className="shrink-0 text-[11px] text-muted">{fmt.ago(a.created_at)}</span>
                    </div>
                    <div className="mt-0.5 text-sm text-muted">{a.message}</div>
                    <div className="mt-1 font-mono text-[11px] text-faint">{a.vin}</div>
                  </button>
                  {ctx.can('alert:ack') && !done && (
                    <button onClick={() => ack(a.id)} aria-label="Acknowledge" title="Acknowledge"
                      className="grid h-8 w-8 shrink-0 place-items-center rounded-full border border-line text-muted hover:text-ok"><Check size={14} /></button>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}
      {cursor && <button onClick={more} className="mt-3 w-full rounded-lg border border-line py-2 text-xs text-muted hover:text-fg">Load more</button>}
    </div>
  )
}
