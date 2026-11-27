<h1 align="center">Shisei (拾戏): a fully open-source, self-hosted AI video studio (dsh plugin)</h1>
<p align="center"><b>Drama</b>: turn one sentence into a vertical short drama · <b>Remake</b>: put an AI character into a dance video<br>all on your own cloud GPU</p>
<p align="center"><a href="README.md">简体中文</a> · <b>English</b> · <a href="README.ja.md">日本語</a> · <a href="README.ko.md">한국어</a></p>
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
<p align="center">⭐ Star this repo if you find it useful so you can find it again later</p>
<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/demo.gif" width="800" alt="Opening the project Clothesline from the home page: scrolling back through the Director panel to the exchange where segment B's first frame was sent back for a close-up reshoot with a price quote, viewing segment B's new first frame, its lines, and the candidate takes marked Lines match, previewing the 40-second final cut, and typing @ to mention characters and segments"></p>
<p align="center"><sub>A real session on <i>Clothesline</i>: send segment B back for a close-up, pick takes, preview the 40-second cut, and use @ to point at what to change.</sub></p>

A 30-second short drama has cost as little as ¥3 in real runs. Shisei (`dsh-shisei`) is a plugin I built for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`, DeepSeek's open-source agent tool with a web UI). The code is open source under Apache-2.0 (three workflows are GPL-3.0), and it reads the public CC0 asset library at `shisei.indaloop.app` online.

- **Pay by the second, no subscription**: The plugin is free and runs no servers of its own. Scriptwriting, image review, picking takes, and the final cut all run on your machine at no cost. You only rent a GPU in your own [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) (优云智算) account when it's time to generate images and video. In real runs, the 29.4-second drama *Last Bus* cost about ¥3, and the 40-second *Clothesline* cost about ¥5.9 including idle time.
- **Fix only the shot that's wrong**: Type `@` to name a character, prop, or segment, or circle part of a frame and tell the director what's off. Only that segment gets reshot, and you can switch back to the old version anytime.
- **Nothing gets billed without your click**: The LLM director only writes storyboards and makes suggestions. The code locks it out of starting a GPU or approving frames for you, and every project has a ¥5 budget cap by default.

## Quick start

<details>
<summary><b>What you need before installing</b></summary>

- **System**: macOS or Windows 11, [`dsh`](https://github.com/deepseek-ai/deepseek-harness) 0.2.x (web UI). With dsh Desktop there is nothing else to install: Python is the one Desktop carries, ssh and the compshare tool come with the plugin, ffmpeg is fetched in the background the first time Shisei opens if the computer has none, and the ssh key for the GPU machine is made when the machine is created. With the command-line dsh you also need [Node](https://nodejs.org/) 20+ and [Python](https://www.python.org/downloads/) 3.9+ (on Windows, the 64-bit installer with the `py` launcher).
- **Audio/video**: `ffmpeg` (4.4+ for "Ref video"; if it's missing, install it with one click in the plugin's "Settings"). Optional: `whisper.cpp` (used to check spoken lines).
- **Director model**: An LLM configured in `dsh`. The "Start" page recommends Ling-3.2-Flash on Ant MaaS (it can read images and has a free tier).
- **GPU**: A [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) (优云智算) account with identity verification done and credit added, and an API key from its console; the GPU is a 4090 48G.
- **Language**: The UI comes in Simplified Chinese, English, Japanese, and Korean; scripts and subtitles are in Chinese.

</details>

Install from npm, **with the version number**: the pnpm inside dsh does not pick versions published less than a day ago, so plain `dsh-shisei` may install an older one.

**dsh Desktop**: on the dsh plugins page, click "Add plugin", enter `dsh-shisei@0.2.2`, click Install, then quit dsh completely and open it again. The Desktop profile can only be changed from the app; the commands below do not work on it.

**dsh on the command line**:

```bash
# First time only: create a profile from dsh's built-in web template; skip this if you already have one, and use your profile name below
dsh --profile web --from-default-profile web --dump-config >/dev/null
dsh plugin --profile web add dsh-shisei@0.2.2
dsh --profile web
```

To hack on the code or run the latest main, install from a git clone instead:

```bash
git clone https://github.com/wquguru/dsh-shisei.git
dsh plugin --profile web add "$PWD/dsh-shisei"
```

<details>
<summary><b>Upgrading the plugin</b></summary>

When a new version is out, a "New" notice appears in the plugin's sidebar, and the "Version" card at the top of Settings gives the steps for the dsh you use. After you restart, it tells you the upgrade worked.

- **dsh Desktop**: no in-place upgrade. On the plugins page, **uninstall** dsh-shisei first, then click "Add plugin", enter `dsh-shisei@<new version>`, click Install, and quit and reopen dsh. Skip the uninstall and you get `This plugin is already installed`.
- **dsh on the command line**: `dsh plugin --profile web add dsh-shisei@<new version>`, then restart dsh.
- **New version not found, or the old one installed again**: your registry mirror may not have it yet. Switch to the official registry and install again (in Desktop, pick it under Registry, right of the install box).
- Projects, settings and the spending ledger live in `~/.dsh/shisei/`; uninstalling and upgrading leave them alone. Before 1.0, a new minor version (say 0.1 → 0.2) may have breaking changes: read the [changelog](https://github.com/wquguru/dsh-shisei/blob/main/CHANGELOG.md) first.

</details>

Open "Shisei" in the left sidebar of the `dsh` web UI and follow the "Start" page to set up the director model, your [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) key, and a GPU machine. Then copy one of the three bundled starter scripts, *After Closing*, *Last Bus*, or *Clothesline*, and start shooting.

Creating the GPU machine takes about 10 minutes and costs under ¥1: the plugin builds it from the free Shisei community image, with the environment already installed.

## What you can do

| Scenario | What you do | What you get |
| :--- | :--- | :--- |
| **Shoot a vertical short drama** | Write a one-line story, review each character, scene, prop, and first frame, click "Start the GPU and shoot", and pick one of several takes | A 9:16 MP4 with dialogue, inner monologue, ambient sound, normalized loudness, and soft subtitles, with no watermark on the picture |
| **Revise shots by chatting** | Complain to the director in the right sidebar with `@` or by circling the frame, then confirm the new storyboard and quote to reshoot | Only the segments you named are reshot; old first frames and old videos stay available to switch back to |
| **Write from a reference video** | Pick a local reference video (up to 3 minutes), then give a one-line story of your own | A new script that follows its shot sizes, camera moves, and lighting (it learns the camera work only; the original is never uploaded) |
| **Make a series** | Group episodes into a series, make approved characters regulars, and fix a character's voice from one of its own lines | Each new episode brings the approved characters and their fixed voices along, and the director writes it from the recap you settled |
| **Remake** | Upload a dance clip you shot or have the rights to (a 2–15 second stretch, or the whole dance up to 5 minutes), a full-body shot and face close-up of an adult character (AI-generated, or a real person you may use), and a one-line outfit description | A three-column video: original clip / 3D clay render / remake result |

## Real-world test runs

All three results below come from real runs on my own 4090 48G (¥3.8/hour at the time):

<table>
  <tr>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/drama-last-bus.jpg" alt="Three frames from Last Bus side by side: two characters in a city bus at night, the middle frame a close-up of the female lead, vertical 9:16, with an AI-generated badge in the top-right corner"><br>
      <b>Short drama <i>Last Bus</i></b> · 5 shots / 2 characters / 1 prop<br>
      Two takes per shot, about 45 minutes across two GPU sessions, for a 29.4-second final cut. Then three complaints were enough for the director to revise 5 shots and reshoot v2, in about 40 more minutes.<br>
      <b>Cost</b>: about ¥3 for the first cut, about ¥2.5 for the v2 revision
    </td>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/drama-clothesline.jpg" alt="Three frames from Clothesline side by side: a man and a woman hanging sheets on a rooftop at dusk, the man holding a bag of oranges, vertical 9:16, with an AI-generated badge in the top-right corner"><br>
      <b>Short drama <i>Clothesline</i></b> · 4 segments, 11 shots / 2 characters<br>
      A 40.0-second vertical final cut, shot across five sessions.<br>
      <b>Cost</b>: about ¥5.9 (including idle time)
    </td>
    <td width="33%" valign="top">
      <img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/cases/motion-dance.jpg" alt="Three frames from a dance remake trial side by side: the 3D clay render on the left, and two swap takes of the same moment in the middle and on the right, an AI character with a red braid in a sage T-shirt and shorts, vertical"><br>
      <b>Dance remake</b> · 4.5-second trial × 3 takes<br>
      After motion capture and the clay render, it shot 3 AI-character takes of the chosen part of the dance in one GPU run of about 19 minutes.<br>
      <b>Cost</b>: about ¥1.82 (including 10 idle minutes)
    </td>
  </tr>
</table>

<details>
<summary><b>Run details and known issues</b></summary>

| Date | Task | Scale | Time | Cost |
| :--- | :--- | :--- | :--- | :--- |
| 2026-11-10 | Short drama *Last Bus* | 5 shots, 2 characters, 1 prop, 2 takes per shot, 29.4 s final cut | ~45 min across two GPU sessions | ~¥3 |
| 2026-11-10 | Chat revision *Last Bus v2* | Three complaints; the director revised and reshot 5 shots | ~40 min | ~¥2.5 |
| 2026-11-11 | Short drama *Clothesline* | 4 segments / 11 shots, 2 characters, 40.0 s final cut | — | ~¥5.9 (including ~44 min across five idle cooldowns) |
| 2026-11-12 | Dance remake | 4.5 s trial × 3 takes | one GPU run, ~19 min | ~¥1.82 (including 10 idle minutes) |

<sub>The real unit price for each run is whatever the confirmation dialog fetches live from the platform before the GPU starts.</sub>

**Known issues:**
- Close-up first frames often get pulled back to a medium or medium close-up shot by the reference images, and props can change in the video (the bag of oranges in *Clothesline* turned into a black bag).
- A cold start takes 2–3 minutes, and the pre-run estimate sometimes runs high.

</details>

## How to use it

<details>
<summary><b>1. Shoot your first short drama</b></summary>

1. **Write the script (free)**: Click "New project" → "Short drama", enter a title and a one-line story, and click "Have the director write the script, free". It takes about 7–10 minutes. You can also just copy a starter script.
2. **Generate reference images (GPU)**: Click the button in the bottom bar that shows the price. The confirmation dialog lists the GPU, availability zone, unit price, and estimated cost; click "Start the GPU and shoot".
3. **Review images (local)**: For each actor, scene, and prop, click "Approve", or "Send back" with a one-line reason. The director revises based on your reason and redraws.
4. **Review first frames (local)**: Each segment of about 10 seconds gets one first frame. Check it against four reminders (framing, apparent age, outfit and props, restrictions for younger-looking characters), then click "Approve" yourself.
5. **Shoot video and pick takes (GPU)**: MiniMax H3 (the video model) shoots one segment at a time. For each segment, click one of the takes (number keys `1` `2`, `⌘Z` to undo).
6. **Assemble (free)**: Loudness is normalized and soft subtitles are muxed locally, then you download the MP4. There's no watermark on the picture; the MP4 metadata marks it as AI-generated.

</details>

<details>
<summary><b>2. Talk to the director</b></summary>

<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/mention-light.jpg" width="800" alt="Light-mode project page for Clothesline with the final cut player on the left and, in the Director panel on the right, an @ popup listing actors, scenes, props, and segments"></p>

- Each project has one director session. Chatting costs no GPU time, and the director replies in your UI language.
- Type `@` to bring in an actor, scene, prop, segment, or a specific video take, or images from other projects and the asset library.
- Click or drag a box on a frame, and the circled region goes to the director along with the full frame.
- Anything you say while the director is busy waits in a queue, where you can take it back or interject. "Stop" only stops the director, not the GPU.

> *Last Bus v2* in practice: I typed three complaints, and the director pulled frames on its own to find the causes, revised 5 shots, quoted a price, waited for my confirmation, reshot, picked takes, and assembled the cut. About 40 minutes, ¥2.5.

</details>

<details>
<summary><b>3. Three ways to redo a shot</b></summary>

<p align="center"><img src="https://raw.githubusercontent.com/wquguru/dsh-shisei/67e62347992653cc4de240156d1a344ba00fbd17/docs/img/crop-light.jpg" width="800" alt="Light-mode Crop a close-up dialog for segment B: a 9:16 frame at 2.0x dragged over the first frame, with the next shoot at about ¥0.41 and a note that this step charges nothing"></p>

- **Crop a close-up**: When characters are too far away, draw a box on the first frame and have it redrawn as a sharp close-up (up to 6×).
- **Profiles facing each other**: Fixes two characters who should be looking at each other but both turn toward the camera.
- **Outfit swap**: Drag an outfit from the asset library, or one you uploaded, onto an actor's photo to redraw them in it.

All three only change project data; the GPU doesn't start until you next click shoot. Verified on real hardware on 2026-11-10 (about 7 minutes / ¥0.43).

</details>

<details>
<summary><b>4. Reference video: learn the camera work only</b></summary>

Click "Ref video" next to the story box on the home page and pick a local video (up to 3 minutes; needs `ffmpeg` 4.4+). It's split into shots locally and frames are tiled into contact sheets; a vision model fills in each shot's framing, camera move, lighting, and headcount from a fixed set of options, and the shots are grouped into a segment skeleton for the director. The people, faces, dialogue, and imagery of the original never enter your project, and the video itself is never uploaded.

> In practice: an 18-second reference clip was split into 5 shots, and the director's 5-shot script matched it shot for shot on framing and camera movement. About 7 minutes, no GPU cost.

</details>

<details>
<summary><b>5. Remake</b></summary>

Switch to "Remake" on the home page and upload a dance video (pick a 2–15 second stretch, or the whole dance up to 5 minutes), a full-body front shot and a face close-up of an adult character (AI-generated, or a real person you may use), and a one-line outfit description. Once you confirm starting the GPU, it runs SAM 3D Body motion capture, the Blender clay render, and MiniMax H3 + the character-swap LoRA in turn, and outputs a three-column comparison. Not happy? Shoot more takes or change the outfit and reshoot; the motion capture and clay render are reused as is. For a whole dance it first tries the opening 4.5 seconds in a few takes; pick one and click "Make the whole dance like take N", and it shoots the rest in parts and joins them into one video (a 25-second dance took about an hour and ¥4–5; the trial counts toward the cap too). The moves and face follow the take you picked; small outfit details can drift.

</details>

## How it works

1. **The director plans locally**: The LLM you configure in `dsh` writes scripts, turns reference videos into skeletons, and revises storyboards based on your reasons for sending things back. All project files live on your machine in `~/.dsh/shisei/jobs/`.
2. **The GPU starts only when you confirm**: Before any image or video is generated, the plugin estimates the cost. Only after you click "Start the GPU and shoot" does it call the [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) API to start your 4090 48G instance, and it sets a scheduled shutdown on the platform side.
3. **Cloud workflows over a tunnel**: Over an `ssh` tunnel, the plugin reaches ComfyUI (the cloud workflow server), which listens only on `127.0.0.1`, and runs Qwen-Image (the image model), SAM 3D Body, Blender, or MiniMax H3.
4. **Pull back, pick, and assemble locally**: Generated images and videos come back to your machine. After you review images and pick takes, `ffmpeg` normalizes loudness, muxes soft subtitles, and exports a watermark-free MP4 locally. The cloud machine stops on its own after 10 idle minutes.

## Money, safety, and privacy

These rules are built into the code and can't be turned off:

- **You start the GPU**: Without your click on "Start the GPU and shoot", the GPU never starts, and any command in the director's shell that would start a machine or incur charges is refused. Every project has a ¥5 budget cap by default; over the cap, the GPU won't start.
- **The page estimate runs below your bill**: The cap is checked against the run time the plugin logs. After a shoot, the machine idles for 10 minutes before stopping on its own, and the platform bills for that too, so stop-and-go shooting can widen the gap a lot (see *Clothesline* above). Your [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) bill is the final word.
- **Stops, never deletes**: The plugin only stops machines. It never deletes instances or disks, and replaced first frames and videos are kept as older versions.
- **Characters are 18 or older**: real photos and likenesses based on real people (including public figures) can be used as references — the plugin neither checks nor blocks that; getting the person's consent and following local likeness and deep-synthesis labelling rules is the user's job. Characters marked as looking young keep their original body type and can't be put in more revealing clothes; no upskirt shots, and low camera angles go no lower than the knee.
- **ComfyUI stays off the public internet**: On the cloud machine it listens only on `127.0.0.1` and is reached over an `ssh` tunnel. Every startup confirms from the outside that the port is closed, and if a job not submitted by Shisei shows up in the queue, the plugin alerts you and pauses. API keys live only in the `dsh` credential store or environment variables, never written to disk or echoed back.
- **You know where your data goes**: No telemetry, and no Shisei server. Data goes only to the places you configure: the [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) API, your own GPU instance (over an `ssh` tunnel), the director model in `dsh`, the vision model (first frames, extracted frames, and reference-video contact sheets are sent only once you set its key; the original reference video never leaves your machine), and the read-only Shisei asset library (which can see your IP); if the computer has no ffmpeg, the plugin's ffmpeg package is downloaded once from the npm registry dsh is set up with. The only costs are per-second GPU billing and the director and vision models' own API fees. Details in [PRIVACY.md](PRIVACY.md).

<details>
<summary><b>Never forget a running machine: scheduled shutdown, Machines page, leases</b></summary>

- While a job runs, the plugin pushes [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc)'s platform-side scheduled shutdown to 30 minutes ahead every 5 minutes, and sets it to 10 minutes when the job ends. It fires even if the plugin isn't running.
- The left sidebar always shows how many machines are on, what they cost per hour, and who's using them. While `dsh` is open it checks every 3 minutes; the check only tops up the scheduled shutdown and never stops a machine outright.
- You can register several machines (make another from the Start or Machines page). Each run takes a lease first, so it never stops someone else's machine. When every machine is busy you can queue: whichever frees up first is used, at the price you confirmed; waiting is free and lasts up to 90 minutes.

</details>

## FAQ

<details>
<summary><b>How is this different from online AI video products?</b></summary>

Online AI video products work as soon as you open the page. You don't have to set up `dsh` or a cloud GPU machine, and you don't wait 2–3 minutes for a cold start.

Shisei is for people who don't want a monthly subscription and want their project files and old versions kept on their own computer. You pay by the second only for the few minutes of cloud GPU it takes to generate, the final picture has no watermark, the code is open source, and you can circle part of a frame to have the director reshoot just the segment you're not happy with.

</details>

<details>
<summary><b>How much does it cost?</b></summary>

The plugin is free. You pay for two things only: per-second GPU billing (see the test runs above), and the director and vision models' own API fees (the recommended Ling-3.2-Flash has a free tier).

</details>

<details>
<summary><b>Can I use photos of real people or celebrity stills?</b></summary>

Yes. A character reference can be a real person (public figures included); the plugin does not block it, and getting the person's consent and following local likeness and deep-synthesis labelling rules is on the user. The picture has no watermark; the AI-generated note is written only into the file metadata. The upload dialog takes only scenes, props, and outfits because the wardrobe path only redraws clothes; put a person's picture in the project as the reference.

</details>

<details>
<summary><b>Which GPUs and operating systems are supported?</b></summary>

The GPU is a 4090 48G; other cards are added as they pass `gpu-qualify` on real hardware. macOS and Windows are supported: on Windows 11 the whole short-drama path, from copying a template and having the director write the script to the final cut, has been run end to end (including the default GBK encoding of Chinese Windows). Remake, creating a machine on the "Start" page, and "Update environment" have not been tested on Windows yet. Linux is on the roadmap.

</details>

<details>
<summary><b>After updating the plugin, do I need to reinstall the machine's environment?</b></summary>

Not necessarily. The machine has its own environment version, which only goes up when what needs to be installed changes. If it doesn't match, the director tells you and the "Machines" page tags that machine; once it has stopped, click "Update environment…": after you confirm the price, the plugin starts it, reruns [`setup.sh`](skills/shisei-motion/assets/image/setup.sh) (only what changed), runs the security check and stops it again. The rules are in [docs/IMAGE.md](docs/IMAGE.md).

</details>

<details>
<summary><b>Can I use it commercially?</b></summary>

The plugin code, yes. The generation models each have their own license: MiniMax H3 and the character-swap LoRA use `minimax-h3-community-license`, so read it before commercial use (see "License" below).

</details>

<details>
<summary><b>Troubleshooting</b></summary>

| Situation | What to do |
| :--- | :--- |
| No API key / no GPU machine selected / no ssh key | Go to that step on the "Start" page (no ssh key: click "Make ssh key" there), or to "Settings" |
| [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) CLI not installed | "Settings" → "Install compshare-cli" (installs into the plugin's own directory) |
| The director didn't finish the script / has gone quiet | Switch the model at the top of the director panel (it resends your last message), or set up a working model in `dsh` and click retry |
| Over the project's budget cap | Shoot fewer takes or segments, or click "Raise the cap" |
| GPU out of stock | The plugin retries automatically every 2 minutes, and waiting costs nothing |
| A job not submitted by Shisei appeared on the machine | The plugin has already paused the queue; stop the machine on the "Machines" page, then check the instance's security in the console |
| Machine environment version is old / doesn't match | Once the machine has stopped, click "Update environment…" on the "Machines" page; if it says the machine is newer than the plugin, update the plugin |
| The machine is in use by another project | Click "Queue, shoot when free", or make another machine on the Start page; the "Machines" page shows who's using it |
| "Pre-check" never returns a result | Test the vision model in "Settings"; pre-check is only advice and doesn't stop you from approving manually |

</details>

## Roadmap

- **Bring your own GPU**: Local graphics cards and vast.ai.
- **More swappable models**: Both the image and video models become swappable.
- **Aspect ratios and publishing specs**: Landscape, square, and more aspect ratios and publishing specs.
- **And more**: More GPUs, Linux, whisper-aligned subtitles, scripts in other languages, and Shisei Pose (a pose library, plus line art to clay render / depth / normals).

## Community · Acknowledgments

- **Community**: [X @wquguru](https://x.com/wquguru) · [GitHub Issues](https://github.com/wquguru/dsh-shisei/issues) · Report security issues privately via [SECURITY.md](SECURITY.md) · Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- **Thanks to**: [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness), [rea-video](https://github.com/JuneYaooo/rea-video) (the idea of breaking a reference video down shot by shot), ComfyUI, [ComfyUI-BFSNodes](https://github.com/alisson-anjos/ComfyUI-BFSNodes), Qwen-Image, MiniMax H3, SAM 3D Body, SAM 3.1, MoGe, Blender, whisper.cpp, ffmpeg.

## License

- The plugin code is Apache-2.0 ([LICENSE](LICENSE)).
- Exceptions: under `skills/shisei-motion/assets/workflows/`, `swap_studio.json` (adapted from ComfyUI-BFSNodes' character-swap example), `drama_take.json` (which uses its MiniMax H3 sampling chain), and `mocap_glb.json` are distributed under **GPL-3.0-only** ([NOTICE](NOTICE), [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt)); `drama_image.json` in the same directory is Apache-2.0.
- `skills/shisei-motion/assets/vendor/wheels/` holds unmodified wheels of compshare-cli (Apache-2.0) and the packages it needs (MIT, BSD, ISC, Apache-2.0, MPL-2.0), so it installs without a network; each license is listed in [NOTICE](NOTICE).
- `lib/ssh.cjs` is the plugin's own ssh (so the computer needs no OpenSSH); it bundles ssh2 (MIT) and a few small libraries (MIT, BSD-3-Clause, Unlicense), listed in [NOTICE](NOTICE).
- The asset library isn't distributed with the plugin. The plugin reads the public assets at `shisei.indaloop.app` online; its scenes, props, and outfits are CC0.
- Models aren't distributed with the plugin; they're fetched at runtime on your instance. MiniMax H3 and the character-swap LoRA use `minimax-h3-community-license`, and Qwen-Image, SAM 3D Body, SAM 3.1, and MoGe each have their own licenses; read them before any commercial use.
- This is an independent open-source project and is not affiliated with DeepSeek, [compshare](https://passport.compshare.cn/register?referral_code=4Lx1jWqZk3lBVdaBfXlFAc) (优云智算), or MiniMax.
