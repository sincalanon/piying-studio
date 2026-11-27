// The 参考 side of a drama project: outside assets (other projects, the Piying library, my uploads) listed, copied in,
// placed in a segment's first frame, and clothes put on a character. Every change is project data only (shisei.py
// drama-adopt / drama-ref / drama-wardrobe); spending stays with the 拍 button and its start-machine confirmation.
// makeRefs takes the host's job helpers so this file needs no state of its own.
import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { cacheLibImage, libGet, listUploads, loadOnlineLibrary, rankRelated, readUpload, REF_KINDS, scriptText, under } from './library.js'
import { assetInfo, BIND, externalMentions, rewriteMentions } from './mentions.js'

const ASSET_ID = /^[a-z][a-z0-9_]{0,23}$/u
const ASSUME = /^(cast_[a-z0-9_]+_(face|body)|scene_[a-z0-9_]+|prop_[a-z0-9_]+|kf_\d{1,2}-\d{1,2})\.png$/u
export function assumeMissing(list) {
  if (list === undefined || list === null) return []
  if (!Array.isArray(list) || list.length > 12 || list.some((f) => !ASSUME.test(String(f)))) throw new Error('bad picture names')
  return list.length ? ['--assume-missing', ...list.map(String)] : []
}

/** What an outside reference is, from the page: { kind: 'from', job, akind, id } | { kind: 'lib', cat, item, view } | { kind: 'mine', upload }. */
export function refSource(src) {
  const x = src ?? {}
  if (x.kind === 'from' && /^j\d{8}-\d{6}-[a-z0-9]{4}$/u.test(String(x.job)) && x.akind in BIND && ASSET_ID.test(String(x.id))) return { src: 'from', job: x.job, kind: x.akind, id: x.id }
  if (x.kind === 'lib' && ['scene', 'prop', 'clothes'].includes(x.cat) && /^[A-Z]\d{3}$/u.test(String(x.item))) return { src: 'lib', cat: x.cat, item: x.item, view: /^[a-z]{2,8}$/u.test(String(x.view ?? '')) ? x.view : null }
  if (x.kind === 'mine' && /^u\d{8}-\d{6}-[0-9a-f]{4}$/u.test(String(x.upload))) return { src: 'mine', upload: x.upload }
  throw new Error('bad reference source')
}

/**
 * deps: { home, prefix, readJob, jobPath, listJobs, mentionCtx, runShisei, keyEnv, dramaScope, libLoad?, libFetch? }
 * (libLoad / libFetch: the online library and its picture getter, swapped in tests).
 * Returns the functions the host's RPC cases call.
 */
