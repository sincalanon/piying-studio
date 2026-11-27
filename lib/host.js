// Host half of dsh-shisei: GUI RPC routes and the compshare key bridge.
// No peer imports: everything comes from ctx services, so a linked or packed install loads the same way.
import { spawn } from 'node:child_process'
import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { createReadStream, createWriteStream, existsSync, readFileSync } from 'node:fs'
import { copyFile, mkdir, open, readdir, readFile, rename, rm, stat, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { homedir } from 'node:os'
import { basename, dirname, extname, join } from 'node:path'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { fileURLToPath } from 'node:url'
import { assetIndex, costSummary } from './assets.js'
import { fetchLibImage, libImageUrl, MAX_REF_BYTES, readUpload, saveUpload, sniffImage, uploadMeta } from './library.js'
import { assetInfo, BIND, describeMentions, externalMentions, LIMITS, mentionNote, NOTE_MARK, resolveMentions, SEEDS, storyUnits,
  takeRefs, unitRefs } from './mentions.js'
import { cropArgv, directorError, frameError, pageError, stagingArgv, wardrobeFile } from './frame.js'
import { assumeMissing, makeRefs, refSource } from './refs.js'
import { createAsks } from './asks.js'
import { createUpdates } from './update.js'
import { collectReply, openaiToParts, pickVisionSource, verifiedKey, visionPref } from './vision.js'
import { prepare as prepareTools, runPnpm } from './tools.js'

// The mention grammar and checks live in lib/mentions.js, library and uploads in lib/library.js; re-exported for callers and tests.
export { assetInfo, describeMentions, externalMentions, LIMITS, MENTION_RE, mentionNote, NOTE_MARK, parseExternal, parseMentions, resolveMentions,
  rewriteMentions, storyUnits, takeRefs, unitRefs } from './mentions.js'
export { assumeMissing, refSource } from './refs.js'
export { cropArgv, cropBox, directorError, frameError, pageError, stagingArgv, wardrobeFile } from './frame.js'
export { cacheLibImage, fetchLibImage, grams, libImageUrl, LIB_SCHEMA, loadOnlineLibrary, parseOnlineSet, MAX_REF_BYTES, rankRelated, revealing, saveUpload, scriptText, sniffImage, under, uploadMeta } from './library.js'

export const SCRIPT = fileURLToPath(new URL('../skills/shisei-motion/scripts/shisei.py', import.meta.url))
export const KEY_REFS = { public: 'COMPSHARE_PUBLIC_KEY', private: 'COMPSHARE_PRIVATE_KEY' }
export const RPC_CHANNEL = '/api'
export const RPC_PREFIX = 'dsh-shisei'
const SHISEI_HOME = process.env.SHISEI_HOME ?? join(homedir(), '.dsh', 'shisei')
const ASSET_ID = /^[a-z][a-z0-9_]{0,23}$/u

/** Subcommands the GUI may run, with the flags it is allowed to add. */
export const GUI_COMMANDS = {
  'doctor': [], 'status': [], 'install-cli': [], 'secure': [],
  'ssh-keygen': [],         // the plugin's own key pair when this computer has none (never touches ~/.ssh)
  'ffmpeg-check': [],       // where ffmpeg is and how this computer can get it (no install)
  'ffmpeg-install': [],     // Homebrew / winget install, started by the user's click (shisei.py refuses it in a model shell)
  'instances': ['--pick'],   // read-only list of the account's instances; --pick remembers the only suitable one
  'estimate': ['--seeds', '--frames', '--keep-bg'],
  'up-plan': [],            // `up` without --confirm-billing: returns the plan, never starts
  'up': ['--confirm-billing'],
  'down': ['--yes'],
  'config': [],
}
export const CONFIG_KEYS = ['instance_id', 'ssh_key', 'idle_stop_minutes']

/** A dance clip length shisei.py accepts: the H3 grid 17n+5, 2..15 s at 24 fps. */
export const isSwapFrames = (f) => Number.isInteger(f) && f >= 39 && f <= 360 && (f - 5) % 17 === 0

/** Turn a GUI request into shisei.py argv. Throws on anything not explicitly allowed. */
export function guiArgv(command, options = {}) {
  if (!(command in GUI_COMMANDS)) throw new Error(typeof command === 'string' ? `command not allowed: ${command.slice(0, 40)}` : 'no command given')
  if (command === 'up-plan') return ['up']
  if (command === 'up') {
    if (options.confirmBilling !== true) throw new Error('up needs confirmBilling: true (the user confirmed the price)')
    return ['up', '--confirm-billing']
  }
  if (command === 'down') {
    if (options.confirm !== true) throw new Error('down needs confirm: true')
    // from a project's page: stop the machine that project used (its lease), not just the preferred one
    if (options.project !== undefined) {
      if (!JOB_ID.test(String(options.project))) throw new Error('bad project id')
      return ['--holder', String(options.project), 'down', '--yes']
    }
    return ['down', '--yes']
  }
  if (command === 'estimate') {
    const seeds = Number(options.seeds ?? 3)
    if (!Number.isInteger(seeds) || seeds < 1 || seeds > 6) throw new Error('seeds must be 1..6')
    const frames = options.frames === undefined ? null : Number(options.frames)
    // a whole dance (整段换) is any length over one generation, cut into shots by shisei.py
    const whole = Number.isInteger(frames) && frames > 360 && frames <= LONG_MAX_SECONDS * 24
    if (frames !== null && !isSwapFrames(frames) && !whole) throw new Error('frames must be 17n+5, 39..360, or a whole dance up to 300 s')
    return ['estimate', '--seeds', String(seeds), ...(frames !== null ? ['--frames', String(frames)] : []), ...(options.running === true ? ['--running'] : []),
      ...(options.keepBg === true ? ['--keep-bg'] : [])]
  }
  if (command === 'instances') return options.pick === true ? ['instances', '--pick'] : ['instances']
  if (command === 'config') {
    if (options.key === undefined) return ['config', 'show']
    if (!CONFIG_KEYS.includes(options.key)) throw new Error(`config key not allowed: ${options.key}`)
    const value = String(options.value ?? '')
    if (!/^[\w./~@-]{1,200}$/u.test(value)) throw new Error('config value has unsupported characters')
    return ['config', 'set', options.key, value]
  }
  return [command]
}

/** compshare argv the bridge forwards. Deleting or releasing anything is refused here, whatever the caller says. */
export function bridgeArgs(args) {
  if (!Array.isArray(args) || args.length === 0 || args.some((a) => typeof a !== 'string' || a.length > 300)) {
    throw new Error('args must be a non-empty string array')
  }
  if (args.some((a) => /^(delete|release|--release-disk|terminate)$/iu.test(a))) throw new Error('delete/release is not allowed through the bridge')
  if (!['instance', 'image', 'doctor'].includes(args.find((a) => !a.startsWith('-')))) throw new Error('only instance/image/doctor commands are bridged')
  return args
}

export function compshareBin(env = process.env, platform = process.platform, exists = existsSync) {
  const venv = platform === 'win32' ? join(SHISEI_HOME, 'venv', 'Scripts', 'compshare.exe') : join(SHISEI_HOME, 'venv', 'bin', 'compshare')
  for (const c of [env.SHISEI_COMPSHARE, venv]) if (c && exists(c)) return c
  return 'compshare'
}

/**
 * The Python 3.12 dsh Desktop carries (its "primary runtime", what dsh's own Office skills run on): installed under the
 * dsh home on first use, and shipped next to the Node dsh runs on. A 64-bit Python the user never installed, so a
 * new user needs no Python of their own. Only one whose runtime.json names this platform and architecture, and a
 * Python 3.9+, is used; null when there is none (dsh from npm, or a layout this plugin does not know).
 */
export function dshPython(env = process.env, platform = process.platform, { arch = process.arch, execPath = process.execPath, home = homedir(), exists = existsSync, read = (f) => readFileSync(f, 'utf8') } = {}) {
  const roots = [
    env.DSH_PRIMARY_RUNTIME || null,
    join(env.DSH_HOME || join(home, '.dsh'), 'dsh-runtimes', 'dsh-primary-runtime'),
    join(dirname(execPath), '..', 'primary-runtime'),
  ].filter(Boolean)
  for (const root of roots) {
    try {
      const m = JSON.parse(read(join(root, 'runtime.json')))
      const [maj, min] = String(m.python ?? '').split('.').map(Number)
      if (m.platform !== platform || m.arch !== arch || !(maj === 3 && min >= 9)) continue
      const py = platform === 'win32' ? join(root, 'dependencies', 'python', 'python.exe') : join(root, 'dependencies', 'python', 'bin', 'python3')
      if (exists(py)) return py
    } catch { /* no runtime here */ }
  }
  return null
}

/**
 * How to start Python 3: SHISEI_PYTHON when set (a path, or a command and its args); else the Python dsh Desktop
 * carries (dshPython); else the system's: `python3` on macOS / Linux, on Windows the `py -3` launcher the python.org
 * installer puts in (a bare `python` there may be the Microsoft Store stub).
 */
export function pythonCommand(env = process.env, platform = process.platform, opts = {}) {
  if (env.SHISEI_PYTHON) { const [cmd, ...args] = env.SHISEI_PYTHON.split(' ').filter(Boolean); return { cmd, args } }
  const own = dshPython(env, platform, opts)
  if (own) return { cmd: own, args: [] }
  return platform === 'win32' ? { cmd: 'py', args: ['-3'] } : { cmd: 'python3', args: [] }
}
const PY = pythonCommand()
// -X utf8: files, pipes and the console in UTF-8 whatever the system code page (GBK on Chinese Windows); an
// environment variable would not do, as -I makes Python ignore PYTHON* variables
export const PY_UTF8 = ['-X', 'utf8']
// windowsHide: on Windows a detached run (machine setup, a shoot) otherwise opens an empty console window, and closing it
// kills the run halfway (seen 2026-11-13); every other platform ignores it
const spawnPy = (args, opts) => spawn(PY.cmd, [...PY.args, ...PY_UTF8, ...args], { windowsHide: true, ...opts })

function pidAlive(pid) {
  try { process.kill(pid, 0); return true } catch { return false }
}

// One billed detached run per project, and one machine setup, started from this dsh. The key is taken synchronously
// before the price check, so a second click during it, or before the run has written its progress.json, is refused
// instead of starting a second billed run; it stays taken until the child exits.
const launched = new Map()
export function claimLaunch(key, alive = pidAlive) {
  if (launchHeld(key, alive)) return false
  launched.set(key, 'starting')
  return true
}
/** A start under `key` is being priced, or its run is alive. */
export function launchHeld(key, alive = pidAlive) {
  const v = launched.get(key)
  return v === 'starting' || (typeof v === 'number' && alive(v))
}
export function settleLaunch(key, pid) {
  if (pid) launched.set(key, pid)
  else launched.delete(key)
}
async function launchOnce(key, busy, fn) {
  if (!claimLaunch(key)) throw new Error(busy)
  let pid = null
  try {
    const r = await fn()
    pid = r?.pid ?? null
    // freed when the child exits too, so a reused pid never blocks the project until dsh restarts
    r?.child?.once?.('exit', () => { if (launched.get(key) === pid) launched.delete(key) })
    return r?.child ? { ...r, child: undefined } : r
  } finally {
    settleLaunch(key, pid)
  }
}

/** A run's progress.json says it is still going (its process is alive). */
async function runningNow(file) {
  try {
    const p = JSON.parse(await readFile(file, 'utf8'))
    return p?.state === 'running' && !!p.pid && pidAlive(p.pid)
  } catch { return false }
}

/** A compshare zone name from the page (cn-sh2-03). */
/** A compshare instance id from the page, else throws (never starts with '-': it goes into argv). */
export function instanceArg(id) {
  const v = String(id ?? '')
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$/u.test(v)) throw new Error('bad instance id')
  return v
}

/** How long a queued run waits for a machine before it gives up (nothing started, nothing billed). */
export const QUEUE_MINUTES = 90

/**
 * Where a run started from the page goes: `machine` (the one its quote was for: a global --machine before the
 * subcommand), or `queue` (every machine busy: wait for whichever comes free, starting only at the confirmed total
 * `maxCost`). The same `pre` goes in front of the estimate the host checks first, so both price one machine.
 */
export function runPlacement(payload = {}) {
  if (payload.queue === true) {
    const cost = Number(payload.maxCost)
    if (!Number.isFinite(cost) || cost <= 0 || cost > 1000) throw new Error('a queued run needs the confirmed total (maxCost)')
    return { pre: [], post: ['--queue-minutes', String(QUEUE_MINUTES), '--max-cost', cost.toFixed(2)], queue: true, machine: null }
  }
  if (payload.machine === undefined || payload.machine === null) return { pre: [], post: [], queue: false, machine: null }
  const machine = instanceArg(payload.machine)
  return { pre: ['--machine', machine], post: [], queue: false, machine }
}

/** The machine a start was quoted for was taken by another project since (a manual hold gives way): quote again. */
export function machineTaken(leases, machine, id, nowMs) {
  if (!machine) return null
  const l = leases.find((x) => x && x.instance === machine)
  const held = l && l.state === 'active' && Number(l.until) * 1000 > nowMs && l.holder !== id && !['cli', 'page'].includes(l.holder)
  return held ? '这台机器刚被别的项目拿走了：什么也没开、没花钱。关掉这个确认框重新报价，或者排队。' : null // zh-ok
}

export function zoneArg(z) {
  const s = String(z ?? '')
  if (!/^[a-z]{2}-[a-z0-9]{2,8}-\d{2}$/u.test(s)) throw new Error('bad zone')
  return s
}

/** The fields of onboarding.json the page may set (no keys, no prices). */
export function onboardingPatch(set) {
  const out = {}
  if (Number.isInteger(set.step) && set.step >= 0 && set.step <= 5) out.step = set.step
  if (typeof set.done === 'boolean') out.done = set.done
  if (typeof set.watch === 'boolean') out.watch = set.watch
  return out
}

/** A run's error for the page; "machine X is in use by <project>" names that project by its title when it is one of ours. */
async function busyText(s) {
  return (typeof s === 'string' && await leaseRefusal(s)) || pageError(s)
}

/** The machine setup's progress.json, with a dead run said as failed (dsh closed, computer asleep). */
/** How long machine-create is watched for a refusal that comes before any progress (the stock check is one call). */
const SETUP_REFUSAL_MS = 15000

/** The error machine-create printed to setup/run.log after `from` (its last JSON line), for the page. */
export async function setupRefusal(file, from = 0) {
  let text = ''
  try { text = (await readFile(file, 'utf8')).slice(from) } catch { /* nothing written */ }
  for (const line of text.trim().split('\n').reverse()) {
    try {
      const r = JSON.parse(line)
      if (r && r.ok === false && typeof r.error === 'string') return r.error
    } catch { /* not a JSON line */ }
  }
  // no JSON answer (it crashed): what it printed last, so the page shows a reason instead of "try later"
  const tail = text.trim().split(/\r?\n/u).filter((l) => l.trim()).slice(-3).join(' | ').slice(-400)
  return tail ? `machine setup stopped: ${tail}` : '机器没有建成，也没有扣费：稍后再试一次。'
}

/** What a ledger's lines cost, rounded to fen. */
export function ledgerCost(ledger) {
  return Math.round((Array.isArray(ledger) ? ledger : []).reduce((a, e) => a + (Number(e?.cost) || 0), 0) * 100) / 100
}

async function setupProgress() {
  let p = null
  try { p = JSON.parse(await readFile(join(SHISEI_HOME, 'setup', 'progress.json'), 'utf8')) } catch { return null }
  if (p.state === 'running' && p.pid && !pidAlive(p.pid)) p = { ...p, state: 'failed', error: p.error ?? '准备机器的进程意外退出（dsh 被关掉或电脑休眠）。点继续会接着用这台机器。' }
  if (typeof p.error === 'string') p = { ...p, error: pageError(p.error) }
  return p
}

async function keyEnv(credentials) {
  const env = {}
  for (const ref of [...Object.values(KEY_REFS), await visionKeyName()]) {
    const hit = await credentials.resolve(ref).catch(() => null)
    if (hit?.value) env[ref] = hit.value
    else if (process.env[ref]) env[ref] = process.env[ref]
  }
  // the image model is asked through the bridge, which picks dsh's own model or the separate one (lib/vision.js)
  const vs = visionUrl ? await visionState(!!env[await visionKeyName()]) : null
  if (vs?.source) {
    env.SHISEI_VISION_URL = visionUrl
    env.SHISEI_VISION_MODEL = vs.model
  }
  return env
}

// ---------------------------------------------------------------- the image model (lib/vision.js)
let visionUrl = null     // the bridge's /vision, once the bridge is up
let attachments = null   // dsh's attachment store: images reach dsh's model as stored attachments
export function setAttachments(a) { attachments = a ?? null }

async function shiseiConfig() {
  try { return JSON.parse(await readFile(join(SHISEI_HOME, 'config.json'), 'utf8')) } catch { return {} }
}

/** dsh's default model, whether it says it reads images, and whether it passed the fixed test picture here. */
async function directorModel() {
  const sel = defaultModel()
  if (!sel) return null
  let image = null
  try { const info = await modelServices.llm.resolveModelInfo(sel.provider, sel.model); image = Array.isArray(info?.inputModalities) ? info.inputModalities.includes('image') : null } catch { /* unknown */ }
  const v = (await readVerified())[verifiedKey(sel)]
  return { ...sel, image, ...(typeof v?.ok === 'boolean' ? { verified: v.ok } : {}) }
}

// the fixed test picture's result for dsh's models ({ "provider/model": { ok, at } }), written by vision-test
const VERIFIED_FILE = () => join(SHISEI_HOME, 'vision-verified.json')
async function readVerified() {
  try { return JSON.parse(await readFile(VERIFIED_FILE(), 'utf8')) ?? {} } catch { return {} }
}
async function recordVerified(d, ok) {
  const all = await readVerified()
  all[verifiedKey(d)] = { ok: !!ok, at: Date.now() }
  await mkdir(SHISEI_HOME, { recursive: true })
  await writeFile(VERIFIED_FILE(), JSON.stringify(all))
}

/** Which image model answers now: { pref, source: 'dsh'|'key'|null, model, director }. */
async function visionState(hasKey) {
  const cfg = await shiseiConfig()
  const director = await directorModel()
  const canStream = typeof modelServices?.llm?.stream === 'function' && typeof attachments?.saveImages === 'function'
  const pref = visionPref(cfg)
  const source = pickVisionSource({ pref, director, hasKey, canStream })
  const model = source === 'dsh' ? director.model : source === 'key' ? String(cfg.vision_model ?? 'ling-3.2-flash') : null
  return { pref, source, model, director, canStream }
}

/** One picture question to dsh's default model; returns an OpenAI-style reply for shisei.py. */
async function askDshModel(body, signal) {
  const sel = defaultModel()
  if (!sel) throw new Error('dsh has no default model')
  const parts = openaiToParts(body.messages)
  const images = parts.filter((p) => p.type === 'image')
  const refs = images.length ? await attachments.saveImages(images.map(({ data, mediaType }) => ({ data, mediaType }))) : []
  let i = 0
  const content = parts.map((p) => (p.type === 'image' ? { type: 'image', attachment: refs[i++] } : p))
  // reasoning models count their thinking against max_tokens: never fewer than 8000
  const maxTokens = Math.max(8000, Math.min(32000, Number(body.max_tokens) || 0))
  return collectReply(modelServices.llm.stream({ provider: sel.provider, model: sel.model, messages: [{ role: 'user', content }], maxTokens, signal }), sel.model)
}

/** Image model endpoint from the user's shisei config (never from a caller), https only. */
async function visionBaseUrl() {
  try {
    const u = JSON.parse(await readFile(join(SHISEI_HOME, 'config.json'), 'utf8')).vision_base_url
    if (typeof u === 'string' && /^https:\/\//u.test(u)) return u.replace(/\/+$/u, '')
  } catch { /* defaults */ }
  return 'https://maas-api.antdigital.com/v1'
}

/** Name of the env variable holding the image model's key (shisei config `vision_key_env`). */
async function visionKeyName() {
  try {
    const name = JSON.parse(await readFile(join(SHISEI_HOME, 'config.json'), 'utf8')).vision_key_env
    if (typeof name === 'string' && /^[A-Z][A-Z0-9_]{2,60}$/u.test(name)) return name
  } catch { /* defaults */ }
  return 'ANTDIGI_API_KEY'
}

/**
 * Environment for shisei.py runs the page starts. DSH_SHISEI_BRIDGE marks a dsh model shell, where shisei.py refuses to
 * approve pictures or pick takes; the page's own runs are the user's clicks, so they never carry it.
 */
export function pageEnv(extra = {}, base = process.env) {
  // SHISEI_NODE: the Node dsh runs on, for the plugin's own ssh (lib/ssh.cjs), so no OpenSSH is needed
  const env = { SHISEI_NODE: process.execPath, ...base, ...extra, SHISEI_HOLDER: 'page' }   // a machine the page turns on is a manual hold: a project's run may take it over
  delete env.DSH_SHISEI_BRIDGE
  return env
}

function run(cmd, args, env, timeoutMs, signal) {
  return new Promise((resolve) => {
    // PYTHONUTF8: the compshare CLI is a Python program; on Chinese Windows it would otherwise answer in GBK
    const child = spawn(cmd, args, { env: { ...process.env, PYTHONUTF8: '1', ...env }, signal, timeout: timeoutMs, windowsHide: true })
    child.stdout.setEncoding('utf8')   // a character split across two chunks stays whole
    child.stderr.setEncoding('utf8')
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (d) => { stdout += d })
    child.stderr.on('data', (d) => { stderr = (stderr + d).slice(-20000) })
    child.on('error', (e) => resolve({ code: -1, stdout, stderr: String(e) }))
    child.on('close', (code) => resolve({ code, stdout, stderr }))
  })
}

