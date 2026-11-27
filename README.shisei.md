<h1 align="center">拾戏 Shisei：完全开源、自托管的 AI 视频工作台（dsh 插件）</h1>
<p align="center"><b>短剧</b>：一句话拍成竖屏短剧 · <b>翻拍</b>：给舞蹈视频换上 AI 角色<br>跑在你自己的云 GPU 上</p>
<p align="center"><b>简体中文</b> · <a href="README.en.md">English</a> · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></p>
<p align="center">
  <a href="https://shisei.indaloop.app/"><img src="https://img.shields.io/badge/Website-shisei.indaloop.app-E8552B?style=for-the-badge" alt="Website" height="36"></a>
  <a href="https://x.com/wquguru"><img src="https://img.shields.io/badge/Follow-@wquguru-000000?style=for-the-badge&logo=x&logoColor=white" alt="Follow @wquguru on X" height="36"></a>
</p>
<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0%20%2B%20GPL--3.0%20workflows-blue" alt="License: Apache-2.0, three workflows GPL-3.0">
  <img src="https://img.shields.io/badge/DeepSeek%20Harness-plugin-4D6BFE" alt="DeepSeek Harness plugin">
  <img src="https://img.shields.io/badge/UI-中文%20%7C%20EN%20%7C%20日本語%20%7C%20한국어-555" alt="UI: Chinese, English, Japanese, Korean">
  <img src="https://img.shields.io/badge/tested%20on-macOS%20%7C%20Windows-555" alt="Tested on macOS and Windows">
</p>
<p align="center">⭐ 觉得有用请点个 Star，方便随时找回</p>
<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/demo.gif" width="800" alt="从首页打开《晾衣绳》项目：回看导演栏中退回段 B 首帧重拍特写的对话与报价，查看段 B 的新首帧、台词、标有台词对得上的候选镜头，预览 40 秒成片并用 @ 点名角色与段落"></p>
<p align="center"><sub>《晾衣绳》实录，退回段 B 重拍特写、选条预览 40 秒成片并用 @ 点名改镜。</sub></p>

