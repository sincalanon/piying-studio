// The director's questions (ask_user_question) and permission asks (a tool call that needs the user's yes), answered
// on the Piying page instead of nowhere. dsh hands both to "answerers" on two waterfall events:
//   approval/request        → 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'
//   user-questions/request  → { answers: [{ id, selected: [labels], custom? }] }
// A request from a Piying director session is held here until the user answers on the page; every other session's
// request goes on (next()) to dsh's own UI. Nothing here answers by itself: no default outcome, no timeout approval.
// Pure module (no dsh imports), so it can be tested with stand-ins.

/**
 * @param {{ jobForSession: (sessionId: string) => Promise<string | null>, toolCall?: (jobId: string, callId: string) => Promise<object | null> }} deps
 *   jobForSession: the Piying project whose director runs in that session, or null.
 *   toolCall: the logged call a permission ask is about (name and arguments), to show the user what they allow.
 */
// A call that would start a GPU (and billing). The page shows these with 不允许 only and the host refuses 'allow': booting
// needs the GPU, zone and price in front of the user, which only the project page's pay button shows (AGENTS.md).
export const BILLS = /--confirm-billing|StartCompShareInstance|start_?instance|compshare[^\n]*\b(start|create)\b/iu
export const billsOf = (call, reason) => BILLS.test(`${call ? call.arguments : ''}\n${reason || ''}`)

