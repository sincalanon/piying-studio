// dsh-shisei: registers the bundled Shisei skills with the dsh skill registry.
// Plain ESM, no build step and no runtime dependencies, so installing the
// plugin never asks for build-script approval.
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { delimiter, isAbsolute, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHandler, directorAsks, prepareComputer, PY_UTF8, pythonCommand, registerFileRoutes, registerRoutes, setAttachments, setModelServices, setSessionService, setUpdateProfile, startBridge, startPatrol } from './host.js'
import { dshVersion } from './update.js'

/** Cordis plugin identity. */
export const name = 'dsh-shisei'

/** Services this plugin needs from the host. */
export const inject = ['skills']

// Same value as BUNDLED_SKILL_RANK in @deepseek-ai/dsh-skill (600). Kept local
// so the plugin does not depend on that package resolving from the profile.
export const SKILL_RANK = 600

export const PROVIDER = 'dsh-shisei'

const DEFAULT_ROOT = fileURLToPath(new URL('../skills/', import.meta.url))

/**
 * Split a SKILL.md into its description and body. Frontmatter is limited to
 * single-line `key: value` pairs; `description` is required.
 * @param {string} raw
 * @param {string} path
 */
export function parseSkill(raw, path) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/u.exec(raw)
  if (!m) throw new Error(`dsh-shisei: ${path} has no YAML frontmatter`)
  const meta = {}
  for (const line of m[1].split(/\r?\n/u)) {
    const kv = /^([A-Za-z][\w-]*):\s*(.*)$/u.exec(line)
    if (!kv) continue
    let value = kv[2].trim()
    if (/^(["']).*\1$/u.test(value)) value = value.slice(1, -1)
    meta[kv[1]] = value
  }
  if (!meta.description) throw new Error(`dsh-shisei: ${path} has no description`)
  return { name: meta.name, description: meta.description, content: raw.slice(m[0].length).trim() }
}

/**
 * Build one candidate per skill folder under `root`.
 * @param {string} root absolute directory holding <skill>/SKILL.md
 */
export function loadCandidates(root) {
  if (!isAbsolute(root)) throw new Error('dsh-shisei: skillRoot must be absolute')
  return readdirSync(root)
    .filter((d) => statSync(join(root, d)).isDirectory())
    .sort()
    .map((dir) => {
      const directory = join(root, dir)
      const locator = join(directory, 'SKILL.md')
      const { name: declared, description } = parseSkill(readFileSync(locator, 'utf8'), locator)
      if (declared && declared !== dir) throw new Error(`dsh-shisei: ${locator} names "${declared}" but lives in "${dir}"`)
      return {
        name: dir,
        description,
        invocation: { modelInvocable: true, userInvocable: true },
        provider: PROVIDER,
        source: 'bundled',
        rank: SKILL_RANK,
        resourceBase: { kind: 'directory', path: directory },
        locator,
      }
    })
}

const SCRIPT = fileURLToPath(new URL('../skills/shisei-motion/scripts/shisei.py', import.meta.url))

/** Is `cmd` a file, or a program on PATH (with PATHEXT on Windows)? */
export function onPath(cmd, env = process.env, platform = process.platform, exists = existsSync) {
  if (isAbsolute(cmd)) return exists(cmd)
  const exts = platform === 'win32' ? ['', ...(env.PATHEXT ?? '.COM;.EXE;.BAT;.CMD').split(';').filter(Boolean)] : ['']
  const dirs = (env.PATH ?? env.Path ?? '').split(platform === 'win32' ? ';' : delimiter).filter(Boolean)
  return dirs.some((d) => exts.some((x) => exists(join(d, cmd + x))))
}

const quote = (s, ps) => (ps ? `'${s.replaceAll("'", "''")}'` : (/^[\w./:@+-]+$/u.test(s) ? s : `'${s.replaceAll("'", `'\\''`)}'`))

/**
 * The section appended to every Shisei skill when it is loaded: how to run shisei.py on this computer, written for the
 * shell the director has here (dsh gives it PowerShell on Windows and bash elsewhere), with the absolute script path,
 * so the director neither guesses the plugin folder nor reaches for a `python3` that on Windows is the Store stub.
 * Like dsh-skill-office's "Installed LibreOffice Kit". Pure, so it is tested for both systems.
 */
export function runtimeSection({ platform = process.platform, env = process.env, script = SCRIPT, exists = existsSync } = {}) {
  const ps = platform === 'win32'
  const py = pythonCommand(env, platform, { exists })
  const utf8 = py.args.includes('utf8') ? [] : PY_UTF8   // SHISEI_PYTHON may carry it already
  const line = [ps ? '&' : null, quote(py.cmd, ps), ...py.args, ...utf8, '-I', quote(script, ps)].filter(Boolean).join(' ')
  const out = ['', '', '## 在这台电脑上运行 shisei.py', '',
    `上文写成 \`shisei <命令> …\` 的，在这台电脑上一律这样运行（命令行是 ${ps ? 'PowerShell' : 'bash'}，路径已经是绝对路径，照抄，不要换成别的 Python 或去找插件目录）：`, '',
    '```' + (ps ? 'powershell' : 'bash'), `${line} drama-status <项目目录>`, '```']
  if (ps) out.push('', 'PowerShell 里：每个带空格的参数用单引号括起来；不要用 `\\` 续行（写在一行里）、`2>/dev/null`、`&&`（多条命令用 `;` 分开）。')
  if (!onPath(py.cmd, env, platform, exists)) {
    out.push('', `这台电脑上找不到 \`${py.cmd}\`：请用户先装 Python 3（${ps ? 'python.org 的安装包，会带上 py 启动器' : 'macOS 可用 Homebrew：brew install python'}），装好后重启 dsh。`) // zh-ok
  }
  return out.join('\n')
}

/**
 * The provider object the registry calls.
 * @param {ReturnType<typeof loadCandidates>} candidates
 * @param {string} [runtime] appended to every skill's text (runtimeSection())
 */
export function createProvider(candidates, runtime = '') {
  return {
    name: PROVIDER,
    list: () => Promise.resolve(candidates),
    async get(candidate, options = {}) {
      const { rank: _rank, locator, ...summary } = candidate
      const raw = await readFile(locator, { encoding: 'utf8', signal: options.signal })
      return { ...summary, content: parseSkill(raw, locator).content + runtime }
    },
  }
}

/**
 * @param {{ skills: { registerProvider(create: () => unknown): () => void } }} ctx
 * @param {{ skillRoot?: string }} [config]
 */
export function apply(ctx, config = {}) {
  const provider = createProvider(loadCandidates(config.skillRoot ?? DEFAULT_ROOT), runtimeSection())
  ctx.skills.registerProvider(() => provider)

  // The profile dsh launched (name, directory, its pnpm): what an upgrade command needs. Absent outside a dsh profile.
  let profile = null
  try { profile = typeof ctx.get === 'function' ? ctx.get('profileContext') ?? null : null } catch { /* not a dsh profile */ }
  if (profile) {
    const updates = setUpdateProfile(profile, dshVersion(profile.installAnchor))
    // asked a minute after start, then looked at every 6 hours (it asks the registry only when its answer is a day old)
    ctx.effect?.(() => {
      const ask = () => { updates.check().catch(() => {}) }
      const first = setTimeout(ask, 60000)
      const every = setInterval(ask, 6 * 3600 * 1000)
      first.unref?.()
      every.unref?.()
      return () => { clearTimeout(first); clearInterval(every) }
    }, 'dsh-shisei: update check')
  }

  // Optional services: the skill above must keep working in profiles that lack them (headless has no web connection).
  if (typeof ctx.inject !== 'function') return
  ctx.inject(['llm', 'agentDefaultModel'], (m) => {
    setModelServices({ llm: m.llm, agentDefaultModel: m.agentDefaultModel })
    m.effect?.(() => () => setModelServices(null), 'dsh-shisei: model info')
  })
  // dsh's attachment store: the image check asks dsh's own model with stored pictures (lib/vision.js)
  ctx.inject(['attachments'], (a) => {
    setAttachments(a.attachments)
    a.effect?.(() => () => setAttachments(null), 'dsh-shisei: attachments')
  })
  ctx.inject(['userQuestions'], (q) => {
    directorAsks.setQuestionService(q.userQuestions)
    q.effect?.(() => () => directorAsks.setQuestionService(null), 'dsh-shisei: director questions')
  })
  ctx.inject(['sessionController'], (sc) => {
    setSessionService(sc.sessionController)
    sc.effect?.(() => () => setSessionService(null), 'dsh-shisei: director sessions')
  })
  ctx.inject(['credentials'], (c) => {
    // one patrol per dsh process, started with the credentials and stopped with them
    let patrol = null
    c.effect?.(() => { patrol = startPatrol(c.credentials); return () => { patrol?.stop(); patrol = null } }, 'dsh-shisei: machine patrol')
    const handle = createHandler(c.credentials, undefined, { last: () => patrol?.last() ?? null, tick: () => patrol?.tick() })
    c.inject(['connection'], (cc) => {
      registerRoutes(cc, handle)
      registerFileRoutes(cc)
      prepareComputer()   // in the background: the tools are ready before the user reaches the key check
      // The director's questions and permission asks go to the Shisei page, so only where there is a page (a web
      // connection); other sessions' go on to dsh's own UI. Prepended, so a Shisei director's ask is claimed here
      // before dsh forwards it to a browser that never shows it.
      if (typeof cc.on === 'function') {
        cc.on('approval/request', (req, next) => directorAsks.onApproval(req, next), true)
        cc.on('user-questions/request', (req, next) => directorAsks.onQuestion(req, next), true)
      }
    })
    c.inject(['shellEnv'], (s) => {
      s.effect(() => {
        let url
        let close
        const ready = startBridge(c.credentials).then((b) => { url = b.url; close = b.close })
        const off = s.shellEnv.register({
          name: 'dsh-shisei-bridge',
          variables: { DSH_SHISEI_BRIDGE: { description: 'Loopback URL shisei.py uses to run compshare commands with the keys stored in dsh.' } },
          resolve: () => (url ? { DSH_SHISEI_BRIDGE: url } : {}),
        })
        return () => {
          if (typeof off === 'function') off()
          ready.then(() => close?.())
        }
      }, 'dsh-shisei: key bridge')
    })
  })
}
