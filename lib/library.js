// References from outside a project: the Piying library (read online from the public site, CC0), the user's own
// uploads, and how related an item is to a script. Pure helpers plus small file functions that take their root
// directory as an argument, so tests run on temporary folders. lib/host.js wires them to RPC routes.
import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, readdir, readFile, rename, stat, writeFile } from 'node:fs/promises'
import { Agent, get as httpsGet } from 'node:https'
import { join, resolve, sep } from 'node:path'
import { LIB_ITEM_RE, UPLOAD_ID_RE } from './mentions.js'

// ---------------------------------------------------------------- Piying library
// What the public library ships that a drama can use (<site>/*.json + <media>/images/<dir>/<variant>/<tier>/<file>.webp):
//   scenes.json (empty rooms and streets, eye / above / below)  → a project scene
//   props.json + furniture.json (objects on a plain card)       → a project prop
//   costumes.json (outfits on a grey mannequin)                 → clothes for a character (its description, or the picture itself: lib/refs.js wardrobeSource)
// Poses, animals and lights have no place in a drama project and are left out. Only `rating: general` items are listed.
export const LIB_SETS = [
  { cat: 'scene', file: 'scenes.json', dir: 'scene', views: ['eye', 'above', 'below'] },
  { cat: 'prop', file: 'props.json', dir: 'prop', views: ['front', 'diag', 'side', 'back'] },
  { cat: 'prop', file: 'furniture.json', dir: 'furniture', views: ['front', 'diag', 'side', 'above', 'back'] },
  { cat: 'clothes', file: 'costumes.json', dir: 'costume', views: ['front', 'diag', 'side', 'back'] },
]
export const LIB_TIERS = ['w300', 'w600', 'w900']
const VIEW_FILE = /^[a-z]{3,10}_[A-Z]\d{3}_[a-z]{2,8}\.webp$/u

// Clothes for a character marked look-young: the catalogue has no measure of how much a garment shows, so the rule is
// conservative and fixed. Never sportswear (SP), sleepwear / underwear (IN, LG) or anything not rated general, and
// nothing whose description (English, or the Chinese name) shows bare shoulders, midriff, back, chest or thighs, ends above the
// knee, or is sheer, fitted / tight or swimwear. It leaves out many harmless outfits on purpose.
export const LOOK_YOUNG_NO_CATS = new Set(['SP', 'IN', 'LG'])
export const REVEALING_WORDS = /\b(sleeveless|strapless|off[- ]the[- ]shoulder|off[- ]shoulder|one[- ]shoulder|backless|open[- ]back|halter|tube top|crop(?:ped)? top|bralette|bra\b|bikini|swim\w*|leotard|bodysuit|unitard|slip dress|camisole|spaghetti|low[- ]cut|plunging|deep v|sheer|see[- ]through|mesh|lace|corset|bustier|mini ?skirt|mini ?dress|micro|hot pants|shorts|short skirt|thigh|slit|bare|midriff|cut[- ]?out|skin[- ]tight|bodycon|fishnet|garter|stocking|lingerie|negligee|nightie|tank top|fitted|tight|form[- ]fitting|figure[- ]hugging|body[- ]hugging|hugs?|clingy|v[- ]neck\w*|neckline|above the knee|chest (?:band|wrap)|bandeau)\b/iu
export const REVEALING_ZH = /抹胸|吊带|露|短裤|超短|紧身|泳|比基尼|内衣|睡|镂空|透视|蕾丝|一字肩|热裤/u
export function revealing(item) {
  if (!item || item.rating !== 'general') return true
  if (LOOK_YOUNG_NO_CATS.has(item.catCode)) return true
  return REVEALING_WORDS.test(`${item.look ?? ''} ${item.keep ?? ''} ${item.nameEn ?? ''}`) || REVEALING_ZH.test(String(item.name ?? ''))
}

// ---------------------------------------------------------------- the library online
// The page reads the library from the public Piying site (no local clone, nothing to set): the catalogue JSON from
// LIB_SITE, pictures from LIB_MEDIA. Only these two origins are ever fetched. A picture is written to disk only when it
// goes into a project (copied as a scene / prop, or clothes drawn from the picture): ~/.dsh/shisei/library/, the cache
// shisei.py already accepts. Thumbnails on the page are passed through without saving.
export const LIB_SITE = 'https://shisei.indaloop.app/'
export const LIB_MEDIA = 'https://shisei-media.pages.dev/'
export const MAX_LIB_IMAGE = 6 * 1024 * 1024
// The site's data contract (shisei docs/DESIGN.md, 插件读取的数据): version 1 only grows, so a file without the field
// (sites built before it) is version 1 too. A higher number means this plugin is too old to read it.
export const LIB_SCHEMA = 1
const ONLINE_DIR = /^([a-z]{3,12})\/([a-z]{2,12})$/u