export function makeRefs({ home, prefix, readJob, jobPath, listJobs, mentionCtx, runShisei, keyEnv, dramaScope, libLoad = loadOnlineLibrary, libFetch = libGet }) {
  const SHISEI_HOME = home
  const RPC_PREFIX = prefix
  // The Piying library, read online (lib/library.js). Kept 10 minutes; when the site cannot be reached, asked again after 30 s.
  // One load at a time: requests that arrive while it runs (a page of thumbnails) wait for the same answer.
  let libMemo = { at: 0, lib: null, pending: null }
  async function library() {
    const age = Date.now() - libMemo.at
    if (!libMemo.lib || age > (libMemo.lib.available ? 600000 : 30000)) {
      if (!libMemo.pending) libMemo.pending = libLoad(libFetch).then((lib) => { libMemo = { at: Date.now(), lib, pending: null }; return lib }, (e) => { libMemo.pending = null; throw e })
      await libMemo.pending
    }
    return { lib: libMemo.lib, get: libFetch }
  }
  const LIB_CACHE = join(SHISEI_HOME, 'library')
  // why the library cannot be used: the site moved to a format this plugin does not read, or it cannot be reached
  const libDown = (lib) => (lib.why === 'newer' ? '皮影素材库的格式比这个插件新：先升级 dsh-shisei' : '连不上皮影素材库：看一下网络，过一会儿再试')

  /** Copy one outside asset into a project (shisei.py drama-adopt: new id, unreviewed, idempotent per source). */
  async function adoptInto(job, s) {
    const argv = ['drama-adopt', jobPath(job.id)]
    if (s.src === 'from') {
      if (s.job === job.id) throw new Error('这就是这个项目自己的')
      const other = await readJob(s.job)
      if (other.kind !== 'drama') throw new Error('not a drama project')
      argv.push('--kind', s.kind, '--from-job', jobPath(s.job), '--id', s.id)
    } else if (s.src === 'lib') {
      const { lib } = await library()
      if (!lib.available) throw new Error(libDown(lib))
      const { path, item, view } = await cacheLibImage(LIB_CACHE, lib, s.cat, s.item, s.view, 'w900', libFetch)
      if (item.cat === 'clothes') throw new Error('衣服不当参考图：把它放到演员身上，是换衣服')
      argv.push('--kind', item.cat, '--image', path, '--name', item.name, '--name-en', item.nameEn || item.id, '--look', item.look || item.name,
        '--origin', JSON.stringify({ kind: 'library', item: item.id, view }), '--cache', under(SHISEI_HOME, 'library', `${item.set}_${item.id}_${view}.png`))
    } else {
      const rec = await readUpload(SHISEI_HOME, s.upload)
      if (rec.kind === 'clothes') throw new Error('衣服不当参考图：把它放到演员身上，是换衣服')
      if (rec.kind !== 'scene' && rec.kind !== 'prop') throw new Error('这张上传的图不能当参考图')
      argv.push('--kind', rec.kind, '--image', rec.file, '--name', rec.name, '--look', rec.look, '--origin', JSON.stringify({ kind: 'upload', id: rec.id }),
        '--cache', under(SHISEI_HOME, 'uploads', `${rec.id}.${rec.kind}.png`))
    }
    const r = await runShisei(argv, {})
    if (!r.ok) throw new Error(r.error)
    return r
  }

  /** The outside tokens of a message, copied in and rewritten as tokens of this project. */
  async function adoptTokens(job, text) {
    const ext = externalMentions(text)
    const map = new Map()
    const adopted = []
    for (const tk of ext) {
      if (map.has(tk.raw)) continue
      const x = tk.ext
      const s = x.src === 'from' ? x : x.src === 'lib' ? { src: 'lib', cat: x.cat, item: x.item, view: null } : { src: 'mine', upload: x.upload }
      const r = await adoptInto(job, s)
      map.set(tk.raw, { kind: r.kind, id: r.id })
      // the same source sent again comes back as the entry made the first time (again: true): it is not new here
      if (!r.again) adopted.push({ name: r.name, kind: r.kind, id: r.id, label: r.label || '沿用', staged: !!r.staged })
    }
    return { text: map.size ? rewriteMentions(text, map) : text, adopted }
  }

  /**
   * The clothes for a wardrobe change, checked here: a look-young character only gets library clothes that are not
   * revealing, and only as words. payload.picture === true is 「照这张图画」: the clothes picture itself is the second
   * input of the redraw (drama-wardrobe --picture); the result then carries `picture`, the extra argv. The library file
   * is found through the catalogue here, an upload by its id in shisei.py (with its confirmation record).
   */
  async function wardrobeSource(c, payload) {
    const src = payload.src ?? {}
    const picture = payload.picture === true
    if (picture && c.look_young) throw new Error(`${c.name}标了「看起来年轻」：不照图重画，只能按素材库里的文字描述换`)
    if (src.kind === 'lib') {
      const { lib } = await library()
      if (!lib.available) throw new Error(libDown(lib))
      const it = lib.items.find((x) => x.cat === 'clothes' && x.id === String(src.item))
      if (!it) throw new Error('素材库里没有这件衣服（或不适合所有人）')
      if (c.look_young && it.revealing) throw new Error(`${c.name}标了「看起来年轻」：不能换成更暴露的衣服`)
      const out = { outfit: `${it.name}：${it.keep || it.look}`.slice(0, 280), source: `library:${it.id}` }
      return picture ? { ...out, picture: ['--picture', '--image', (await cacheLibImage(LIB_CACHE, lib, 'clothes', it.id, null, 'w900', libFetch)).path] } : out
    }
    if (c.look_young) throw new Error(`${c.name}标了「看起来年轻」：只能换素材库里列出的衣服`)
    if (src.kind === 'mine') {
      const rec = await readUpload(SHISEI_HOME, String(src.upload))
      if (rec.kind !== 'clothes') throw new Error('这张上传的图不是衣服')
      const out = { outfit: `${rec.name}：${rec.look}`.slice(0, 280), source: `upload:${rec.id}` }
      return picture ? { ...out, picture: ['--picture'] } : out
    }
    if (picture) throw new Error('照图换衣要一张衣服的图：从素材库或「我的」里选')
    const outfit = String(payload.outfit ?? '').replace(/\s+/gu, ' ').trim().slice(0, 200)
    if (!outfit) throw new Error('写一句换成什么衣服')
    return { outfit, source: 'text' }
  }

  /** The price of the run a change leads to, or why there is none yet (estimate errors are not the change's errors). */
  async function priceOf(job, stage, opts) {
    try {
      // --pending-ok: a price even while pictures wait for the user (`blocked` names them). It is only a price: the
      // start (drama-start) asks without the flag and refuses, and so does drama-run.
      const r = await runShisei(['drama-estimate', jobPath(job.id), stage, ...dramaScope(opts), ...assumeMissing(opts.assumeMissing), '--pending-ok'], await keyEnv())
      if (!r.ok) return { error: r.error }
      // the run plus the idle minutes before the automatic stop: the same total the start dialog shows by default
      return { cost: Math.round((Number(r.cost) + Number(r.idle_cost || 0)) * 100) / 100, items: r.items, minutes: r.minutes, running: r.running, blocked: r.blocked ?? null }
    } catch (e) { return { error: e instanceof Error ? e.message : String(e) } }
  }

  const thumbOf = (job, f) => `api/${RPC_PREFIX}/file?job=${encodeURIComponent(job)}&name=${encodeURIComponent(f)}`
  const libThumb = (it, tier = 'w300') => `api/${RPC_PREFIX}/ref-file?src=lib&cat=${it.cat}&item=${it.id}&tier=${tier}`
  const mineThumb = (u) => `api/${RPC_PREFIX}/ref-file?src=mine&upload=${u.id}`

  /** Every cast member, scene and prop of the other drama projects (newest first), with whether it was approved there. */
  async function otherAssets(exceptId) {
    const out = []
    for (const j of await listJobs()) {
      if (j.kind !== 'drama' || j.id === exceptId) continue
      let story
      let review = {}
      try { story = JSON.parse(await readFile(jobPath(j.id, 'story.json'), 'utf8')) } catch { continue }
      try { review = JSON.parse(await readFile(jobPath(j.id, 'review.json'), 'utf8')) } catch { /* nothing reviewed */ }
      for (const [kind, pl] of Object.entries(BIND)) {
        for (const x of story[pl] ?? []) {
          if (!ASSET_ID.test(String(x.id))) continue
          const f = kind === 'cast' ? `cast_${x.id}_face.png` : `${kind}_${x.id}.png`
          const has = existsSync(jobPath(j.id, f))
          out.push({ src: 'from', job: j.id, project: j.title || '', kind, id: x.id, name: String(x.name ?? x.id).slice(0, 40), age: x.age, look: String(x.look ?? '').slice(0, 160),
            thumb: has ? thumbOf(j.id, f) : null, approved: has && review.assets?.[f]?.status === 'pass' })
        }
      }
    }
    return out
  }

  /** The composer's 「别处」 group: other projects' assets, library scenes and props, my scene and prop uploads. */
  async function externalOptions(exceptId) {
    const out = []
    for (const a of await otherAssets(exceptId)) out.push({ group: '别的项目', kind: 'from', ref: `${a.job}/${a.kind}/${a.id}`, akind: a.kind, name: a.name, sub: `《${a.project}》${a.age ? ` · ${a.age} 岁` : ''}`, thumb: a.thumb })
    const { lib } = await library()
    for (const it of lib.items) if (it.cat !== 'clothes') out.push({ group: '皮影素材库', kind: 'lib', ref: `${it.cat}/${it.id}`, akind: it.cat, name: it.name, sub: it.group, thumb: libThumb(it) })
    for (const u of await listUploads(SHISEI_HOME)) if (u.kind === 'scene' || u.kind === 'prop') out.push({ group: '我的上传', kind: 'mine', ref: u.id, akind: u.kind, name: u.name, sub: REF_KINDS[u.kind], thumb: mineThumb(u) })
    return out
  }

  /** The 参考 panel: this project, 「和《项目名》有关」, other projects, the library, my uploads. */
  async function refsFor(job) {
    const ctx = job ? await mentionCtx(job) : null
    const story = ctx?.story ?? null
    const project = []
    if (story) {
      for (const kind of Object.keys(BIND)) for (const x of story[BIND[kind]] ?? []) {
        if (!ASSET_ID.test(String(x.id))) continue
        const a = assetInfo(story, kind, x.id, ctx)
        project.push({ src: 'project', kind, id: x.id, name: a.name, age: a.age, look: String(x.look ?? '').slice(0, 160), outfit: x.outfit ?? null, thumb: a.thumb ? thumbOf(job.id, a.thumb) : null,
          approved: a.approved, made: a.made, from: a.from, lookYoung: !!x.look_young, staged: !!story.staged })
      }
    }
    const others = await otherAssets(job?.id)
    const { lib } = await library()
    const items = lib.items.map((it) => ({ src: 'lib', cat: it.cat, id: it.id, name: it.name, group: it.group, look: it.look.slice(0, 140), views: it.views, gender: it.gender,
      revealing: it.cat === 'clothes' ? !!it.revealing : undefined, thumb: libThumb(it), big: libThumb(it, 'w600') }))
    const mine = (await listUploads(SHISEI_HOME)).filter((u) => u.kind in REF_KINDS).map((u) => ({ src: 'mine', id: u.id, kind: u.kind, kindZh: REF_KINDS[u.kind], name: u.name, look: u.look, at: u.at, thumb: mineThumb(u) }))
    const text = scriptText(story) || [job?.title, job?.prompt].filter(Boolean).join(' ')
    const related = rankRelated(text, [...others, ...items], (x) => `${x.name} ${x.group ?? ''} ${x.look ?? ''}`, 12)
      .map((x) => (x.src === 'lib' ? { src: 'lib', cat: x.cat, id: x.id } : { src: 'from', job: x.job, kind: x.kind, id: x.id }))
    return { title: job?.title ?? null, project, others, related, mine, library: { available: lib.available, why: lib.why, online: true, items } }
  }

  return { library, adoptInto, adoptTokens, wardrobeSource, priceOf, otherAssets, externalOptions, refsFor, mineThumb }
}
