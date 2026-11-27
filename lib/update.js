// Is there a newer dsh-shisei? dsh itself never updates plugins and its plugin page lists no registry versions
// (upgrading is `dsh plugin add <pkg>@<version>`, then a restart), so the plugin says so on its own page.
// Pure parts (versions, ranges, what to show, the command) are exported for tests; the lookup asks the registry the
// profile's own pnpm is configured with (`pnpm view`, run in the profile directory, the way dsh's plugin manager
// inspects a spec), at most once a day, and never installs anything.
import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'

export const PACKAGE = 'dsh-shisei'
export const CHECK_EVERY_MS = 24 * 3600 * 1000
const VIEW_TIMEOUT_MS = 15000

const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/u

/** '0.2.0-rc.2' → { major, minor, patch, pre: ['rc', 2] }; null when it is not a version. */
export function parseVersion(v) {
  const m = VERSION_RE.exec(String(v ?? '').trim())
  if (!m) return null
  const pre = m[4] ? m[4].split('.').map((p) => (/^\d+$/u.test(p) ? Number(p) : p)) : []
  return { major: Number(m[1]), minor: Number(m[2]), patch: Number(m[3]), pre }
}

/** semver precedence: negative when a < b, 0 when equal, positive when a > b. Unparsable sorts lowest. */
export function compareVersions(a, b) {
  const x = typeof a === 'string' ? parseVersion(a) : a
  const y = typeof b === 'string' ? parseVersion(b) : b
  if (!x || !y) return (x ? 1 : 0) - (y ? 1 : 0)
  for (const k of ['major', 'minor', 'patch']) if (x[k] !== y[k]) return x[k] - y[k]
  if (!x.pre.length || !y.pre.length) return (x.pre.length ? -1 : 0) - (y.pre.length ? -1 : 0)
  for (let i = 0; i < Math.max(x.pre.length, y.pre.length); i++) {
    const p = x.pre[i]
    const q = y.pre[i]
    if (p === undefined || q === undefined) return p === undefined ? -1 : 1
    if (p === q) continue
    if (typeof p === 'number' && typeof q === 'number') return p - q
    if (typeof p === 'number' || typeof q === 'number') return typeof p === 'number' ? -1 : 1
    return p < q ? -1 : 1
  }
  return 0
}

// the lowest version a caret range excludes: ^1.2.3 → 2.0.0-0, ^0.2.3 → 0.3.0-0, ^0.0.3 → 0.0.4-0
function caretCeiling(v) {
  if (v.major > 0) return { major: v.major + 1, minor: 0, patch: 0, pre: [0] }
  if (v.minor > 0) return { major: 0, minor: v.minor + 1, patch: 0, pre: [0] }
  return { major: 0, minor: 0, patch: v.patch + 1, pre: [0] }
}

/**
 * Does `version` satisfy `range`, prereleases included (as dsh checks peers: semver.satisfies with
 * includePrerelease)? Handles what dsh plugins write: `||` unions of `^x.y.z`, `~x.y.z`, exact versions and
 * `>=` / `<` pairs. Anything else answers null (unknown), so the caller never calls a plugin incompatible on a guess.
 */
export function satisfiesRange(version, range) {
  const v = parseVersion(version)
  if (!v || typeof range !== 'string' || !range.trim()) return null
  let unknown = false
  for (const alt of range.split('||')) {
    const parts = alt.trim().split(/\s+/u).filter(Boolean)
    if (!parts.length) { unknown = true; continue }
    let ok = true
    for (const part of parts) {
      const m = /^(\^|~|>=|<=|>|<|=)?(.+)$/u.exec(part)
      const b = m && parseVersion(m[2])
      if (!b) { ok = null; break }
      const c = compareVersions(v, b)
      const op = m[1] || '='
      if (op === '^') ok = ok && c >= 0 && compareVersions(v, caretCeiling(b)) < 0
      else if (op === '~') ok = ok && c >= 0 && compareVersions(v, { major: b.major, minor: b.minor + 1, patch: 0, pre: [0] }) < 0
      else if (op === '>=') ok = ok && c >= 0
      else if (op === '>') ok = ok && c > 0
      else if (op === '<=') ok = ok && c <= 0
      else if (op === '<') ok = ok && c < 0
      else ok = ok && c === 0
    }
    if (ok === true) return true
    if (ok === null) unknown = true
  }
  return unknown ? null : false
}

/** A prerelease (rc) install follows `next` as well as `latest`; a release follows `latest` only. */
export function channelsFor(installed) {
  const v = parseVersion(installed)
  return v && v.pre.length ? ['next', 'latest'] : ['latest']
}

/**
 * How the profile names this plugin: a registry spec (a version, range or tag) can be upgraded with `add`; a local
 * path, link, tarball or git address is someone's own copy (a developer's checkout) and gets no notice.
 */
export function installSource(spec) {
  if (typeof spec !== 'string' || !spec.trim()) return 'unknown'
  const s = spec.trim()
  if (/^(file|link|portal|workspace|git|git\+[a-z]+|github|gitlab|bitbucket|https?):/iu.test(s)) return 'local'
  if (/^npm:/u.test(s)) return 'registry'
  if (/^[./~]/u.test(s) || /^[A-Za-z]:[\\/]/u.test(s) || s.includes('/')) return 'local'
  return 'registry'
}

