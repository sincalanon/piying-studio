// Read-only views across all projects, for the asset library and the GPU cost page. Pure functions: lib/host.js reads
// the files (story.json, review.json, job.json, ledger.json) and passes them in, so tests need no folders.

const KINDS = [
  ['cast', 'cast', (id) => `cast_${id}_face.png`],
  ['scenes', 'scenes', (id) => `scene_${id}.png`],
  ['props', 'props', (id) => `prop_${id}.png`],
]
const segOf = (shotId) => String(shotId ?? '').split('-')[0]

/**
 * Every character, scene and prop of every drama project, with what the page needs to show where it belongs:
 * the project, whether its picture passed review, the segments that use it, where it was copied from (story `origin`)
 * and the other projects that copied it.
 * projects: [{ job: { id, title, stage, createdAt }, story, review, files: Set<string> | string[] }]  (newest first)
 */
export function assetIndex(projects) {
  const out = { projects: [], cast: [], scenes: [], props: [] }
  const copies = new Map()   // `${job}/${kind}/${id}` → [{ job, project }]
  for (const p of projects) {
    for (const [key] of KINDS) {
      for (const e of p.story?.[key] ?? []) {
        const o = e?.origin
        if (o && o.kind === 'project' && o.job && o.id) {
          const k = `${o.job}/${key}/${o.id}`
          if (!copies.has(k)) copies.set(k, [])
          copies.get(k).push({ job: p.job.id, project: p.job.title || p.story?.title || '' })
        }
      }
    }
  }
  for (const p of projects) {
    const files = p.files instanceof Set ? p.files : new Set(p.files ?? [])
    const shots = p.story?.shots ?? []
    const title = p.job.title || p.story?.title || ''
    // the project page names segments A, B, C … (shisei.py drama_units); without segments a shot is its own unit
    const segLabel = new Map()
    ;(Array.isArray(p.story?.segments) ? p.story.segments : []).forEach((g, i) => {
      for (const id of g?.shots ?? []) segLabel.set(id, String.fromCharCode(65 + i))
    })
    const unitOf = (id) => segLabel.get(id) ?? segOf(id)
    let count = 0
    for (const [key, field, fileOf] of KINDS) {
      for (const e of p.story?.[key] ?? []) {
        if (!e || typeof e.id !== 'string') continue
        const file = fileOf(e.id)
        const has = files.has(file)
        const status = p.review?.assets?.[file]?.status
        const used = shots.filter((s) => field === 'scenes' ? s.scene === e.id : Array.isArray(s[field]) && s[field].includes(e.id))
        const segs = [...new Set(used.map((s) => unitOf(s.id)))].filter(Boolean).sort((a, b) => Number(a) - Number(b) || a.localeCompare(b))
        const o = e.origin
        const origin = !o ? null : o.kind === 'project' ? { kind: 'project', job: String(o.job ?? ''), title: String(o.title ?? '') }
          : o.kind === 'library' ? { kind: 'library' } : o.kind === 'upload' ? { kind: 'upload' } : null
        out[key].push({
          job: p.job.id, project: title, id: e.id, name: e.name, age: e.age ?? null, look: e.look,
          file: has ? file : null,
          review: !has ? 'none' : status === 'pass' ? 'pass' : status === 'reject' ? 'reject' : 'pending',
          lookYoung: key === 'cast' ? !!e.look_young : undefined,
          segs, shots: used.length, origin,
          copies: copies.get(`${p.job.id}/${key}/${e.id}`) ?? [],
        })
        count += 1
      }
    }
    out.projects.push({ id: p.job.id, title, stage: p.job.stage ?? null, createdAt: p.job.createdAt ?? null, assets: count })
  }
  return out
}

const STAGES = ['assets', 'keyframes', 'takes', 'trial', 'swap', 'setup']   // swap: a dance-swap run (shisei.py job-run); setup: making a machine (machine-create)
const dayKey = (sec) => { const d = new Date(sec * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` }
const money = (x) => Math.round(x * 100) / 100

/**
 * The GPU spend recorded in each project's ledger.json, added up. Only the numbers shisei.py wrote (`cost`, `minutes`,
 * `stage`, `state`, `at`) are used; nothing is priced here.
 * projects: [{ job: { id, title, budget?, kind?: 'drama' | 'swap', createdAt? }, ledger: [...] }]; days: how many days back the daily series covers.
 */
export function costSummary(projects, { now = Date.now() / 1000, days = 30 } = {}) {
  const byDay = new Map()
  const end = new Date(now * 1000); end.setHours(0, 0, 0, 0)
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(end); d.setDate(end.getDate() - i)
    byDay.set(dayKey(d.getTime() / 1000), { day: dayKey(d.getTime() / 1000), assets: 0, keyframes: 0, takes: 0, trial: 0, swap: 0, setup: 0, total: 0 })
  }
  const byStage = { assets: 0, keyframes: 0, takes: 0, trial: 0, swap: 0, setup: 0 }
  const byKind = { drama: 0, swap: 0, setup: 0 }
  // the same totals per kind, for the page's 短剧 / 翻拍 filter
  const blank = () => ({ total: 0, minutes: 0, runs: 0, failedCost: 0, failedRuns: 0, projects: 0, last: null })
  const kinds = { drama: blank(), swap: blank(), setup: blank() }
  const byProject = []
  let total = 0; let minutes = 0; let runs = 0; let failedCost = 0; let failedRuns = 0; let first = null; let last = null
  for (const p of projects) {
    let cost = 0; let n = 0; let mins = 0
    const kind = p.job.kind === 'swap' || p.job.kind === 'setup' ? p.job.kind : 'drama'
    const k = kinds[kind]
    for (const e of Array.isArray(p.ledger) ? p.ledger : []) {
      const c = Number(e?.cost); const at = Number(e?.at)
      if (!Number.isFinite(c) || c < 0 || !Number.isFinite(at)) continue
      const stage = STAGES.includes(e.stage) ? e.stage : 'takes'
      cost += c; n += 1; mins += Number(e.minutes) || 0
      byStage[stage] += c
      if (e.state && e.state !== 'done') { failedCost += c; failedRuns += 1; k.failedCost += c; k.failedRuns += 1 }
      first = first === null ? at : Math.min(first, at); last = last === null ? at : Math.max(last, at)
      k.last = k.last === null ? at : Math.max(k.last, at)
      const row = byDay.get(dayKey(at))
      if (row) { row[stage] = money(row[stage] + c); row.total = money(row.total + c) }
    }
    total += cost; minutes += mins; runs += n
    byKind[kind] += cost
    k.total += cost; k.minutes += mins; k.runs += n; if (n) k.projects += 1
    if (n) byProject.push({ id: p.job.id, title: p.job.title || '', kind, createdAt: p.job.createdAt ?? null, cost: money(cost), runs: n, minutes: Math.round(mins), cap: Number(p.job.budget) > 0 ? Number(p.job.budget) : null })
  }
  byProject.sort((a, b) => b.cost - a.cost)
  return {
    total: money(total), minutes: Math.round(minutes), runs, failedCost: money(failedCost), failedRuns,
    byStage: Object.fromEntries(Object.entries(byStage).map(([k, v]) => [k, money(v)])),
    byKind: Object.fromEntries(Object.entries(byKind).map(([k, v]) => [k, money(v)])),
    byDay: [...byDay.values()], byProject, first, last, days,
    kinds: Object.fromEntries(Object.entries(kinds).map(([kk, v]) => [kk, { ...v, total: money(v.total), failedCost: money(v.failedCost), minutes: Math.round(v.minutes) }])),
  }
}