/** Long GUI runs (up, down, install) report progress through a job record the page polls. */
export class Jobs {
  constructor() { this.jobs = new Map(); this.seq = 0 }
  start(label, promiseFactory) {
    const id = `j${++this.seq}`
    const job = { id, label, state: 'running', startedAt: Date.now(), log: '', result: undefined }
    this.jobs.set(id, job)
    // a thrown error ends the job as failed (otherwise the page would show it running forever)
    Promise.resolve().then(() => promiseFactory((chunk) => { job.log = (job.log + chunk).slice(-4000) })).then((r) => {
      job.state = r?.ok ? 'done' : 'failed'
      job.result = r
    }, (e) => {
      job.state = 'failed'
      job.result = { ok: false, error: String(e?.message ?? e) }
    })
    // keep the last 20 finished ones; a job still running is never dropped (its page is polling it)
    const done = [...this.jobs.values()].filter((j) => j.state !== 'running' && j !== job)
    for (const old of done.slice(0, Math.max(0, this.jobs.size - 20))) this.jobs.delete(old.id)
    return job
  }
  get(id) { return this.jobs.get(id) }
  running() { return [...this.jobs.values()].filter((j) => j.state === 'running') }
}

// Python's bytecode cache in a folder of ours: the system Python cannot write next to its own library, so without this
// every shisei.py call re-compiles ~50 standard modules (about half of a drama-status call). -X works under -I.
const PY_CACHE = ['-X', `pycache_prefix=${join(SHISEI_HOME, 'pycache')}`]
/**
 * Why shisei.py gave no JSON, for the page: Python missing (spawn ENOENT), too old to parse shisei.py (a SyntaxError
 * before any output), or the py launcher with no Python 3 behind it all become 'python unusable: …'; anything else
 * keeps the tail of what it printed, stderr included, instead of a bare 'no output'.
 */
export function shiseiFailure({ spawnError = null, stdout = '', stderr = '', code = null, cmd = PY.cmd } = {}) {
  if (spawnError) {
    return spawnError.code === 'ENOENT' ? `python unusable: ${cmd} not found` : String(spawnError)
  }
  const tail = (stdout.trim() || stderr.trim()).slice(-500)
  if (!stdout.trim() && /SyntaxError|No suitable Python runtime|Python was not found|requires Python 3/u.test(stderr)) {
    return `python unusable: ${tail}`
  }
  return tail || `no output (exit ${code})`
}

function runShisei(argv, env, onLog) {
  return new Promise((resolve) => {
    const child = spawnPy(['-I', ...PY_CACHE, SCRIPT, ...argv], { env: pageEnv(env) })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (d) => { stdout += d })
    child.stderr.on('data', (d) => { stderr = (stderr + d).slice(-4000); onLog?.(String(d)) })
    child.on('error', (e) => resolve({ ok: false, error: shiseiFailure({ spawnError: e }) }))
    child.on('close', (code) => {
      try { resolve(JSON.parse(stdout)) } catch { resolve({ ok: false, error: shiseiFailure({ stdout, stderr, code }) }) }
    })
  })
}

// Ready what this computer needs, quietly and once per dsh run (lib/tools.js): compshare-cli from the bundled wheels,
// then ffmpeg from the plugin's own npm package when this computer has none. The key check waits for compshare-cli
// only; the page shows the state only when something failed. A failed run is tried again on the next call.
let preparing = null
let cliReady = null
// the machine patrol asks to run again once compshare-cli is ready (startPatrol): a patrol that ran before it was
// installed (a new computer, or a venv rebuilt for dsh Desktop's Python) showed 看不到机器状态 for minutes (seen 2026-11-13)
let afterCli = null
let toolState = null
let toolProfile = null
export function prepareComputer({ run = runShisei, pnpm } = {}) {
  if (!preparing) {
    let doneCli
    cliReady = new Promise((ok) => { doneCli = ok })
    const viaPnpm = pnpm !== undefined ? pnpm : toolProfile ? (args) => runPnpm(toolProfile, args) : null
    preparing = prepareTools({
      run: (argv) => run(argv, {}),
      pnpm: viaPnpm,
      home: SHISEI_HOME,
      onState: (st) => {
        toolState = st
        if (st.cli.state !== 'preparing') {
          doneCli(st.cli)
          if (st.cli.state === 'ready') afterCli?.()
        }
      },
    }).then((st) => {
      if (st.cli.state === 'failed' || st.ffmpeg.state === 'failed') preparing = null
      return st
    }, (e) => {
      preparing = null
      toolState = { cli: { state: 'failed', error: String(e) }, ffmpeg: { state: 'failed', error: String(e) } }
      doneCli(toolState.cli)
      return toolState
    })
  }
  return { cli: cliReady, all: preparing }
}
export const toolsState = () => toolState

/** One answer of the read-only account check (shisei.py instances) in the page's words: ok (with the machine count),
 * auth (the platform refused the key pair's signature), transport (no answer: retry, the keys may be fine), other. */
export function keyCheckResult(r) {
  if (r && r.ok) return { state: 'ok', count: (r.instances ?? []).length }
  const kind = r?.error_kind
  const detail = String(r?.error ?? '').slice(0, 600)
  return { state: kind === 'auth' ? 'auth' : kind === 'transport' ? 'transport' : 'other', detail: /^python unusable/u.test(detail) ? pageError(detail) : detail }
}

// the latest keys-check (the page asks after saving keys, on the settings page and on 开始): the sidebar's 还差 N 步
// reads it, so saved keys the platform refuses are not counted as connected
let lastKeyCheck = null

async function keyState(credentials) {
  const out = {}
  for (const [k, ref] of Object.entries(KEY_REFS)) {
    const d = await credentials.describe(ref)
    out[k] = { configured: !!d.configured, source: d.source ?? null, writable: d.writable !== false }
  }
  return out
}

// dsh's host session service (optional: absent in some profiles). The drama director panel talks to a real dsh session.
let sessionService = null
export function setSessionService(svc) { sessionService = svc ?? null }

// The director's questions and permission asks, answered on the Shisei page (lib/asks.js). index.js hooks these onto
// dsh's approval/request and user-questions/request events.
async function jobForSession(sessionId) {
  if (!sessionId) return null
  let names = []
  try { names = await readdir(JOBS_DIR) } catch { return null }
  for (const n of names.filter((x) => JOB_ID.test(x))) {
    try { if ((await readJob(n)).directorSession === sessionId) return n } catch { /* not a project */ }
  }
  return null
}
async function loggedToolCall(jobId, callId) {
  const job = await readJob(jobId)
  if (!job.directorSession || !sessionService) return null
  const ev = (await sessionService.inspect(job.directorSession))?.events ?? []
  for (let i = ev.length - 1; i >= 0; i -= 1) {
    const e = ev[i]?.event ?? ev[i]
    if (e?.type === 'tool/call' && e.data?.callId === callId) return { name: e.data.name, arguments: String(e.data.arguments ?? '').slice(0, 4000) }
  }
  return null
}
export const directorAsks = createAsks({ jobForSession, toolCall: loggedToolCall })
// Is there a newer dsh-shisei (lib/update.js)? index.js sets the profile facts once dsh hands them over; without a
// dsh-launched profile there is nothing to upgrade with, so the page shows only the installed version.
export const UPDATE_FILE = join(SHISEI_HOME, 'update.json')
async function updateCheckOn() {
  try { return JSON.parse(await readFile(join(SHISEI_HOME, 'config.json'), 'utf8')).update_check !== false } catch { return true }
}
let updates = createUpdates({ file: UPDATE_FILE, enabled: updateCheckOn })
export function setUpdateProfile(profile, dsh, view) {
  toolProfile = profile ?? null   // dsh's own pnpm, for the ffmpeg package (lib/tools.js)
  updates = createUpdates({ file: UPDATE_FILE, profile: profile ?? null, dsh: dsh ?? null, enabled: updateCheckOn, ...(view ? { view } : {}) })
  return updates
}
export function updateChecker() { return updates }

let modelServices = null   // { llm, agentDefaultModel }: which model the director uses and whether it reads images
export function setModelServices(svc) { modelServices = svc ?? null }

// The page's language (zh / en / ja / ko, sent with every request): the director replies in it. story.json, the
// prompts and every other Chinese the director reads stay Chinese — the page translates what it shows (src/client/i18n.js).
const REPLY_IN = { zh: '中文', en: 'English', ja: '日本語', ko: '한국어' }
const replyLine = (lang) => lang && lang !== 'zh' && REPLY_IN[lang]
  ? `用${REPLY_IN[lang]}回复我（我读不懂中文也没关系）；但 story.json 里的文字、提示词和 drama-* 命令的参数照旧写中文。\n`
  : '用中文回复。\n'
let pageLang = 'zh'
export const DIRECTOR_PREAMBLE = (dir, lang = 'zh') => `你是这个 Shisei 短剧项目的导演，用 shisei-drama skill。项目目录：${dir}\n` +
  '规则：开机前必须先报价并等我同意；画面由我在 Shisei 页面里审核，你不能替我通过。\n' +
  '用哪一条也由我在页面里点：你只能用 drama-recommend 推荐一条并写一句理由，不能运行 drama-review pick。\n' +
  '每个项目有预算上限（drama-budget 可查），超了 drama-run 会拒绝；上限只有我能改，超了就少拍几条或告诉我。\n' +
  '我在 Shisei 页面的导演栏里和你说话，那里看不到 ask_user_question 的提问框：要问我就直接写在回复里（列出选项），然后结束这一轮等我回。' + replyLine(lang) +
  '我的消息里 @[名字](类别:编号) 是我从页面里选的东西，以编号为准（名字可能是旧的）：cast/scene/prop 是 story.json 的演员、场景、道具编号，' +
  '表示我想把它放进正在改的那一段（真要改剧本先告诉我改哪几个镜头、要不要重拍）；unit/take/ver/final/point 是段、某一条、旧版本、成片时间点和我在画面上点的一处，只是告诉你我在说哪里。' +
  '消息末尾的【Shisei 附注】是页面核对后附上的编号、文件路径和每段参考图的数量（每段首帧最多 3 张、最多 2 个人）。附注里点出的图只给你看，永远不要当生成的参考图。\n\n'

/**
 * The page's language changed since the director was told which language to reply in (job.json directorLang; missing
 * on older projects): one line after the user's words saying so. '\n\n（我在' is a mark userWords cuts at.
 */
export const langSwitchNote = (prev, now) => !REPLY_IN[now] || prev === now ? ''
  : `\n\n（我在 Shisei 页面上换了界面语言，从这条起${replyLine(now).trim()}）`   // zh-ok

/** The user's own words in a sent message: without the rules preamble and the block the page appended. */
export function userWords(text) {
  let t = String(text ?? '')
  if (t.startsWith('你是这个 Shisei 短剧项目的导演')) { const i = t.indexOf('\n\n'); t = i < 0 ? '' : t.slice(i + 2) }
  if (t.startsWith(RETRY_NOTE)) t = t.slice(RETRY_NOTE.length)
  for (const mark of [NOTE_MARK, '\n\n（我在']) { const i = t.indexOf(mark); if (i >= 0) t = t.slice(0, i) }
  // the page's own instructions to the director are not the user's words: the bubble keeps the story or the reasons
  const write = WRITE_SCRIPT('')
  if (t.startsWith(write)) t = t.slice(write.length)
  for (const mark of ['\n原因已经写清楚：', '\n请看一下这张图']) { const i = t.indexOf(mark); if (i >= 0) t = t.slice(0, i) }   // zh-ok
  return t.trim()
}

/** Turn session events into chat lines for the director panel: what the user typed, what dsh answered, turn errors, pending approvals. */
export function chatLines(events, limit = 30) {
  const lines = []
  const open = new Map()   // ask_user_question calls still waiting for an answer
  const asked = new Map()  // approval/asked id → its line, so approval/decided can say what happened
  const textOf = (parts) => (Array.isArray(parts) ? parts : [parts]).map((p) => (typeof p === 'string' ? p : p?.type === 'text' ? p.text : '')).join('').trim()
  for (const e of events ?? []) {
    const ev = e?.event ?? e
    const type = ev?.type
    const d = ev?.data ?? {}
    const m = d.message ?? d
    if (type === 'user/message') {
      if ((m.source ?? d.source)?.kind !== 'user') continue   // runtime context, skill catalog, reminders
      const text = userWords(textOf(m.content))
      if (text) lines.push({ who: 'user', text: text.slice(0, 4000) })
    } else if (type === 'assistant/message') {
      const text = textOf(m.content)
      if (text) lines.push({ who: 'dsh', text: text.slice(0, 4000) })
    } else if (type === 'tool/call' && d.name === 'ask_user_question') {
      // the panel cannot answer dsh's question form; show it so the wait is not invisible
      let qs = []
      try { qs = JSON.parse(d.arguments ?? '{}').questions ?? [] } catch { /* keep empty */ }
      open.set(d.callId, lines.length)
      lines.push({ who: 'question', text: qs.map((q) => [q.question, ...(q.options ?? []).map((o) => `· ${o.label}`)].join('\n')).join('\n\n').slice(0, 4000) })
    } else if (type === 'tool/result' && open.has(m.toolCallId ?? d.callId)) {
      lines[open.get(m.toolCallId ?? d.callId)].who = 'dsh'
    } else if (type === 'turn/end' && d.reason?.kind === 'error') {
      lines.push({ who: 'error', text: `导演这一轮出错了：${directorError(d.reason.error?.message ?? '没说原因')}` })
    } else if (type === 'approval/asked') {
      // answered on the Shisei page (lib/asks.js); the line records the ask and, once decided, what happened
      asked.set(d.id, lines.length)
      lines.push({ who: 'system', text: `导演请你同意一个操作（${d.toolName ?? '工具'}）：在下面的卡片里回答。` })
    } else if (type === 'approval/decided' && asked.has(d.id)) {
      const line = lines[asked.get(d.id)]
      const what = line.text.replace(/^导演请你同意一个操作（(.*)）：.*$/u, '$1')
      line.text = d.outcome === 'allowed-once' ? `你允许了导演的这个操作（${what}），只这一次。`
        : d.outcome === 'rejected' ? `你没有允许导演的这个操作（${what}）。`
        : d.outcome === 'cancelled' ? `导演收回了这个请求（${what}）。` : `没人回答，这个操作没有执行（${what}）。`
    }
  }
  return lines.slice(-limit)
}

/** The first message to a new project's director: write the script from the user's idea and save it. */
export const WRITE_SCRIPT = (idea) => `按 shisei-drama 的格式写剧本和分镜，写完用 drama-set 保存。故事：${idea}`
/** A new episode of a series: the recap (only summaries the user fixed) and the regulars already in adopted.json. */
export const WRITE_SCRIPT_SERIES = (idea, recap) => (recap ? `${recap}\n\n` : '') +   // zh-ok
  '这是系列的新的一集。常驻角色已经在 adopted.json 里（定妆照我在系列里审过）：写 story.json 时原样用它们的 id、名字、年龄和 look，' +   // zh-ok
  '不要改长相和年龄（改了图就要重画重审）；换衣服写 outfit。接着前情写，不要重复已经发生的事。' +   // zh-ok
  `按 shisei-drama 的格式写剧本和分镜，写完用 drama-set 保存。这一集：${idea}`   // zh-ok
/** The same with a reference video (对标片): its structure first, then the script in its rhythm. */
// what the image model is asked about a frame the user paused on and sent to the director (「在这一帧对导演说」)
export const FRAME_ASK = '用一两句话描述这一帧画面：几个人、各在画面哪边、朝哪边、表情和动作，景别，光线，看得到的道具。只说看到的，不猜剧情。' // zh-ok
export const WRITE_SCRIPT_STUDY =(idea) => '这个项目带了对标片（study.json，已在本机拆好镜头；看图模型在后台看联系表，通常一分钟内看完，不要重跑它）。' +
  '按 shisei-drama 的「对标片」一节：先 drama-study-plan --wait 120 拿到它的节奏、景别和分段骨架，再按骨架写剧本和分镜（每段写上 study 范围），' +
  `写完用 drama-set 保存。只学镜头和节奏：故事是我的，出场角色是新编的成年虚构人物，不写对标片里的人、台词和画面。故事：${idea}`

// What the director is doing, in the page's words. Only the tool's name and the shisei.py command are looked at:
// raw arguments carry paths and never reach the page.
const SHISEI_STEP = [[/drama-study-plan\b/u, '在按对标片分段'], [/drama-study-look\b/u, '在看对标片'], [/drama-study-link\b/u, '在记下对标的镜头'],
  [/drama-study\b/u, '在拆对标片'], [/drama-set\b/u, '在保存剧本'],[/drama-edit\b/u, '在改剧本'], [/drama-status\b/u, '在看项目进度'], [/drama-estimate\b/u, '在算价钱'],
  [/drama-frames\b/u, '在抽帧看画面'], [/drama-precheck\b/u, '在预审首帧'], [/drama-recommend\b/u, '在比较几条视频'], [/drama-check\b/u, '在核对台词'], [/drama-shots\b/u, '在逐镜核对']]
function stepWords(name, args) {
  if (name === 'skill') return '在读 Shisei 的写法'
  if (name === 'read_image') return '在看图'
  if (name === 'read' || name === 'glob' || name === 'grep' || name === 'ls') return '在看项目文件'
  if (name === 'write' || name === 'edit') return '在写文件'
  if (name === 'ask_user_question') return '在等你回答它的问题'
  if (name === 'bash' || name === 'pwsh') {   // dsh's shell tool: pwsh on Windows
    let cmd = ''
    try { cmd = String(JSON.parse(args ?? '{}').command ?? '') } catch { /* keep empty */ }
    for (const [re, words] of SHISEI_STEP) if (re.test(cmd)) return words
    return '在运行命令'
  }
  return '在用工具'
}
/**
 * The director session at a glance: { busy, since, activity, steps, error, done, lastAt, waiting }. busy: a turn started (or the user's
 * message is queued) and has not ended; since: when that turn began (ms); activity: the last step in plain words;
 * error: how the last turn failed; done: the last turn completed; lastAt: when the session last logged anything (ms),
 * so the page can tell a long silence; waiting: during a turn, 'model' (asked the model, no answer yet) or 'tool' (a
 * command or tool is still running). Pure, so it can be tested without dsh.
 */
export function directorState(events) {
  const st = { busy: false, since: null, activity: null, steps: 0, error: null, done: false, lastAt: null, waiting: null, model: null, context: null }
  let lastTokens = 0   // what the model was last sent and wrote back (its usage), and the text added since
  let added = 0
  const textLen = (parts) => (Array.isArray(parts) ? parts : [parts]).reduce((n, p) => n + (typeof p === 'string' ? p.length : p?.type === 'text' ? String(p.text ?? '').length : 0), 0)
  for (const e of events ?? []) {
    const ev = e?.event ?? e
    const d = ev?.data ?? {}
    const t = Number(ev?.time) || null
    if (t) st.lastAt = t
    // the model the session asks: the first request's header, then each switch (dsh's model/selection)
    if (ev?.type === 'request/header' && d.header?.config?.model) st.model = { provider: String(d.header.config.provider ?? ''), model: String(d.header.config.model) }
    else if (ev?.type === 'model/selection' && d.model) st.model = { provider: String(d.provider ?? ''), model: String(d.model) }
    if (ev?.type === 'assistant/message') {
      const u = d.usage ?? d.message?.usage
      if (u) { lastTokens = (Number(u.inputTokens) || 0) + (Number(u.cacheReadTokens) || 0) + (Number(u.outputTokens) || 0); added = 0 }
    } else if (ev?.type === 'tool/result') added += textLen(d.message?.content)
    else if (ev?.type === 'user/message') added += textLen((d.message ?? d).content)
    if (ev?.type === 'turn/start' || ev?.type === 'step/start' || ev?.type === 'tool/result') st.waiting = 'model'
    else if (ev?.type === 'tool/call') st.waiting = 'tool'
    switch (ev?.type) {
      case 'user/message':
        if (((d.message ?? d).source ?? d.source)?.kind === 'user' && !st.busy) Object.assign(st, { busy: true, since: t, activity: '在读你的话', steps: 0, error: null, done: false })
        break
      case 'turn/start':
        if (!st.busy || st.since === null) Object.assign(st, { busy: true, since: t, steps: 0, error: null, done: false })
        st.activity = st.activity ?? '在想'
        break
      case 'step/start': st.steps += 1; st.activity = st.activity ?? '在想'; break   // keeps the last tool's words while it reads the result
      case 'tool/call': st.activity = stepWords(d.name, d.arguments); break
      case 'turn/end': {
        const kind = d.reason?.kind
        Object.assign(st, { busy: false, activity: null, error: kind === 'error' ? directorError(String(d.reason.error?.message ?? '没说原因')).slice(0, 300) : null, done: kind === 'completed', waiting: null })
        break
      }
      default:
    }
  }
  // roughly how much the next model call carries (a character of Chinese or web text is about 0.6 token): a stall on a
  // very long context reads differently from a slow model
  if (lastTokens || added) st.context = { tokens: Math.round(lastTokens + added * 0.6), added }
  return st
}