/** dsh Desktop's own profile: the `dsh` command refuses it ("managed exclusively by the Electron application"). */
export function isDesktopProfile(profile) {
  return String(profile ?? '').toLowerCase() === 'desktop'
}

/** The command the page shows; the profile name is quoted only when the shell would need it. */
export function upgradeCommand(profile, version) {
  const p = String(profile ?? '')
  const name = /^[A-Za-z0-9._-]+$/u.test(p) ? p : `'${p.replace(/'/gu, `'\\''`)}'`
  return `dsh plugin --profile ${name} add ${PACKAGE}@${version}`
}

/** dsh's own peers among a version's peerDependencies (the ones dsh checks against its runtime version). */
export function dshPeers(peers) {
  if (!peers || typeof peers !== 'object') return {}
  return Object.fromEntries(Object.entries(peers).filter(([k, r]) => typeof r === 'string' && (k === '@deepseek-ai/dsh' || k.startsWith('@deepseek-ai/dsh-'))))
}

/**
 * What the page shows. state:
 *   none      nothing newer (or not checked, or a local install)
 *   patch     a newer version in the same minor line (1.x: same major)
 *   minor     a newer minor before 1.0 (or major after): may be incompatible, read the changes first
 *   needs-dsh the newer version does not accept this dsh; upgrade dsh first (the command is not shown)
 */
export function updateView({ installed, dsh = null, profile = null, source = 'registry', latest = null, dismissed = null } = {}) {
  const desktop = isDesktopProfile(profile)
  const base = { installed, dsh, state: 'none', version: null, command: null, dismissed: false, needDsh: null, desktop }
  if (source !== 'registry' || !latest || !parseVersion(latest.version) || !parseVersion(installed)) return base
  if (compareVersions(latest.version, installed) <= 0) return base
  const a = parseVersion(installed)
  const b = parseVersion(latest.version)
  const peers = dshPeers(latest.peers)
  const fits = Object.values(peers).map((r) => (dsh ? satisfiesRange(dsh, r) : null))
  const view = { ...base, version: latest.version, dismissed: dismissed === latest.version }
  if (fits.includes(false)) return { ...view, state: 'needs-dsh', needDsh: Object.values(peers)[fits.indexOf(false)] }
  const sameLine = a.major === b.major && (a.major > 0 || a.minor === b.minor)
  return { ...view, state: sameLine ? 'patch' : 'minor', command: profile && !desktop ? upgradeCommand(profile, latest.version) : null }
}

/** The newest of the answers (one per channel); null when none named a version. */
export function newest(answers) {
  return answers.filter((x) => x && parseVersion(x.version)).sort((x, y) => compareVersions(y.version, x.version))[0] ?? null
}

/**
 * Read one `pnpm view <pkg>@<tag> version peerDependencies --json` answer. A package or tag that does not exist yet
 * (not published, no rc on `next`) is not an error: { version: null }. Anything else is { error }.
 */
export function parseView(code, stdout) {
  let j = null
  try { j = JSON.parse(String(stdout ?? '').trim() || 'null') } catch { /* not JSON */ }
  if (j && j.error) {
    const c = String(j.error.code ?? '')
    if (/404|NOT_FOUND|E404|NO_MATCHING_VERSION/u.test(c)) return { version: null }
    return { error: c || 'unknown' }
  }
  if (code !== 0 || !j || typeof j !== 'object') return { error: code === 0 ? 'unreadable' : `exit ${code}` }
  if (Array.isArray(j)) j = j[j.length - 1] ?? {}
  return { version: typeof j.version === 'string' ? j.version : null, peers: j.peerDependencies && typeof j.peerDependencies === 'object' ? j.peerDependencies : {} }
}

// what pnpm needs to reach the registry the user set up (proxy, registry config, its own home); never the compshare
// keys or the model keys the dsh process holds
const PASS_ENV = /^(PATH|HOME|USERPROFILE|APPDATA|LOCALAPPDATA|XDG_[A-Z_]+|TMPDIR|TEMP|TMP|SYSTEMROOT|COMSPEC|PNPM_HOME|NODE_EXTRA_CA_CERTS|SSL_CERT_FILE|SSL_CERT_DIR|https?_proxy|HTTPS?_PROXY|no_proxy|NO_PROXY|ALL_PROXY|all_proxy|npm_config_[a-z_]+|NPM_CONFIG_[A-Z_]+)$/u
export function viewEnv(base = process.env, extra = {}) {
  const env = {}
  for (const [k, v] of Object.entries(base)) if (PASS_ENV.test(k) && v != null) env[k] = v
  return { ...env, ...extra }
}

/** Run one `pnpm view` in the profile directory; resolves { code, stdout }, never rejects. */
export function pnpmView(profile, tag) {
  const pm = profile.packageManager
  const cmd = pm?.command ?? 'pnpm'
  const args = [...(pm?.args ?? []), 'view', `${PACKAGE}@${tag}`, 'version', 'peerDependencies', '--json', '--config.fetch-retries=0']
  return new Promise((resolve) => {
    let out = ''
    let child
    try {
      child = spawn(cmd, args, { cwd: profile.dir, env: viewEnv(process.env, pm?.env ?? {}), stdio: ['ignore', 'pipe', 'ignore'], timeout: VIEW_TIMEOUT_MS, shell: process.platform === 'win32' && !pm, windowsHide: true })
    } catch (e) { resolve({ code: -1, stdout: '' }); return }
    child.stdout.on('data', (d) => { out += d })
    child.on('error', () => resolve({ code: -1, stdout: out }))
    child.on('close', (code) => resolve({ code: code ?? -1, stdout: out }))
  })
}

/** This plugin's own version (its package.json). */
export function pluginVersion() {
  try { return JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8')).version ?? null } catch { return null }
}

/** The running dsh version: dsh-app-boot's package.json, found from the profile's install anchor (dsh's own package). */
export function dshVersion(installAnchor) {
  if (!installAnchor) return null
  try {
    const req = createRequire(installAnchor)
    return JSON.parse(readFileSync(req.resolve('@deepseek-ai/dsh-app-boot/package.json'), 'utf8')).version ?? null
  } catch { /* fall through */ }
  try { return JSON.parse(readFileSync(installAnchor, 'utf8')).version ?? null } catch { return null }
}

/** How the profile's package.json names this plugin (null when it is not a dependency there). */
export function profileSpec(dir) {
  try { return JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')).dependencies?.[PACKAGE] ?? null } catch { return null }
}

/**
 * The update checker: holds the profile facts, keeps its last answer in `file` (also read by `shisei.py doctor`),
 * asks the registry at most once a day unless forced. `view` and `now` are injectable for tests.
 */
export function createUpdates({ file, profile = null, installed = pluginVersion(), dsh = null, view = pnpmView, now = () => Date.now(), enabled = async () => true } = {}) {
  let inflight = null
  const source = () => (profile ? installSource(profileSpec(profile.dir)) : 'unknown')
  async function load() {
    try { return JSON.parse(await readFile(file, 'utf8')) } catch { return {} }
  }
  async function save(data) {
    await mkdir(dirname(file), { recursive: true })
    const tmp = `${file}.${process.pid}.tmp`
    await writeFile(tmp, JSON.stringify(data, null, 2))
    await rename(tmp, file)
  }
  // `ran`: the version that last ran here. A newer one now means the user just upgraded: `upgradedFrom` holds the old
  // one until they close the notice (an older one, a downgrade, says nothing)
  let noted = false
  async function noteRun(d) {
    if (noted || !installed) return d
    noted = true
    if (d.ran === installed) return d
    const up = d.ran && compareVersions(installed, d.ran) > 0
    const next = { ...d, ran: installed, upgradedFrom: up ? d.ran : null }
    try { await save(next) } catch { return d }
    return next
  }
  async function current(data) {
    const d = await noteRun(data ?? (await load()))
    const src = source()
    const v = updateView({ installed, dsh, profile: profile?.name ?? null, source: src, latest: d.latest ?? null, dismissed: d.dismissed ?? null })
    return { ...v, profile: profile?.name ?? null, source: src, checkedAt: d.checkedAt ?? null, error: d.error ?? null, off: !(await enabled()), upgradedFrom: d.upgradedFrom ?? null }
  }
  async function check(force = false) {
    if (!profile || source() !== 'registry' || !(await enabled())) return current()
    const d = await load()
    // gated on the last attempt, failed ones included: offline, the page must not start pnpm on every visit
    const last = d.triedAt ?? d.checkedAt
    const fresh = last && now() - Date.parse(last) < CHECK_EVERY_MS && d.installed === installed
    if (fresh && !force) return current(d)
    inflight ??= (async () => {
      const answers = []
      let error = null
      for (const tag of channelsFor(installed)) {
        const { code, stdout } = await view(profile, tag)
        const r = parseView(code, stdout)
        if (r.error) error = r.error
        else answers.push(r)
      }
      const best = newest(answers)
      // a failed lookup keeps the last good answer (the notice stays), and says when it last worked
      const at = new Date(now()).toISOString()
      // re-read before writing: a version skipped while pnpm was answering stays skipped
      const nowOnDisk = await load()
      const next = { ...nowOnDisk, installed, triedAt: at, checkedAt: error && !answers.length ? nowOnDisk.checkedAt ?? null : at, error: answers.length ? null : error,
        latest: answers.length ? (best ? { version: best.version, peers: dshPeers(best.peers) } : null) : nowOnDisk.latest ?? null }
      await save(next)
      return next
    })().finally(() => { inflight = null })
    return current(await inflight)
  }
  async function dismiss(version) {
    const d = await load()
    await save({ ...d, dismissed: version ? String(version) : null })
    return current()
  }
  async function seen() {
    const d = await noteRun(await load())
    await save({ ...d, upgradedFrom: null })
    return current()
  }
  return { current: () => current(), check, dismiss, seen }
}