export function createAsks({ jobForSession, toolCall }) {
  const asks = new Map()          // id → record (functions and the agent stay here, never sent to the page)
  const versions = new Map()      // jobId → change counter, part of the project watch's director version
  let questions = null            // dsh's ctx.userQuestions, for timed questions and late answers
  let seq = 0
  const bump = (job) => versions.set(job, (versions.get(job) ?? 0) + 1)
  const sessionOf = (req) => req?.agent?.session?.id ?? null
  const reasonOf = (req) => (req.displayReason && (req.displayReason['zh-CN'] || req.displayReason.zh || req.displayReason.en)) || req.reason || ''

  async function onApproval(req, next) {
    const job = await jobForSession(sessionOf(req)).catch(() => null)
    if (!job) return next()
    if (req.signal?.aborted) return 'cancelled'
    let call = null
    if (req.callId && toolCall) call = await toolCall(job, req.callId).catch(() => null)
    return new Promise((resolve) => {
      const id = `p${(seq += 1)}`
      asks.set(id, { id, job, kind: 'approval', toolName: req.toolName, reason: reasonOf(req), callId: req.callId ?? null, call, bills: billsOf(call, reasonOf(req)), at: Date.now(), settle: resolve })
      bump(job)
      const cancel = () => { if (asks.delete(id)) { bump(job); resolve('cancelled') } }
      req.signal?.addEventListener?.('abort', cancel, { once: true })
      if (req.signal?.aborted) cancel()   // aborted while the tool call was looked up: the event has already fired
    })
  }

  async function onQuestion(req, next) {
    const job = await jobForSession(sessionOf(req)).catch(() => null)
    if (!job) return next()
    const callId = req.wait?.callId ?? null
    return new Promise((resolve, reject) => {
      const id = `q${(seq += 1)}`
      const rec = { id, job, kind: 'question', questions: req.questions, callId, at: Date.now(), live: true, deadline: null, agent: req.agent, settle: resolve }
      asks.set(id, rec)
      bump(job)
      const stop = new AbortController()
      rec.stop = stop
      const end = (err, keep) => {
        if (!rec.live) return
        rec.live = false
        stop.abort()
        if (!keep) asks.delete(id)
        bump(job)
        reject(err)
      }
      const aborted = () => end(Object.assign(new Error('ask_user_question was aborted before the user answered'), { name: 'UserQuestionError', code: 'ASK_ABORTED' }), false)
      req.signal?.addEventListener?.('abort', aborted, { once: true })
      if (req.signal?.aborted) { aborted(); return }   // aborted while the project was looked up
      // A timed question waits only while some page holds it (dsh's attachWait). This holds it for the director's
      // window; when the window ends the tool returns "pending" and the director goes on, and the question stays on the
      // page: the user's later answer reaches the director as a reply (userQuestions.answer).
      if (req.wait?.timed && callId && questions && req.agent) {
        ;(async () => {
          try {
            // the claim lasts while this loop is inside the stream: no break, it ends when `stop` aborts (answered,
            // withdrawn, or the window below ran out)
            for await (const frame of questions.attachWait(req.agent, callId, stop.signal)) {
              rec.deadline = Date.now() + frame.remainingMs
              bump(job)
              setTimeout(() => end(Object.assign(new Error('ask_user_question timed out before the user answered'), { name: 'UserQuestionError', code: 'ASK_TIMED_OUT' }), true), frame.remainingMs)
            }
          } catch { /* the wait ended on dsh's side; the abort path above reports it */ }
        })()
      }
    })
  }

  /** What the page shows for one project: plain data only. */
  function pending(job) {
    return [...asks.values()].filter((a) => a.job === job).map((a) => (a.kind === 'approval'
      ? { id: a.id, kind: a.kind, toolName: a.toolName, reason: a.reason, call: a.call, bills: a.bills, at: a.at }
      : { id: a.id, kind: a.kind, questions: a.questions, live: a.live, deadline: a.deadline, at: a.at }))
  }

  /**
   * The user's answer from the page. approval: { outcome: 'allow' | 'reject' }.
   * question: { answers: [{ id, selected: [labels], custom? }] }, one per question, labels from its options.
   */
  async function answer(job, id, payload) {
    const a = asks.get(String(id))
    if (!a || a.job !== job) throw new Error('这个问题已经不在了：导演可能已经停下或换了问题。')
    if (a.kind === 'approval') {
      const outcome = payload.outcome === 'allow' ? 'allowed-once' : payload.outcome === 'reject' ? 'rejected' : null
      if (!outcome) throw new Error('只能「允许这一次」或「不允许」')
      if (outcome === 'allowed-once' && a.bills) throw new Error('这一步会开机计费，导演栏里不能允许：请用项目页底栏的按钮，那里有 GPU、可用区和价钱。')
      asks.delete(a.id)
      bump(job)
      a.settle(outcome)
      return { outcome }
    }
    const answers = checkAnswers(a.questions, payload.answers)
    if (a.live) {
      a.live = false
      a.stop?.abort()
      asks.delete(a.id)
      bump(job)
      a.settle({ answers })
      return { answered: true }
    }
    if (!questions || !a.callId || !a.agent) throw new Error('这个问题已经过了等待时间，dsh 也不收迟到的回答了。请在导演栏直接打字回答。')
    questions.answer(a.agent, a.callId, { answers })
    asks.delete(a.id)
    bump(job)
    return { answered: true, late: true }
  }

  return {
    onApproval, onQuestion, pending, answer,
    version: (job) => versions.get(job) ?? 0,
    setQuestionService: (svc) => { questions = svc ?? null },
  }
}

/** One answer per question, labels only from that question's options (custom text is separate and capped). */
export function checkAnswers(qs, given) {
  if (!Array.isArray(given)) throw new Error('回答的格式不对')
  const byId = new Map(given.map((g) => [String(g && g.id), g]))
  return qs.map((q) => {
    const g = byId.get(q.id)
    if (!g) throw new Error(`还有问题没回答：${q.header || q.question}`)
    const labels = new Set((q.options || []).map((o) => o.label))
    const selected = (Array.isArray(g.selected) ? g.selected : []).map(String).filter((l) => labels.has(l))
    if (!q.multiSelect && selected.length > 1) throw new Error(`「${q.header || q.question}」只能选一个`)
    const custom = typeof g.custom === 'string' && g.custom.trim() ? g.custom.trim().slice(0, 2000) : undefined
    if (!selected.length && !custom) throw new Error(`还有问题没回答：${q.header || q.question}`)
    // single-select: a typed answer replaces the choice (dsh's rule)
    return custom !== undefined ? { id: q.id, selected: q.multiSelect ? selected : [], custom } : { id: q.id, selected }
  })
}