/**
 * What the user said that the director has not read yet: dsh keeps a message sent during a turn in the session's inbox
 * (agent/inbox/spliced), not as a user/message, until the turn reads it. next-turn: queued, read when this turn ends;
 * next-step: an interjection, read when the current step ends. Replays the splices like dsh's own inbox projection and
 * keeps only the user's own messages (tool results land in next-step too). Pure.
 */
export function directorQueue(events) {
  return pendingUserMessages(events).map(({ content: _c, ...q }) => q)
}
/** The user's last message in the session (their words with the page's note, without the rules preamble), or ''. */
export function lastUserMessage(events) {
  let out = ''
  for (const e of events ?? []) {
    const ev = e?.event ?? e
    if (ev?.type !== 'user/message') continue
    const m = ev.data?.message ?? ev.data ?? {}
    if (m.source?.kind !== 'user') continue
    let text = (Array.isArray(m.content) ? m.content : [m.content]).map((p) => (typeof p === 'string' ? p : p?.type === 'text' ? p.text : '')).join('')
    if (text.startsWith('你是这个 Shisei 短剧项目的导演')) { const i = text.indexOf('\n\n'); text = i < 0 ? '' : text.slice(i + 2) }
    if (text.startsWith(RETRY_NOTE)) text = text.slice(RETRY_NOTE.length)
    if (text.trim()) out = text
  }
  return out
}
/** Put before the message sent again after the page stopped a stuck turn and switched the model. */
export const RETRY_NOTE = '（上一轮一直没等到模型回话，我停下来换了个模型。请接着做下面这件事；要读网页的话只读需要的那一篇，不要整卷整页地抓。）\n\n'   // zh-ok

function pendingUserMessages(events) {
  const inbox = { 'next-turn': [], 'next-step': [] }
  for (const e of events ?? []) {
    const ev = e?.event ?? e
    if (ev?.type !== 'agent/inbox/spliced') continue
    const d = ev.data ?? {}
    const list = inbox[d.target]
    if (!list) continue
    const start = Math.max(0, Math.min(Number(d.start) || 0, list.length))
    list.splice(start, Number(d.removedCount) || 0, ...(Array.isArray(d.inserted) ? d.inserted : []))
  }
  const textOf = (parts) => (Array.isArray(parts) ? parts : [parts]).map((p) => (typeof p === 'string' ? p : p?.type === 'text' ? p.text : '')).join('').trim()
  const out = []
  for (const target of ['next-step', 'next-turn']) {
    for (const m of inbox[target]) {
      if (m?.source?.kind !== 'user' || !m.id) continue
      const text = userWords(textOf(m.content))
      if (text) out.push({ id: String(m.id), target, text: text.slice(0, 4000), content: (Array.isArray(m.content) ? m.content : [m.content]).filter((p) => p?.type === 'text') })
    }
  }
  return out
}

/** What the director is told when the user sends a picture back: which one, and the user's reason in their own words. */
export function rejectMessage({ kind, key, label, note }) {
  const what = kind === 'keyframes' ? `${label || `镜 ${key}`}的首帧（kf_${key}.png）` : `「${label || key}」这张图（${key}）`
  return `我退回了${what}。原因：${note}\n` +
    '请看一下这张图，告诉我你打算怎么改、重拍大约多少钱。不要自己开机，也不要替我通过。'
}

/**
 * One message for the pictures the user sent back since the last hand-off (the page holds each rejection and sends
 * them together, so the director reads them once and does not ask the same question per picture). The reasons are
 * written, so the director changes the descriptions straight away; only a free change, never a run.
 */
export function handoffMessage(items) {
  const lines = items.map((it) => {
    const what = it.kind === 'keyframes' ? `${it.label || `镜 ${it.key}`}的首帧（kf_${it.key}.png）` : `「${it.label || it.key}」（${it.key}）`
    return `- ${what}：${it.note}`
  })
  return `我退回了 ${items.length} 张：\n${lines.join('\n')}\n` +
    '原因已经写清楚：直接按原因改描述，用 drama-set 保存（不花钱，不用先问我；真拿不准的地方一次问完）。' +
    '改完告诉我哪些要重拍、大约多少钱。不要自己开机，也不要替我通过。'
}

/** The rejections with a reason that the director has not been handed yet (review.json, newer than the last hand-off). */
export function pendingHandoff(review, since) {
  const out = []
  for (const kind of ['assets', 'keyframes']) {
    for (const [key, r] of Object.entries(review?.[kind] ?? {})) {
      // r.inputs: held by this version of the page; older rejections were sent one by one when they were made
      if (r?.status === 'reject' && r.note && r.inputs && Number(r.at) > Number(since || 0)) out.push({ kind, key, note: String(r.note), at: Number(r.at) })
    }
  }
  return out.sort((a, b) => a.at - b.at)
}

/** The director's name for a picture (Chinese, like everything it reads): 林岚 · 定妆照, 雨夜车站, 段 B. */
export function pictureLabel(story, kind, key) {
  if (kind === 'keyframes') {
    const u = storyUnits(story).find((x) => x.id === key)
    return u ? `段 ${u.label}` : ''   // zh-ok
  }
  const m = /^(cast|scene|prop)_([a-z0-9_]+?)(?:_(face|body))?\.png$/u.exec(key) || []
  const list = { cast: story?.cast, scene: story?.scenes, prop: story?.props }[m[1]] || []
  const item = list.find((x) => x.id === m[2])
  if (!item) return ''
  return m[1] === 'cast' ? `${item.name} · ${m[3] === 'face' ? '定妆照' : '全身'}` : String(item.name || '')   // zh-ok
}

/**
 * The director's last reply asks the user something: a question mark in its last lines (it often closes with a line
 * like 「我等你说了再动」 after the questions). Nothing running + this = it is waiting for the user.
 */
export function endsWithQuestion(text) {
  const lines = String(text ?? '').trim().split(/\n+/u).filter((l) => l.trim())
  return /[?？]/u.test(lines.slice(-3).join('\n').slice(-400))
}

/** A project's spending cap in yuan, as typed by the user. */
export function budgetCap(value) {
  const n = Math.round(Number(value) * 100) / 100
  if (!Number.isFinite(n) || n < 0.5 || n > 500) throw new Error('预算上限要在 ¥0.5 到 ¥500 之间')
  return n
}

/** Refusal shown when a run would go past the cap (same numbers as shisei.py drama-estimate / job-estimate). */
export function overBudgetMessage(est, swap = false, stopAfter = false) {
  const b = est?.budget
  // the idle stop the run arms is billed too (est.idle_cost); stopping right after the run leaves only the run
  const over = stopAfter && b && typeof b.over_stop === 'number' ? b.over_stop : b?.over
  if (!b || !(over > 0)) return null
  const cost = Number(est.cost || 0) + (stopAfter ? 0 : Number(est.idle_cost || 0))
  // 整段换 (est.shots) shoots one take: only the cap is a way out
  const fewer = swap && est.shots ? '整段只拍 1 条，调高上限才能拍。' : swap ? '这次少拍几条，或者调高上限。' : '每段少拍几条、少拍几段，或者点页面顶上的「已花（估）」调高上限。'
  return `超出这个项目的上限：已花（估）¥${b.spent.toFixed(2)}，这次约 ¥${cost.toFixed(2)}，加起来比上限 ¥${b.cap} 多 ¥${over.toFixed(2)}。${fewer}`
}

/** The take seeds of a dance-swap job (shisei.py job_seeds): `takes` once more were asked for, else 1001… by count. */
export function swapSeeds(params) {
  if (Array.isArray(params?.takes) && params.takes.length) return params.takes.map(Number)
  return Array.from({ length: Number(params?.seeds) || 3 }, (_, i) => 1001 + i)
}

/**
 * job.json after the user asks for `add` more takes, optionally with a new character description. Earlier takes keep
 * their seeds and the description they were made with (seedPrompts); new takes get fresh seeds.
 */
export function addSwapTakes(job, add, prompt = null, prompt2 = null) {
  const n = Number(add)
  if (!Number.isInteger(n) || n < 1 || n > 6) throw new Error('一次加 1 到 6 条')
  const have = swapSeeds(job.params)
  if (have.length + n > 12) throw new Error('一个项目最多 12 条')
  const clean = (v, old) => (v === null || v === undefined ? (old ?? '') : String(v).trim().slice(0, 2000))
  const text = clean(prompt, job.prompt)
  // two people: each take keeps a [left, right] pair (shisei.py swap_take_people)
  const duo = swapPeople(job.params) === 2
  const text2 = duo ? clean(prompt2, job.prompt2) : null
  const asked = (l, r) => (duo ? [l, r] : l)
  const seedPrompts = { ...(job.seedPrompts ?? {}) }
  for (const s of have) if (!(String(s) in seedPrompts)) seedPrompts[s] = asked(job.prompt ?? '', job.prompt2 ?? '')
  const next = Math.max(...have) + 1
  const fresh = Array.from({ length: n }, (_, i) => next + i)
  for (const s of fresh) seedPrompts[s] = asked(text, text2)
  return { ...job, prompt: text, ...(duo ? { prompt2: text2 } : {}), params: { ...job.params, takes: [...have, ...fresh] }, seedPrompts }
}

/** The picture names after the two characters change sides: each picture is renamed for its new side, extension kept. */
export function swapSideFiles(files) {
  const ext = (name) => /\.[A-Za-z0-9]+$/u.exec(name)?.[0] ?? ''
  return { front: `front${ext(files.front2)}`, front2: `front2${ext(files.front)}` }
}

/** 1 or 2: how many people a dance-swap job replaces (shisei.py swap_people). */
export function swapPeople(params) {
  return Number(params?.people) === 2 ? 2 : 1
}

/** Swapped on the source clip (no motion capture, no white model): two people, or mode keepbg (shisei.py swap_keep_bg). */
export function swapKeepBg(params) {
  return swapPeople(params) === 2 || params?.mode === 'keepbg'
}

/** The params a new dance-swap job takes from the page; anything else is dropped. */
export function swapParams(params = {}) {
  const people = swapPeople(params)
  return {
    shot: params.shot === 'full' ? 'full' : 'close',
    head: params.head === 'egg' ? 'egg' : 'face',
    seeds: Math.min(6, Math.max(1, Number(params.seeds) || 3)),
    mode: people === 2 || params.mode === 'keepbg' ? 'keepbg' : 'studio',
    people,
  }
}

// 整段换 (shisei.py long_frames): a dance over one generation (15 s), up to five minutes
export const LONG_MIN_SECONDS = 15
export const LONG_MAX_SECONDS = 300

/** The range a recast swaps as a whole dance ({ start, seconds }), checked against the video; null when it is not one. */
export function longRange(whole, duration) {
  if (whole === undefined || whole === null) return null
  const start = Math.max(0, Number(whole.start) || 0)
  const seconds = Math.round(Number(whole.seconds) * 1000) / 1000
  if (!(seconds > LONG_MIN_SECONDS && seconds <= LONG_MAX_SECONDS)) throw new Error(`整段要 ${LONG_MIN_SECONDS}–${LONG_MAX_SECONDS} 秒`)
  if (Number.isFinite(Number(duration)) && start + seconds > Number(duration) + 0.05) throw new Error('整段超出了视频的结尾')
  return { start, seconds }
}

/**
 * job.json of the whole-dance project made from a recast's take `seed` (「照这条拍完整段」): the same pictures, the
 * description and seed that take was made with, one take, the range the user chose. Nothing is billed here.
 */
export function longJob(parent, seed, id, now = new Date()) {
  const lg = parent.params?.long
  if (!lg) throw new Error('这个项目没有选整段')
  const s = Number(seed)
  if (!swapSeeds(parent.params).includes(s)) throw new Error('没有这一条')
  const duo = swapPeople(parent.params) === 2
  const asked = parent.seedPrompts?.[String(s)]
  const p1 = duo ? (Array.isArray(asked) ? asked[0] : parent.prompt) : typeof asked === 'string' ? asked : parent.prompt
  const p2 = duo ? (Array.isArray(asked) ? asked[1] : parent.prompt2) : null
  const second = duo ? 'front2' : 'face'
  return {
    id, createdAt: now.toISOString(), state: 'draft',
    prompt: p1 ?? '', ...(duo ? { prompt2: p2 ?? '' } : {}),
    seedPrompts: { [String(s)]: duo ? [p1 ?? '', p2 ?? ''] : p1 ?? '' },
    params: { ...swapParams(parent.params), seeds: 1, takes: [s], long: { from: parent.id, seed: s, start: lg.start, seconds: lg.seconds } },
    files: { source: parent.files.source, front: parent.files.front, [second]: parent.files[second] },
    ...(parent.probe ? { probe: parent.probe } : {}),
  }
}

/** precheck.json entries whose file is still the one that was judged (same mtime), like shisei.py current_precheck. */
async function currentPrecheck(id) {
  let pre = {}
  try { pre = JSON.parse(await readFile(jobPath(id, 'precheck.json'), 'utf8')) } catch { return {} }
  const out = {}
  for (const [name, e] of Object.entries(pre ?? {})) {
    if (!/^seed_\d{4,5}\.mp4$/u.test(name) || !e || typeof e !== 'object') continue
    try {
      const m = (await stat(jobPath(id, name))).mtimeMs / 1000
      if (Math.abs(m - Number(e.mtime)) < 0.01) out[name] = e
    } catch { /* take gone */ }
  }
  return out
}

/** dsh's default model ({ provider, model }), which a new director session starts on, or null. */
function defaultModel() {
  try { const sel = modelServices?.agentDefaultModel?.currentSelection?.(); return sel ? { provider: String(sel.provider), model: String(sel.model) } : null } catch { return null }
}

/** Why the director cannot run here, in the page's words, or null. Unknown (an older dsh) counts as fine. */
function directorMissing() {
  if (!sessionService) return '这个 dsh 版本没有提供会话服务，导演不能在 Shisei 页面里工作。请在 dsh 对话里直接说。'
  try {
    const pick = modelServices?.agentDefaultModel?.currentSelection
    if (typeof pick === 'function' && !pick()) return '导演还没有模型：先在 dsh 的设置里选一个默认模型，再回来点「重试」。'
  } catch { /* unknown */ }
  return null
}

/** Send one message to the project's director session (created, with the rules preamble, on first use). */
/** When the page last handed rejections to the director (handoff.json), in review.json's seconds. */
async function handoffAt(job) {
  try { return Number(JSON.parse(await readFile(jobPath(job.id, 'handoff.json'), 'utf8')).at) || 0 } catch { return 0 }
}

async function sendToDirector(job, text, note = '', mode = 'queue') {
  if (!sessionService) throw new Error('这个 dsh 版本没有提供会话服务，请在 dsh 对话里直接和导演说')
  let sid = job.directorSession
  const first = !sid
  if (first) {
    sid = (await sessionService.create({ cwd: jobPath(job.id) })).sessionId
    await updateJob(job.id, (j) => ({ ...j, directorSession: sid }))
    try { await sessionService.rename?.({ sessionId: sid, title: `Shisei 导演 · ${job.title ?? ''}`.slice(0, 60) }) } catch { /* title is cosmetic */ }
  }
  const lang = pageLang
  const sw = first ? '' : langSwitchNote(job.directorLang, lang)
  if (first || sw) await updateJob(job.id, (j) => ({ ...j, directorLang: lang }))
  const content = [{ type: 'text', text: (first ? DIRECTOR_PREAMBLE(jobPath(job.id), lang) : '') + text + note + sw }]
  // steer (插话): read at the end of the director's current step, inside the same turn; only while a turn is running
  // (an idle director just starts a turn, which queue does too)
  let how = 'queue'
  if (mode === 'steer' && !first) {
    try { if (directorState((await sessionService.inspect(sid))?.events).busy) how = 'steer' } catch { /* queue */ }
  }
  await sessionService.prompt({ sessionId: sid, requestId: randomUUID(), mode: how, content }, new AbortController().signal)
  return { sid, mode: how }
}

/** What this dsh lets the page do with a running director: interject, take back a queued message, stop the turn. */
const directorCan = () => ({ queue: typeof sessionService?.updateQueue === 'function', stop: typeof sessionService?.cancel === 'function' })
/**
 * Send what is still waiting in the inbox again, in order, as ordinary messages. After a turn is cancelled dsh does not
 * pick up an interjection (next-step) by itself, and a queued message can sit in a session whose turn is over: this
 * takes each one out and prompts it again, with the words and note it was sent with.
 */
async function resendPending(sid) {
  const pending = pendingUserMessages((await sessionService.inspect(sid))?.events)
  let sent = 0
  const back = []   // taken out but not sent again: the page puts these words back in the composer, so nothing is lost
  for (const m of pending) {
    try { await sessionService.updateQueue({ sessionId: sid, itemId: m.id, action: { kind: 'remove' } }) } catch (e) {
      if (remoteCode(e) === 'session/queue-item-not-found') continue   // dsh picked it up meanwhile
      throw e
    }
    try {
      await sessionService.prompt({ sessionId: sid, requestId: randomUUID(), mode: 'queue', content: m.content }, new AbortController().signal)
      sent += 1
    } catch { back.push(m.text) }
  }
  return { sent, back }
}
/** dsh's refusal code (RemoteError.code), or ''. */
const remoteCode = (e) => String(e?.code ?? e?.data?.code ?? (/^session\/[a-z-]+/u.exec(String(e?.message ?? ''))?.[0] ?? ''))

// ---------------------------------------------------------------- change watch
/** Version of a project's files: names, sizes and times of everything in its folder (one level; a new file in a
 * subfolder changes the subfolder's time). A short hash, so the page can hand it back. */
export async function filesVersion(dir) {
  let names
  try { names = (await readdir(dir)).filter((n) => !n.startsWith('.')).sort() } catch { return 'gone' }
  const parts = await Promise.all(names.map(async (n) => { try { const st = await stat(join(dir, n)); return `${n}:${st.size}:${st.mtimeMs}` } catch { return n } }))
  return createHash('sha1').update(parts.join('|')).digest('hex').slice(0, 16)
}
/** Version of the director's session: how many events it has and whether the last turn has ended. */
async function directorVersion(job) {
  const asked = directorAsks.version(job.id)
  if (!job.directorSession || !sessionService) return `none:${asked}`
  try {
    const ev = (await sessionService.inspect(job.directorSession))?.events ?? []
    return `${ev.length}:${ev.length ? ev[ev.length - 1].type ?? '' : ''}:${asked}`
  } catch { return `gone:${asked}` }
}
/**
 * Wait until the project's files or its director differ from `since` ({ files, director }), at most waitMs, and say
 * which. Files are checked every `everyMs`, the director every other round (a session snapshot costs more than a stat).
 */
export async function watchProject(job, since, { waitMs = 20000, everyMs = 600, signal, readVersions } = {}) {
  const versions = readVersions ?? (async (round) => ({ files: await filesVersion(jobPath(job.id)), director: round % 2 === 0 ? await directorVersion(job) : null }))
  const end = Date.now() + waitMs
  let director = since.director ?? null
  for (let round = 0; ; round += 1) {
    const v = await versions(round)
    if (v.director !== null && v.director !== undefined) director = v.director
    const filesChanged = v.files !== since.files
    const directorChanged = director !== (since.director ?? null)
    if (filesChanged || directorChanged || Date.now() >= end || signal?.aborted) {
      return { files: v.files, director, changed: { files: filesChanged, director: directorChanged } }
    }
    await new Promise((r) => { const t = setTimeout(r, everyMs); signal?.addEventListener?.('abort', () => { clearTimeout(t); r() }, { once: true }) })
  }
}

/** The RPC handler, separated from transport so it can be unit tested. */
/**
 * The machine patrol: while dsh runs, every few minutes `shisei.py patrol` keeps each registered machine that is on
 * under a platform scheduled stop (it never stops one itself). The platform stop is the first guard and works with
 * dsh closed; this is the second. Runs only when the compshare keys are there; one run at a time; the last answer is
 * what the page shows (state.machines).
 */
export function startPatrol(credentials, { everyMs = 180000, run = runShisei, firstMs = 15000, cli = () => cliReady } = {}) {
  let last = null
  let running = null   // the patrol in flight
  let again = null     // one more after it, for whoever asked meanwhile (现在检查, a run that just started)
  let timer = null
  const once = async () => {
    try {
      const env = await keyEnv(credentials)
      if (!env.COMPSHARE_PUBLIC_KEY || !env.COMPSHARE_PRIVATE_KEY) return
      await cli()   // compshare-cli being installed or rebuilt: wait for it rather than report the machines unreadable
      const r = await run(['patrol'], env)
      last = r && r.ok ? { ...r, at: Date.now() } : { ...(last ?? {}), error: r?.error ?? 'patrol failed', errorKind: r?.error_kind ?? null, at: Date.now() }
    } catch (e) {
      last = { ...(last ?? {}), error: String(e?.message ?? e), at: Date.now() }
    }
  }
  // A tick asked while one is in flight waits for a fresh one after it: the one in flight may have read the account
  // before the machine came on, and answering with it showed a working machine as 停着.
  const tick = () => {
    if (!running) {
      running = once().finally(() => { running = null })
      return running
    }
    again ??= running.then(() => { again = null; return tick() })
    return again
  }
  const first = setTimeout(() => { tick(); timer = setInterval(tick, everyMs); timer.unref?.() }, firstMs)
  first.unref?.()
  // compshare-cli just became ready: if the last patrol could not read the account, read it again now
  const retry = () => { if (last?.error) tick() }
  afterCli = retry
  return { last: () => last, tick, retry, stop: () => { clearTimeout(first); if (timer) clearInterval(timer); if (afterCli === retry) afterCli = null } }
}