// One agent for these two hosts only. proxyEnv: the user's https_proxy / no_proxy apply to these requests (Node 24+;
// older Node ignores the option) without changing how the rest of dsh connects.
let agent = null
/** GET one address of LIB_SITE / LIB_MEDIA: { status, body: Buffer }, at most maxBytes, no redirects followed. */
export function libGet(url, { timeoutMs = 10000, maxBytes = MAX_LIB_IMAGE } = {}) {
  if (!url.startsWith(LIB_SITE) && !url.startsWith(LIB_MEDIA)) return Promise.reject(new Error('bad library address'))
  agent = agent ?? new Agent({ keepAlive: true, proxyEnv: process.env })
  return new Promise((ok, fail) => {
    const req = httpsGet(url, { agent, timeout: timeoutMs, headers: { accept: '*/*' } }, (res) => {
      const parts = []
      let n = 0
      res.on('data', (c) => { n += c.length; if (n > maxBytes) { req.destroy(new Error('too large')); return } parts.push(c) })
      res.on('end', () => ok({ status: res.statusCode ?? 0, body: Buffer.concat(parts) }))
      res.on('error', fail)
    })
    req.on('timeout', () => req.destroy(new Error('timeout')))
    req.on('error', fail)
  })
}

/** One catalogue file of the site (scenes.json …) → the items the page lists. General items only. */
export function parseOnlineSet(set, data) {
  const m = ONLINE_DIR.exec(String(data?.dir ?? ''))
  if (!m || m[1] !== set.dir) return []
  const variant = m[2]
  const items = []
  for (const it of Array.isArray(data?.items) ? data.items : []) {
    if (it?.r !== 'general' || !LIB_ITEM_RE.test(String(it.id))) continue
    const files = {}
    for (const f of Array.isArray(it.v) ? it.v : []) {
      if (!VIEW_FILE.test(String(f))) continue
      const view = set.views.find((v) => String(f).endsWith(`_${v}.webp`))
      if (view && !files[view]) files[view] = f
    }
    const views = set.views.filter((v) => files[v])
    if (!views.length) continue
    const n = it.n ?? {}
    const item = { cat: set.cat, set: set.dir, id: it.id, name: String(n.zh ?? n.en ?? it.id).slice(0, 40), nameEn: String(n.en ?? '').slice(0, 60),
      group: String(it.sub?.zh ?? '').slice(0, 20), look: String(it.desc ?? '').slice(0, 600), keep: String(it.clause ?? '').replace(/^wearing\s+/iu, '').slice(0, 300),
      catCode: String(it.cat ?? ''), gender: it.g ?? null, rating: it.r, views, files, variant }
    if (set.cat === 'clothes') item.revealing = revealing(item)
    items.push(item)
  }
  return items
}

/** The whole catalogue from the site. Offline or a bad answer → { available: false, why: 'offline' }; a file in a
 *  newer format than this plugin reads → why: 'newer' (upgrade the plugin, the network is fine). */
export async function loadOnlineLibrary(get = libGet) {
  const items = []
  try {
    for (const set of LIB_SETS) {
      const r = await get(`${LIB_SITE}${set.file}`, { maxBytes: 8 * 1024 * 1024 })
      if (r.status !== 200) throw new Error(`${set.file}: HTTP ${r.status}`)
      const data = JSON.parse(r.body.toString('utf8'))
      const schema = data?.schema ?? 1
      if (!Number.isInteger(schema) || schema > LIB_SCHEMA) return { available: false, why: 'newer', schema, items: [], online: true }
      items.push(...parseOnlineSet(set, data))
    }
  } catch (e) {
    return { available: false, why: 'offline', error: e instanceof Error ? e.message : String(e), items: [], online: true }
  }
  return { available: items.length > 0, why: items.length ? null : 'empty', items, online: true }
}

