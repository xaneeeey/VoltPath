import { ArrowUp, CheckCircle2, Sparkles, Wrench } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { Ctx } from '../App'
import { api } from '../api'
import { Button, PanelHeader, Spinner, cx } from '../ui'

type Action = { action_id: string; summary: string }
type Msg = { role: 'user' | 'assistant'; content: string; tools?: { tool: string; ok: boolean }[]; actions?: Action[]; mode?: string }

function renderText(text: string) {
  // minimal markdown: **bold** and "- " bullets, rendered as React nodes (no HTML injection)
  return text.split('\n').map((line, i) => {
    const bullet = line.startsWith('- ')
    const parts = (bullet ? line.slice(2) : line).split(/(\*\*[^*]+\*\*)/g).map((p, j) =>
      p.startsWith('**') && p.endsWith('**') ? <strong key={j} className="font-semibold">{p.slice(2, -2)}</strong> : <span key={j}>{p}</span>)
    return bullet
      ? <div key={i} className="flex gap-2 pl-1"><span className="text-faint">•</span><span>{parts}</span></div>
      : <div key={i} className={line ? '' : 'h-2'}>{parts}</div>
  })
}

export function CopilotPanel({ ctx }: { ctx: Ctx }) {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmed, setConfirmed] = useState<Record<string, string>>({})
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth' }) }, [msgs, busy])

  const vin = ctx.selectedVin
  const suggestions = [
    'How is the fleet doing right now?',
    vin ? `Can ${vin} reach a free charger?` : 'Which alerts need attention?',
    vin ? `Cheapest overnight charging plan for ${vin}` : 'Which batteries are degrading abnormally?',
    vin ? `Book the cheapest overnight charge for ${vin}` : 'What does DTC P0A7E mean?',
  ]

  async function send(text: string) {
    if (!text.trim() || busy) return
    const next: Msg[] = [...msgs, { role: 'user', content: text.trim() }]
    setMsgs(next); setInput(''); setBusy(true)
    try {
      const r = await api<{ reply: string; tools: { tool: string; ok: boolean }[]; actions: Action[]; mode: string }>('/v1/copilot/chat', {
        method: 'POST', body: JSON.stringify({ messages: next.slice(-12).map(({ role, content }) => ({ role, content })) }),
      })
      setMsgs([...next, { role: 'assistant', content: r.reply, tools: r.tools, actions: r.actions, mode: r.mode }])
    } catch (e) {
      setMsgs([...next, { role: 'assistant', content: `Sorry — ${(e as Error).message}` }])
    } finally { setBusy(false) }
  }

  async function confirm(a: Action) {
    try {
      const r = await api<{ schedule_id: number }>(`/v1/copilot/actions/${a.action_id}/confirm`, { method: 'POST' })
      setConfirmed((c) => ({ ...c, [a.action_id]: `Booked as schedule #${r.schedule_id}` }))
    } catch (e) { setConfirmed((c) => ({ ...c, [a.action_id]: (e as Error).message })) }
  }

  const submit = (e: FormEvent) => { e.preventDefault(); send(input) }

  return (
    <div className="flex min-h-[60vh] flex-col">
      <PanelHeader title="Copilot" sub="Answers from your live fleet data. Actions always need your confirmation." />
      {msgs.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center py-8 text-center">
          <div className="grid h-12 w-12 place-items-center rounded-full bg-accent-soft text-accent"><Sparkles size={20} /></div>
          <div className="mt-3 text-sm text-muted">Ask about range, chargers, costs, battery health or alerts.</div>
          <div className="mt-5 flex flex-col gap-2">
            {suggestions.map((s) => (
              <button key={s} onClick={() => send(s)} className="rounded-full border border-line px-4 py-2 text-xs text-muted hover:border-fg hover:text-fg">{s}</button>
            ))}
          </div>
        </div>
      )}
      <div className="flex-1 space-y-4">
        {msgs.map((m, i) => (
          <div key={i} className={cx('flex', m.role === 'user' ? 'justify-end' : 'justify-start')}>
            <div className={cx('max-w-[92%] rounded-2xl px-4 py-3 text-sm leading-relaxed',
              m.role === 'user' ? 'bg-fg text-bg' : 'border border-line bg-raised/60')}>
              {m.role === 'assistant' ? renderText(m.content) : m.content}
              {m.tools && m.tools.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5 border-t border-line pt-2">
                  {m.tools.map((t, j) => (
                    <span key={j} className={cx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px]', t.ok ? 'bg-sunken text-muted' : 'bg-bad-soft text-bad')}>
                      <Wrench size={9} />{t.tool}
                    </span>
                  ))}
                  {m.mode === 'rules' && <span className="rounded-full bg-sunken px-2 py-0.5 text-[10px] text-faint">offline mode</span>}
                </div>
              )}
              {m.actions?.map((a) => (
                <div key={a.action_id} className="mt-3 rounded-xl border border-accent/40 bg-accent-soft p-3">
                  <div className="text-xs">{a.summary}</div>
                  {confirmed[a.action_id]
                    ? <div className="mt-2 flex items-center gap-1 text-xs text-ok"><CheckCircle2 size={13} />{confirmed[a.action_id]}</div>
                    : <Button onClick={() => confirm(a)} className="mt-2 h-8 text-xs" disabled={!ctx.can('charging:schedule')}>Confirm booking</Button>}
                </div>
              ))}
            </div>
          </div>
        ))}
        {busy && <div className="flex items-center gap-2 text-xs text-muted"><Spinner /> Working with your fleet data…</div>}
        <div ref={end} />
      </div>
      <form onSubmit={submit} className="sticky bottom-20 mt-4 flex items-center gap-2 rounded-full border border-line bg-panel p-1.5 pl-4 lg:bottom-0">
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask the copilot…" aria-label="Message"
          className="h-9 flex-1 bg-transparent text-sm outline-none placeholder:text-faint" maxLength={2000} />
        <button type="submit" disabled={!input.trim() || busy} aria-label="Send"
          className="grid h-9 w-9 place-items-center rounded-full bg-accent text-white disabled:opacity-30"><ArrowUp size={16} /></button>
      </form>
    </div>
  )
}
