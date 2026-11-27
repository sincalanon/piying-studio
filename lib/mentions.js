// @ mentions: the one token format, checking tokens against story.json, and the note the director gets with them.
// Pure functions (no file access); lib/host.js reads the project and does the copying.
import { join } from 'node:path'

// One wire format, everywhere (composer, director-send, director-history, the director's own reading):
//   @[label](kind:ref)
// label is display text only (no [ ] or newline, ≤ 40 chars); the host never trusts it and re-derives names from story.json.
//   cast:<id> · scene:<id> · prop:<id>          binding: the user wants this in the current segment
//     …~<kind>:<id>                              binding that replaces that reference of the segment (「换掉一张」)
//   from:<job>/<cast|scene|prop>/<id>            binding from another project: copied in (new id, unreviewed) when sent
//   lib:<scene|prop>/<item>                      binding from the Shisei library (item E001, T001, F001): imported when sent
//   mine:<upload id>                             binding from the user's uploads (a scene or prop picture): imported when sent
//     (from / lib / mine take the same ~<kind>:<id> suffix; the host rewrites them to cast/scene/prop before the director
//      sees the message, so the director only ever reads ids of this project)
//   unit:<uid>                                   pointing: a segment (uid = its first shot id, e.g. 1-3)
//   take:<uid>/<seed>[@<t>]                      pointing: one take (seed 1001…5005), optionally a time in it
//   ver:<uid>/<n>                                pointing: an archived version of a segment
//   final:<t>                                    pointing: a time in the final cut
//   point:kf/<uid>#x,y[,w,h] · point:take/<uid>/<seed>@<t>#… · point:final@<t>#…
//                                                pointing: a spot clicked on a picture (fractions 0..1; w,h = a box)
//   study:<n>[-<m>]                              pointing: shots of the project's reference video (对标片, study.json):
//                                                its framing, move, light and rhythm only, never its people or pictures
// Anything that does not match the grammar stays plain text. A matching token with an unknown kind or id is refused.
export const MENTION_RE = /@\[([^[\]\n]{1,40})\]\(([a-z]{2,6}):([A-Za-z0-9_.@#,~:/-]{1,80})\)/gu
export const LIMITS = { images: 3, people: 2 }
export const NOTE_MARK = '\n\n【Shisei 附注】'
const ASSET_ID = /^[a-z][a-z0-9_]{0,23}$/u
export const JOB_ID_RE = /^j\d{8}-\d{6}-[a-z0-9]{4}$/u
export const UPLOAD_ID_RE = /^u\d{8}-\d{6}-[0-9a-f]{4}$/u
export const LIB_ITEM_RE = /^[A-Z]\d{3}$/u
const UNIT_ID = /^\d{1,2}-\d{1,2}$/u
export const SEEDS = [1001, 2002, 3003, 4004, 5005]
export const BIND = { cast: 'cast', scene: 'scenes', prop: 'props' }
export const EXTERNAL = new Set(['from', 'lib', 'mine'])
const KIND_ZH = { cast: '演员', scene: '场景', prop: '道具' }
const MAX_POINTS = 3

/** Every well-formed token in a message, in order: { start, end, raw, label, kind, ref }. */
export function parseMentions(text) {
  return [...String(text ?? '').matchAll(MENTION_RE)].map((m) => ({ start: m.index, end: m.index + m[0].length, raw: m[0], label: m[1], kind: m[2], ref: m[3] }))
}

/**
 * Where an outside binding comes from, or null when the ref is malformed:
 *   { src: 'from', job, kind, id } · { src: 'lib', cat, item } · { src: 'mine', upload }, each with `swap` ('scene:bus' or null).
 */
export function parseExternal(kind, ref) {
  const [main, swap = null] = String(ref).split('~')
  if (swap !== null && !/^(cast|scene|prop):[a-z][a-z0-9_]{0,23}$/u.test(swap)) return null
  if (kind === 'from') {
    const [job, k, id, ...rest] = main.split('/')
    return !rest.length && JOB_ID_RE.test(job) && k in BIND && ASSET_ID.test(id ?? '') ? { src: 'from', job, kind: k, id, swap } : null
  }
  if (kind === 'lib') {
    const [cat, item, ...rest] = main.split('/')
    return !rest.length && (cat === 'scene' || cat === 'prop') && LIB_ITEM_RE.test(item ?? '') ? { src: 'lib', cat, item, swap } : null
  }
  if (kind === 'mine') return UPLOAD_ID_RE.test(main) ? { src: 'mine', upload: main, swap } : null
  return null
}

/** The outside bindings of a message, checked for form (the host then looks each one up and copies it in). */
export function externalMentions(text) {
  const out = []
  const errs = []
  for (const tk of parseMentions(text)) {
    if (!EXTERNAL.has(tk.kind)) continue
    const x = parseExternal(tk.kind, tk.ref)
    if (x) out.push({ ...tk, ext: x })
    else errs.push(`${tk.raw}：写法不对`)
  }
  if (errs.length) throw new Error(`@ 有问题：\n${errs.join('\n')}`)
  return out
}

/** Replace outside tokens by tokens of this project: map raw token → { kind, id }; the label stays, ~swap is kept. */
export function rewriteMentions(text, map) {
  return String(text ?? '').replace(MENTION_RE, (raw, label, kind, ref) => {
    const to = map.get(raw)
    if (!to) return raw
    const swap = String(ref).split('~')[1]
    return `@[${label}](${to.kind}:${to.id}${swap ? `~${swap}` : ''})`
  })
}

/** What the pipeline calls a segment (mirror of shisei.py units()): consecutive shots, id = first shot id, label A, B, … */
export function storyUnits(story) {
  const shots = Array.isArray(story?.shots) ? story.shots : []
  const by = new Map(shots.map((s) => [s.id, s]))
  const segs = Array.isArray(story?.segments) && story.segments.length ? story.segments : null
  const groups = segs ? segs.map((g) => (g.shots ?? []).map((x) => by.get(x)).filter(Boolean)).filter((g) => g.length) : shots.map((s) => [s])
  const uniq = (xs) => [...new Set(xs)]
  let t = 0
  return groups.map((m, i) => {
    const seconds = m.reduce((n, s) => n + Number(s.seconds ?? 5), 0)
    const u = { id: m[0].id, label: segs ? String.fromCharCode(65 + i) : m[0].id, members: m.map((s) => s.id), scene: m[0].scene, first: m[0],
      cast: uniq(m.flatMap((s) => s.cast ?? [])), props: uniq(m.flatMap((s) => s.props ?? [])), t0: t, t1: t + seconds, seconds }
    t += seconds
    return u
  })
}

export const assetFiles = (kind, id) => (kind === 'cast' ? [`cast_${id}_face.png`, `cast_${id}_body.png`] : [`${kind}_${id}.png`])

/**
 * A segment's reference images as the pipeline feeds them: its first frame is drawn from the first shot's scene + cast +
 * props (validate_story allows at most 3), and the whole segment may hold at most 2 people. Mentions count against these.
 */
export function unitRefs(story, unit) {
  const first = unit.first ?? {}
  const images = [{ kind: 'scene', id: first.scene }, ...(first.cast ?? []).map((id) => ({ kind: 'cast', id })), ...(first.props ?? []).map((id) => ({ kind: 'prop', id }))]
  return { images, people: [...unit.cast] }
}

/**
 * The video's images (mirror of shisei.py take_refs, for the shot studio): the first frame counts as one, then the
 * character sheets (one person: full body + portrait; two: both full bodies) or, with nobody, the scene. At most 3.
 */
export function takeRefs(story, unit) {
  const cast = unit.cast ?? []
  const refs = cast.length === 1 ? [{ kind: 'castbody', id: cast[0] }, { kind: 'cast', id: cast[0] }]
    : cast.length === 2 ? cast.map((id) => ({ kind: 'castbody', id })) : [{ kind: 'scene', id: unit.scene }]
  return { images: [{ kind: 'kf', id: unit.id }, ...refs], people: [...cast] }
}

export function assetInfo(story, kind, id, ctx = {}) {
  const item = (story?.[BIND[kind]] ?? []).find((x) => x.id === id)
  if (!item) return null
  const files = assetFiles(kind, id)
  const have = ctx.have ?? new Set()
  const rv = ctx.review?.assets ?? {}
  const made = files.every((f) => have.has(f))
  const o = item.origin
  const from = o?.kind === 'project' ? `沿用自《${o.title ?? ''}》` : o?.kind === 'library' ? '来自 Shisei 素材库' : o?.kind === 'upload' ? '来自我的上传' : null
  return { kind, id, name: String(item.name ?? id), age: item.age, files, made, approved: made && files.every((f) => rv[f]?.status === 'pass'),
    thumb: have.has(files[0]) ? files[0] : null, from, lookYoung: kind === 'cast' ? !!item.look_young : undefined }
}

// a reference shot's look (study.json, fixed choices) in a few words: ' · 特写 · 推 · 侧光 · 1 人 · 抓注意'
const STUDY_KEYS = ['framing', 'move', 'light', 'people', 'role']
export const studyWords = (look) => STUDY_KEYS.filter((k) => look?.[k]).map((k) => ` · ${look[k]}${k === 'people' ? ' 人' : ''}`).join('')
const fmtT = (s) => { const n = Math.max(0, Math.round(Number(s) || 0)); return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}` }
const num = (s, lo, hi) => { const n = Number(s); return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : null }

/**
 * Check every token against story.json and the job's files (ctx: { have: Set of top-level names, review, cut, dir,
 * scope: 'frame' (default) | 'take' }). Pure: no file access. Throws one Chinese error naming every bad token. Returns
 * the resolved mentions plus the segment-limit outcome for binding mentions (what goes in, what replaces what, what
 * stays out and why). Outside bindings (from / lib / mine) must have been copied in and rewritten first.
 */
export function resolveMentions(text, story, ctx = {}) {
  const toks = parseMentions(text)
  if (!toks.length) return { items: [], current: null, refs: null, added: [], swaps: [], notAdded: [], points: 0 }
  if (!story && toks.some((tk) => tk.kind !== 'study')) throw new Error('这个项目还没有剧本，@ 不到东西')
  const us = story ? storyUnits(story) : []
  const unitOf = (uid) => us.find((u) => u.id === uid)
  const have = ctx.have ?? new Set()
  const errs = []
  const items = []
  const unitTok = (uid, raw) => { const u = UNIT_ID.test(uid) ? unitOf(uid) : null; if (!u) errs.push(`${raw}：没有这一段（${uid}）`); return u }
  let points = 0
  for (const tk of toks) {
    const { kind, ref, raw } = tk
    if (kind in BIND) {
      const [id, swap] = ref.split('~')
      if (!ASSET_ID.test(id)) { errs.push(`${raw}：编号格式不对`); continue }
      const a = assetInfo(story, kind, id, ctx)
      if (!a) { errs.push(`${raw}：这个项目没有编号为 ${id} 的${KIND_ZH[kind]}`); continue }
      let replace = null
      if (swap !== undefined) {
        const m = /^(cast|scene|prop):([a-z][a-z0-9_]{0,23})$/u.exec(swap)
        replace = m && assetInfo(story, m[1], m[2], ctx)
        if (!replace) { errs.push(`${raw}：要换掉的「${swap}」不是这个项目的演员、场景或道具`); continue }
      }
      items.push({ ...tk, bind: true, asset: a, replace })
    } else if (EXTERNAL.has(kind)) {
      errs.push(`${raw}：还没有带进这个项目（发送时会先复制进来）`)
    } else if (kind === 'unit') {
      const u = unitTok(ref, raw)
      if (u) items.push({ ...tk, unit: u })
    } else if (kind === 'take') {
      const m = /^(\d{1,2}-\d{1,2})\/(\d{4})(?:@(\d{1,4}(?:\.\d{1,3})?))?$/u.exec(ref)
      if (!m) { errs.push(`${raw}：条的写法是 take:<段>/<种子>[@秒]`); continue }
      const u = unitTok(m[1], raw); if (!u) continue
      const seed = Number(m[2])
      if (!SEEDS.includes(seed) || !have.has(`take_${u.id}_${seed}.mp4`)) { errs.push(`${raw}：段 ${u.label} 没有这一条`); continue }
      items.push({ ...tk, unit: u, seed, t: m[3] === undefined ? null : num(m[3], 0, 600) })
    } else if (kind === 'ver') {
      const m = /^(\d{1,2}-\d{1,2})\/(\d{1,2})$/u.exec(ref)
      if (!m) { errs.push(`${raw}：版本的写法是 ver:<段>/<版本号>`); continue }
      const u = unitTok(m[1], raw); if (!u) continue
      const v = Number(m[2])
      const files = [`kf_${u.id}.v${v}.png`, ...SEEDS.map((s) => `take_${u.id}_${s}.v${v}.mp4`)].filter((f) => have.has(f))
      if (!files.length) { errs.push(`${raw}：段 ${u.label} 没有 v${v}`); continue }
      items.push({ ...tk, unit: u, version: v, files })
    } else if (kind === 'final') {
      const t = /^\d{1,4}(?:\.\d{1,3})?$/u.test(ref) ? num(ref, 0, 3600) : null
      if (t === null) { errs.push(`${raw}：时间点的写法是 final:<秒>`); continue }
      if (!have.has('final.mp4')) { errs.push(`${raw}：还没有成片`); continue }
      items.push({ ...tk, t, unit: finalUnit(ctx.cut, us, t) })
    } else if (kind === 'study') {
      const m = /^(\d{1,3})(?:-(\d{1,3}))?$/u.exec(ref)
      const shots = ctx.study?.shots ?? []
      if (!m) { errs.push(`${raw}：对标的写法是 study:<镜头号>[-<镜头号>]`); continue }
      if (!shots.length) { errs.push(`${raw}：这个项目还没有对标片`); continue }
      const a = Number(m[1])
      const b = Number(m[2] ?? m[1])
      const sel = shots.filter((x) => x.n >= a && x.n <= b)
      if (a > b || sel.length !== b - a + 1) { errs.push(`${raw}：对标片只有 ${shots.length} 个镜头`); continue }
      items.push({ ...tk, study: { from: a, to: b, shots: sel } })
    } else if (kind === 'point') {
      const m = /^(?:kf\/(\d{1,2}-\d{1,2})|take\/(\d{1,2}-\d{1,2})\/(\d{4})@(\d{1,4}(?:\.\d{1,3})?)|final@(\d{1,4}(?:\.\d{1,3})?))#([0-9.]{1,6}),([0-9.]{1,6})(?:,([0-9.]{1,6}),([0-9.]{1,6}))?$/u.exec(ref)
      if (!m) { errs.push(`${raw}：点的位置写法不对`); continue }
      if (++points > MAX_POINTS) { errs.push(`一条消息最多指 ${MAX_POINTS} 处`); continue }
      const [x, y, w, h] = [m[6], m[7], m[8] ?? 0, m[9] ?? 0].map((v) => num(v, 0, 1))
      if ([x, y, w, h].includes(null)) { errs.push(`${raw}：位置不是数字`); continue }
      const at = { x, y, w, h }
      if (m[1]) {
        const u = unitTok(m[1], raw); if (!u) continue
        if (!have.has(`kf_${u.id}.png`)) { errs.push(`${raw}：段 ${u.label} 还没有首帧`); continue }
        items.push({ ...tk, unit: u, src: 'kf', at })
      } else if (m[2]) {
        const u = unitTok(m[2], raw); if (!u) continue
        const seed = Number(m[3])
        if (!SEEDS.includes(seed) || !have.has(`take_${u.id}_${seed}.mp4`)) { errs.push(`${raw}：段 ${u.label} 没有这一条`); continue }
        items.push({ ...tk, unit: u, src: 'take', seed, t: num(m[4], 0, 600), at })
      } else {
        if (!have.has('final.mp4')) { errs.push(`${raw}：还没有成片`); continue }
        const t = num(m[5], 0, 3600)
        items.push({ ...tk, src: 'final', t, unit: finalUnit(ctx.cut, us, t), at })
      }
    } else {
      errs.push(`${raw}：不能 @「${kind}」，只能 @ 演员、场景、道具、段、条、版本、时间点和画面上的一处`)
    }
  }
  if (errs.length) throw new Error(`@ 有问题：\n${[...new Set(errs)].join('\n')}`)
  // the segment being talked about: the first pointing mention that names one
  const current = items.find((i) => !i.bind && i.unit)?.unit ?? null
  const out = { items, current, refs: null, added: [], swaps: [], notAdded: [], points, scope: ctx.scope === 'take' ? 'take' : 'frame' }
  if (!current) return out
  const r = out.scope === 'take' ? takeRefs(story, current) : unitRefs(story, current)
  const imgs = r.images.map((x) => `${x.kind}:${x.id}`)
  const people = new Set(r.people)
  const has = (key) => imgs.includes(key) || (key.startsWith('cast:') && imgs.includes(`castbody:${key.slice(5)}`))
  const seen = new Set()
  for (const it of items.filter((i) => i.bind)) {
    const key = `${it.kind}:${it.asset.id}`
    if (seen.has(key)) continue
    seen.add(key)
    if (it.replace) {
      const rk = `${it.replace.kind}:${it.replace.id}`
      if (!imgs.includes(rk)) { out.notAdded.push({ item: it, why: `段 ${current.label} 的${out.scope === 'take' ? '视频' : '首帧'}参考图里没有${it.replace.name}，换不了` }); continue }
      if (has(key)) { out.notAdded.push({ item: it, why: `${it.asset.name}已经在段 ${current.label} 里` }); continue }
      if ((it.kind === 'scene') !== (it.replace.kind === 'scene')) { out.notAdded.push({ item: it, why: '场景只能换场景，演员和道具不能换掉场景' }); continue }
      if (it.kind === 'cast' && !people.has(it.asset.id) && people.size - (it.replace.kind === 'cast' && people.has(it.replace.id) ? 1 : 0) >= LIMITS.people) {
        out.notAdded.push({ item: it, why: `这一段已有 ${LIMITS.people} 个人` }); continue
      }
      imgs[imgs.indexOf(rk)] = key
      if (it.replace.kind === 'cast') people.delete(it.replace.id)
      if (it.kind === 'cast') people.add(it.asset.id)
      out.swaps.push(it)
      continue
    }
    if (has(key)) continue                    // already one of this segment's references
    if (it.kind === 'scene' && out.scope === 'frame') { out.notAdded.push({ item: it, why: '每段只有 1 个场景（要换场景就用「换掉一张」换掉原来的场景）' }); continue }
    if (it.kind === 'cast' && !people.has(it.asset.id) && people.size >= LIMITS.people) { out.notAdded.push({ item: it, why: `这一段已有 ${LIMITS.people} 个人` }); continue }
    if (imgs.length >= LIMITS.images) { out.notAdded.push({ item: it, why: out.scope === 'take' ? `视频已满 ${LIMITS.images} 张（首帧算 1 张）` : `这一段已满 ${LIMITS.images} 张` }); continue }
    imgs.push(key)
    if (it.kind === 'cast') people.add(it.asset.id)
    out.added.push(it)
  }
  out.refs = { images: imgs, people: [...people] }
  return out
}

function finalUnit(cut, us, t) {
  const cu = (cut?.units ?? []).filter((x) => Number(x.start) <= t + 0.01).pop()
  return cu ? us.find((u) => u.id === cu.id) ?? null : null
}

export const refFile = (k) => {
  const [kind, id] = k.split(':')
  return kind === 'kf' ? `kf_${id}.png` : kind === 'castbody' ? `cast_${id}_body.png` : kind === 'cast' ? `cast_${id}_face.png` : `${kind}_${id}.png`
}

/**
 * The block appended to the user's words (the tokens stay in the words). `extra` holds what the host made on disk for
 * this message: extra.frames[i] = { path } for take@t / final:t, extra.points[i] = { crop, full, where },
 * extra.adopted = [{ name, kind, id, label }] for outside assets copied in just now.
 */
export function mentionNote(res, story, dir, extra = {}) {
  if (!res.items.length && !extra.adopted?.length) return ''
  const p = (f) => join(dir, f)
  const nameOf = (k) => { const [kind, id] = k.split(':'); return (story?.[BIND[kind === 'castbody' ? 'cast' : kind]] ?? []).find((x) => x.id === id)?.name ?? id }
  const segName = (u) => `段 ${u.label}（unit ${u.id}${u.members.length > 1 ? `，镜 ${u.members.join('、')}` : ''}，约 ${fmtT(u.t0)}–${fmtT(u.t1)}）`
  const lines = ['下面是这条消息里 @ 的东西，由 Shisei 按 story.json 核对过。@[名字](类别:编号) 里以编号为准。']
  if (res.current) lines.push(`在改：${segName(res.current)}${res.scope === 'take' ? '（单镜创作台：只改这一段的视频，首帧算 3 张里的 1 张）' : ''}`)
  for (const a of extra.adopted ?? []) {
    lines.push(`· 刚带进这个项目：${a.name}（${a.kind}:${a.id}，${a.label}）。图在这个项目里还没审，审过之前不能拍。${a.staged ? '还没有剧本：写 story.json 时用这个编号和它原来的 look。' : ''}`)
  }
  // what the image model said about a paused frame (the host asks it when the message is sent)
  const seen = (f) => f?.seen ? `。看图模型看这一帧说（它可能看错；转述时说是看图模型说的）：「${f.seen}」` : ''
  const told = new Set()   // the same asset mentioned twice is described once
  res.items.forEach((it, i) => {
    if (it.bind) {
      const a = it.asset
      const key = `${a.kind}:${a.id}~${it.replace ? `${it.replace.kind}:${it.replace.id}` : ''}`
      if (told.has(key)) return
      told.add(key)
      const state = !a.made ? '还没有图' : a.approved ? '已审' : '待审：你审过之前不能拍'
      const files = a.made ? `：${a.kind === 'cast' ? `定妆照 ${p(a.files[0])}，全身 ${p(a.files[1])}` : `图 ${p(a.files[0])}`}` : ''
      lines.push(`· ${a.name}（${a.kind}:${a.id}${a.age ? `，${a.age} 岁` : ''}${a.from ? `，${a.from}` : ''}，${state}）${files}${it.replace ? `；要换掉 ${it.replace.name}（${it.replace.kind}:${it.replace.id}）` : ''}`)
    } else if (it.kind === 'unit') {
      if (it.unit === res.current && res.items.findIndex((x) => x.kind === 'unit' && x.unit === it.unit) !== i) return
      if (it.unit === res.current) { lines.push(`· 段 ${it.unit.label} 的首帧：${p(`kf_${it.unit.id}.png`)}`); return }
      lines.push(`· ${segName(it.unit)}：首帧 ${p(`kf_${it.unit.id}.png`)}`)
    } else if (it.kind === 'take') {
      const f = extra.frames?.[i]
      lines.push(`· 段 ${it.unit.label} 第 ${SEEDS.indexOf(it.seed) + 1} 条（take:${it.unit.id}/${it.seed}）：${p(`take_${it.unit.id}_${it.seed}.mp4`)}${it.t !== null ? `，${it.t.toFixed(1)} 秒处${f ? ` 那一帧 ${f.path}` : ''}` : ''}${seen(f)}`)
    } else if (it.kind === 'ver') {
      lines.push(`· 段 ${it.unit.label} 旧版本 v${it.version}（ver:${it.unit.id}/${it.version}）：${it.files.map(p).join('，')}`)
    } else if (it.kind === 'final') {
      const f = extra.frames?.[i]
      lines.push(`· 成片 ${fmtT(it.t)}（${it.t.toFixed(1)} 秒${it.unit ? `，在段 ${it.unit.label}` : ''}）${f ? `：那一帧 ${f.path}` : ''}${seen(f)}。要细看用 drama-frames --final --at ${it.t.toFixed(1)} --ask "你的问题"`)
    } else if (it.kind === 'study') {
      const s = it.study
      const range = s.to === s.from ? `#${s.from}` : `#${s.from}–#${s.to}`
      const desc = s.shots.map((x) => `#${x.n} ${(Math.round((x.t1 - x.t0) * 10) / 10)} 秒${studyWords(x.look)}`).join(' → ')
      lines.push(`· 对标 ${range}（study:${s.to === s.from ? s.from : `${s.from}-${s.to}`}）：${desc}。联系表（只给你看）：${s.shots.map((x) => p(x.sheet)).join('，')}。` +
        `完整结构用 drama-study-plan --from ${s.from} --to ${s.to}。只学镜头和节奏，不写对标片里的人、台词和画面。`)
    } else if (it.kind === 'point') {
      const pt = extra.points?.[i]
      const where = it.src === 'kf' ? `段 ${it.unit.label} 的首帧` : it.src === 'take' ? `段 ${it.unit.label} 第 ${SEEDS.indexOf(it.seed) + 1} 条 ${fmtT(it.t)}` : `成片 ${fmtT(it.t)}${it.unit ? `（段 ${it.unit.label}）` : ''}`
      const box = it.at.w > 0 && it.at.h > 0
      lines.push(`· 我在${where}上${box ? '框了一块' : '点了一处'}${pt ? `，在${pt.where}。局部图（${box ? '画了框' : '画了圈'}）：${pt.crop}；整帧（有标记）：${pt.full}` : '（图在发送时生成）'}`)
    }
  })
  if (res.refs) {
    lines.push(`${res.scope === 'take' ? '视频' : '首帧'}参考图 ${res.refs.images.length}/${LIMITS.images}：${res.refs.images.map(refFile).join(' · ')}；人物 ${res.refs.people.length}/${LIMITS.people}` +
      `${res.refs.people.length ? `：${res.refs.people.map((id) => nameOf(`cast:${id}`)).join('、')}` : ''}`)
  }
  for (const n of res.notAdded) lines.push(`没加进参考图：${n.item.asset.name}（${n.item.kind}:${n.item.asset.id}${n.item.asset.approved ? '' : '，待审'}）— ${n.why}`)
  if (res.added.length || res.swaps.length) lines.push('要把演员、场景、道具加进或换进这一段，就改 story.json 里对应镜头的 cast / props（drama-set），先告诉我改哪几个镜头、要重拍什么、大约多少钱，我同意后再改。drama-set 会自己把改到的那一段的首帧退回待审、旧图和旧视频存成版本，没改到的段不动。')
  lines.push('段、条、版本、时间点和点出的图只是告诉你我在说哪里，不要把这些图当生成的参考图或输入图。')
  return NOTE_MARK + '\n' + lines.join('\n')
}

/** What the history shows for a mention: the current name from story.json (ids survive renames), or the old label. */
export function describeMentions(text, story, have = new Set()) {
  const us = storyUnits(story)
  return parseMentions(text).map((tk) => {
    const out = { raw: tk.raw, kind: tk.kind, name: tk.label, bind: tk.kind in BIND || EXTERNAL.has(tk.kind), missing: false, thumb: null }
    if (tk.kind in BIND) {
      const a = story ? assetInfo(story, tk.kind, tk.ref.split('~')[0], { have }) : null
      if (a) { out.name = a.name; out.thumb = a.thumb } else out.missing = true
    } else if (tk.kind === 'study') {
      out.missing = false
    } else if (!EXTERNAL.has(tk.kind)) {
      const uid = /(\d{1,2}-\d{1,2})/u.exec(tk.ref)?.[1]
      const u = uid && us.find((x) => x.id === uid)
      if (uid && !u) out.missing = true
    }
    return out
  })
}