实测拍一部 30 秒短剧最低只要 3 块钱。Shisei（`dsh-shisei`）是我做的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`，DeepSeek 的开源 agent 工具，带网页界面）插件，代码按 Apache-2.0 开源（三个工作流为 GPL-3.0），在线读取 `shisei.indaloop.app` 的 CC0 公开素材库。

- **按秒算钱，零订阅费**：插件免费，没有自己的服务器。写剧本、审图、选条、合成都在本机跑，不花钱。只有生成画面才租你自己的[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) GPU，实测 29.4 秒短剧《末班车》约 ¥3，40 秒《晾衣绳》含空闲待机约 ¥5.9。
- **哪镜不对改哪镜**：打 `@` 点名角色、道具、段落，或在画面上画圈跟导演聊，只重拍那一段，旧版随时换回。
- **开机扣费全由你点头**：大模型导演只管写分镜和给建议，代码锁死了它开不了机，也不能替你通过画面，每个项目默认 ¥5 预算封顶。

## 快速开始

<details>
<summary><b>安装前要准备什么</b></summary>

- **系统**：macOS 或 Windows 11，[`dsh`](https://github.com/deepseek-ai/deepseek-harness) 0.2.x（网页界面）。用 dsh 桌面版不用再装别的：Python 用桌面版自带的，ssh 和连优云智算的工具随插件带着，没有 ffmpeg 的话插件第一次打开就在后台下好，登录 GPU 机器的 ssh 密钥在建机器时自动生成。用命令行版 dsh 时还要 [Node](https://nodejs.org/) 20+ 和 [Python](https://www.python.org/downloads/) 3.9+（Windows 选 64 位、带 `py` 启动器的安装包）。
- **音视频**：`ffmpeg`（对标片要 4.4+，没有可在插件“设置”里一键装）。可选 `whisper.cpp`（用来核对台词）。
- **导演模型**：在 `dsh` 里配一个大模型，“开始”页推荐蚂蚁 MaaS 的 Ling-3.2-Flash（能看图、有免费用量）。
- **GPU**：[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)（compshare）账号，完成实名、充值，在控制台拿 API 密钥；显卡用 4090 48G。
- **语言**：界面有简体中文、English、日本語、한국어；剧本和字幕用中文。

</details>

从 npm 安装，**要带版本号**：dsh 用的 pnpm 默认不选发布不到一天的版本，只写 `dsh-shisei` 可能装成旧版。

**dsh 桌面版**：在 dsh 的插件页点「Add plugin」，填 `dsh-shisei@0.2.2`，点 Install，然后完全退出 dsh 再打开。桌面版的 profile 只能在桌面版里装，下面的命令对它不管用。

**命令行版 dsh**：

```bash
# 仅首次需要：从 dsh 内置的 web 模板创建一个 profile；已有就跳过，下面改用你的 profile 名
dsh --profile web --from-default-profile web --dump-config >/dev/null
dsh plugin --profile web add dsh-shisei@0.2.2
dsh --profile web
```

想改代码或用最新的 main，从 git 克隆安装：

```bash
git clone https://github.com/wquguru/dsh-shisei.git
dsh plugin --profile web add "$PWD/dsh-shisei"
```

<details>
<summary><b>升级插件</b></summary>

有新版时，插件左栏会出现「新版」提示，「设置」页顶上的「版本」卡片按你用的 dsh 给出步骤，装好重启后会告诉你已升级。

- **dsh 桌面版**：不能原地升级。在插件页先**卸载** dsh-shisei，再点「Add plugin」填 `dsh-shisei@新版本号`、点 Install，最后完全退出 dsh 再打开。跳过卸载会报 `This plugin is already installed`。
- **命令行版 dsh**：`dsh plugin --profile web add dsh-shisei@新版本号`，然后重启 dsh。
- **找不到新版，或装上的还是旧版**：镜像源可能还没同步，换成官方源再装（桌面版在安装框右边的 Registry 里选）。
- 项目、配置和花费账本都在 `~/.dsh/shisei/`，卸载、升级都不会动它们。1.0 之前换 minor 版本（例如 0.1 → 0.2）可能有不兼容的改动，先看[更新记录](https://github.com/wquguru/dsh-shisei/blob/main/CHANGELOG.md)。

</details>

打开 `dsh` 网页左栏的“Shisei”，跟着“开始”页配好导演模型、[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)密钥和 GPU 机器，就能复制内置的《打烊之后》《末班车》《晾衣绳》三个练手剧本开拍。

建 GPU 机器约 10 分钟、不到 ¥1：插件用免费的 Shisei 社区镜像建，环境已装好。

## 能做什么

| 场景 | 你做什么 | 得到什么 |
| :--- | :--- | :--- |
| **拍竖屏短剧** | 写一句话故事，逐张审角色、场景、道具和首帧，点“开机并拍”，从几条里选一条 | 9:16 MP4 成片，含对白、内心独白、环境声、统一响度、软字幕，画面无水印 |
| **对话改镜头** | 在右栏用 `@` 或画圈跟导演抱怨，确认新分镜和报价后重拍 | 只重拍指定段落，旧首帧和旧视频随时换回 |
| **照对标片的拍法写** | 选一段本机参考视频（≤ 3 分钟），再给一句你自己的故事 | 照着它的景别、运镜、光线写出的新剧本（只学拍法，原片不上传） |
| **拍系列** | 把几集归进一个系列，审过的角色设为常驻，从角色自己的一句台词定声 | 新的一集直接带上审过的角色和固定的声音，导演照定下的前情写下一集 |
| **翻拍** | 上传你自己拍的或有权使用的舞蹈视频（一段 2–15 秒，或整段最长 5 分钟）、成年角色的全身图和脸部特写、一句衣服描述 | 原视频 / 3D 白模 / 翻拍结果三栏对照成片 |

## 实测案例

下面三张图的成片都来自我用自己的 4090 48G（当时 ¥3.8/小时）跑出来的真机实测：

<table>
  <tr>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/drama-last-bus.jpg" alt="《末班车》成片三帧并排：夜里公交车厢里的两个角色，中间一帧为女主角特写，竖屏 9:16，右上角有 AI 生成角标"><br>
      <b>短剧《末班车》</b> · 5 镜 / 2 角色 / 1 道具<br>
      每镜拍 2 条，两次开机共约 45 分钟，做出 29.4 秒成片；之后只打三句抱怨让导演改 5 镜重拍 v2，又跑了约 40 分钟。<br>
      <b>花费</b>：首版约 ¥3，v2 改镜约 ¥2.5
    </td>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/drama-clothesline.jpg" alt="《晾衣绳》成片三帧并排：黄昏楼顶晾床单的一男一女，男角色拎着一袋橘子，竖屏 9:16，右上角有 AI 生成角标"><br>
      <b>短剧《晾衣绳》</b> · 4 段 11 镜 / 2 角色<br>
      做出 40.0 秒竖屏成片，分五次拍完。<br>
      <b>花费</b>：约 ¥5.9（含空闲待机）
    </td>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/motion-dance.jpg" alt="舞蹈翻拍试拍三帧并排：左边是 3D 白模，中间和右边是同一时刻的两条换人结果，红色麻花辫、灰绿色 T 恤和短裤的 AI 角色，竖屏"><br>
      <b>舞蹈翻拍</b> · 试拍 4.5 秒 × 3 条<br>
      跑完动捕与白模后，照选中的那段舞拍出 3 条 AI 角色候选，一次开机约 19 分钟。<br>
      <b>花费</b>：约 ¥1.82（含 10 分钟空闲待机）
    </td>
  </tr>
</table>

<details>
<summary><b>实测明细与已知的坑</b></summary>

| 日期 | 做了什么 | 规模 | 用时 | 花费 |
| :--- | :--- | :--- | :--- | :--- |
| 2026-11-10 | 短剧《末班车》 | 5 镜、2 角色、1 道具，每镜 2 条，成片 29.4 秒 | 两次开机共约 45 分钟 | 约 ¥3 |
| 2026-11-10 | 对话改镜头《末班车 v2》 | 三句抱怨，导演改 5 镜重拍 | 约 40 分钟 | 约 ¥2.5 |
| 2026-11-11 | 短剧《晾衣绳》 | 4 段 11 镜、2 角色，成片 40.0 秒 | — | 约 ¥5.9（含五次空闲待机约 44 分钟） |
| 2026-11-12 | 舞蹈翻拍 | 试拍 4.5 秒 × 3 条 | 一次开机约 19 分钟 | 约 ¥1.82（含 10 分钟空闲待机） |

<sub>每次真实单价以开机确认框向平台实时查到的为准。</sub>

**已知的坑：**
- 特写首帧常被参考图拉成中景或近景，道具在视频里会变样（《晾衣绳》的橘子袋变成了黑包）。
- 机器冷启动要 2–3 分钟，开机前的估价有时偏高。

</details>

## 怎么用

<details>
<summary><b>1. 拍第一部短剧</b></summary>

1. **写剧本（不花钱）**：“新建项目”→“短剧”，填片名和一句话故事，点“让导演写剧本”，约 7–10 分钟。也可以直接复制练手剧本。
2. **画设定图（GPU）**：点底栏写着价钱的按钮，确认框里列出 GPU、可用区、单价和预计费用，点“开机并拍”。
3. **审图（本机）**：演员、场景、道具逐张“通过”或“退回”写一句原因，导演照原因改了再画。
4. **审首帧（本机）**：每段约 10 秒一张首帧，对照取景、长相年龄、服装道具、显年轻限制四项提示，由你点“通过”。
5. **拍视频、选条（GPU）**：MiniMax H3（出视频模型）一次拍一段，每段几条里点一条（数字键 `1` `2`，`⌘Z` 撤销）。
6. **合成（不花钱）**：本机统一响度、封软字幕，下载 MP4。画面无水印，MP4 元数据里标了 AI 生成。

</details>

<details>
<summary><b>2. 和导演说话</b></summary>

<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/mention-light.jpg" width="800" alt="《晾衣绳》项目页亮色界面，左侧为成片播放器，右侧导演栏输入 @ 弹出演员、场景、道具和段落列表"></p>

- 每个项目一个导演会话，聊天不花 GPU 的钱，导演按你的页面语言回复。
- 打 `@` 带上演员、场景、道具、某一段或某一条视频，也能带别的项目或素材库里的图。
- 在画面上点一下或拖个框，圈出的局部和整帧一起发给导演。
- 导演忙时说的话会排队，可撤回或插话，“停下”只停导演，不停 GPU。

> 《末班车 v2》实测：我只打了三句抱怨，导演自己抽帧找原因、改 5 镜、报价、等确认、重拍、选条、合成，约 40 分钟 ¥2.5。

</details>

<details>
<summary><b>3. 三个重做办法</b></summary>

<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/crop-light.jpg" width="800" alt="亮色界面下的段 B 裁特写弹窗，在首帧画面上拖出 2.0× 的 9:16 取景框，底部显示下次拍约 ¥0.41 及本步不扣费提示"></p>

- **裁特写**：人物太远时在首帧上框一块，重画成清晰特写（最多 6 倍）。
- **两人侧脸相对**：治两人对视却都扭头看镜头。
- **换衣**：把素材库或你上传的衣服图拖到演员照片上重画。

都只改项目数据，下次点拍才开机。2026-11-10 真机验证过（约 7 分钟 / ¥0.43）。

</details>

<details>
<summary><b>4. 对标片：只学拍法</b></summary>

在首页故事框旁点“对标片”，选一段本机视频（≤ 3 分钟，需要 `ffmpeg` 4.4+）。本机切镜、抽帧拼成联系表，看图模型从固定选项里填每镜的景别、运镜、光线、人数，归并成段的骨架交给导演。原片的人、长相、台词和画面不进项目，原视频不上传。

> 实测：18 秒对标片拆成 5 镜，导演写出的 5 镜在景别和运镜上逐镜对上，约 7 分钟，不花 GPU 的钱。

</details>

<details>
<summary><b>5. 翻拍</b></summary>

首页切到“翻拍”，上传舞蹈视频（选 2–15 秒的一段，或整段最长 5 分钟）、成年角色的全身正面图和脸部特写、一句衣服描述。确认开机后依次跑 SAM 3D Body 动捕、Blender 白模、MiniMax H3 + 换人 LoRA，输出三栏对照。不满意可以加拍或改衣服重拍，动捕和白模直接复用。选整段时先试拍开头 4.5 秒的几条，挑一条再点“照第 N 条拍完整段”，分几部分接着拍、拼成一条（25 秒的舞约 1 小时、¥4–5，试拍的花费也算进上限）。整段的动作和脸照着选中的那条，衣服细节可能有出入。

</details>

## 原理

1. **本机导演编排**：你在 `dsh` 里配的大模型负责写剧本、拆对标片骨架、按你的退回原因改分镜，所有项目文件存在本机 `~/.dsh/shisei/jobs/`。
2. **你点确认才开机**：每次生成画面前算出预估费用，你点“开机并拍”后插件才调[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) API 启动你的 4090 48G 实例，并设置平台侧定时关机。
3. **隧道调云端工作流**：经 `ssh` 隧道连上云端只监听 `127.0.0.1` 的 ComfyUI（云端工作流服务），跑 Qwen-Image（出图模型）、SAM 3D Body、Blender 或 MiniMax H3。
4. **拉回本机选条合成**：生成的图片和视频传回本机，你审图、选条后，本机用 `ffmpeg` 统一响度、封软字幕导出无水印 MP4，云端空闲 10 分钟自动停机。

## 花钱、安全和隐私

这些规则写在代码里，不能关：

- **开机必须你点**：不点“开机并拍”绝不开机，导演的 shell 里任何会开机计费的命令都被拒绝。每个项目默认预算上限 ¥5，超了就不开机。
- **页面估算会比账单低**：上限按插件记的运行时长检查，拍完后机器空闲 10 分钟才自动停机，这段平台也计费，走走停停拍会差得多（见上面的《晾衣绳》）。以[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)账单为准。
- **只停机，不删东西**：插件只会停机，从不删实例和磁盘，换掉的首帧和视频都留旧版本。
- **角色至少 18 岁**：真人照片和照着真人（含公众人物）做的形象都可以当参考，插件不检查、也不阻止；取得本人同意、遵守当地肖像权和深度合成标识规定是用户的事。标为“看起来年轻”的角色只用原始身形、不换更暴露的衣服，镜头不拍裙底，低机位最低到膝盖。
- **ComfyUI 不暴露到公网**：云端只监听 `127.0.0.1`，经 `ssh` 隧道访问，每次开机从外网确认端口关着，队列里出现不是 Shisei 提交的任务就告警暂停。密钥只放在 `dsh` 凭据库或环境变量里，不落盘、不回显。
- **数据去向透明**：没有遥测，也没有 Shisei 自己的服务器。数据只发往你配置的几处：[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) API、你自己的 GPU 实例（经 `ssh` 隧道）、`dsh` 里的导演模型、看图模型（配了密钥才发首帧、抽帧和对标片联系表，对标片原视频不出本机），以及只读的 Shisei 素材库（对方能看到你的 IP）；电脑上没有 ffmpeg 时，还会从你给 dsh 配的 npm 源下载一次插件的 ffmpeg 包。花钱的地方只有 GPU 按秒计费和导演模型、看图模型各自的 API 费用。详见 [PRIVACY.md](PRIVACY.md)。

<details>
<summary><b>防忘关机：定时关机、机器页、租约</b></summary>

- 任务运行时，插件每 5 分钟把[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)平台侧的定时关机推到 30 分钟后，结束时改成 10 分钟后停机，插件没开着也会触发。
- 左栏常驻显示开着几台机器、每小时多少钱、谁在用。`dsh` 开着时每 3 分钟巡检一次，巡检只补定时关机，从不直接停机。
- 可以登记多台机器（“开始”页或“机器”页能再建一台），每次运行先拿租约，不会停掉别人的机器。机器都在忙时可以排队：哪台先空出来就用哪台，按确认时的价钱开拍，等待不计费，最多等 90 分钟。

</details>

## 常见问题

<details>
<summary><b>和在线 AI 视频产品有什么区别？</b></summary>

在线 AI 视频产品点开网页就能用，不用自己配 `dsh` 和云端 GPU 机器，也不用等 2–3 分钟的机器冷启动。

Shisei 适合不想付包月订阅费、想把工程文件和旧版本都留在自己电脑上的人。你只为生成画面那几分钟的云 GPU 按秒付费，成片画面无水印，代码开源，还能在画面上画圈让导演只重拍不满意的那一段。

</details>

<details>
<summary><b>要花多少钱？</b></summary>

插件免费。花钱只有两处：GPU 按秒计费（见上面的实测表），以及导演模型、看图模型各自的 API 费用（推荐的 Ling-3.2-Flash 有免费用量）。

</details>

<details>
<summary><b>能用真人照片或明星剧照吗？</b></summary>

能。角色参考可以是真人（含公众人物），插件不拦截；取得本人同意、遵守当地肖像权和深度合成标识规定由使用者负责。成片画面不加水印，AI 生成标记只写在文件元数据里。上传对话框只收场景、道具、衣服，因为换装链路只画衣服；人物图直接放在项目里当参考。

</details>

<details>
<summary><b>支持哪些显卡和系统？</b></summary>

显卡用 4090 48G，别的卡在真机跑通 `gpu-qualify` 后陆续加入。系统支持 macOS 和 Windows：Windows 11 上已走通短剧从复制模板、导演写剧本到成片的整条路（中文系统默认的 GBK 编码也行）；翻拍、“开始”页建机器和“更新环境”还没在 Windows 上验过。Linux 在路线图里。

</details>

<details>
<summary><b>插件升级后机器要重装环境吗？</b></summary>

不一定。机器有自己的环境版本号，只有要装的东西变了才加一。对不上时导演会告诉你，“机器”页也会在那台上标“环境旧了”；等它停下，点“更新环境…”即可：确认价钱后插件开机、重跑 [`setup.sh`](skills/shisei-motion/assets/image/setup.sh)（只补变了的部分）、做完安全检查自动停机。规则见 [docs/IMAGE.md](docs/IMAGE.md)。

</details>

<details>
<summary><b>能商用吗？</b></summary>

插件代码可以。生成用的模型各有许可，MiniMax H3 和换人 LoRA 是 `minimax-h3-community-license`，商用前先读（见下方“许可”）。

</details>

<details>
<summary><b>遇到问题怎么办</b></summary>

| 情况 | 怎么办 |
| :--- | :--- |
| 没填密钥 / 没选 GPU 机器 / 没有 ssh 密钥 | 去“开始”页对应那一步（ssh 密钥点“生成 ssh 密钥”），或“设置” |
| 没装 [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) 命令行 | “设置”→“安装 compshare-cli”（装在插件自己的目录） |
| 导演没写成 / 很久没动静 | 在导演栏顶上换一个模型（会把你的上一条重发），或在 `dsh` 里配一个能用的模型再点重试 |
| 超出项目上限 | 少拍几条 / 几段，或点“调高上限” |
| GPU 没货 | 插件每 2 分钟自动重试，等待不计费 |
| 机器上出现不是 Shisei 提交的任务 | 插件已暂停队列；去“机器”页停机，再到控制台查实例安全 |
| 机器环境版本旧了 / 对不上 | 等机器停下，在“机器”页点“更新环境…”；提示“比插件新”就升级插件 |
| 机器被别的项目占着 | 点“排队，空出来就拍”，或在“开始”页再建一台；“机器”页能看到谁在用 |
| 预审一直不出结果 | “设置”里测一下看图模型；预审只是建议，不影响你手动通过 |

</details>

## 路线图

- **自带 GPU**：本地显卡和 vast.ai。
- **更多可替换的模型**：出图、出视频的模型都能换。
- **画幅和发布规格**：横屏、方形等更多画幅和发布规格。
- **更多**：更多显卡、Linux、whisper 对齐字幕、其他语言的剧本、Shisei Pose（姿势库、线稿转白模 / 深度 / 法线）。

## 社区 · 致谢

- **社区**：[X @wquguru](https://x.com/wquguru) · [GitHub Issues](https://github.com/wquguru/dsh-shisei/issues) · 安全问题私下报告见 [SECURITY.md](SECURITY.md) · 参与开发见 [CONTRIBUTING.md](CONTRIBUTING.md)
- **致谢**：[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)、[rea-video](https://github.com/JuneYaooo/rea-video)（对标片逐镜拆解的思路）、ComfyUI、[ComfyUI-BFSNodes](https://github.com/alisson-anjos/ComfyUI-BFSNodes)、Qwen-Image、MiniMax H3、SAM 3D Body、SAM 3.1、MoGe、Blender、whisper.cpp、ffmpeg。

## 许可

- 插件代码是 Apache-2.0（[LICENSE](LICENSE)）。
- 例外：`skills/shisei-motion/assets/workflows/` 下的 `swap_studio.json`（改自 ComfyUI-BFSNodes 的换人示例）、`drama_take.json`（用了它的 MiniMax H3 采样链）、`mocap_glb.json`按 **GPL-3.0-only** 分发（[NOTICE](NOTICE)、[LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt)）；同目录的 `drama_image.json` 是 Apache-2.0。
- `skills/shisei-motion/assets/vendor/wheels/` 是原样打包的 compshare-cli（Apache-2.0）和它用到的包（MIT、BSD、ISC、Apache-2.0、MPL-2.0 等），让它不联网就能装上；逐个许可见 [NOTICE](NOTICE)。
- `lib/ssh.cjs` 是插件自带的 ssh（让电脑不用装 OpenSSH），里面打包了 ssh2（MIT）等几个小库（MIT、BSD-3-Clause、Unlicense），见 [NOTICE](NOTICE)。
- 素材库不随插件分发，插件在线读取 `shisei.indaloop.app` 的公开素材，里面的场景、道具和衣服是 CC0。
- 模型不随插件分发，运行时在你的实例上获取。MiniMax H3 和换人 LoRA 是 `minimax-h3-community-license`，Qwen-Image、SAM 3D Body、SAM 3.1、MoGe 各有许可；商用前请先读。
- 本项目是独立开源项目，和 DeepSeek、[优云智算](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)、MiniMax 都没有关系。