/** Where one view of one item lives online, found through the catalogue (callers never pass a file name or URL). */
export function libImageUrl(lib, cat, id, view, tier = 'w600') {
  const it = lib.items.find((x) => x.cat === cat && x.id === id)
  if (!it) throw new Error('这个素材不在素材库里（或不适合所有人）')
  const v = view && it.views.includes(view) ? view : it.views[0]
  if (!LIB_TIERS.includes(tier)) throw new Error('bad tier')
  const file = it.files[v]
  if (!VIEW_FILE.test(file) || !/^[a-z]{3,12}$/u.test(it.set) || !/^[a-z]{2,12}$/u.test(it.variant)) throw new Error('bad library file')
  return { url: `${LIB_MEDIA}images/${it.set}/${it.variant}/${tier}/${file}`, file, item: it, view: v, tier }
}

/** Fetch a library picture: a WebP of at most MAX_LIB_IMAGE bytes, or an error. */
export async function fetchLibImage(url, get = libGet) {
  if (!url.startsWith(LIB_MEDIA)) throw new Error('bad library address')
  let r
  try { r = await get(url, { timeoutMs: 20000, maxBytes: MAX_LIB_IMAGE }) } catch (e) { throw new Error(/too large/u.test(String(e?.message)) ? '素材库的图太大' : '连不上皮影素材库：看一下网络') }
  if (r.status !== 200) throw new Error(`素材库的图取不到（HTTP ${r.status}）`)
  const buf = r.body
  if (sniffImage(buf.subarray(0, 16)) !== 'webp') throw new Error('素材库给的不是图片')
  return buf
}

/** A library picture on disk under cacheDir (downloaded once), for shisei.py to copy into a project. */
export async function cacheLibImage(cacheDir, lib, cat, id, view, tier, get = libGet) {
  const { url, file, item, view: v } = libImageUrl(lib, cat, id, view, tier)
  const path = join(cacheDir, `${item.set}_${item.variant}_${tier}_${file}`)
  if (!existsSync(path)) {
    const buf = await fetchLibImage(url, get)
    await mkdir(cacheDir, { recursive: true })
    const tmp = `${path}.${randomBytes(3).toString('hex')}.tmp`
    await writeFile(tmp, buf)
    await rename(tmp, path)
  }
  return { path, item, view: v }
}

// ---------------------------------------------------------------- my uploads (~/.dsh/shisei/uploads)
export const REF_KINDS = { scene: '场景', prop: '道具', clothes: '衣服' }
export const MAX_REF_BYTES = 15 * 1024 * 1024
export const REF_EXT = { png: 'png', jpeg: 'jpg', webp: 'webp' }

