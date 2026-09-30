import { useEffect, useState } from 'react'
import { api, type Page } from '../api'
import { Empty, PanelHeader, Segmented, Spinner, cx, fmt } from '../ui'

type Row = { id: number; ts: string; user_id: number | null; actor_type: string; action: string; resource: string
  status_code: number; ip: string | null; details: Record<string, unknown> }

export function AuditPanel() {
  const [actor, setActor] = useState<'all' | 'user' | 'agent'>('all')
  const [rows, setRows] = useState<Row[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)

  const q = (c?: string) => `/v1/audit?limit=40${actor !== 'all' ? `&actor_type=${actor}` : ''}${c ? `&cursor=${c}` : ''}`
  useEffect(() => {
    setRows(null)
    api<Page<Row>>(q()).then((d) => { setRows(d.items); setCursor(d.next_cursor) }).catch(() => setRows([]))
  }, [actor]) // eslint-disable-line react-hooks/exhaustive-deps

  async function more() {
    if (!cursor) return
    const d = await api<Page<Row>>(q(cursor))
    setRows((r) => [...(r ?? []), ...d.items]); setCursor(d.next_cursor)
  }

  return (
    <div>
      <PanelHeader title="Audit trail" sub="Every data access and every copilot tool call. Append-only."
        right={<Segmented value={actor} onChange={setActor} options={[{ value: 'all', label: 'All' }, { value: 'user', label: 'Users' }, { value: 'agent', label: 'Agent' }]} />} />
      {rows === null ? <div className="flex justify-center py-10"><Spinner /></div> : rows.length === 0 ? <Empty>No records.</Empty> : (
        <ul className="divide-y divide-line">
          {rows.map((r) => (
            <li key={r.id} className="py-2.5">
              <div className="flex items-center gap-2">
                <span className={cx('rounded px-1.5 py-0.5 text-[10px] font-medium uppercase', r.actor_type === 'agent' ? 'bg-accent-soft text-accent' : 'bg-raised text-muted')}>{r.actor_type}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-xs">{r.action}</span>
                <span className={cx('tabular text-[11px]', r.status_code >= 400 ? 'text-bad' : 'text-muted')}>{r.status_code}</span>
              </div>
              <div className="mt-0.5 flex justify-between text-[11px] text-faint">
                <span>user {r.user_id ?? '—'} · {r.ip ?? 'internal'}</span>
                <span>{fmt.time(r.ts)} · {new Date(r.ts).toLocaleDateString('en-IN')}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {cursor && <button onClick={more} className="mt-3 w-full rounded-lg border border-line py-2 text-xs text-muted hover:text-fg">Load more</button>}
    </div>
  )
}
