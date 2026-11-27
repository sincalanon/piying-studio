// The tools this computer needs, made ready quietly when Shisei opens, so no page shows an install step:
//   compshare-cli — from the wheels shipped with the plugin (shisei.py install-cli, offline);
//   ffmpeg       — the plugin's own static build, from the npm package dsh-shisei-ffmpeg-<platform>-<arch>, installed
//                  with dsh's own pnpm into ~/.dsh/shisei/tools (the registry, mirror and proxy the user set up for dsh;
//                  pnpm checks the package's integrity). Skipped when this computer already has a usable ffmpeg.
// Only a failure is shown (the 开始 page and its menu entry), with a retry. Pure helpers are exported for the tests.
import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import { viewEnv } from './update.js'

export const FFMPEG_PKG_VERSION = '9.0.2'
const FFMPEG_TARGETS = new Set(['darwin-arm64', 'darwin-x64', 'win32-x64', 'win32-arm64'])
const PNPM_TIMEOUT_MS = 15 * 60 * 1000

/** The ffmpeg package for this computer, or null when there is none for it (Linux: the system's ffmpeg). */
export function ffmpegPackage(platform = process.platform, arch = process.arch) {
  const target = `${platform}-${arch}`
  return FFMPEG_TARGETS.has(target) ? `dsh-shisei-ffmpeg-${target}` : null
}

/** pnpm's arguments to put that package in `dir` (its own package.json there; no scripts, exact version). */
export function ffmpegAddArgs(pkg, dir, version = FFMPEG_PKG_VERSION) {
  return ['add', `${pkg}@${version}`, '--dir', dir, '--save-exact', '--ignore-scripts', '--config.fetch-retries=2', '--reporter=append-only']
}

/** Is a pnpm failure "no such package / version" (not published for this computer yet) rather than a real failure?
 * pnpm prints its errors on stdout, so pass both streams. */
export function notPublished(output) {
  return /ERR_PNPM_FETCH_404|ERR_PNPM_NO_MATCHING_VERSION|E404|404 Not Found|Not Found - 404/u.test(String(output ?? ''))
}

/** The lines of a pnpm failure worth showing under 技术细节: its ERR_PNPM_ line(s), else the last lines it printed.
 * pnpm prints errors on stdout (with --reporter=silent it prints nothing at all, seen on Windows 2026-11-13). */
export function pnpmError(r) {
  const lines = `${r?.stdout ?? ''}\n${r?.stderr ?? ''}`.split(/\r?\n/u).map((l) => l.trim()).filter(Boolean)
  const err = lines.filter((l) => /ERR_PNPM_|^Error\b|E[A-Z]{3,}\b/u.test(l))
  return (err.length ? err : lines.slice(-3)).join(' | ').slice(-400) || `pnpm exit ${r?.code}`
}

/** Run dsh's pnpm (the profile's package manager) with `args`, in the profile's folder; resolves { code, stdout,
 * stderr }, never rejects. */
export function runPnpm(profile, args, { timeout = PNPM_TIMEOUT_MS } = {}) {
  const pm = profile?.packageManager
  const cmd = pm?.command ?? 'pnpm'
  return new Promise((resolve) => {
    let err = ''
    let out = ''
    let child
    try {
      child = spawn(cmd, [...(pm?.args ?? []), ...args], {
        cwd: profile?.dir,
        env: viewEnv(process.env, pm?.env ?? {}),
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout,
        shell: process.platform === 'win32' && !pm,
        windowsHide: true,
      })
    } catch (e) {
      resolve({ code: -1, stdout: '', stderr: String(e) })
      return
    }
    child.stdout.on('data', (d) => { out = (out + d).slice(-4000) })
    child.stderr.on('data', (d) => { err = (err + d).slice(-4000) })
    child.on('error', (e) => resolve({ code: -1, stdout: out, stderr: String(e) }))
    child.on('close', (code) => resolve({ code: code ?? -1, stdout: out, stderr: err }))
  })
}

/**
 * Get this computer ready. `run(argv)` runs shisei.py and resolves its JSON answer; `pnpm(args)` runs dsh's pnpm.
 * Returns { cli, ffmpeg }, each { state: 'ready' | 'unsupported' | 'unavailable' | 'failed', error? }:
 * 'unsupported' / 'unavailable' (no package for this computer, or no pnpm to fetch it) are not failures — the page then
 * offers what it did before (settings › 安装 ffmpeg).
 */
export async function prepare({ run, pnpm, home, platform = process.platform, arch = process.arch, onState = () => {} }) {
  const out = { cli: { state: 'preparing' }, ffmpeg: { state: 'preparing' } }
  const say = () => onState(JSON.parse(JSON.stringify(out)))
  say()
  const cli = await run(['install-cli', '--if-missing'])
  out.cli = cli?.ok ? { state: 'ready' } : { state: 'failed', error: String(cli?.error ?? 'install-cli failed') }
  say()
  let ff = await run(['ffmpeg-check'])
  if (ff?.ok && ff.ready) {
    out.ffmpeg = { state: 'ready', version: ff.version ?? null }
  } else {
    const pkg = ffmpegPackage(platform, arch)
    if (!pkg) out.ffmpeg = { state: 'unsupported' }
    else if (!pnpm) out.ffmpeg = { state: 'unavailable' }
    else {
      out.ffmpeg = { state: 'downloading' }
      say()
      // the registry the profile is set up with (its .npmrc, a mirror): `--dir` would read the tools folder's instead
      const reg = await pnpm(['config', 'get', 'registry'])
      const registry = /^https?:\/\/\S+$/u.test(String(reg?.stdout ?? '').trim()) ? String(reg.stdout).trim() : null
      const dir = join(home, 'tools')
      await mkdir(dir, { recursive: true }).catch(() => {})   // pnpm add --dir wants the folder to be there
      const r = await pnpm([...ffmpegAddArgs(pkg, dir), ...(registry ? [`--registry=${registry}`] : [])])
      if (r.code !== 0) {
        out.ffmpeg = notPublished(`${r.stdout ?? ''}\n${r.stderr ?? ''}`) ? { state: 'unavailable' } : { state: 'failed', error: pnpmError(r) }
      } else {
        ff = await run(['ffmpeg-check'])
        out.ffmpeg = ff?.ok && ff.ready ? { state: 'ready', version: ff.version ?? null } : { state: 'failed', error: 'ffmpeg downloaded but does not run here' }
      }
    }
  }
  say()
  return out
}