/** What a picture really is, from its first bytes (never from its name): 'png' | 'jpeg' | 'webp' | null. */
export function sniffImage(buf) {
  const b = buf instanceof Uint8Array ? buf : new Uint8Array(buf ?? [])
  if (b.length >= 8 && [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((x, i) => b[i] === x)) return 'png'
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'jpeg'
  if (b.length >= 12 && String.fromCharCode(...b.slice(0, 4)) === 'RIFF' && String.fromCharCode(...b.slice(8, 12)) === 'WEBP') return 'webp'
  return null
}

export function newUploadId(now = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `u${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}-${randomBytes(2).toString('hex')}`
}

const yes = (v) => v === true || v === '1' || v === 'true'

/**
 * The confirmation that goes with every outside picture (the page's Import dialog, checked again here):
 * what it is (a scene, a prop or clothes) and 「我有权使用这张图」. A name and one line describing it (used when it is
 * redrawn from text). Whether the picture shows a real person is not checked here: clearing likeness rights is the
 * user's job, the same way the copyright tick is theirs to give.
 */
export function uploadMeta(q) {
  const kind = String(q.kind ?? '')
  if (!Object.hasOwn(REF_KINDS, kind)) throw new Error('先选它是什么：场景、道具或衣服')
  if (!yes(q.rights)) throw new Error('要先确认：你有权使用这张图')
  const name = String(q.name ?? '').replace(/\s+/gu, ' ').trim().slice(0, 40)
  if (!name) throw new Error('给它起个名字')
  const look = String(q.look ?? '').replace(/\s+/gu, ' ').trim().slice(0, 300)
  if (!look) throw new Error('写一句它是什么样的（重画时按这句画）')
  return { kind, name, look, confirmed: { rights: true } }
}

export const uploadsDir = (home) => join(home, 'uploads')

/** A file in the uploads folder by id; refuses anything else. */
export function uploadPath(home, id, ext) {
  if (!UPLOAD_ID_RE.test(String(id))) throw new Error('bad upload id')
  if (ext !== 'json' && !Object.values(REF_EXT).includes(ext) && ext !== 'png') throw new Error('bad upload type')
  return join(uploadsDir(home), `${id}.${ext}`)
}

/** Store one confirmed picture: <id>.<ext> plus <id>.json with what was confirmed, when, and the picture's checksum. */
export async function saveUpload(home, buf, meta, { now = new Date(), sha256 } = {}) {
  if (!buf || buf.byteLength === 0) throw new Error('空文件')
  if (buf.byteLength > MAX_REF_BYTES) throw new Error(`图太大了：最多 ${MAX_REF_BYTES / 1024 / 1024} MB`)
  const type = sniffImage(buf.subarray(0, 16))
  if (!type) throw new Error('这不是图片（只收 PNG、JPEG、WebP）')
  const id = newUploadId(now)
  await mkdir(uploadsDir(home), { recursive: true })
  const file = uploadPath(home, id, REF_EXT[type])
  await writeFile(`${file}.part`, buf)
  await rename(`${file}.part`, file)
  const rec = { id, ...meta, type, ext: REF_EXT[type], bytes: buf.byteLength, sha256: sha256 ?? null, at: now.toISOString() }
  await writeFile(uploadPath(home, id, 'json'), JSON.stringify(rec, null, 1))
  return rec
}

export async function readUpload(home, id) {
  const rec = JSON.parse(await readFile(uploadPath(home, id, 'json'), 'utf8'))
  return { ...rec, file: uploadPath(home, id, rec.ext) }
}

export async function listUploads(home) {
  let names = []
  try { names = await readdir(uploadsDir(home)) } catch { return [] }
  const out = []
  for (const n of names.filter((x) => x.endsWith('.json')).sort().reverse().slice(0, 200)) {
    const id = n.slice(0, -5)
    if (!UPLOAD_ID_RE.test(id)) continue
    try { out.push(await readUpload(home, id)) } catch { /* half-written */ }
  }
  return out
}

/** A path under `root` (no traversal out of it): for caches the host writes under ~/.dsh/shisei. */
export function under(root, ...parts) {
  const base = resolve(root)
  const p = resolve(base, ...parts)
  if (p !== base && !p.startsWith(base + sep)) throw new Error('path leaves the Piying folder')
  return p
}

export async function fileSize(p) { try { return (await stat(p)).size } catch { return null } }

// ---------------------------------------------------------------- 「和《项目名》有关」
/** The words of a text: Chinese character pairs and latin words of 3+ letters (no dictionary, no dependencies). */
export function grams(text) {
  const out = new Set()
  const s = String(text ?? '').toLowerCase()
  for (const run of s.match(/[一-鿿]+/gu) ?? []) {
    if (run.length === 1) out.add(run)
    for (let i = 0; i + 1 < run.length; i++) out.add(run.slice(i, i + 2))
  }
  for (const w of s.match(/[a-z]{3,}/gu) ?? []) if (!STOP.has(w)) out.add(w)
  return out
}
const STOP = new Set(['the', 'and', 'with', 'for', 'her', 'his', 'one', 'two', 'from', 'into', 'that', 'this', 'shot', 'medium', 'close', 'wide', 'plain', 'small', 'large'])

/** What the script is about: prose, shot descriptions, and the names and looks of its scenes and props. */
export function scriptText(story) {
  if (!story) return ''
  return [story.title, story.script, ...(story.shots ?? []).flatMap((s) => [s.keyframe, s.action, s.line?.text]),
    ...(story.scenes ?? []).flatMap((x) => [x.name, x.look]), ...(story.props ?? []).flatMap((x) => [x.name, x.look]),
    ...(story.cast ?? []).flatMap((x) => [x.look, x.outfit])].filter(Boolean).join(' ')
}

/** Items ordered by how many words they share with the script (ties keep their order); items sharing none are dropped. */
export function rankRelated(text, items, textOf, limit = 12) {
  const g = grams(text)
  if (!g.size) return []
  return items.map((it, i) => {
    let n = 0
    for (const w of grams(textOf(it))) if (g.has(w)) n += w.length > 2 ? 2 : 1
    return { it, n, i }
  }).filter((x) => x.n > 0).sort((a, b) => b.n - a.n || a.i - b.i).slice(0, limit).map((x) => x.it)
}
