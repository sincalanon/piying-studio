---
name: shisei-motion
description: Shisei Motion（页面上叫「翻拍」，英文 Remake）：把一段真人舞蹈视频变成 3D 白模，再换成用户指定的角色，在用户自己的优云智算（compshare）GPU 上运行。用户说「把这段舞换成某个角色」「翻拍」「换角」「舞蹈换人」（旧说法）「做白模视频」「Shisei Motion」时使用。
---

# Shisei Motion

流程：舞蹈视频 →（GPU）SAM3D Body 动捕出动画 GLB →（本机 Blender）影棚白模视频 →（GPU）MiniMax H3 Ref2VA + 换人 LoRA → 角色视频。

所有操作都通过本 skill 目录下的 `scripts/shisei.py` 完成，下文写成 `shisei <命令>`；在这台电脑上怎么运行它（Python 命令、脚本的绝对路径、命令行是 bash 还是 PowerShell）见本文最后一节「在这台电脑上运行 shisei.py」。每条命令都在 stdout 输出一个 JSON 对象：`ok` 为 false 时，把 `error` 原样告诉用户。

```
shisei doctor
```

## 必须遵守

- **开机只由用户在 Shisei 页面上点。** 先运行 `job-estimate` 和 `up`（不带 `--confirm-billing`，只报价），把 `plan`（GPU、可用区、每小时价格）和预计时长、费用告诉用户，再请用户在 Shisei 页面打开这个项目点「下一步：确认开机」。你跑带 `--confirm-billing` 的命令会被拒绝（用户在对话里同意了也一样），不要申请更宽的权限去开机。
- **安全检查不能跳过。** `up` 会让 ComfyUI 只监听 127.0.0.1，并检查有没有陌生任务、`input/` 里的 `.so` 文件和可疑进程。返回 `alert: true` 时，立即停止提交新任务，告诉用户发现了什么；把已完成的成片拉回后运行 `down --yes`，并提醒用户这台机器不要再复用。
- **机器环境版本。** `up` 的返回里有 `env`：`state` 不是 `current` 时（`old` / `drift` / `missing` / `newer`），把 `hint` 告诉用户，建议等这次跑完、机器停下后，在 Shisei 的「机器」页对这台点「更新环境…」（`newer` 是升级插件），结果可能和预期不一样；不要自己去改机器上的节点或权重，也不要让用户自己 ssh 上去跑 `setup.sh`。见 docs/IMAGE.md。
- 安全检查只认本插件提交的任务。如果用户通过隧道在 ComfyUI 网页界面里提交过任务，也会触发告警：先和用户确认是不是他自己提交的，不要自行放宽。确实需要时，由用户决定是否用 `config set allowed_clients` 加白名单。
- **参考图可以是真人。** 插件不检查、也不阻止真人照片或照着真人（含公众人物）做的形象；用别人的样子之前先取得本人同意，并遵守当地法律（肖像权、深度合成标识）。用户没说这张图是谁、有没有授权时，问一句再开工。
- **看起来年轻的角色**（例如校服感造型）只用原始身形，不改身材，也不换成更暴露的衣服。
- **不删除任何东西。** 本 skill 只会停机（`down`），不会删除实例或磁盘。
- **机器有租约。** `up` 先取机器的租约，别的项目正在用时会拒绝（`machine … is in use by …`）：告诉用户是谁在用、还要多久，不要换着法子重试，也不要停别人的机器（`down` 会拒绝，`--force-other` 在你的 shell 里也会被拒）。工作中平台定时关机保持在 30 分钟内，命令会自己续；跑完设空闲停机并交还租约。
- 密钥由用户在 dsh 的「插件 › dsh-shisei」页面配置，存在 dsh 凭据库。dsh 会把名字含 KEY 的环境变量从你的 shell 里去掉，脚本会自动经插件的本机桥（`DSH_SHISEI_BRIDGE`）调用 compshare，你看不到也不需要密钥。不要尝试读取、打印或另存密钥。`doctor` 报 keys 为空时，请用户去插件页配置。
- 成片要标明是 AI 生成。

## 第一次使用

0. 让用户在 dsh 的「插件 › dsh-shisei」页面填好 compshare 密钥、实例 ID 和 ssh 私钥路径（那里也能装 compshare-cli、看机器状态、开机停机）。
1. `doctor`：检查密钥、compshare-cli、ssh、ffmpeg、Blender。缺 compshare-cli 时运行 `install-cli`（装到 `~/.dsh/shisei/venv`）。
2. `config set instance_id <cpod-...>`：使用的 4090 48G 实例。
3. ssh 密钥：默认自动用 `~/.ssh` 的 id_ed25519 / id_rsa / id_ecdsa，`up` 被实例拒绝时插件自己把公钥放进去。本机一把都没有时，让用户在「开始」页点「生成 ssh 密钥」（导演 shell 里不能生成），不要让用户开终端。另有私钥时才 `config set ssh_key <路径>`。

## 一次完整运行（推荐：作业模式）