/** What a machine's lease holder is doing, for the machines page: the project's name and how far its run is. */
export function runProgress(progress) {
  if (!progress || progress.state !== 'running') return null
  if (progress.items && typeof progress.items === 'object') {
    const v = Object.values(progress.items)
    return { stage: progress.stage ?? null, done: v.filter((x) => x === 'done').length, total: v.length }
  }
  if (progress.seeds && typeof progress.seeds === 'object') {
    const v = Object.values(progress.seeds)
    return { stage: 'swap', done: v.filter((x) => x === 'done').length, total: v.length }
  }
  return { stage: null, done: null, total: null }
}

async function holderInfo(holder) {
  if (!JOB_ID.test(String(holder ?? ''))) return null
  try {
    const job = await readJob(holder)
    let progress = null
    try { progress = JSON.parse(await readFile(jobPath(holder, 'progress.json'), 'utf8')) } catch { /* no run yet */ }
    return { id: job.id, kind: job.kind === 'drama' ? 'drama' : 'swap', title: job.title || (job.prompt || '').slice(0, 24) || null, run: runProgress(progress) }
  } catch { return null }
}

/**
 * How the machines page stops a machine: a project's run that is alive is asked to stop itself (stop file: it pulls
 * what is done, writes its ledger line and stops the machine); anything else (no lease, a run that died) is stopped
 * directly. Pure; tested.
 */
export function stopPlan(lease, progress, nowMs, alive) {
  const held = lease && lease.state === 'active' && Number(lease.until) * 1000 > nowMs
  if (held && progress && progress.state === 'running' && progress.pid && alive(progress.pid)) return 'project'
  return 'machine'
}

/** The patrol's last answer with each holder turned into its project (name, kind, progress). */
/** The environment version this plugin wants (shisei.py SHISEI_ENV), read once. */
let envWant = null
async function wantedEnv() {
  if (envWant === null) {
    const m = /^SHISEI_ENV = (\d+)/mu.exec(await readFile(SCRIPT, 'utf8').catch(() => ''))
    envWant = m ? Number(m[1]) : 0
  }
  return envWant
}

/** What the machines page says about a machine's environment, from the last one read on it (machines-env.json) and
 * the version this plugin wants now (the plugin may have moved on since): null when nothing was ever read. */
export function envBadge(rec, want) {
  if (!rec || !want) return null
  const have = Number.isInteger(rec.have) ? rec.have : null
  if (have === null) return { state: 'missing', have, want }
  if (have < want) return { state: 'old', have, want }
  if (have > want) return { state: 'newer', have, want }
  return { state: rec.state === 'drift' ? 'drift' : 'current', have, want }
}

export async function machinesView(last) {
  if (!last) return null
  let seen = {}
  try { seen = JSON.parse(await readFile(join(SHISEI_HOME, 'machines-env.json'), 'utf8')) } catch { /* none read yet */ }
  const want = await wantedEnv()
  const machines = await Promise.all((last.machines ?? []).map(async (m) => ({
    ...m, project: m.holder ? await holderInfo(m.holder) : null, env: envBadge(seen?.[m.id], want),
  })))
  return { ...last, machines }
}

