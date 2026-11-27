// The user's first-frame instructions from the page: 裁特写 (shisei.py drama-crop), 侧脸相对 (drama-edit --staging) and
// clothes from a picture (drama-wardrobe --picture). Pure helpers: they turn a page request into shisei.py argv and
// refuse anything else. Every one changes project data only; nothing here boots a machine or carries a price.
// lib/host.js runs the argv through runShisei, which drops DSH_SHISEI_BRIDGE (pageEnv): the page's call is the
// user's click, while the same command typed in the director shell is refused by shisei.py.
import { join } from 'node:path'

const UNIT = /^\d{1,2}-\d{1,2}$/u
const CAST = /^[a-z][a-z0-9_]{0,23}$/u
const oneLine = (s, max) => String(s ?? '').replace(/\s+/gu, ' ').trim().slice(0, max)

/** A crop box from the page: four finite fractions, inside the picture, not empty. Returns 'x,y,w,h' (4 decimals). */
export function cropBox(box) {
  if (!Array.isArray(box) || box.length !== 4 || box.some((v) => typeof v !== 'number' || !Number.isFinite(v))) throw new Error('bad crop box')
  const [x, y, w, h] = box
  if (x < 0 || y < 0 || w <= 0 || h <= 0 || x + w > 1.0001 || y + h > 1.0001) throw new Error('bad crop box')
  // rounded down to 4 decimals, so a box flush against the right or bottom edge never rounds past it
  return box.map((v) => String(Math.floor(Math.min(1, Math.max(0, v)) * 10000 + 1e-6) / 10000)).join(',')
}

/** { unit, box?, cast?, note?, clear?, dryRun? } → drama-crop argv. Never a price: the page asks drama-estimate. */
export function cropArgv(dir, payload = {}) {
  const unit = String(payload.unit ?? '')
  if (!UNIT.test(unit)) throw new Error('bad unit')
  const argv = ['drama-crop', dir, unit]
  if (payload.clear === true) argv.push('--clear')
  else {
    argv.push(`--box=${cropBox(payload.box)}`)
    if (payload.cast !== undefined && payload.cast !== null) {
      if (!CAST.test(String(payload.cast))) throw new Error('bad character id')
      argv.push('--cast', String(payload.cast))
    }
    const note = oneLine(payload.note, 200)
    if (note) argv.push(`--note=${note}`)   // `=`: the user's words may start with a dash
  }
  if (payload.dryRun) argv.push('--dry-run')
  return argv
}

/** { unit, on, keyframe?, dryRun? } → drama-edit argv for 侧脸相对 on or off, with the first shot's text as the user left it. */
export function stagingArgv(dir, payload = {}) {
  const unit = String(payload.unit ?? '')
  if (!UNIT.test(unit)) throw new Error('bad unit')
  if (typeof payload.on !== 'boolean') throw new Error('bad staging')
  const argv = ['drama-edit', dir, unit, '--staging', payload.on ? 'profile' : 'none']
  if (payload.keyframe !== undefined && payload.keyframe !== null) {
    const text = oneLine(payload.keyframe, 400)
    if (text.length < 4) throw new Error('首帧的文字太短了')
    argv.push(`--keyframe=${text}`)
  }
  argv.push(`--why=${payload.on ? '用户在页面上打开「两人侧脸相对」' : '用户在页面上关掉「两人侧脸相对」'}`)
  if (payload.dryRun) argv.push('--dry-run')
  return argv
}

/** shisei.py answers in English; the few refusals a page user can run into, said the way the page speaks. */
export function frameError(message) {
  const m = String(message ?? '')
  const big = /enlarged ([\d.]+)x \(at most ([\d.]+)x\)/u.exec(m)
  if (big) return `框太小了：要放大 ${big[1]} 倍，最多 ${big[2]} 倍。把框拉大一些。`
  if (/say whose close-up/u.test(m)) return '首帧里有两个人：先选这是谁的特写。'
  if (/nothing to change/u.test(m)) return '没有改动。'
  if (/no approved portrait/u.test(m)) return '照图换衣要靠通过了的定妆照认人：先通过定妆照，或者只用文字描述。'
  if (/looking young: no redraws from a picture/u.test(m)) return '标了「看起来年轻」的角色不照图重画，只能按素材库里的文字描述换。'
  if (/not from the director shell/u.test(m)) return '这一步只能由你在皮影页面上做。'
  if (/has no crop/u.test(m)) return '这一段没有裁特写。'
  if (/draws a medium two-shot/u.test(m)) return '「侧脸相对」画的是中景双人：首帧的文字不要以近景或特写开头。'
  if (/puts two people face to face/u.test(m)) return '「侧脸相对」只用在首镜正好两个人的段。'
  return m
}