用户也可以直接在 dsh 左侧栏的「Shisei」页面里点选完成。用作业模式时，你跑的作业也会出现在那个页面的「最近」里，用户可以在那里看进度、挑种子、下载。

```
# 1. 建作业：复制素材，裁成 3:4。--start 选舞者抬头多的片段起点（秒）；--seconds 片段长度 2–15 秒（默认 4.46 秒 = 107 帧，
#    帧数取 17n+5，所以 15 秒实际是 345 帧 14.4 秒）。越长越慢越贵，报价用 estimate --frames <job.json 的 params.frames>
#    整段换（超过 15 秒，最长 5 分钟）在页面上做：先试开头 4.5 秒拍 3 条，用户挑一条后点「照这条拍完整段」建一个新项目，
#    job-run 按段（每段 ≤328 帧）一段接一段拍、存在机器上，最后拼成一条；中途停了，再跑从没拍完的段接着拍
shisei job-new --video in.mp4 --front front.png --face face.png --start 3.2 --details "She has long black box braids, wears ..." --seeds 3
#    返回 job 和 dir

# 2. 报价，等用户明确同意（必须）。job-estimate 只算这个作业还缺的步骤和条数，并对照项目上限（默认 ¥5，只有用户在页面里改）
shisei job-estimate <dir>
shisei up                       # 只返回 plan，不开机

# 3. 用户在 Shisei 页面里打开这个项目、点「下一步：确认开机」，跑完整流程（开机 → 安全检查 → 上传 → 动捕 → 白模 → 换人 → 三栏成片），大约 25 分钟

# 4. 隔几分钟看一次进度；失败时用户在页面上点「继续」，已完成的步骤会跳过
shisei job-status <dir>
```

- 跑完后成片在 `<dir>/showcase.mp4`，每个种子在 `<dir>/seed_<种子>.mp4`。机器空闲 10 分钟会自动停机，用户看过成片后也可以 `down --yes`。
- 要中途停止：在 `<dir>` 下建一个名为 `stop` 的文件，作业会先保存已完成的种子，然后停机。
- `job-status` 返回 `state: alert` 时，按上面的安全告警规则处理。
- 每次 `job-run` 结束（跑完、停止、失败、告警）都往 `<dir>/ledger.json` 记一行花费，GPU 花费页按它加总；超过上限 `job-run` 会拒绝开机。
- 用户想多要几条（或换个描述再拍）：在 Shisei 页面的结果页点「重做」。动捕和白模不重跑，只拍新加的几条；旧的几条和它们当时的描述都留着。
- 跑完后会让看图模型预审每一条（`job-precheck`，只写 `precheck.json`）。它只列问题；用哪一条只由用户在页面上点，你不要替用户选。

### 保留原背景、换两个人（实验）

视频里有两个人，或者用户想留着原视频的背景时，不跑动捕和白模，直接在原片上换（`mask` 模式）：

```
# 两个人：左边的人用 --front，右边的人用 --front2，各写各的衣服；没有脸部特写。总是保留原背景、不裁 3:4
shisei job-new --video duo.mp4 --people 2 --front left.png --front2 right.png --details "white hoodie, grey cargo pants" --details2 "red cropped jacket, black leather trousers" --start 6
# 一个人但保留原背景
shisei job-new --video in.mp4 --mode keepbg --front front.png --face face.png --details "..."
```

- 左右按片段第一帧里站的位置分。选两人分开站、都抬头的起点；画面里多于 2 人不支持。
- 报价、开机和单人一样；成片是双栏（原片 · 角色），没有 `mannequin.mp4` 和 `motion.glb`。
- 两人贴身或交叉换位时偶尔会互换参考或串衣服；预审会逐条看左右认没认对，用哪一条仍只由用户点。

### 分步模式（调试用）

`tunnel` / `upload` / `submit mocap|swap` / `wait` / `pull` / `mannequin --remote` / `composite` 可以单独跑，参数见 `shisei <命令> --help`。

## 效果要点

- 舞者低头，角色也会一直垂眼。选舞者抬头看镜头的片段（`prep --start`）。
- 换人时用 `--head face`（中性脸）。`egg`（蛋头）没有五官，眼睛会变形，只适合纯白模展示。
- `--shot close`（膝上跟拍）比 `full` 脸大约 2 倍，眼睛更稳定。
- `--details` 写清楚衣服和鞋子。表情不要写死成「开心大笑」，也不要按时间写表情剧本，不然笑容会僵住、眼睛会变形。
- 头部替换强度保持默认 0.3。调到 0.6 会削弱笑容，鞋子也容易变成袜子。

## 出错时

- `up` 报没货：等待期间不计费。告诉用户可以稍后再试，或者换个时间。
- 显卡自检失败：`up` 会停机重开，最多 3 次，仍失败就停下来报告。
- 某一步失败：已完成的种子先 `pull` 拉回，再问用户是重试、跳过，还是 `down --yes`。
- 隧道断了：重新运行 `tunnel`。任务在实例上继续跑，不受影响。