export function createHandler(credentials, jobs = new Jobs(), patrol = null) {
  const { adoptInto, adoptTokens, wardrobeSource, priceOf, externalOptions, refsFor } = refsWith(credentials)
  const LONG = new Set(['up', 'down', 'install-cli', 'secure', 'ffmpeg-install'])
  // the machine badge follows a run's start and end without waiting for the next 3-minute patrol
  const followMachine = (child) => {
    for (const ms of [40000, 120000]) setTimeout(() => patrol?.tick?.(), ms).unref?.()
    child.on?.('exit', () => patrol?.tick?.())
  }
  // the image model reads the 对标片 contact sheets in the background (minutes); study.json carries its progress
  async function spawnStudyLook(job, force = false) {
    const out = await open(jobPath(job.id, 'run.log'), 'a')
    const child = spawnPy(['-I', SCRIPT, 'drama-study-look', jobPath(job.id), ...(force ? ['--force'] : [])],
      { env: pageEnv(await keyEnv(credentials)), detached: true, stdio: ['ignore', out.fd, out.fd] })
    child.unref()
    await out.close()
  }
  // every refusal the page can show goes through one mapping (lib/frame.js pageError); results that carry an error
  // instead of throwing (run, doctor) get the same words
  /** For 开始 and 设置: whether a separate key is saved, and which image model answers (lib/vision.js). */
  async function visionView() {
    const d = await credentials.describe(await visionKeyName()).catch(() => ({}))
    const hasKey = !!d?.configured || !!process.env[await visionKeyName()]
    const vs = await visionState(hasKey)
    return { visionKey: !!d?.configured, vision: { pref: vs.pref, source: vs.source, model: vs.model, director: vs.director, canStream: vs.canStream, hasKey } }
  }

  return async function handle(endpoint, payload = {}, opts = {}) {
    let r
    if (payload && REPLY_IN[payload.lang]) pageLang = payload.lang   // one user, one page language: the latest wins
    try {
      // an archived project is read-only until the user restores it on the page: nothing that changes it or starts a machine
      if (ARCHIVE_LOCKED.has(endpoint) && JOB_ID.test(String(payload?.id ?? ''))) {
        let job = null
        try { job = await readJob(String(payload.id)) } catch { /* the endpoint reports a missing project itself */ }
        if (job?.archivedAt) throw new Error(ARCHIVED_SAY)
      }
      r = await route(endpoint, payload, opts)
    } catch (e) { throw new Error(pageError(e instanceof Error ? e.message : String(e))) }
    if (r && typeof r === 'object' && r.ok === false && typeof r.error === 'string') return { ...r, error: pageError(r.error) }
    return r
  }
  async function route(endpoint, payload = {}, opts = {}) {
    switch (endpoint) {
      // One parked request per open project page: answers as soon as the project's files or its director session change
      // (or after `wait` ms), so the page re-reads only when there is something new. Reads timestamps only; no shisei.py.
      case 'drama-watch': {
        const job = await readJob(String(payload.id))
        const since = payload.since && typeof payload.since === 'object' ? payload.since : {}
        return watchProject(job, since, { waitMs: Math.min(Math.max(Number(payload.wait) || 20000, 0), 25000), signal: opts.signal })
      }
      // the page's 重试 after a failed preparation (lib/tools.js); answers the state once it is done
      case 'tools-retry':
        return prepareComputer().all
      case 'state':
        return { tools: toolsState(), keys: await keyState(credentials), keyCheck: lastKeyCheck?.state ?? (patrol?.last()?.errorKind === 'auth' ? 'auth' : null), jobs: jobs.running().map(({ id, label }) => ({ id, label })), machines: await machinesView(patrol?.last() ?? null) }
      // the version card and the menu's notice: the last answer at once; `check` asks the registry now (the page's
      // 重新检查, or when the answer is older than a day)
      case 'version':
        return payload.check ? updates.check(payload.check === 'force') : updates.current()
      case 'version-dismiss':
        return updates.dismiss(payload.version == null ? null : String(payload.version).slice(0, 64))
      case 'version-seen':
        return updates.seen()
      case 'set-keys': {
        for (const [k, ref] of Object.entries(KEY_REFS)) {
          const v = typeof payload[k] === 'string' ? payload[k].trim() : ''
          if (v) {
            if (!/^[\x21-\x7E]{8,200}$/u.test(v)) throw new Error(`${k} key has unexpected characters or length`)
            await credentials.set(ref, v)
          }
        }
        lastKeyCheck = null
        return { keys: await keyState(credentials) }
      }
      case 'unset-keys':
        for (const ref of Object.values(KEY_REFS)) await credentials.unset(ref)
        lastKeyCheck = null
        return { keys: await keyState(credentials) }
      // Is the pair in use accepted? One read-only listing of the account (instances --pick, as before): it never
      // starts or stops anything, and the keys never leave the host.
      case 'keys-check': {
        const keys = await keyState(credentials)
        const env = await keyEnv(credentials)
        if (!env[KEY_REFS.public] || !env[KEY_REFS.private]) {
          lastKeyCheck = null
          return { keys, check: { state: 'none' }, instances: null }
        }
        await prepareComputer().cli
        const r = await runShisei(['instances', '--pick'], env)
        const check = keyCheckResult(r)
        lastKeyCheck = check
        // keys just accepted: a patrol that failed with the old (or no) keys reads the account again now, or the
        // sidebar kept 看不到机器状态 · 密钥没通过 for up to three minutes after 已连接 (seen 2026-11-14)
        if (check.state === 'ok') patrol?.tick?.()
        return { keys, check, instances: r.ok ? r : null }
      }
      case 'run': {
        const argv = guiArgv(payload.command, payload.options)
        const env = await keyEnv(credentials)
        if (LONG.has(payload.command)) {
          const job = jobs.start(payload.command, (onLog) => runShisei(argv, env, onLog))
          return { job: job.id }
        }
        return runShisei(argv, env)
      }
      // the machines page: the patrol's answer (now: run it first), and stopping one machine after the page asked
      case 'machines': {
        if (payload.now && patrol?.tick) await patrol.tick()
        return { machines: await machinesView(patrol?.last() ?? null) }
      }
      case 'machine-register': {
        const id = String(payload.id ?? '')
        if (!/^[A-Za-z0-9_-]{4,64}$/u.test(id)) throw new Error('bad instance id')
        const r = await runShisei(['instances', '--register', id], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        await patrol?.tick?.()
        return { machines: await machinesView(patrol?.last() ?? null) }
      }
      case 'machine-stop': {
        const id = String(payload.id ?? '')
        if (!/^[A-Za-z0-9_-]{4,64}$/u.test(id)) throw new Error('bad instance id')
        if (payload.confirm !== true) throw new Error('stopping a machine needs the user to confirm on the page')
        let cfg = {}
        try { cfg = JSON.parse(await readFile(join(SHISEI_HOME, 'config.json'), 'utf8')) } catch { /* none */ }
        if (![cfg.instance_id, ...(cfg.machines ?? [])].includes(id)) throw new Error(`${id} is not a registered machine`)
        let lease = null
        try { lease = JSON.parse(await readFile(join(SHISEI_HOME, 'leases', `${id}.json`), 'utf8')) } catch { /* none */ }
        let progress = null
        if (lease && JOB_ID.test(String(lease.holder))) {
          try { progress = JSON.parse(await readFile(jobPath(lease.holder, 'progress.json'), 'utf8')) } catch { /* no run */ }
        }
        if (stopPlan(lease, progress, Date.now(), pidAlive) === 'project') {
          await writeFile(jobPath(lease.holder, 'stop'), String(Date.now()))
          return { stopping: 'project', project: lease.holder }
        }
        const env = await keyEnv(credentials)
        // the page asked the user (the dialog names the project): this stop may take a machine someone else holds
        const job = jobs.start('down', (onLog) => runShisei(['--machine', id, 'down', '--yes', '--force-other'], env, onLog).then((r) => { patrol?.tick?.(); return r }))
        return { job: job.id }
      }
      case 'jobs':
        return { jobs: await listJobs() }
      // archive / restore: only the project's archivedAt changes; no file is deleted, no machine is touched
      case 'job-archive': {
        const id = String(payload.id ?? '')
        const job = await readJob(id)
        if (payload.archived === false) {
          if (!job.archivedAt) return { id, archivedAt: null }
          await updateJob(id, (j) => { const { archivedAt, ...rest } = j; return rest })
          return { id, archivedAt: null }
        }
        if (job.archivedAt) return { id, archivedAt: job.archivedAt }
        let progress = null
        try { progress = JSON.parse(await readFile(jobPath(id, 'progress.json'), 'utf8')) } catch { /* never run */ }
        // a start being priced (or a run that has not written progress.json yet) blocks too: see claimLaunch
        const why = archiveBlock(progress, await readLeases(), id, Date.now(), pidAlive) ?? (launchHeld(`job:${id}`) ? '正在拍：拍完或停下才能归档' : null)
        if (why) throw new Error(why)
        const at = new Date().toISOString()
        await updateJob(id, (j) => ({ ...j, archivedAt: at }))
        return { id, archivedAt: at }
      }
      case 'job-create': {
        const params = payload.params ?? {}
        const job = {
          id: newJobId(), createdAt: new Date().toISOString(), state: 'draft',
          prompt: String(payload.prompt ?? '').slice(0, 2000),
          ...(swapPeople(params) === 2 ? { prompt2: String(payload.prompt2 ?? '').slice(0, 2000) } : {}),
          params: swapParams(params),
          files: {},
        }
        return writeJob(job)
      }
      case 'job-get': {
        const job = await readJob(String(payload.id))
        let progress = null
        try { progress = JSON.parse(await readFile(jobPath(job.id, 'progress.json'), 'utf8')) } catch { /* not started */ }
        if (progress && progress.state === 'running' && progress.pid && !pidAlive(progress.pid)) progress = { ...progress, state: 'failed', error: progress.error ?? '运行进程意外退出（dsh 被关掉或电脑休眠）。可以继续，已完成的步骤会跳过。' }
        if (job.kind === 'drama') {
          if (progress && typeof progress.error === 'string') progress = { ...progress, error: await busyText(progress.error) }
          return { ...job, progress }
        }
        // a recast refused because another project holds the machine: same words as a short drama's, no stop button
        const held = progress && progress.state === 'failed' && typeof progress.error === 'string' ? await leaseRefusal(progress.error) : null
        if (progress && typeof progress.error === 'string') progress = held ? { ...progress, leaseRefused: true, error: held } : { ...progress, error: await busyText(progress.error) }
        if (progress && progress.stages) progress = { ...progress, stages: Object.fromEntries(await Promise.all(Object.entries(progress.stages).map(async ([k, v]) => [k, v && v.status === 'failed' && typeof v.detail === 'string' ? { ...v, detail: held ?? await busyText(v.detail) } : v]))) }
        // a run from before ledgers gets its line once, from progress.json (shisei.py works it out, nothing is priced here)
        if (progress && progress.state !== 'running' && !existsSync(jobPath(job.id, 'ledger.json'))) await runShisei(['job-ledger', jobPath(job.id)], {})
        let ledger = []
        try { ledger = JSON.parse(await readFile(jobPath(job.id, 'ledger.json'), 'utf8')) } catch { /* never ran */ }
        const cap = job.params?.budget == null ? 5 : Number(job.params.budget)   // 5: shisei.py DEFAULT_BUDGET
        // a whole dance counts the trial it was made from, like shisei.py budget_state (trial_spent)
        const from = job.params?.long?.from
        let trial = 0
        if (typeof from === 'string' && /^[A-Za-z0-9_-]{1,64}$/u.test(from) && from !== job.id) {
          try { trial = ledgerCost(JSON.parse(await readFile(jobPath(from, 'ledger.json'), 'utf8'))) } catch { /* no trial ledger */ }
        }
        const spent = Math.round((ledgerCost(ledger) + trial) * 100) / 100
        const budget = { cap, spent, left: Math.max(0, Math.round((cap - spent) * 100) / 100), ...(trial ? { trial } : {}) }
        return { ...job, progress, ledger, budget, precheck: await currentPrecheck(job.id), seeds: swapSeeds(job.params) }
      }
      case 'job-estimate': {
        const job = await readJob(String(payload.id))
        if (job.kind === 'drama') throw new Error('not a recast')
        const more = payload.more === undefined ? null : Number(payload.more)
        if (more !== null && !(Number.isInteger(more) && more >= 1 && more <= 6)) throw new Error('bad more')
        const r = await runShisei([...runPlacement({ machine: payload.machine }).pre, 'job-estimate', jobPath(job.id), ...(more ? ['--more', String(more)] : [])], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      // read-only: the machine this project's run would use now (the one the confirmation quotes), and who holds the others
      case 'machine-pick': {
        const job = await readJob(String(payload.id))
        const r = await runShisei(['--holder', job.id, ...runPlacement({ machine: payload.machine }).pre, 'machine-pick'], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'job-budget': {
        const job = await readJob(String(payload.id))
        if (job.kind === 'drama') throw new Error('not a recast')
        if (payload.cap !== undefined) {
          const cap = budgetCap(payload.cap)
          await updateJob(job.id, (j) => ({ ...j, params: { ...(j.params ?? {}), budget: cap } }))
        }
        const r = await runShisei(['job-ledger', jobPath(job.id)], {})
        if (!r.ok) throw new Error(r.error)
        return { budget: r.budget, ledger: r.ledger }
      }
      case 'job-precheck': {
        const job = await readJob(String(payload.id))
        if (job.kind === 'drama') throw new Error('not a recast')
        const out = await open(jobPath(job.id, 'run.log'), 'a')
        const child = spawnPy(['-I', SCRIPT, 'job-precheck', jobPath(job.id), ...(payload.force ? ['--force'] : [])],
          { env: pageEnv(await keyEnv(credentials)), detached: true, stdio: ['ignore', out.fd, out.fd] })
        child.unref()
        await out.close()
        return { started: true }
      }
      case 'job-start': {
        if (payload.confirmBilling !== true) throw new Error('job-start needs confirmBilling: true (the user confirmed the price)')
        const job = await readJob(String(payload.id))
        if (job.kind === 'drama') throw new Error('drama projects run per stage (drama-start)')
        if (!job.prep) throw new Error('finish the footage check first')
        return launchOnce(`job:${job.id}`, '这个项目正在拍', async () => {
          if (await runningNow(jobPath(job.id, 'progress.json'))) throw new Error('这个项目正在拍')
          const place = runPlacement(payload)
          const leases = await readLeases()
          const other = ownRunBlock(leases, job.id, Date.now(), pidAlive) || machineTaken(leases, place.machine, job.id, Date.now())
          if (other) throw new Error(other)
          const env = await keyEnv(credentials)
          // 「再拍几条」: the new takes are priced and checked against the cap before job.json changes
          const more = payload.more === undefined ? null : Number(payload.more)
          const est = await runShisei([...place.pre, 'job-estimate', jobPath(job.id), ...(more ? ['--more', String(more)] : [])], env)
          if (!est.ok) throw new Error(est.error)
          if (est.nothing) throw new Error('每一条都拍好了：在结果页点「重做」加几条。')
          const over = overBudgetMessage(est, true)
          if (over) throw new Error(over)
          if (more) await updateJob(job.id, (j) => addSwapTakes(j, more, payload.prompt === undefined ? null : payload.prompt, payload.prompt2 === undefined ? null : payload.prompt2))
          await rm(jobPath(job.id, 'stop'), { force: true })
          const out = await open(jobPath(job.id, 'run.log'), 'a')
          // detached: the run survives a dsh restart; the page re-attaches through progress.json
          const child = spawnPy(['-I', SCRIPT, ...place.pre, 'job-run', jobPath(job.id), '--confirm-billing', ...place.post],
            { env: pageEnv(env), detached: true, stdio: ['ignore', out.fd, out.fd] })
          child.unref()
          followMachine(child)
          await out.close()
          await updateJob(job.id, (j) => ({ ...j, state: 'running', startedAt: j.startedAt ?? new Date().toISOString() }))
          return { started: true, pid: child.pid, child }
        })
      }
      case 'job-stop': {
        const job = await readJob(String(payload.id))
        await writeFile(jobPath(job.id, 'stop'), String(Date.now()))
        return { stopping: true }
      }
      case 'job-pick': {
        const seed = String(payload.seed ?? '')
        if (!/^\d{4}$/u.test(seed)) throw new Error('bad seed')
        const job = await readJob(String(payload.id))
        // three panels with the white model, two for a keep-background job (shisei.py job_composite_args)
        const r = await runShisei(['job-pick', jobPath(job.id), '--seed', seed], {})
        if (!r.ok) throw new Error(r.error)
        return updateJob(job.id, (j) => ({ ...j, pick: seed, showcaseAt: Date.now() }))
      }
      case 'job-sides': {
        // two people: the left-hand and right-hand characters change places (pictures and descriptions), before any take
        const job = await readJob(String(payload.id))
        if (swapPeople(job.params) !== 2) throw new Error('only a two-person recast has sides')
        if (existsSync(jobPath(job.id, 'progress.json'))) throw new Error('已经开始拍了：左右要在开机前对调')
        const { front, front2 } = job.files ?? {}
        if (!front || !front2) throw new Error('两边的全身图都上传了才能对调')
        // the pictures change names too, so a later upload of one side never overwrites the other
        const next = swapSideFiles(job.files)
        await rename(jobPath(job.id, front), jobPath(job.id, 'sides.tmp'))
        await rename(jobPath(job.id, front2), jobPath(job.id, next.front))
        await rename(jobPath(job.id, 'sides.tmp'), jobPath(job.id, next.front2))
        return updateJob(job.id, (j) => ({ ...j, prompt: j.prompt2 ?? '', prompt2: j.prompt ?? '', files: { ...(j.files ?? {}), ...next } }))
      }
      case 'probe': {
        const job = await readJob(String(payload.id))
        if (!job.files?.source) throw new Error('no source video uploaded')
        const r = await runShisei(['probe', jobPath(job.id, job.files.source), '--strip', jobPath(job.id, 'strip.png')], {})
        if (!r.ok) throw new Error(r.error)
        return updateJob(job.id, (j) => ({ ...j, probe: { duration: r.duration, width: r.width, height: r.height, fps: r.fps, segment: r.segment_seconds, maxSeconds: r.max_seconds, longMax: r.long_max_seconds, longShot: r.long_shot_seconds } }))
      }
      case 'prep': {
        const job = await readJob(String(payload.id))
        if (!job.files?.source) throw new Error('no source video uploaded')
        const start = Math.max(0, Number(payload.start) || 0)
        const cropX = Number.isFinite(Number(payload.cropX)) && payload.cropX !== null ? Math.min(1, Math.max(0, Number(payload.cropX))) : 0.5
        const seconds = payload.seconds === undefined ? null : Number(payload.seconds)
        if (seconds !== null && !(seconds >= 2 && seconds <= 15.5)) throw new Error('clip length must be 2..15 s')
        // 整段换: the clip prepared here is the trial; the whole range is kept for 「照这条拍完整段」 after the user picks a take
        const whole = longRange(payload.whole, job.probe?.duration)
        // a keep-background job keeps the whole frame (no 3:4 crop)
        const r = await runShisei(['prep', jobPath(job.id, job.files.source), jobPath(job.id, 'dance.mp4'), '--start', String(start),
          ...(swapKeepBg(job.params) ? ['--keep-frame'] : ['--crop-x', String(cropX)]),
          ...(seconds !== null ? ['--seconds', String(seconds)] : [])], {})
        if (!r.ok) throw new Error(r.error)
        return updateJob(job.id, (j) => {
          const { long: _old, ...params } = j.params ?? {}
          return { ...j, params: { ...params, frames: r.frames, ...(whole ? { long: whole } : {}) }, prep: { start, cropX, file: 'dance.mp4', size: r.size, at: Date.now() } }
        })
      }
      case 'job-long': {
        // 「照这条拍完整段」: a new project with the whole range, the picked take's seed and description; priced and
        // started from its own confirm page like any recast
        const parent = await readJob(String(payload.id))
        if (parent.kind === 'drama') throw new Error('not a recast')
        if (parent.params?.long?.job) {
          try { return await readJob(parent.params.long.job) } catch { /* removed: make it again */ }
        }
        const seed = String(payload.seed ?? '')
        if (!/^\d{4}$/u.test(seed)) throw new Error('bad seed')
        if (!existsSync(jobPath(parent.id, `seed_${seed}.mp4`))) throw new Error('这一条还没拍好')
        const child = longJob(parent, seed, newJobId())
        await writeJob(child)
        try {
          for (const name of Object.values(child.files)) await copyFile(jobPath(parent.id, name), jobPath(child.id, name))
          const lg = child.params.long
          const r = await runShisei(['prep', jobPath(child.id, child.files.source), jobPath(child.id, 'dance.mp4'), '--long',
            '--start', String(lg.start), '--seconds', String(lg.seconds),
            ...(swapKeepBg(child.params) ? ['--keep-frame'] : ['--crop-x', String(parent.prep?.cropX ?? 0.5)])], {})
          if (!r.ok) throw new Error(r.error)
          const made = await updateJob(child.id, (j) => ({ ...j, params: { ...j.params, frames: r.frames, long: { ...j.params.long, shots: r.shots.length } },
            prep: { start: lg.start, cropX: parent.prep?.cropX ?? 0.5, file: 'dance.mp4', size: r.size, at: Date.now() } }))
          await updateJob(parent.id, (j) => ({ ...j, params: { ...j.params, long: { ...j.params.long, job: child.id } } }))
          return made
        } catch (e) {
          await rm(jobPath(child.id), { recursive: true, force: true })
          throw e
        }
      }
      // ---------- drama projects (kind: drama). dsh writes story.json through the skill; the page reviews and runs stages.
      // ---- series: episodes that share regulars approved once (shisei.py series-*; docs/SERIES.md)
      case 'series-list':
        return ok(await runShisei(['series-list'], {}))
      case 'series-get':
        return ok(await runShisei(['series-show', seriesId(payload.id), ...(payload.before ? ['--before', jobId(payload.before)] : [])], {}))
      case 'series-create':
        return ok(await runShisei(['series-new', '--title', pageText(payload.title, 60, true), '--logline', pageText(payload.logline, 400)], {}))
      case 'series-update': {
        const argv = ['series-set', seriesId(payload.id)]
        for (const [k, n] of [['title', 60], ['logline', 400], ['rules', 600]]) if (typeof payload[k] === 'string') argv.push(`--${k}`, pageText(payload[k], n, k === 'title'))
        return ok(await runShisei(argv, {}))
      }
      case 'series-add':
      case 'series-remove':
        return ok(await runShisei([endpoint, seriesId(payload.id), jobPath(jobId(payload.job))], {}))
      case 'series-order': {
        const order = Array.isArray(payload.order) ? payload.order.map(jobId) : []
        return ok(await runShisei(['series-order', seriesId(payload.id), '--order', order.join(',')], {}))
      }
      case 'series-counts':
        return ok(await runShisei(['series-counts', seriesId(payload.id), jobId(payload.job), payload.value === false ? 'no' : 'yes'], {}))
      case 'series-summary':
        return ok(await runShisei(['series-summary', seriesId(payload.id), jobId(payload.job), '--text', pageText(payload.text, 300), '--state', payload.state === 'set' ? 'set' : 'draft'], {}))
      case 'series-cast-add':
        return ok(await runShisei(['series-cast-add', seriesId(payload.id), jobPath(jobId(payload.job)), castId(payload.cast)], {}))
      // 定声: one of the character's own voice tracks becomes the reference its takes are made with (the user's choice)
      case 'series-voice': {
        const argv = ['series-voice', seriesId(payload.id), castId(payload.cast)]
        if (payload.clear === true) argv.push('--clear')
        else if (payload.upload) argv.push(...voiceUpload(join(SERIES_DIR, seriesId(payload.id)), payload.upload))
        else argv.push('--job', jobPath(jobId(payload.job)), '--file', voiceTrack(payload.file), ...shotArg(payload))
        return ok(await runShisei(argv, {}))
      }
      case 'drama-voice': {
        const argv = ['drama-voice', jobPath(jobId(payload.id)), castId(payload.cast)]
        if (payload.clear === true) argv.push('--clear')
        else if (payload.upload) argv.push(...voiceUpload(jobPath(jobId(payload.id)), payload.upload))
        else argv.push('--file', voiceTrack(payload.file), ...shotArg(payload))
        return ok(await runShisei(argv, {}))
      }
      // 换定妆照: the regular takes the pictures the user approved for it in one episode (also a new regular's first ones)
      case 'series-cast-update':
        return ok(await runShisei(['series-cast-update', seriesId(payload.id), castId(payload.cast), '--job', jobPath(jobId(payload.job))], {}))
      // a regular added on the series page from a description (and the photo cast-upload saved): pictures come later
      case 'series-cast-new': {
        const argv = ['series-cast-new', seriesId(payload.id), '--name', pageText(payload.name, 40, true),
          '--name-en', pageText(payload.nameEn, 40), '--age', String(Number.parseInt(payload.age, 10) || ''),
          '--look', pageText(payload.look, 600)]
        if (pageText(payload.outfit, 300)) argv.push('--outfit', pageText(payload.outfit, 300))
        if (pageText(payload.voice, 200)) argv.push('--voice', pageText(payload.voice, 200))
        if (payload.lookYoung === true) argv.push('--look-young')
        if (payload.photo) {
          if (!/^\.photo_upload\.(?:png|jpe?g|webp)$/u.test(String(payload.photo))) throw new Error('bad photo')
          argv.push('--photo', join(SERIES_DIR, seriesId(payload.id), String(payload.photo)))
        }
        return ok(await runShisei(argv, {}))
      }
      case 'series-cast-retire':
        return ok(await runShisei(['series-cast-retire', seriesId(payload.id), castId(payload.cast), ...(payload.back === true ? ['--back'] : [])], {}))
      // archive / restore a whole series: every episode with it; refused while any episode is being shot
      case 'series-archive': {
        const sid = seriesId(payload.id)
        if (payload.archived !== false) {
          const s = ok(await runShisei(['series-show', sid], {}))
          const leases = await readLeases()
          for (const e of s.episodes ?? []) {
            let progress = null
            try { progress = JSON.parse(await readFile(jobPath(e.job, 'progress.json'), 'utf8')) } catch { /* never run */ }
            const why = archiveBlock(progress, leases, e.job, Date.now(), pidAlive) ?? (launchHeld(`job:${e.job}`) ? '正在拍：拍完或停下才能归档' : null)
            if (why) throw new Error(`第 ${e.n} 集：${why}`)
          }
        }
        return ok(await runShisei(['series-archive', sid, ...(payload.archived === false ? ['--restore'] : [])], {}))
      }
      // a new episode: the project, the regulars with their approved pictures, then the director writes from the recap
      case 'series-episode-new': {
        const sid = seriesId(payload.id)
        const idea = pageText(payload.idea, 4000)
        const argv = ['series-episode-new', sid, '--idea', idea, '--quality', payload.quality === '768p' ? '768p' : '576p',
          '--seeds', String(Math.min(3, Math.max(1, Number(payload.seeds) || 2)))]
        if (typeof payload.title === 'string' && payload.title.trim()) argv.push('--title', pageText(payload.title, 60))
        if (Array.isArray(payload.cast)) argv.push('--cast', payload.cast.map(castId).join(','))
        const r = ok(await runShisei(argv, {}))
        let director = { sent: false }
        if (payload.start !== false) {
          try {
            const why = directorMissing()
            if (why) throw new Error(why)
            const s = ok(await runShisei(['series-show', sid, '--before', r.job], {}))
            await sendToDirector(await readJob(r.job), WRITE_SCRIPT_SERIES(idea.trim() || s.title, payload.recap === false ? '' : s.recap))
            director = { sent: true }
          } catch (e) { director = { sent: false, error: e instanceof Error ? e.message : String(e) } }
        }
        return { id: r.job, dir: r.dir, n: r.n, cast: r.cast, carried: r.carried, director }
      }
      case 'drama-create': {
        const title = String(payload.title ?? '').trim().slice(0, 60)
        if (!title) throw new Error('title is required')
        const quality = payload.quality === '768p' ? '768p' : '576p'
        const seeds = Math.min(3, Math.max(1, Number(payload.seeds) || 2))
        const idea = String(payload.idea ?? '').slice(0, 4000)
        externalMentions(idea)   // malformed outside tokens are refused before a project exists
        const r = await runShisei(['drama-new', '--title', title, '--idea', idea, '--quality', quality, '--seeds', String(seeds)], {})
        if (!r.ok) throw new Error(r.error)
        // what the home composer @-mentioned (library, other projects, uploads) is copied into the new project now; the
        // story does not exist yet, so the entries wait in adopted.json and the idea keeps tokens of this project
        const ad = await adoptTokens(await readJob(r.job), idea)
        if (ad.adopted.length) await updateJob(r.job, (j) => ({ ...j, prompt: ad.text }))
        // the idea is already given: the director starts writing now. A failed send does not undo the project; the page
        // shows the reason and a retry.
        let director = { sent: false }
        // with a reference video the page uploads it next and calls study-start, which reads it and then starts the director
        if (payload.study === true) director = { sent: false, waiting: 'study' }
        else if (payload.start !== false) {
          try {
            const why = directorMissing()
            if (why) throw new Error(why)
            await sendToDirector(await readJob(r.job), WRITE_SCRIPT(ad.text.trim() || title))
            director = { sent: true }
          } catch (e) { director = { sent: false, error: e instanceof Error ? e.message : String(e) } }
        }
        return { id: r.job, dir: r.dir, adopted: ad.adopted, director }
      }
      case 'drama-get': {
        const job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        const r = await runShisei(['drama-status', jobPath(job.id)], {})
        if (!r.ok) throw new Error(r.error)
        const { ok: _ok, ...status } = r
        let story = null
        let review = { assets: {}, keyframes: {}, picks: {} }
        try { story = JSON.parse(await readFile(jobPath(job.id, 'story.json'), 'utf8')) } catch { /* not written yet */ }
        try { review = { ...review, ...JSON.parse(await readFile(jobPath(job.id, 'review.json'), 'utf8')) } } catch { /* nothing reviewed */ }
        let check = {}
        try { check = JSON.parse(await readFile(jobPath(job.id, 'check.json'), 'utf8')) } catch { /* not checked */ }
        // the shot-by-shot check of each take, only while the take is still the file that was checked (a re-shot or
        // restored take drops it, so its pieces and edit buttons never point at other seconds)
        let shots = {}
        try {
          const all = JSON.parse(await readFile(jobPath(job.id, 'shots.json'), 'utf8'))
          for (const [name, v] of Object.entries(all ?? {})) {
            if (!/^take_[\w-]+_\d+\.mp4$/u.test(name)) continue
            try { if (Math.abs((await stat(jobPath(job.id, name))).mtimeMs / 1000 - Number(v?.mtime)) < 0.01) shots[name] = v } catch { /* take gone */ }
          }
        } catch { /* not checked shot by shot */ }
        // only verdicts on the files as they are now: a remade first frame drops its old one (shisei.py current_precheck)
        const precheck = status.precheck ?? {}
        let cut = null
        try { cut = JSON.parse(await readFile(jobPath(job.id, 'cut.json'), 'utf8')) } catch { /* not cut */ }
        let changes = []
        try { changes = JSON.parse(await readFile(jobPath(job.id, 'changes.json'), 'utf8')) } catch { /* none */ }
        let ledger = []
        try { ledger = JSON.parse(await readFile(jobPath(job.id, 'ledger.json'), 'utf8')) } catch { /* no GPU run yet */ }
        const study = await readStudy(job)
        let progress = status.progress
        if (progress && progress.state === 'running' && progress.pid && !pidAlive(progress.pid)) progress = { ...progress, state: 'failed', error: progress.error ?? '运行进程意外退出（dsh 被关掉或电脑休眠）。可以继续，已完成的会跳过。' }
        // a start refused because another project holds the machine: nothing of ours is on, so no billing note and no
        // stop button (it would be someone else's machine); the holder is named by its title, not its folder id
        if (progress && progress.state === 'failed' && typeof progress.error === 'string') {
          const held = await leaseRefusal(progress.error)
          progress = held ? { ...progress, leaseRefused: true, error: held } : { ...progress, leaseRefused: !!progress.refused, error: pageError(progress.error) }
        }
        // before there is a story: what the director is doing, so the page shows progress or the failure, not a dead panel
        let writer = null
        if (!story || status.story_error) {
          writer = { started: !!job.directorSession, missing: directorMissing() }
          if (job.directorSession && sessionService) {
            try { const snap = await sessionService.inspect(job.directorSession); Object.assign(writer, directorState(snap?.events), { last: chatLines(snap?.events, 1)[0] ?? null }) } catch { /* session gone */ }
          }
        }
        const handoff = pendingHandoff(review, await handoffAt(job)).map(({ kind, key }) => ({ kind, key }))
        return { ...job, dir: jobPath(job.id), status: { ...status, progress }, story, review, check, shots, precheck, cut, changes, ledger, budget: status.budget, director: !!sessionService, writer, study, handoff }
      }
      case 'drama-review': {
        const job = await readJob(String(payload.id))
        const kind = String(payload.kind)
        if (!['assets', 'keyframes', 'pick'].includes(kind)) throw new Error('bad review kind')
        const key = String(payload.key ?? '')
        const value = String(payload.value ?? '')
        if (!/^[\w.-]{1,80}$/u.test(key) || !/^[\w]{1,12}$/u.test(value)) throw new Error('bad review item')
        const note = String(payload.note ?? '').replace(/\s+/gu, ' ').trim().slice(0, 300)
        const argv = ['drama-review', jobPath(job.id), kind, key, value]
        if (note) argv.push('--note', note)
        const r = await runShisei(argv, {})
        if (!r.ok) throw new Error(r.error)
        // A rejection with a reason also goes to the director. The verdict above is saved whether or not this works.
        // hold: the page collects rejections and hands them over together (drama-handoff)
        let director = { sent: false, held: payload.hold === true && value === 'reject' && !!note }
        if (value === 'reject' && note && kind !== 'pick' && payload.hold !== true) {
          try {
            await sendToDirector(job, rejectMessage({ kind, key, label: String(payload.label ?? '').slice(0, 40), note }))
            director = { sent: true }
          } catch (e) { director = { sent: false, error: e instanceof Error ? e.message : String(e) } }
        }
        return { review: r.review, director }
      }
      // the rejections held on the page, as one message to the director; labels are the page's words for each picture
      case 'drama-handoff': {
        const job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        let review = {}
        try { review = JSON.parse(await readFile(jobPath(job.id, 'review.json'), 'utf8')) } catch { /* nothing reviewed */ }
        const items = pendingHandoff(review, await handoffAt(job))
        if (!items.length) return { sent: 0 }
        let story = null
        try { story = JSON.parse(await readFile(jobPath(job.id, 'story.json'), 'utf8')) } catch { /* no story */ }
        for (const it of items) it.label = pictureLabel(story, it.kind, it.key)
        await sendToDirector(job, handoffMessage(items))
        await writeFile(jobPath(job.id, 'handoff.json'), JSON.stringify({ at: Math.max(...items.map((x) => x.at)) }))
        return { sent: items.length }
      }
      // what the director is doing, for the spending button: working, asking (a question card or a reply ending in ?)
      case 'director-status': {
        const job = await readJob(String(payload.id))
        if (!job.directorSession || !sessionService) return { busy: false, asking: false }
        const snap = await sessionService.inspect(job.directorSession)
        const st = directorState(snap?.events)
        const last = chatLines(snap?.events, 1)[0]
        const asks = directorAsks.pending(job.id).length
        const question = !st.busy && last?.who === 'dsh' && endsWithQuestion(last.text)
        return { busy: !!st.busy, since: st.since, asking: asks > 0 || question, asks }
      }
      case 'drama-budget': {
        const job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        if (payload.cap !== undefined) {
          const cap = budgetCap(payload.cap)
          await updateJob(job.id, (j) => ({ ...j, params: { ...(j.params ?? {}), budget: cap } }))
        }
        const r = await runShisei(['drama-budget', jobPath(job.id)], {})
        if (!r.ok) throw new Error(r.error)
        return { budget: r.budget, ledger: r.ledger }
      }
      case 'drama-estimate': {
        const job = await readJob(String(payload.id))
        const stage = String(payload.stage)
        if (!['assets', 'keyframes', 'takes'].includes(stage)) throw new Error('bad stage')
        const r = await runShisei([...runPlacement({ machine: payload.machine }).pre, 'drama-estimate', jobPath(job.id), stage, ...dramaScope(payload), ...assumeMissing(payload.assumeMissing)], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      // ---------- references: the 参考 panel, drops and 「用作参考」. These change project data only; spending stays
      // with the 拍 button and its start-machine confirmation.
      // ---------- 对标片 (reference video): read locally by shisei.py drama-study*; only its structure is kept ----------
      case 'study-start': {
        // after the page uploaded the video (POST upload?kind=study): read it (cuts, contact sheets, tone; seconds),
        // start the image model on the sheets in the background, and, for a new project, hand the director the script
        let job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        const up = job.files?.study
        if (!up) throw new Error('no reference video uploaded')
        const name = String(payload.name ?? '').replace(/[\s\n]+/gu, ' ').trim().slice(0, 80)
        const r = await runShisei(['drama-study', jobPath(job.id), '--video', jobPath(job.id, up), ...(name ? ['--name', name] : [])], {})
        await rm(jobPath(job.id, up), { force: true })
        job = await updateJob(job.id, (j) => { const { study: _s, ...files } = j.files ?? {}; return { ...j, files } })
        if (!r.ok) throw new Error(r.error)
        await spawnStudyLook(job)
        let director = { sent: false }
        if (payload.write === true) {
          try {
            const why = directorMissing()
            if (why) throw new Error(why)
            await sendToDirector(job, WRITE_SCRIPT_STUDY(String(job.prompt ?? '').trim() || String(job.title ?? '')))
            director = { sent: true }
          } catch (e) { director = { sent: false, error: e instanceof Error ? e.message : String(e) } }
        }
        return { study: r.study, director }
      }
      case 'study-estimate': {
        // the rough price of the reference's whole rhythm against the cap (before there is a script; the 拍 button
        // prices each real run). Needs the machine's price: the compshare keys, read here.
        const job = await readJob(String(payload.id))
        const r = await runShisei(['drama-study-estimate', jobPath(job.id)], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'study-look': {
        const job = await readJob(String(payload.id))
        await spawnStudyLook(job, !!payload.force)
        return { started: true }
      }
      case 'study-set': {
        // the user's pick on the 对标 tab (fixed choices; '' clears). runShisei drops DSH_SHISEI_BRIDGE: it counts as theirs
        const job = await readJob(String(payload.id))
        const field = String(payload.field ?? '')
        if (!STUDY_FIELDS.includes(field)) throw new Error('bad field')
        const r = await runShisei(['drama-study-set', jobPath(job.id), String(Math.trunc(Number(payload.n)) || 0), `--${field}=${String(payload.value ?? '').slice(0, 8)}`], {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'study-cut': {
        const job = await readJob(String(payload.id))
        const argv = payload.merge !== undefined ? ['--merge', String(Math.trunc(Number(payload.merge)) || 0)]
          : ['--split', String(Math.trunc(Number(payload.split)) || 0), '--at', String(Number(payload.at) || 0)]
        const r = await runShisei(['drama-study-cut', jobPath(job.id), ...argv], {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'study-apply': {
        // a stretch of the reference dropped on one of our segments: the director is asked to re-plan that segment's
        // shots on it (what changes, what is re-shot, the price) and to wait for the user. Nothing is generated here.
        const job = await readJob(String(payload.id))
        const unit = String(payload.unit ?? '')
        if (!/^\d{1,2}-\d{1,2}$/u.test(unit)) throw new Error('bad unit')
        const from = Math.trunc(Number(payload.from))
        const to = Math.trunc(Number(payload.to ?? payload.from))
        if (!(from >= 1 && to >= from && to <= 999)) throw new Error('bad range')
        const { story } = await mentionCtx(job)
        const u = storyUnits(story).find((x) => x.id === unit)
        if (!u) throw new Error('这个项目没有这一段')
        const range = from === to ? `${from}` : `${from}-${to}`
        const label = from === to ? `对标 #${from}` : `对标 #${from}–#${to}`
        const note = String(payload.note ?? '').replace(/\s+/gu, ' ').trim().slice(0, 200)
        const text = `@[段 ${u.label}](unit:${unit}) 照 @[${label}](study:${range}) 的镜头和节奏改这一段。${note ? `还想：${note}` : ''}\n` +
          '先告诉我打算怎么改、哪些要重拍、大约多少钱；不要自己开机。改好后用 drama-study-link 记下这一段对标的范围。'
        const ctx = { ...(await mentionCtx(job)), scope: 'frame' }
        const res = resolveMentions(text, ctx.story, ctx)
        return { director: { sent: true, sessionId: await sendToDirector(job, text, mentionNote(res, ctx.story, jobPath(job.id))) }, label, unit: u.label }
      }
      case 'refs':
        return refsFor(payload.id ? await readJob(String(payload.id)) : null)
      case 'ref-adopt': {
        const job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        return adoptInto(job, refSource(payload.src))
      }
      case 'ref-place': {
        const job = await readJob(String(payload.id))
        const unit = String(payload.unit ?? '')
        if (!/^\d{1,2}-\d{1,2}$/u.test(unit)) throw new Error('bad unit')
        let add = payload.add === undefined ? null : String(payload.add)
        const swap = payload.swap === undefined ? null : String(payload.swap)
        if (payload.src) {   // an outside asset dropped on a first frame: copied in first (idempotent), then placed
          // nothing is copied for a dry run: only what re-making this first frame costs (the copy then waits for review)
          if (payload.dryRun) { refSource(payload.src); return { external: true, price: await priceOf(job, 'keyframes', { shots: [unit], assumeMissing: [`kf_${unit}.png`] }) } }
          const a = await adoptInto(job, refSource(payload.src))
          add = `${a.kind}:${a.id}`
        }
        if (add !== null && !/^(cast|prop|scene):[a-z][a-z0-9_]{0,23}$/u.test(add)) throw new Error('bad reference')
        if (swap !== null && !/^(scene|prop):[a-z][a-z0-9_]{0,23}=(scene|prop):[a-z][a-z0-9_]{0,23}$/u.test(swap)) throw new Error('bad swap')
        const note = String(payload.note ?? '').replace(/\s+/gu, ' ').trim().slice(0, 200)
        const argv = ['drama-ref', jobPath(job.id), unit, ...(add ? ['--add', add] : []), ...(swap ? ['--swap', swap] : []), ...(note ? ['--why', note] : []), ...(payload.dryRun ? ['--dry-run'] : [])]
        const r = await runShisei(argv, {})
        if (!r.ok) throw new Error(r.error)
        if (payload.dryRun) return { ...r, price: await priceOf(job, 'keyframes', { shots: [unit], assumeMissing: [`kf_${unit}.png`] }) }
        let director = { sent: false }
        if (note) {
          try { await sendToDirector(job, `@[段 ${r.label}](unit:${unit}) ${r.what}。还想改：${note}\n先告诉我你打算怎么改、大约多少钱；不要自己开机。`); director = { sent: true } } catch (e) { director = { sent: false, error: e instanceof Error ? e.message : String(e) } }
        }
        return { ...r, director }
      }
      case 'wardrobe': {
        const job = await readJob(String(payload.id))
        const cast = String(payload.cast ?? '')
        if (!ASSET_ID.test(cast)) throw new Error('bad character id')
        const { story } = await mentionCtx(job)
        const c = (story?.cast ?? []).find((x) => x.id === cast)
        if (!c) throw new Error('这个项目没有这个演员')
        // picture: true = 「照这张图画」 (drama-wardrobe --picture, the user's choice on the page); w.picture is the checked
        // argv for it, or the call was refused in wardrobeSource (look-young, typed clothes)
        const w = await wardrobeSource(c, payload)
        const note = String(payload.note ?? '').replace(/\s+/gu, ' ').trim().slice(0, 120)
        const r = await runShisei(['drama-wardrobe', jobPath(job.id), cast, `--outfit=${w.outfit}`, '--source', w.source, ...(w.picture ?? []), ...(note ? [`--note=${note}`] : []), ...(payload.dryRun ? ['--dry-run'] : [])], {})
        if (!r.ok) throw new Error(frameError(r.error))
        return payload.dryRun ? { ...r, price: await priceOf(job, 'assets', { assumeMissing: r.redraw }) } : r
      }
      // ---------- the user's first-frame instructions (lib/frame.js). Project data only: the first frame goes back to
      // 待拍 and the 拍 button does the run. runShisei drops DSH_SHISEI_BRIDGE, so these are the user's clicks.
      case 'crop': {
        const job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        const r = await runShisei(cropArgv(jobPath(job.id), payload), {})
        if (!r.ok) throw new Error(frameError(r.error))
        return payload.dryRun ? { ...r, price: await priceOf(job, 'keyframes', { shots: [r.unit], assumeMissing: [`kf_${r.unit}.png`] }) } : r
      }
      case 'staging': {
        const job = await readJob(String(payload.id))
        if (job.kind !== 'drama') throw new Error('not a drama project')
        const r = await runShisei(stagingArgv(jobPath(job.id), payload), {})
        if (!r.ok) throw new Error(frameError(r.error))
        return payload.dryRun ? { ...r, price: await priceOf(job, 'keyframes', { shots: [r.unit], assumeMissing: [`kf_${r.unit}.png`] }) } : r
      }
      case 'cast-flag': {
        const job = await readJob(String(payload.id))
        const cast = String(payload.cast ?? '')
        if (!ASSET_ID.test(cast) || typeof payload.lookYoung !== 'boolean') throw new Error('bad flag')
        const r = await runShisei(['drama-cast-flag', jobPath(job.id), cast, '--look-young', payload.lookYoung ? 'on' : 'off'], {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'drama-start': {
        if (payload.confirmBilling !== true) throw new Error('drama-start needs confirmBilling: true (the user confirmed the price)')
        const job = await readJob(String(payload.id))
        const stage = String(payload.stage)
        if (!['assets', 'keyframes', 'takes'].includes(stage)) throw new Error('bad stage')
        const extra = dramaScope(payload)
        return launchOnce(`job:${job.id}`, '这个项目正在拍', async () => {
          if (await runningNow(jobPath(job.id, 'progress.json'))) throw new Error('这个项目正在拍')
          const place = runPlacement(payload)
          const leases = await readLeases()
          const other = ownRunBlock(leases, job.id, Date.now(), pidAlive) || machineTaken(leases, place.machine, job.id, Date.now())
          if (other) throw new Error(other)
          const env = await keyEnv(credentials)
          // The run is detached, so a refusal would only reach run.log: ask for the same estimate first and refuse here.
          const est = await runShisei([...place.pre, 'drama-estimate', jobPath(job.id), stage, ...extra], env)
          if (!est.ok) throw new Error(est.error)
          const stopAfter = payload.stopAfter === true
          const over = overBudgetMessage(est, false, stopAfter)
          if (over) throw new Error(over)
          await rm(jobPath(job.id, 'stop'), { force: true })
          const out = await open(jobPath(job.id, 'run.log'), 'a')
          const child = spawnPy(['-I', SCRIPT, ...place.pre, 'drama-run', jobPath(job.id), stage, '--confirm-billing', ...(stopAfter ? ['--stop-after'] : []), ...extra, ...place.post],
            { env: pageEnv(env), detached: true, stdio: ['ignore', out.fd, out.fd] })
          child.unref()
          followMachine(child)
          await out.close()
          return { started: true, pid: child.pid, child }
        })
      }
      case 'drama-precheck': {
        const job = await readJob(String(payload.id))
        const kinds = (Array.isArray(payload.kind) ? payload.kind : ['keyframes', 'takes']).filter((k) => k === 'keyframes' || k === 'takes')
        const out = await open(jobPath(job.id, 'run.log'), 'a')
        const child = spawnPy(['-I', SCRIPT, 'drama-precheck', jobPath(job.id), '--kind', ...kinds, ...(payload.force ? ['--force'] : [])],
          { env: pageEnv(await keyEnv(credentials)), detached: true, stdio: ['ignore', out.fd, out.fd] })
        child.unref()
        await out.close()
        return { started: true }
      }
      case 'vision-test': {
        // the result is remembered for dsh's model: 'auto' uses it only after it passed (lib/vision.js)
        const { vision: before } = await visionView()
        const r = await runShisei(['vision-test'], await keyEnv(credentials))
        if (before.source === 'dsh' && before.director) await recordVerified(before.director, r.ok && r.sees_images === true)
        return { ...r, ...(await visionView()) }
      }
      // ---- 开始 (onboarding): its own progress file; keys stay in the credential store
      case 'onboarding': {
        const file = join(SHISEI_HOME, 'onboarding.json')
        let cur = {}
        try { cur = JSON.parse(await readFile(file, 'utf8')) } catch { /* first time */ }
        if (payload.set && typeof payload.set === 'object') {
          cur = { ...cur, ...onboardingPatch(payload.set), at: Date.now() }
          await mkdir(SHISEI_HOME, { recursive: true })
          await writeFile(file, JSON.stringify(cur))
        }
        return { ...cur, ...(await visionView()), setup: await setupProgress() }
      }
      case 'set-vision-source': {
        const v = String(payload.source ?? '')
        if (!['auto', 'dsh', 'key'].includes(v)) throw new Error('bad vision source')
        const r = await runShisei(['config', 'set', 'vision_source', v], {})
        if (!r.ok) throw new Error(r.error)
        return visionView()
      }
      case 'set-vision-key': {
        const v = typeof payload.key === 'string' ? payload.key.trim() : ''
        if (!/^[\x21-\x7E]{8,300}$/u.test(v)) throw new Error('vision key has unexpected characters or length')
        await credentials.set(await visionKeyName(), v)
        return visionView()
      }
      case 'machine-plan': {
        const argv = ['machine-plan']
        if (payload.zone) argv.push('--zone', zoneArg(payload.zone))
        if (payload.base === true) argv.push('--base')   // the official base image (the page no longer offers it; kept for a caller that asks)
        const r = await runShisei(argv, await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'machine-update-plan': {
        // 更新环境 on the machines page: the quote only (machine-create --instance without --confirm-billing never starts)
        const r = await runShisei(['machine-create', '--instance', instanceArg(payload.instance)], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'machine-create': {
        // creates one instance (or, with instance, reruns the setup on a registered one) and starts billing: only after
        // the user confirmed the quote on the page
        if (payload.confirmBilling !== true) throw new Error('machine-create needs confirmBilling: true (the user confirmed the price)')
        const argv = ['machine-create', '--confirm-billing']
        if (payload.instance !== undefined) argv.push('--instance', instanceArg(payload.instance))
        if (payload.zone) argv.push('--zone', zoneArg(payload.zone))
        if (payload.fresh === true) argv.push('--new')
        if (payload.base === true) argv.push('--base')
        return launchOnce('setup', '已经在准备一台机器了', async () => {
          const now = await setupProgress()
          if (now && now.state === 'running') throw new Error('已经在准备一台机器了')
          await mkdir(join(SHISEI_HOME, 'setup'), { recursive: true })
          const out = await open(join(SHISEI_HOME, 'setup', 'run.log'), 'a')
          const logFile = join(SHISEI_HOME, 'setup', 'run.log')
          const before = (await stat(logFile).catch(() => null))?.size ?? 0
          const child = spawnPy(['-I', ...PY_CACHE, SCRIPT, ...argv],
            { env: pageEnv(await keyEnv(credentials)), detached: true, stdio: ['ignore', out.fd, out.fd] })
          child.unref()
          await out.close()
          // a refusal before anything is made (no stock, over the limit) exits within seconds and writes no progress:
          // say why here, or the page just stays on the quote (真机 2026-11-12: sold out between quote and click)
          const t0 = Date.now() / 1000
          let code = null
          child.once('exit', (c) => { code = c })
          for (let waited = 0; waited < SETUP_REFUSAL_MS && code === null; waited += 250) {
            const p = await setupProgress()
            if (p && (p.started_at || 0) >= t0 - 1) break          // it began: the page follows its progress
            await new Promise((ok) => setTimeout(ok, 250))
          }
          const began = await setupProgress()
          if (code !== null && code !== 0 && !(began && (began.started_at || 0) >= t0 - 1)) {
            throw new Error(pageError(await setupRefusal(logFile, before)))   // refused before anything was made
          }
          return { started: true, pid: child.pid, child }
        })
      }
      case 'machine-setup':
        return { setup: await setupProgress() }
      case 'templates': {
        const r = await runShisei(['drama-templates'], {})
        if (!r.ok) throw new Error(r.error)
        // each template with the user's own copies of it (newest first), so 拍第一部 opens one instead of copying again
        const copies = await templateCopies()
        return { ...r, templates: (r.templates ?? []).map((x) => ({ ...x, copies: copies.get(x.id) ?? [] })) }
      }
      case 'drama-from-template': {
        const id = String(payload.template ?? '')
        if (!/^[a-z][a-z0-9_-]{0,31}$/u.test(id)) throw new Error('bad template')
        // one copy at a time per template: a double click or a second tab gets the same project
        if (fromTemplate.has(id)) return fromTemplate.get(id)
        const p = (async () => {
          if (!payload.fresh) {
            const have = (await templateCopies()).get(id)
            if (have?.length) return { id: have[0].id, reused: true }
          }
          const r = await runShisei(['drama-new', '--template', id], {})
          if (!r.ok) throw new Error(r.error)
          return { id: r.job, reused: false }
        })()
        fromTemplate.set(id, p)
        try { return await p } finally { fromTemplate.delete(id) }
      }
      case 'models': {
        let director = null
        try {
          const sel = modelServices?.agentDefaultModel?.currentSelection?.()
          if (sel) {
            let image = null
            try { const info = await modelServices.llm.resolveModelInfo(sel.provider, sel.model); image = Array.isArray(info?.inputModalities) ? info.inputModalities.includes('image') : null } catch { /* unknown */ }
            director = { provider: sel.provider, model: sel.model, image }
          }
        } catch { /* no model service in this profile */ }
        const cfg = await runShisei(['config', 'show'], {})
        const c = cfg.config ?? {}
        const { vision: vs } = await visionView()
        return { director, vision: { model: c.vision_model, baseUrl: c.vision_base_url, keyEnv: c.vision_key_env, pref: vs.pref, source: vs.source, answers: vs.model, hasKey: vs.hasKey } }
      }
      case 'drama-shots': {
        // shot by shot against the script: cuts found locally, one frame per shot to the image model (its key from keyEnv)
        const job = await readJob(String(payload.id))
        const r = await runShisei(['drama-shots', jobPath(job.id), '--force'], await keyEnv(credentials))
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'drama-trim': {
        // the user's edit of a picked take (cut / hold / swap a piece), applied at the cut; refused in the model shell
        const job = await readJob(String(payload.id))
        const segment = String(payload.segment ?? '')
        if (!/^[\w-]{1,8}$/u.test(segment)) throw new Error('bad segment')
        const range = (v) => {
          if (!Array.isArray(v) || v.length !== 2) throw new Error('bad range')
          const [a, b] = v.map(Number)
          if (!(Number.isFinite(a) && Number.isFinite(b) && a >= 0 && b > a && b < 60)) throw new Error('bad range')
          return `${a.toFixed(3)}-${b.toFixed(3)}`
        }
        const argv = ['drama-trim', jobPath(job.id), segment]
        if (payload.clear === true) argv.push('--clear')
        else if (payload.undo) argv.push('--undo', range(payload.undo))
        else {
          const mode = String(payload.mode ?? 'cut')
          if (!['cut', 'hold', 'swap'].includes(mode)) throw new Error('bad edit')
          argv.push('--at', range(payload.at), '--mode', mode)
          if (mode === 'swap') argv.push('--take', String(Number(payload.take) || 0))
          if (payload.withLine === true) argv.push('--with-line')
        }
        const r = await runShisei(argv, {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'drama-check': {
        const job = await readJob(String(payload.id))
        const r = await runShisei(['drama-check', jobPath(job.id)], {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'director-send': {
        const missing = directorMissing()
        if (missing) throw new Error(missing)
        let job = await readJob(String(payload.id))
        // 重试 after the director failed: a dsh session keeps the model it started with, so a model the user has fixed
        // since is only picked up by a new session. The failed one stays in dsh's list; nothing is deleted.
        if (payload.write === true && job.directorSession) {
          let failed = false
          try { failed = !!directorState((await sessionService.inspect(job.directorSession))?.events).error } catch { /* keep the session */ }
          if (failed) job = await updateJob(job.id, (j) => { const { directorSession: _old, ...rest } = j; return rest })
        }
        // write: true = 「让导演开始写」 / 重试 on a project without a script: the same first message drama-create sends
        const writeMsg = (await readStudy(job)) ? WRITE_SCRIPT_STUDY : WRITE_SCRIPT
        const typed = (payload.write === true ? writeMsg(String(job.prompt ?? '').trim() || String(job.title ?? '')) : String(payload.text ?? '')).trim().slice(0, 4000)
        if (!typed) throw new Error('empty message')
        // outside assets in the message are copied in first (new ids, unreviewed); the director reads ids of this project
        const { text, adopted } = await adoptTokens(job, typed)
        let note = ''
        const at = payload.attach
        if (at && typeof at === 'object') {
          const t = Math.max(0, Number(at.t) || 0)
          const argv = ['drama-frames', jobPath(job.id), '--width', '480']
          if (at.final) argv.push('--final', '--at', t.toFixed(2))
          else if (/^\d{1,2}-\d{1,2}$/u.test(String(at.unit))) argv.push('--unit', String(at.unit), '--at', t.toFixed(2))
          const r = await runShisei(argv, {})
          if (r.ok && r.frames?.[0]) {
            note = `\n\n（我在${at.final ? '成片' : `段 ${at.label ?? at.unit}`} ${t.toFixed(1)} 秒处暂停了${at.label ? `，这是段 ${at.label}` : ''}。那一帧：${r.frames[0].path}。` +
              `要看就用 drama-frames ${at.final ? `--final --at ${t.toFixed(1)}` : `--unit ${at.unit} --at ${t.toFixed(1)}`} --ask "你的问题"。）`
          }
        }
        // @ mentions: checked against story.json; frames and point pictures are made now, and the block names them
        const ctx = { ...(await mentionCtx(job)), scope: payload.scope === 'take' ? 'take' : 'frame' }
        const res = resolveMentions(text, ctx.story, ctx)
        const extra = { frames: {}, points: {}, adopted }
        for (const [i, it] of res.items.entries()) {
          let argv = null
          if (it.kind === 'point') {
            argv = ['drama-point', jobPath(job.id), '--x', String(it.at.x), '--y', String(it.at.y)]
            if (it.at.w > 0 && it.at.h > 0) argv.push('--w', String(it.at.w), '--h', String(it.at.h))
            if (it.src === 'kf') argv.push('--kf', it.unit.id)
            else if (it.src === 'take') argv.push('--unit', it.unit.id, '--seed', String(it.seed), '--at', it.t.toFixed(2))
            else argv.push('--final', '--at', it.t.toFixed(2))
            const r = await runShisei(argv, {})
            if (!r.ok) throw new Error(`没能做出指出的那一处的图：${r.error}`)
            extra.points[i] = { crop: r.crop, full: r.full, where: r.where }
          } else if ((it.kind === 'take' && it.t !== null) || it.kind === 'final') {
            argv = it.kind === 'final' ? ['drama-frames', jobPath(job.id), '--width', '480', '--final', '--at', it.t.toFixed(2)]
              : ['drama-frames', jobPath(job.id), '--width', '480', '--unit', it.unit.id, '--seed', String(it.seed), '--at', it.t.toFixed(2)]
            // the image model describes the frame now, so the director knows what is on it without having to think of
            // asking; no image model, or it fails: the frame alone, as before
            let r = await runShisei([...argv, '--ask', FRAME_ASK, '--no-think', '--timeout', '30'], await keyEnv(credentials))
            if (!r.ok) r = await runShisei(argv, {})
            if (r.ok && r.frames?.[0]) extra.frames[i] = { path: r.frames[0].path, seen: String(r.answer ?? '').trim().slice(0, 400) || null }
          }
        }
        const sent = await sendToDirector(job, text, mentionNote(res, ctx.story, jobPath(job.id), extra) + note, payload.mode === 'steer' ? 'steer' : 'queue')
        return { sessionId: sent.sid, mode: sent.mode, mentions: res.items.length, adopted }
      }
      case 'director-preview': {
        // what the director would get, without sending or making pictures (for checking by hand; the page does not call it)
        const job = await readJob(String(payload.id))
        const text = String(payload.text ?? '').trim().slice(0, 4000)
        externalMentions(text)
        const ctx = { ...(await mentionCtx(job)), scope: payload.scope === 'take' ? 'take' : 'frame' }
        const res = resolveMentions(text, ctx.story, ctx)
        return { text: text + mentionNote(res, ctx.story, jobPath(job.id)), current: res.current?.id ?? null, refs: res.refs, notAdded: res.notAdded.map((n) => ({ kind: n.item.kind, id: n.item.asset.id, why: n.why })) }
      }
      case 'mention-options': {
        // with no project (the home page) only outside sources: the library, other projects, my uploads
        const job = payload.id ? await readJob(String(payload.id)) : null
        const external = await externalOptions(job && job.id)
        if (!job) return { cast: [], scenes: [], props: [], units: [], final: false, limits: LIMITS, external }
        const { story, have, review, cut, study } = await mentionCtx(job)
        // the reference's shots, for @ 对标 #n (pointing only: what the shot does, never a picture to generate from)
        const studyShots = (study?.shots ?? []).map((x) => ({ n: x.n, seconds: Math.round((x.t1 - x.t0) * 10) / 10, sheet: x.sheet, look: x.look ?? {} }))
        if (!story) return { cast: [], scenes: [], props: [], units: [], final: false, limits: LIMITS, external, study: studyShots }
        const assets = (kind) => (story[BIND[kind]] ?? []).filter((x) => ASSET_ID.test(String(x.id))).map((x) => assetInfo(story, kind, x.id, { have, review }))
        const units = storyUnits(story).map((u) => {
          const vs = new Set()
          for (const f of have) { const m = /^(?:kf_|take_)(\d{1,2}-\d{1,2})(?:_\d{4})?\.v(\d+)\.[a-z0-9]+$/u.exec(f); if (m && m[1] === u.id) vs.add(Number(m[2])) }
          const cu = (cut?.units ?? []).find((x) => x.id === u.id)
          return { id: u.id, label: u.label, members: u.members, t0: u.t0, t1: u.t1, seconds: u.seconds, scene: u.scene, keyframe: have.has(`kf_${u.id}.png`),
            kfReview: review?.keyframes?.[u.id]?.status ?? 'pending', what: String(u.first.keyframe ?? '').slice(0, 24), refs: unitRefs(story, u),
            takes: SEEDS.filter((sd) => have.has(`take_${u.id}_${sd}.mp4`)), versions: [...vs].sort((a, b) => a - b), cutStart: cu ? Number(cu.start) : null }
        })
        return { cast: assets('cast'), scenes: assets('scene'), props: assets('prop'), units, final: have.has('final.mp4'), limits: LIMITS, external, study: studyShots,
          take: Object.fromEntries(storyUnits(story).map((u) => [u.id, takeRefs(story, u)])) }
      }
      case 'director-history': {
        const job = await readJob(String(payload.id))
        if (!job.directorSession || !sessionService) return { lines: [], available: !!sessionService, state: sessionService ? { model: defaultModel() } : null }
        const snap = await sessionService.inspect(job.directorSession)
        const { story, have } = await mentionCtx(job)
        const lines = chatLines(snap?.events).map((l) => (l.who === 'user' ? { ...l, mentions: describeMentions(l.text, story, have) } : l))
        const state = directorState(snap?.events)
        return { lines, available: true, sessionId: job.directorSession, state: { ...state, model: state.model ?? defaultModel() }, asks: directorAsks.pending(job.id),
          queue: directorQueue(snap?.events).map((q) => ({ ...q, mentions: describeMentions(q.text, story, have) })), can: directorCan(), now: Date.now() }
      }
      // a message the director has not read yet: steer = read it at the end of the current step (插话); remove = take it
      // back (the page puts the words back in the composer; nothing was read, nothing is lost)
      case 'director-queue': {
        const job = await readJob(String(payload.id))
        const action = String(payload.action)
        if (action !== 'steer' && action !== 'remove' && action !== 'resend') throw new Error('bad queue action')
        if (!job.directorSession || !directorCan().queue) throw new Error('这个 dsh 版本不能改排队的话，请等导演这一轮做完')
        const item = directorQueue((await sessionService.inspect(job.directorSession))?.events).find((q) => q.id === String(payload.item))
        if (!item) throw new Error('导演已经读到这句话了')
        // resend: the director is not working and the message still waits (left over after a stop): send it again
        if (action === 'resend') { const r = await resendPending(job.directorSession); return { ok: true, action, resent: r.sent, back: r.back } }
        try {
          await sessionService.updateQueue({ sessionId: job.directorSession, itemId: item.id, action: { kind: action } })
        } catch (e) {
          const code = remoteCode(e)
          if (code === 'session/queue-item-not-found') throw new Error('导演已经读到这句话了')
          if (code === 'session/steer-unavailable') throw new Error('导演这一轮已经结束，这句话会在下一轮先读')
          throw e
        }
        return { ok: true, action, text: item.text }
      }
      // stop the director's running turn; what is queued stays and is read next. A GPU run it started is not stopped.
      case 'director-stop': {
        const job = await readJob(String(payload.id))
        if (!job.directorSession || !directorCan().stop) throw new Error('这个 dsh 版本不能从这里停下导演，请在 dsh 的会话里停')
        await sessionService.cancel({ sessionId: job.directorSession })
        // once the turn has ended, what the user sent and the director never read is sent again (dsh leaves an
        // interjection in the inbox after a cancel)
        let resent = 0
        let back = []
        if (directorCan().queue) {
          for (let i = 0; i < 20; i += 1) {
            if (!directorState((await sessionService.inspect(job.directorSession))?.events).busy) break
            await new Promise((r) => setTimeout(r, 250))
          }
          if (directorQueue((await sessionService.inspect(job.directorSession))?.events).length) ({ sent: resent, back } = await resendPending(job.directorSession))
        }
        return { ok: true, resent, back }
      }
      // which model the director talks to and what else dsh offers (dsh's own catalog, grouped by provider)
      case 'director-models': {
        const job = await readJob(String(payload.id))
        let current = null
        if (job.directorSession && sessionService) {
          try { current = directorState((await sessionService.inspect(job.directorSession))?.events).model } catch { /* session gone */ }
        }
        const fallback = defaultModel()
        let groups = []
        try {
          const cat = typeof sessionService?.modelCatalog === 'function' ? await sessionService.modelCatalog() : null
          groups = (cat?.groups ?? []).map((g) => ({ id: String(g.id), name: String(g.name ?? g.id), models: (g.models ?? []).map((m) => ({ id: String(m.id), name: String(m.name ?? m.id) })) }))
            .filter((g) => g.models.length)
        } catch { /* no catalog: the page shows the current model only */ }
        return { current: current ?? fallback, session: !!job.directorSession, groups, can: typeof sessionService?.selectModel === 'function' }
      }
      // switch the director's model (dsh session/selectModel: from the next model call on; dsh also makes it its default).
      // retry: the turn is stuck, so stop it, switch, and send the user's last message again, said as such.
      case 'director-model': {
        const job = await readJob(String(payload.id))
        const provider = String(payload.provider ?? '')
        const model = String(payload.model ?? '')
        if (!provider || !model) throw new Error('没有选模型')
        if (!job.directorSession) {
          // no conversation yet: the director starts on dsh's default, so the choice is saved as that
          if (typeof modelServices?.agentDefaultModel?.saveSelection !== 'function') throw new Error('这个 dsh 版本不能从这里换模型，请在 dsh 的设置里改默认模型')
          await modelServices.agentDefaultModel.saveSelection({ provider, model })
          return { selected: { provider, model }, retried: false }
        }
        if (typeof sessionService?.selectModel !== 'function') throw new Error('这个 dsh 版本不能从这里换模型，请在 dsh 的会话里用 /model 换')
        const sid = job.directorSession
        const retry = payload.retry === true
        if (retry) {
          if (!directorCan().stop) throw new Error('这个 dsh 版本不能从这里停下导演，请在 dsh 的会话里停')
          if (directorState((await sessionService.inspect(sid))?.events).busy) {
            await sessionService.cancel({ sessionId: sid })
            for (let i = 0; i < 40; i += 1) {
              if (!directorState((await sessionService.inspect(sid))?.events).busy) break
              await new Promise((r) => setTimeout(r, 250))
            }
          }
        }
        let selected
        try {
          ({ selected } = await sessionService.selectModel({ sessionId: sid, provider, model }))
        } catch (e) {
          if (remoteCode(e) === 'session/model-unavailable') throw new Error(`这个模型现在用不了：${provider}/${model}`)
          throw e
        }
        if (!retry) return { selected, retried: false }
        const last = lastUserMessage((await sessionService.inspect(sid))?.events)
        if (!last) return { selected, retried: false }
        await sessionService.prompt({ sessionId: sid, requestId: randomUUID(), mode: 'queue', content: [{ type: 'text', text: RETRY_NOTE + last }] }, new AbortController().signal)
        return { selected, retried: true }
      }
      // the user's answer to a question or permission ask the director is waiting on (lib/asks.js)
      case 'director-answer': {
        const job = await readJob(String(payload.id))
        return directorAsks.answer(job.id, String(payload.ask), payload)
      }
      case 'library': {
        const projects = []
        for (const j of await listJobs()) {
          if (j.kind !== 'drama') continue
          let story
          try { story = JSON.parse(await readFile(jobPath(j.id, 'story.json'), 'utf8')) } catch { continue }
          let review = {}
          try { review = JSON.parse(await readFile(jobPath(j.id, 'review.json'), 'utf8')) } catch { /* nothing reviewed yet */ }
          let files = []
          try { files = await readdir(jobPath(j.id)) } catch { /* gone */ }
          projects.push({ job: { id: j.id, title: j.title, stage: j.stage, createdAt: j.createdAt }, story, review, files })
        }
        return assetIndex(projects)
      }
      case 'costs': {
        const projects = []
        for (const j of await listJobs()) {
          // dance-swap runs from before ledgers get their line once, from progress.json (shisei.py job-ledger)
          if (j.kind !== 'drama' && j.state && j.state !== 'draft' && j.state !== 'running' && !existsSync(jobPath(j.id, 'ledger.json'))) await runShisei(['job-ledger', jobPath(j.id)], {})
          let ledger = []
          try { ledger = JSON.parse(await readFile(jobPath(j.id, 'ledger.json'), 'utf8')) } catch { continue }
          let budget = null
          try { const b = (await readJob(j.id)).params?.budget; budget = b == null ? 5 : Number(b) } catch { /* unreadable */ }   // 5: shisei.py DEFAULT_BUDGET
          const title = j.kind === 'drama' ? j.title : (j.prompt || '').slice(0, 40)
          projects.push({ job: { id: j.id, title, budget, kind: j.kind === 'drama' ? 'drama' : 'swap', createdAt: j.createdAt }, ledger })
        }
        // what making a machine cost (开始 › GPU 机器) is not a project's: one row of its own
        try {
          const ledger = JSON.parse(await readFile(join(SHISEI_HOME, 'setup', 'ledger.json'), 'utf8'))
          if (Array.isArray(ledger) && ledger.length) projects.push({ job: { id: 'setup', title: '准备 GPU 机器', budget: null, kind: 'setup', createdAt: null }, ledger })
        } catch { /* no machine made by the plugin */ }
        const days = [7, 14, 30, 90].includes(Number(payload.days)) ? Number(payload.days) : 30
        return costSummary(projects, { days })
      }
      case 'drama-version': {
        const job = await readJob(String(payload.id))
        if (!/^\d{1,2}-\d{1,2}$/u.test(String(payload.unit))) throw new Error('bad unit')
        const v = Number(payload.restore)
        if (!Number.isInteger(v) || v < 1 || v > 99) throw new Error('bad version')
        const r = await runShisei(['drama-version', jobPath(job.id), String(payload.unit), '--restore', String(v)], {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'drama-cut': {
        const job = await readJob(String(payload.id))
        const r = await runShisei(['drama-cut', jobPath(job.id)], {})
        if (!r.ok) throw new Error(r.error)
        return r
      }
      case 'job': {
        const job = jobs.get(String(payload.id))
        if (!job) throw new Error('unknown job')
        // a failed job says why in the page's words too (pageError), like every other answer
        const result = job.result && job.result.ok === false && typeof job.result.error === 'string' ? { ...job.result, error: pageError(job.result.error) } : job.result
        return { id: job.id, label: job.label, state: job.state, log: job.log, result }
      }
      default:
        throw new Error(`unknown endpoint ${endpoint}`)
    }
  }
}

/** What mentions are checked against: story.json, the job's top-level files, review.json, cut.json. */
async function mentionCtx(job) {
  const dir = jobPath(job.id)
  const read = async (f) => { try { return JSON.parse(await readFile(join(dir, f), 'utf8')) } catch { return null } }
  let have = new Set()
  try { have = new Set((await readdir(dir, { withFileTypes: true })).filter((e) => e.isFile()).map((e) => e.name)) } catch { /* empty */ }
  let story = await read('story.json')
  if (!story) {
    const staged = (await read('adopted.json')) ?? []
    if (staged.length) story = { cast: [], scenes: [], props: [], shots: [], staged: true, ...Object.fromEntries(Object.entries(BIND).map(([k, pl]) => [pl, staged.filter((e) => e.kind === k).map((e) => e.entry)])) }
  }
  return { dir, story, review: (await read('review.json')) ?? {}, cut: await read('cut.json'), have, study: await read('study.json') }
}

let refsMemo = null
function refsWith(credentials) {
  if (credentials) refsMemo = makeRefs({ home: SHISEI_HOME, prefix: RPC_PREFIX, readJob, jobPath, listJobs, mentionCtx, runShisei, keyEnv: () => keyEnv(credentials), dramaScope })
  return refsMemo ?? refsWith({ resolve: async () => null })
}

/** Optional single-shot scope for drama-run / drama-estimate: one shot id and a take count. */
export function dramaScope(payload = {}) {
  const out = []
  // one segment (shot) or several (shots): the run covers only the segments that need work
  const ids = [...(payload.shot !== undefined ? [payload.shot] : []), ...(Array.isArray(payload.shots) ? payload.shots : [])].map(String)
  if (payload.shots !== undefined && payload.shots !== null && !Array.isArray(payload.shots)) throw new Error('bad shot id')
  if (ids.length > 40 || ids.some((x) => !/^\d{1,2}-\d{1,2}$/u.test(x))) throw new Error('bad shot id')
  if (ids.length) out.push('--shots', ...new Set(ids))
  if (payload.seeds !== undefined) {
    const n = Number(payload.seeds)
    if (!Number.isInteger(n) || n < 1 || n > 5) throw new Error('seeds must be 1..5')
    out.push('--seeds', String(n))
  }
  return out
}

// ---------------------------------------------------------------- jobs on disk (~/.dsh/shisei/jobs/<id>)

export const JOBS_DIR = join(SHISEI_HOME, 'jobs')
const JOB_ID = /^j\d{8}-\d{6}-[a-z0-9]{4}$/u
const FILE_NAME = /^[A-Za-z0-9_.-]{1,120}$/u
export const SERIES_DIR = join(SHISEI_HOME, 'series')
const SERIES_ID = /^s\d{8}-\d{6}-[0-9a-f]{4}$/u
const CAST_ID = /^[a-z][a-z0-9_]{0,40}$/u
const SERIES_PICTURE = /^(?:cast_[a-z][a-z0-9_]{0,40}_(?:face|body)\.png|photo_[a-z][a-z0-9_]{0,40}\.png|voice_[a-z][a-z0-9_]{0,40}\.wav)$/u
/** Checked ids and text for the series calls (shisei.py checks again). */
export function seriesId(v) { if (!SERIES_ID.test(String(v ?? ''))) throw new Error('bad series id'); return String(v) }
export function jobId(v) { if (!JOB_ID.test(String(v ?? ''))) throw new Error('bad job id'); return String(v) }
/** A character's own voice track in a project (vo_<shot>.wav), the only thing 定声 is made from. */
export function voiceTrack(v) { if (!/^(?:vo_\d{1,2}-\d{1,2}\.wav|take_\d{1,2}-\d{1,2}_\d{4}\.mp4)$/u.test(String(v ?? ''))) throw new Error('bad voice track'); return String(v) }
/** --shot for a take: which line in it to cut out (a take holds several). */
export function shotArg(p) {
  if (!String(p?.file ?? '').startsWith('take_')) return []
  if (!/^\d{1,2}-\d{1,2}$/u.test(String(p?.shot ?? ''))) throw new Error('bad shot id')
  return ['--shot', String(p.shot)]
}
export function castId(v) { if (!CAST_ID.test(String(v ?? ''))) throw new Error('bad character id'); return String(v) }
export function pageText(v, max, required = false) {
  const s = typeof v === 'string' ? v.trim().slice(0, max) : ''
  if (required && !s) throw new Error('要写一个名字')
  return s
}
/**
 * What the page uploads for a character (cast-upload): a recording to fix a voice with (project or series), or the
 * photo a new regular is drawn after (series only). Saved next to it under one fixed name, which shisei.py converts and
 * removes (--upload / --photo). Whose voice or face it is, is the user's to clear: the page only reminds them.
 */
export const CAST_UPLOAD = {
  voice: { stem: '.voice_upload', ext: ['wav', 'mp3', 'm4a', 'aac', 'ogg', 'opus', 'flac', 'webm'], max: 20 * 1024 * 1024 },
  photo: { stem: '.photo_upload', ext: ['png', 'jpg', 'jpeg', 'webp'], max: MAX_REF_BYTES },
}
export function castUploadPath(q) {
  const kind = CAST_UPLOAD[String(q.what ?? '')]
  if (!kind) throw new Error('bad upload')
  const ext = (/\.([A-Za-z0-9]{2,5})$/u.exec(String(q.filename ?? ''))?.[1] ?? '').toLowerCase()
  if (!kind.ext.includes(ext)) throw new Error(q.what === 'voice' ? '录音要是 wav、mp3、m4a、aac、ogg、opus、flac 或 webm' : '照片要是 PNG、JPEG 或 WebP')
  if (q.series) return join(SERIES_DIR, seriesId(q.series), `${kind.stem}.${ext}`)
  if (q.what !== 'voice') throw new Error('a photo is added on the series page')
  return jobPath(jobId(q.job), `${kind.stem}.${ext}`)
}
/** --upload for 定声: the recording cast-upload saved (its name comes from the page; only these names are accepted). */
function voiceUpload(dir, name) {
  if (!/^\.voice_upload\.(?:wav|mp3|m4a|aac|ogg|opus|flac|webm)$/u.test(String(name ?? ''))) throw new Error('bad recording')
  return ['--upload', join(dir, String(name))]
}

/** shisei.py's answer, or its error thrown (the page shows it). */
function ok(r) { if (!r?.ok) throw new Error(r?.error ?? 'no answer'); const { ok: _ok, ...rest } = r; return rest }
/** A regular's picture inside a series folder; refuses anything else. */
export function seriesPicture(sid, name) {
  if (!SERIES_ID.test(String(sid)) || !SERIES_PICTURE.test(String(name))) throw new Error('bad series file')
  return join(SERIES_DIR, String(sid), String(name))
}
// study: the reference video (对标片) on its way to `drama-study --video`, which copies it to study_src.* and drops this
// front2: the right-hand character of a two-person recast
export const UPLOAD_KINDS = { source: 'source', front: 'front', face: 'face', front2: 'front2', study: 'study_upload' }
const STUDY_FIELDS = ['framing', 'move', 'light', 'people', 'role']
const MIME = { '.mp4': 'video/mp4', '.wav': 'audio/wav', '.mov': 'video/quicktime', '.webm': 'video/webm', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.glb': 'model/gltf-binary', '.json': 'application/json' }
export const MAX_UPLOAD_BYTES = 800 * 1024 * 1024

export function newJobId(now = new Date()) {
  const p = (n) => String(n).padStart(2, '0')
  return `j${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}-${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}-${randomBytes(2).toString('hex')}`
}

/** Resolve a file inside a job folder; refuses anything that could escape it. */
export function jobPath(id, name) {
  if (!JOB_ID.test(String(id))) throw new Error('bad job id')
  if (name === undefined) return join(JOBS_DIR, id)
  if (!FILE_NAME.test(String(name)) || name.includes('..')) throw new Error('bad file name')
  return join(JOBS_DIR, id, name)
}

export function uploadName(kind, filename = '') {
  if (!(kind in UPLOAD_KINDS)) throw new Error('bad upload kind')
  const ext = (/\.([A-Za-z0-9]{2,5})$/u.exec(filename)?.[1] ?? '').toLowerCase()
  const okExt = kind === 'source' || kind === 'study' ? ['mp4', 'mov', 'webm', 'm4v'] : ['png', 'jpg', 'jpeg', 'webp']
  if (!okExt.includes(ext)) throw new Error(`unsupported ${kind} file type .${ext}`)
  return `${UPLOAD_KINDS[kind]}.${ext}`
}

/** The project's 对标片 (study.json), or null. */
async function readStudy(job) {
  let s
  try { s = JSON.parse(await readFile(jobPath(job.id, 'study.json'), 'utf8')) } catch { return null }
  // an image-model run whose process is gone (dsh closed, computer slept) is not shown as still looking
  const lk = s.look ?? {}
  if (lk.status === 'running' && (lk.pid ? !pidAlive(lk.pid) : Date.now() / 1000 - Number(lk.started_at ?? 0) > 1800)) {
    s.look = { ...lk, status: lk.done ? 'partial' : 'failed', error: lk.error ?? '看图进程意外退出' }
  }
  return s
}

/**
 * A start refused because another project holds the machine (shisei.py lease_refusal), in the page's words: the holder
 * by its title, and that nothing was started or spent. Null for any other error. Nothing of ours is on, so the page
 * shows no billing note and no stop button (it would be someone else's machine).
 */
async function leaseRefusal(error) {
  const held = /machine \S+ is in use by (\S+) \(lease runs (\d+) more min/u.exec(error)
  if (!held) return null
  let title = ''
  try {
    const j = await readJob(held[1])
    title = j.title || (j.kind === 'drama' ? '' : (j.prompt || '').slice(0, 14))
  } catch { /* another home's project, or gone */ }
  return title ? `这台机器正在拍《${title}》：可以排队等它拍完，或者在「开始」页再建一台。什么也没开、没花钱。` : pageError(error) // zh-ok
}

async function readJob(id) {
  return JSON.parse(await readFile(jobPath(id, 'job.json'), 'utf8'))
}

const fromTemplate = new Map()   // template id → the copy being made right now

/** template id → the user's projects copied from it and not archived, newest first ({ id, createdAt, state }). */
export async function templateCopies(dir = JOBS_DIR) {
  const out = new Map()
  let names = []
  try { names = await readdir(dir) } catch { return out }
  for (const n of names.filter((x) => JOB_ID.test(x))) {
    let j
    try { j = JSON.parse(await readFile(join(dir, n, 'job.json'), 'utf8')) } catch { continue }
    if (!j?.template || j.archivedAt || j.kind !== 'drama') continue
    const list = out.get(j.template) ?? []
    list.push({ id: n, createdAt: String(j.createdAt ?? ''), state: j.state ?? null })
    out.set(j.template, list)
  }
  for (const list of out.values()) list.sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0))
  return out
}

async function writeJob(job) {
  await mkdir(jobPath(job.id), { recursive: true })
  await writeFile(jobPath(job.id, 'job.json'), JSON.stringify(job, null, 1))
  return job
}

// job.json is read-modify-written by concurrent uploads and RPCs: serialize per job
const jobLocks = new Map()
export function withJobLock(id, fn) {
  const prev = jobLocks.get(id) ?? Promise.resolve()
  const next = prev.then(fn, fn)
  jobLocks.set(id, next.catch(() => {}))
  return next
}

async function updateJob(id, mutate) {
  return withJobLock(id, async () => writeJob(await mutate(await readJob(id))))
}

// ---------------------------------------------------------------- archive
// Archiving only hides a project from the home page and the menu (job.json archivedAt). shisei.py refuses to take a
// machine for an archived project (ensure_claim), and these page calls are refused until the user restores it.
export const ARCHIVED_SAY = '这个项目已归档，只能看：先在页面上点「恢复并继续」'
export const ARCHIVE_LOCKED = new Set(['drama-voice', 'job-budget', 'job-precheck', 'job-start', 'job-pick', 'job-sides', 'job-long', 'probe', 'prep',
  'drama-review', 'drama-budget', 'drama-start', 'drama-precheck', 'drama-version', 'drama-cut',
  'study-start', 'study-look', 'study-set', 'study-cut', 'study-apply',
  'ref-adopt', 'ref-place', 'wardrobe', 'crop', 'staging', 'cast-flag', 'director-send'])

/** Why a project cannot be archived now (the page's words), or null: a live run, or a machine it still leases. Pure; tested. */
export function archiveBlock(progress, leases, id, nowMs, alive) {
  if (progress && progress.state === 'running' && progress.pid && alive(progress.pid)) return '正在拍：拍完或停下才能归档'
  if (leases.some((l) => l && l.holder === id && l.state === 'active' && Number(l.until) * 1000 > nowMs)) return '还租着机器：拍完或停下、机器交还后才能归档'
  return null
}

/**
 * Another run of this project holds a machine and its process is alive (e.g. the director's trial from its shell, which
 * writes no progress.json): the page's words, or null. One run per project at a time. Pure; tested.
 */
export function ownRunBlock(leases, id, nowMs, alive) {
  const live = leases.some((l) => l && l.holder === id && l.state === 'active' && Number(l.until) * 1000 > nowMs && l.pid && alive(l.pid))
  return live ? '这个项目已经有一次运行在用机器（比如导演的试拍）。等它结束再拍。' : null
}

async function readLeases() {
  let names = []
  try { names = await readdir(join(SHISEI_HOME, 'leases')) } catch { return [] }
  const out = []
  for (const n of names.filter((x) => x.endsWith('.json'))) {
    try { out.push(JSON.parse(await readFile(join(SHISEI_HOME, 'leases', n), 'utf8'))) } catch { /* being written */ }
  }
  return out
}

async function listJobs() {
  let names = []
  try { names = await readdir(JOBS_DIR) } catch { return [] }
  const jobs = []
  for (const n of names.filter((x) => JOB_ID.test(x)).sort().reverse().slice(0, 60)) {
    try {
      const job = await readJob(n)
      let progress = null
      try { progress = JSON.parse(await readFile(jobPath(n, 'progress.json'), 'utf8')) } catch { /* draft */ }
      const pick = job.pick ?? '1001'
      const thumbs = {}
      for (const [k, f] of [['source', 'thumb_source.png'], ['mannequin', 'thumb_mannequin.png'], ['character', `thumb_seed_${pick}.png`]]) {
        if (existsSync(jobPath(n, f))) thumbs[k] = f
      }
      if (job.kind === 'drama') {
        const files = await readdir(jobPath(n))
        const kfs = files.filter((f) => /^kf_\d{1,2}-\d{1,2}\.png$/u.test(f)).sort().slice(0, 3)
        jobs.push({ id: n, kind: 'drama', title: job.title, createdAt: job.createdAt, archivedAt: job.archivedAt ?? null, series: job.series ?? null, prompt: job.prompt, state: progress?.state ?? job.state, kfs, hasFinal: files.includes('final.mp4'), stage: roughStage(files) })
        continue
      }
      jobs.push({ id: n, kind: 'motion', createdAt: job.createdAt, archivedAt: job.archivedAt ?? null, prompt: job.prompt, long: job.params?.long ? (job.params.long.from ? 'whole' : 'trial') : null, state: progress?.state ?? job.state, thumbs, hasShowcase: existsSync(jobPath(n, 'showcase.mp4')) })
    } catch { /* half-written job, skip */ }
  }
  return jobs
}

/** Which drama stage a project is roughly at, from its files alone (the project page asks shisei.py for the exact gate). */
export function roughStage(files) {
  const has = (re) => files.some((f) => re.test(f))
  if (!files.includes('story.json')) return 'script'
  if (files.includes('final.mp4') || has(/^take_/u)) return files.includes('final.mp4') ? 'cut' : 'takes'
  if (has(/^kf_/u)) return 'keyframes'
  return 'assets'
}

export function rangeOf(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/u.exec(header ?? '')
  if (!m) return null
  let start = m[1] === '' ? size - Number(m[2]) : Number(m[1])
  let end = m[1] === '' || m[2] === '' ? size - 1 : Number(m[2])
  if (!(start >= 0) || start >= size) return 'unsatisfiable'
  end = Math.min(end, size - 1)
  return { start, end }
}

/** GET /api/dsh-shisei/file?job=&name=  (Range aware, for <video>) and POST /api/dsh-shisei/upload?job=&kind=&filename= */
export function registerFileRoutes(ctx) {
  ctx.effect(() => ctx.connection.fetch.register({
    path: `${RPC_CHANNEL}/${RPC_PREFIX}/file`,
    methods: ['GET'],
    requestBody: 'buffered',
    fetch: async (request) => {
      const url = new URL(request.url)
      let path
      try {
        const name = url.searchParams.get('name')
        // one subfolder file is shown by the page: the clothes picture of a picture wardrobe (wardrobe/<cast>_<checksum>.png)
        // a series' regulars are shown from the series folder (?series=&name=cast_<id>_face.png)
        path = url.searchParams.has('series') ? seriesPicture(url.searchParams.get('series'), name)
          : wardrobeFile(jobPath(url.searchParams.get('job')), name) ?? jobPath(url.searchParams.get('job'), name)
      } catch (e) { return new Response(String(e.message), { status: 400 }) }
      let info
      try { info = await stat(path) } catch { return new Response('not found', { status: 404 }) }
      const type = MIME[extname(path).toLowerCase()] ?? 'application/octet-stream'
      const range = rangeOf(request.headers.get('range'), info.size)
      if (range === 'unsatisfiable') return new Response(null, { status: 416, headers: { 'content-range': `bytes */${info.size}` } })
      const headers = { 'content-type': type, 'accept-ranges': 'bytes', 'cache-control': 'no-store' }
      if (range) {
        return new Response(Readable.toWeb(createReadStream(path, range)), { status: 206, headers: { ...headers, 'content-length': String(range.end - range.start + 1), 'content-range': `bytes ${range.start}-${range.end}/${info.size}` } })
      }
      return new Response(Readable.toWeb(createReadStream(path)), { headers: { ...headers, 'content-length': String(info.size) } })
    },
  }), 'dsh-shisei: file route')
  ctx.effect(() => ctx.connection.fetch.register({
    path: `${RPC_CHANNEL}/${RPC_PREFIX}/upload`,
    methods: ['POST'],
    requestBody: 'streaming',
    fetch: async (request) => {
      const url = new URL(request.url)
      let path
      try {
        await readJob(url.searchParams.get('job'))
        path = jobPath(url.searchParams.get('job'), uploadName(url.searchParams.get('kind'), url.searchParams.get('filename') ?? ''))
      } catch (e) { return new Response(String(e.message), { status: 400 }) }
      if (!request.body) return new Response('empty body', { status: 400 })
      let received = 0
      const tmp = `${path}.part`
      try {
        const limiter = new TransformStream({ transform(chunk, c) { received += chunk.byteLength; if (received > MAX_UPLOAD_BYTES) c.error(new Error('file too large')); else c.enqueue(chunk) } })
        await pipeline(Readable.fromWeb(request.body.pipeThrough(limiter)), createWriteStream(tmp))
        await rename(tmp, path)
      } catch (e) {
        await rm(tmp, { force: true })
        return new Response(String(e.message || e), { status: received > MAX_UPLOAD_BYTES ? 413 : 500 })
      }
      await updateJob(url.searchParams.get('job'), (job) => ({ ...job, files: { ...(job.files ?? {}), [url.searchParams.get('kind')]: basename(path) } }))
      return Response.json({ ok: true, name: basename(path), bytes: received })
    },
  }), 'dsh-shisei: upload route')
  // POST cast-upload?what=voice|photo&job=|series=&filename=  body: the recording or photo (CAST_UPLOAD)
  ctx.effect(() => ctx.connection.fetch.register({
    path: `${RPC_CHANNEL}/${RPC_PREFIX}/cast-upload`,
    methods: ['POST'],
    requestBody: 'streaming',
    fetch: async (request) => {
      const q = Object.fromEntries(new URL(request.url).searchParams)
      let path
      try {
        path = castUploadPath(q)
        if (q.series) await stat(join(SERIES_DIR, seriesId(q.series), 'series.json'))
        else await readJob(q.job)
      } catch (e) { return new Response(String(e.message), { status: 400 }) }
      if (!request.body) return new Response('empty body', { status: 400 })
      const max = CAST_UPLOAD[q.what].max
      const chunks = []
      let size = 0
      for await (const c of Readable.fromWeb(request.body)) {
        size += c.length
        if (size > max) return new Response(`文件太大了：最多 ${max / 1024 / 1024} MB`, { status: 413 })
        chunks.push(c)
      }
      const buf = Buffer.concat(chunks)
      if (q.what === 'photo' && !sniffImage(buf.subarray(0, 16))) return new Response('这不是一张 PNG、JPEG 或 WebP 照片', { status: 415 })
      await writeFile(path, buf)
      return Response.json({ ok: true, name: basename(path), bytes: size })
    },
  }), 'dsh-shisei: cast upload route')
  const libThumbs = new Map()   // library thumbnails in memory only (at most 300, oldest out first)
  // GET ref-file?src=lib&cat=&item=[&view=][&tier=] | src=mine&upload=  (a library picture found through the catalogue, or my upload)
  ctx.effect(() => ctx.connection.fetch.register({
    path: `${RPC_CHANNEL}/${RPC_PREFIX}/ref-file`,
    methods: ['GET'],
    requestBody: 'buffered',
    fetch: async (request) => {
      const q = new URL(request.url).searchParams
      let path
      try {
        if (q.get('src') === 'lib') {
          // passed through from the online library, not saved (a picture is written to disk only when it goes into a project)
          const { lib, get } = await refsWith().library()
          const { url } = libImageUrl(lib, String(q.get('cat')), String(q.get('item')), q.get('view'), q.get('tier') || 'w300')
          const key = url
          let buf = libThumbs.get(key)
          if (!buf) {
            try { buf = await fetchLibImage(url, get) } catch (e) { return new Response(String(e.message), { status: 502 }) }
            libThumbs.set(key, buf)
            if (libThumbs.size > 300) libThumbs.delete(libThumbs.keys().next().value)
          }
          return new Response(buf, { headers: { 'content-type': 'image/webp', 'content-length': String(buf.length), 'cache-control': 'private, max-age=3600' } })
        } else if (q.get('src') === 'mine') {
          path = (await readUpload(SHISEI_HOME, String(q.get('upload')))).file
        } else throw new Error('bad source')
      } catch (e) { return new Response(String(e.message), { status: 400 }) }
      let buf
      try { buf = await readFile(path) } catch { return new Response('not found', { status: 404 }) }
      const type = { png: 'image/png', jpeg: 'image/jpeg', webp: 'image/webp' }[sniffImage(buf.subarray(0, 16))]
      if (!type) return new Response('not an image', { status: 415 })
      return new Response(buf, { headers: { 'content-type': type, 'content-length': String(buf.length), 'cache-control': 'private, max-age=600' } })
    },
  }), 'dsh-shisei: reference file route')
  // POST ref-upload?kind=&name=&look=&rights=1  body: the picture. Checked here, whatever the page did:
  // the confirmation, the size, and that the bytes are a PNG / JPEG / WebP (not the file name).
  ctx.effect(() => ctx.connection.fetch.register({
    path: `${RPC_CHANNEL}/${RPC_PREFIX}/ref-upload`,
    methods: ['POST'],
    requestBody: 'streaming',
    fetch: async (request) => {
      let meta
      try { meta = uploadMeta(Object.fromEntries(new URL(request.url).searchParams)) } catch (e) { return new Response(String(e.message), { status: 400 }) }
      if (!request.body) return new Response('empty body', { status: 400 })
      const chunks = []
      let size = 0
      try {
        for await (const c of Readable.fromWeb(request.body)) {
          size += c.length
          if (size > MAX_REF_BYTES) return new Response(`图太大了：最多 ${MAX_REF_BYTES / 1024 / 1024} MB`, { status: 413 })
          chunks.push(c)
        }
        const buf = Buffer.concat(chunks)
        const rec = await saveUpload(SHISEI_HOME, buf, meta, { sha256: createHash('sha256').update(buf).digest('hex') })
        return Response.json({ ok: true, upload: { id: rec.id, kind: rec.kind, name: rec.name, look: rec.look, at: rec.at, thumb: refsWith().mineThumb(rec) } })
      } catch (e) {
        return new Response(String(e.message || e), { status: 400 })
      }
    },
  }), 'dsh-shisei: reference upload route')
}

export const ENDPOINTS = ['state', 'tools-retry', 'series-voice', 'drama-voice', 'series-list', 'series-get', 'series-create', 'series-update', 'series-add', 'series-remove', 'series-order', 'series-counts', 'series-summary', 'series-cast-add', 'series-cast-update', 'series-cast-new', 'series-cast-retire', 'series-archive', 'series-episode-new', 'machines', 'machine-pick', 'machine-stop', 'machine-register', 'set-keys', 'unset-keys', 'keys-check', 'run', 'job', 'jobs', 'job-create', 'job-get', 'job-archive', 'job-sides', 'probe', 'prep', 'job-start', 'job-stop', 'job-pick', 'job-long', 'job-estimate', 'job-budget', 'job-precheck',
  'drama-create', 'drama-get', 'drama-watch', 'drama-review', 'drama-handoff', 'director-status', 'drama-estimate', 'drama-start', 'drama-cut', 'drama-check', 'drama-shots', 'drama-trim',
  'director-send', 'director-history', 'director-answer', 'director-queue', 'director-stop', 'director-models', 'director-model', 'director-preview', 'mention-options', 'library', 'drama-precheck', 'vision-test', 'models', 'drama-version', 'drama-budget',
  'refs', 'ref-adopt', 'ref-place', 'wardrobe', 'cast-flag', 'crop', 'staging', 'costs',
  'study-start', 'study-estimate', 'study-look', 'study-set', 'study-cut', 'study-apply',
  'onboarding', 'set-vision-key', 'set-vision-source', 'machine-plan', 'machine-update-plan', 'machine-create', 'machine-setup', 'templates', 'drama-from-template', 'version', 'version-dismiss', 'version-seen']

/** Mount the GUI routes on the dsh web connection (absent in headless profiles). */
export function registerRoutes(ctx, handle) {
  for (const endpoint of ENDPOINTS) {
    ctx.effect(() => ctx.connection.fetch.register({
      path: `${RPC_CHANNEL}/${RPC_PREFIX}/${endpoint}`,
      methods: ['POST'],
      requestBody: 'buffered',
      fetch: async (request) => {
        let body
        try { body = await request.json() } catch { return new Response('body is not JSON', { status: 400 }) }
        if (!body || body.method !== `${RPC_PREFIX}/${endpoint}`) return new Response('invalid RPC envelope', { status: 400 })
        let result
        try {
          result = { ok: true, value: await handle(endpoint, body.payload ?? {}, { signal: request.signal }) }
        } catch (e) {
          // dsh connection protocol: failures carry { code, message, details }
          result = { ok: false, error: { code: 'dsh-shisei', message: e instanceof Error ? e.message : String(e), details: {} } }
        }
        return Response.json({ type: 'server-response', rpcId: body.rpcId, result })
      },
    }), `dsh-shisei: rpc ${endpoint}`)
  }
}

/**
 * Key bridge for the agent's shell. dsh scrubs variables named like KEY or TOKEN from model shells, so shisei.py
 * cannot see the compshare keys. A loopback HTTP server holds them instead; the shell only gets an
 * unguessable URL in DSH_SHISEI_BRIDGE and can ask for compshare commands, never for the keys.
 */
export function startBridge(credentials, { bin, timeoutMs = 120000 } = {}) {
  const secret = randomBytes(24).toString('hex')
  const server = createServer(async (req, res) => {
    const reply = (code, obj) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)) }
    const route = req.method === 'POST' && req.url?.startsWith(`/${secret}/`) ? req.url.slice(secret.length + 2) : ''
    if (route !== 'compshare' && route !== 'vision') return reply(404, { error: 'not found' })
    const limit = route === 'vision' ? 40e6 : 20000
    let raw = ''
    for await (const chunk of req) { raw += chunk; if (raw.length > limit) return reply(413, { error: 'too large' }) }
    if (route === 'vision') {
      // Image-model chat call for drama-frames --ask / drama-precheck. The endpoint comes from the user's config and the
      // key is added here, so the shell can ask questions about pictures but never learns the key or picks the server.
      // With dsh's default model reading images (and the user not picking the separate one), it answers instead.
      try {
        const body = JSON.parse(raw)
        const key = (await keyEnv(credentials))[await visionKeyName()]
        const vs = await visionState(!!key)
        if (vs.source === 'dsh') {
          try {
            return reply(200, await askDshModel(body, AbortSignal.timeout(600000)))
          } catch (e) {
            // a refused dsh call falls back to the separate model when it has a key and the user did not pick dsh
            if (!key || vs.pref === 'dsh') return reply(502, { error: `dsh model: ${e instanceof Error ? e.message : String(e)}` })
          }
        }
        if (!key) return reply(400, { error: vs.pref === 'dsh' ? "dsh's default model cannot read images" : 'image model key not set in dsh credentials' })
        const r = await fetch(`${await visionBaseUrl()}/chat/completions`, {
          method: 'POST', headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json' },
          // enable_thinking: false is passed on (shisei.py asks it for quick fixed-choice answers); nothing else is
          body: JSON.stringify({ model: body.model, max_tokens: body.max_tokens, messages: body.messages, ...(body.enable_thinking === false ? { enable_thinking: false } : {}) }),
          signal: AbortSignal.timeout(600000),
        })
        res.writeHead(r.status, { 'content-type': 'application/json' }); res.end(await r.text())
      } catch (e) {
        reply(502, { error: e instanceof Error ? e.message : String(e) })
      }
      return
    }
    try {
      const args = bridgeArgs(JSON.parse(raw).args)
      // looked up per request: the CLI is usually installed from the page after dsh started (seen on Windows, 2026-11-13)
      const r = await run(bin ?? compshareBin(), args, await keyEnv(credentials), timeoutMs)
      reply(200, r)
    } catch (e) {
      reply(400, { error: e instanceof Error ? e.message : String(e) })
    }
  })
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const url = `http://127.0.0.1:${server.address().port}/${secret}`
      visionUrl = `${url}/vision`   // the page's own shisei.py runs ask the image model here too (keyEnv)
      resolve({ url, close: () => new Promise((r) => server.close(() => { if (visionUrl === `${url}/vision`) visionUrl = null; r() })) })
    })
  })
}