// The errors a page user commonly meets, said the way the page speaks, each with where to go next. shisei.py and node
// answer in English; anything not listed keeps its text (with home-folder paths shortened, so they never reach the page).
const PAGE_ERRORS = [
  [/no source video uploaded/u, () => '还没上传视频：先选一段舞蹈视频。'],
  [/these keyframes are not approved yet: ([\w ,-]+)/u, (m) => `这些首帧还没通过：${m[1]}。先在项目页审完再拍。`],
  [/^(\S+) has no take picked/u, (m) => `${m[1]} 还没选条：在项目页点一条画面选它，再合成。`],
  [/^(\S+): the first frame was rejected; remake it/u, (m) => `${m[1]} 的首帧被打回了：先重画首帧、重拍这一段，再合成。`],
  [/not a readable video/u, () => '这个视频读不出来：换一个 mp4 或 mov 文件再试。'],
  [/ffmpeg failed/u, () => '处理视频没成功：换个起点或换一个视频再试。'],
  [/every machine is in use/u, () => '机器都在给别的项目用：等其中一台跑完，或者在「机器」页再登记一台。'],
  [/machine \S+ is in use by (\S+) \(lease runs (\d+) more min/u, (m) => `这台机器正在给别的项目用（${m[1]}）：可以排队等它跑完，或者换一台。`],
  [/queued (\d+) min and no machine came free/u, (m) => `排队等了 ${m[1]} 分钟，机器还是没空出来：什么也没开、没花钱。可以再排一次，或者在「开始」页再建一台。`],
  [/price changed while queued: confirmed ¥([\d.]+), now ¥([\d.]+)/u, (m) => `排队时价钱变了：确认的是 ¥${m[1]}，现在要 ¥${m[2]}。什么也没开、没花钱，重新确认价钱再拍。`],
  [/compshare keys not available/u, () => '还没有填优云智算（compshare）的密钥：到皮影的「设置」里填公钥和私钥。'],
  [/compshare-cli not found/u, () => '还没装 compshare 命令行：到皮影的「设置」里点「安装 compshare-cli」。'],
  [/instance_id not configured/u, () => '还没有 GPU 机器：到皮影的「开始」接上一台（插件可以帮你建）。'],
  [/ssh_key not configured or missing/u, () => '还没有配 ssh 私钥（或文件不在了）：到皮影的「设置」里填私钥的位置。'],
  [/over the project budget: spent ¥([\d.]+) \+ this run ≈ ¥([\d.]+) is ¥([\d.]+) above the cap ¥(\d+(?:\.\d+)?)\. Remake/u,
    (m) => `超出这个项目的上限：已花（估）¥${m[1]}，这次约 ¥${m[2]}，加起来比上限 ¥${m[4]} 多 ¥${m[3]}。这次少拍几条，或者调高上限。`],
  [/over the project budget: spent ¥([\d.]+) \+ this run ≈ ¥([\d.]+) is ¥([\d.]+) above the cap ¥(\d+(?:\.\d+)?)/u,
    (m) => `超出这个项目的上限：已花（估）¥${m[1]}，这次约 ¥${m[2]}，加起来比上限 ¥${m[4]} 多 ¥${m[3]}。每段少拍几条、少拍几段，或者点页面顶上的「已花（估）」调高上限。`],
  [/out of stock in every zone right now/u, () => '这种显卡现在各可用区都没货，什么也没建。可以稍后再查，或者让页面有货时提醒你。'],
  [/over the ¥(\d+(?:\.\d+)?) setup budget/u, (m) => `准备机器最多可能花到的钱超过了上限 ¥${m[1]}，没有建。`],
  // the platform's own reason, when it gave one (a new account: no balance, or not verified yet), instead of "try later"
  [/the machine was not created[^\n]*?(?:余额|欠费|balance|arrear)/iu, () => '机器没有建成，也没有扣费：优云智算说账户余额不足。到优云智算控制台充值后再点。'],
  [/the machine was not created[^\n]*?(?:实名|认证|verif)/iu, () => '机器没有建成，也没有扣费：优云智算账号还没实名认证。到控制台完成实名认证后再点。'],
  [/the machine was not created[^\n]*?"message":\s*"((?:[^"\\]|\\.){1,200})"/u, (m) => `机器没有建成，也没有扣费。优云智算的回答：${m[1]}`],
  [/the machine was not created/u, () => '机器没有建成，也没有扣费：稍后再试一次。'],
  [/^machine setup stopped: (.+)$/su, (m) => `机器没有建成，也没有扣费。出错的地方：${m[1]}`],
  [/could not start the machine: this GPU type is out of stock in the zone/u,
    () => '这台机器所在的可用区现在没货，开不了机。机器停着，只收磁盘费；稍后再点继续。'],
  [/could not start the machine: (.+)$/u, (m) => `机器开不起来：${m[1]}。机器停着，只收磁盘费；稍后再点继续。`],
  [/could not put your ssh key on the machine/u, () => '没能把 ssh 公钥放进这台机器（机器已停下）：在优云智算控制台给这台机器加上公钥，再点继续。'],
  [/compshare-cli install failed: pip install[^\n]*(?:stopped after|ConnectTimeout|ReadTimeout|Read timed out|ConnectionError|NewConnectionError|ProxyError|SSLError|No matching distribution|Could not find a version)/u,
    () => '装 compshare-cli 要从 PyPI（pypi.org）下载，这台电脑连不上或太慢：检查网络或代理（开了代理的话确认它对 pypi.org 生效），再点安装。'],
  [/compshare-cli install failed/u, () => '装 compshare-cli 没有成功：展开技术细节看 pip 的报错；还不行就把它发给作者。'],
  [/^python unusable/u, () => '这台电脑上的 Python 用不了：没找到，或者版本太旧（要 3.9 或更新）。到 python.org 装 Python 3.12（Windows 安装时勾上 py 启动器和 Add to PATH），装好后重启 dsh 再试。'],
  [/^(?:ssh|scp|ssh-keygen) not found: this computer has no OpenSSH client/u, () => '这台电脑没有 ssh 工具（OpenSSH 客户端），插件靠它登录 GPU 机器。Windows：设置 › 系统 › 可选功能 › 添加功能，装「OpenSSH 客户端」；装好后重启 dsh 再点。'],
  [/no ssh key pair|no ssh key: none of/u, () => '这台电脑还没有 ssh 密钥：到「开始」的 GPU 机器一步点「生成 ssh 密钥」，再点继续。'],
  [/no ssh forward \(InternalPort 23\)/u, () => '这台机器没有 ssh 端口映射，插件连不上它：在优云智算控制台看一下这台机器的状态。'],
  [/ssh never connected/u, () => '机器开着（在计费），但一直登录不上（公钥没放进去，或者网络不通）：到「机器」页停机，再到「设置」看登录方式。'],
  [/must be stopped before its environment is updated/u, () => '这台机器开着：等它停下（或在「机器」页停机）再更新环境。'],
  [/is not a registered machine; only registered machines are updated/u, () => '只能更新登记过的机器：先在「机器」页登记它。'],
  [/did not give a price for/u, () => '优云智算没给出这台机器的价钱，什么也没开：稍后再试。'],
  [/setup\.sh failed|did not finish within \d+ minutes|model files missing after setup|ComfyUI lacks nodes after setup/u,
    () => '装环境没有成功（机器已停下，没有删除）：再试一次会接着装；还不行就把「准备 GPU 机器」的记录发给作者。'],
  [/security check (?:after setup )?found something/u, () => '安全检查在新机器上发现了不是本插件的东西：机器已停下、没有使用。不要再用这台，到控制台自己处理。'],
  [/no story yet/u, () => '还没有剧本：先让导演写剧本和分镜。'],
  [/the reference video is (\d+) s; at most (\d+) s/u, (m) => `对标片最长 3 分钟，这条有 ${m[1]} 秒。剪短一点再加。`],
  [/not a video file/u, () => '对标片要是视频文件（mp4、mov、webm）。'],
  [/could not read the reference video|could not read shot \d+ of the reference video/u, () => '读不出这条对标片（是视频文件吗？）。'],
  [/no reference video/u, () => '这个项目还没有对标片。'],
  [/ffmpeg install finished \(exit \d+\) but ffmpeg 4\.4\+ is still not found; try in a terminal: (.+)$/u,
    (m) => `ffmpeg 装完了，但还是找不到能用的 ffmpeg（要 4.4 或更新）。请在终端里运行：${m[1]}`],
  [/ffmpeg cannot be installed from here: (.+)$/u, (m) => `这台电脑没法在这里装 ffmpeg：${m[1]}`],
  [/^(?:ffmpeg|ffprobe) not found$|ffmpeg and ffprobe not found|ffmpeg not found on this computer|ffprobe not found on this computer/u,
    () => '这台电脑没装 ffmpeg（本机处理视频要用它，不下载模型）。到皮影的「设置」里点「安装 ffmpeg」，或在终端里自己装。'],
  [/ffmpeg is too old to find cuts/u, () => '这台电脑的 ffmpeg 太旧，找不到切点（要 4.4 或更新）。升级 ffmpeg 后再加对标片。'],
  [/this job is a remake, not a drama/u, () => '这是翻拍的作业，不是短剧项目。'],
  [/not a job folder|ENOENT[^\n]*job\.json/u, () => '找不到这个项目：它的文件夹可能被删掉或移走了。回首页看看还有哪些项目。'],
  [/job\.json[^\n]*JSON|JSON[^\n]*job\.json/u, () => '这个项目的 job.json 坏了，读不出来。'],
  [/title is required/u, () => '先写片名。'],
  [/^empty message$/u, () => '消息是空的。'],
  [/"transport_error"|SSLEOFError|SSLError/u, () => '和优云智算（compshare）的连接断了（重试几次也没连上）。检查网络或代理，再点继续；已完成的步骤会跳过。机器如果开着会照常计费，空闲 10 分钟会自动停机。'],
  [/ETIMEDOUT|timed out/iu, () => '等太久没有回应（网络或机器太慢）。稍后再试一次。'],
  [/ENOTFOUND|ECONNREFUSED|ECONNRESET|getaddrinfo|Network is unreachable/u, () => '网络连不上：检查网络或代理，再试一次。'],
]
/** Any error message → what the page shows. Known refusals become plain Chinese with the next step; paths are shortened. */
export function pageError(message) {
  const m = String(message ?? '')
  for (const [re, say] of PAGE_ERRORS) { const x = re.exec(m); if (x) return say(x) }
  return frameError(m).replace(/(?:\/Users|\/home|\/private\/var|[A-Z]:\\Users)[^\s'"，。:：)]*/gu, (p) => `…/${p.split(/[\\/]/u).filter(Boolean).pop()}`)
}

// A director turn that failed: these come from the director's model (dsh's LLM provider), never from compshare, so the
// next step is dsh's model settings. Kept apart from pageError, where a 401 or an empty balance could be compshare's.
const MODEL_ERRORS = [
  // a key that is there but refused: dsh's settings show a green dot for any key filled in, so say it was refused
  [/invalid credential|unauthori[sz]ed|\b401\b|invalid api key/iu, () => '导演用的模型拒绝了密钥（401）：dsh 设置里模型旁的绿点只说明填了密钥，不代表能用。换一个能用的密钥或模型，再点重试。'],
  [/api key (?:is )?(?:missing|not set)|apiKeyEnv/iu, () => '导演用的模型没有可用的密钥：到 dsh 的设置里检查默认模型和它的 API 密钥，再点重试。'],
  [/\b429\b|rate limit|quota|insufficient (?:balance|quota)/iu, () => '导演用的模型这会儿被限流或额度用完了：稍后再试，或在 dsh 的设置里换一个模型。'],
  [/model[^\n]{0,40}not (?:found|exist)|unknown model|no model selected/iu, () => '导演用的模型找不到：到 dsh 的设置里重新选默认模型，再点重试。'],
]
/** Why a director turn failed, in page words. */
export function directorError(message) {
  const m = String(message ?? '')
  for (const [re, say] of MODEL_ERRORS) { const x = re.exec(m); if (x) return say(x) }
  return pageError(m)
}

// The one subfolder file the page shows: the clothes picture of a picture wardrobe (drama-status cast[].outfit_ref.file).
const WARDROBE_FILE = /^wardrobe\/[a-z][a-z0-9_]{0,23}_[0-9a-f]{12}\.png$/u
/** Path of `wardrobe/<cast>_<checksum>.png` inside a job folder, or null when `name` is not exactly that shape. */
export function wardrobeFile(jobDir, name) {
  return WARDROBE_FILE.test(String(name ?? '')) ? join(jobDir, ...String(name).split('/')) : null
}
