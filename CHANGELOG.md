# Changelog

One section per version, newest first. `scripts/release.sh` writes it from the `feat:` / `fix:` commits since the last version tag, grouped by scope (Drama `drama`, Remake `motion`, Machines & billing `machines`, Reference videos `study`, Interface `ui`; anything else goes under Other). Lines written by hand under `## Unreleased` become the version's summary. A stable version lists the changes of every rc before it. See [docs/RELEASE.md](docs/RELEASE.md).

## 0.2.2 — 2026-11-14

### Interface

#### Features

- Upgrade steps that work on dsh Desktop, and the version card confirms the upgrade after restart ([53b2495](https://github.com/wquguru/dsh-shisei/commit/53b249579ffc3bed2da34481996423c59e48d11d))

## 0.2.1 — 2026-11-14

## 0.2.0 — 2026-11-14

Nothing to install by hand: the plugin brings compshare-cli, its own ssh and, when the computer has none, ffmpeg, and makes the ssh key when needed (tested from an empty setup on macOS and on Windows with dsh Desktop). Short drama gains a progress rail, one-by-one picture review and one-click recommended takes; runs now survive the ssh drops that compshare's gateway sometimes makes. Tested with dsh 0.2.0-rc.2 and dsh Desktop 0.2.2-rc.3.

### Drama

#### Features

- Series page leads with the episodes and the regulars, and asks only what needs you ([1471afd](https://github.com/wquguru/dsh-shisei/commit/1471afdbb029e9d188b9761f9366dd745456fed4))
- Takes play in place with sound; pick with its own button; no more pointing at a spot ([79f1817](https://github.com/wquguru/dsh-shisei/commit/79f181707c7002244d8cc22bf2e1efde20ca34fd))
- A frame sent to the director comes with what the image model sees on it ([e90b883](https://github.com/wquguru/dsh-shisei/commit/e90b88363f944fc0babd7c10df456deabd5c08e8))
- One click uses the director's recommended takes and cuts; ask for recommendations from the bottom bar ([bfe9c5b](https://github.com/wquguru/dsh-shisei/commit/bfe9c5b3518b1a1fa5daae19fada3a313857df2e))
- A five-step progress rail under the project title ([fe8c84d](https://github.com/wquguru/dsh-shisei/commit/fe8c84d50959e12edb7cd2aa6dbc3954fe5e9086))
- Review the waiting pictures one after another in a dialog ([a061119](https://github.com/wquguru/dsh-shisei/commit/a0611190df1b415a464c6f0ede382a98b695569b))
- Fix a voice from an uploaded recording, change a regular's portraits, add a regular from words or a photo ([c4c9310](https://github.com/wquguru/dsh-shisei/commit/c4c931061c29e3f7804cca912de507a0595ba48f))

#### Fixes

- Describe a paused frame without the image model's reasoning, so it arrives in seconds ([f7c0b47](https://github.com/wquguru/dsh-shisei/commit/f7c0b47befc83078941bfddb286a441868518a76))
- Rail, take buttons and notices from the second walkthrough ([56f02e8](https://github.com/wquguru/dsh-shisei/commit/56f02e8c184ac3d96919d069b307193c1c153a09))
- A segment whose inner voice a cut-off run never brought back offers to shoot just that line, and the cut says so instead of leaving silence under the subtitle ([d1e5c23](https://github.com/wquguru/dsh-shisei/commit/d1e5c239a65544cdcda25179e08f1343180c8e1f))

### Remake

#### Fixes

- Tell a whole-dance trial from the whole dance, and drop "fewer takes" when over its cap ([fc73e02](https://github.com/wquguru/dsh-shisei/commit/fc73e02b15ffd351cf6e788680af11a0ae2b9707))
- A whole dance logs each part as it starts and is stored ([b1fab45](https://github.com/wquguru/dsh-shisei/commit/b1fab45f3494e906cca7c4d1fb544cea2b171b53))
- The take pre-check no longer calls clothes changed on purpose a fault, and one take drops the "takes differ" hint ([037c326](https://github.com/wquguru/dsh-shisei/commit/037c3269488b8d3cbbb2422d2256f2ff78b88dc6))
- One more take of a whole dance counts its parts from the first ([988d5e5](https://github.com/wquguru/dsh-shisei/commit/988d5e527f510f4f3d33f8da3699c2c4a82e2754))
- A stopped recast can be continued from its page, and a stopped whole dance is priced for the parts left ([e0f812e](https://github.com/wquguru/dsh-shisei/commit/e0f812ebd060b618ad584082c904099639c7dd32))
- A remake left during the footage check opens on what to do next, instead of a start button that is refused in English ([5df0df3](https://github.com/wquguru/dsh-shisei/commit/5df0df35d8854db5a5e466508b8ccd6b6e9025ab))

### Machines & billing

#### Features

- Nothing to install by hand: compshare-cli comes with the plugin, the ssh key is made when needed, and the computer stays awake during billed runs ([3c6af26](https://github.com/wquguru/dsh-shisei/commit/3c6af267d32977df2f230d78d07e5350098805fd))
- The plugin brings its own ssh, so a computer without OpenSSH can still create and use machines ([8d52b16](https://github.com/wquguru/dsh-shisei/commit/8d52b16e6c38e15dae1d09ffc991c178f245aede))
- Ffmpeg is fetched in the background when this computer has none, and the page speaks up only if that fails ([cd6a5bf](https://github.com/wquguru/dsh-shisei/commit/cd6a5bfb50fe7901f0042f04f4680b15c0ca8ed3))

#### Fixes

- First-time setup on Windows says what is wrong and works with dsh's own Python ([b00acbd](https://github.com/wquguru/dsh-shisei/commit/b00acbd26438b2791bb7b4f07b04bb93e4e44d3b))
- The ffmpeg download makes its folder first and uses the registry the dsh profile is set up with ([b4913de](https://github.com/wquguru/dsh-shisei/commit/b4913deca68b379f1fb0cd561d3a494fa3a7da9e))
- A failed ffmpeg download says why, and a package not published yet is no longer shown as a failure ([886dd46](https://github.com/wquguru/dsh-shisei/commit/886dd4616110ee63a9a92e8ad99fc78f9ab010cf))
- On dsh Desktop the plugin's ssh finds the desktop's Node, and closing or reopening the tunnel finds the one Shisei opened ([71aface](https://github.com/wquguru/dsh-shisei/commit/71aface8fae3a888bbd7395936788ce019fb82b4))
- Start ComfyUI again when it died after the machine booted, instead of failing the run ([d8de595](https://github.com/wquguru/dsh-shisei/commit/d8de59529a3bbbcdd2f9ed89e1776fecba304e4d))
- Right after Shisei gets compshare-cli ready, the machine status shows at once instead of 看不到机器状态 for minutes ([6609c5c](https://github.com/wquguru/dsh-shisei/commit/6609c5c6291aadd83ac1692273d04b77f4294a10))
- On Windows, background runs no longer open a console window for each program they start ([6f91adc](https://github.com/wquguru/dsh-shisei/commit/6f91adcda9036eec13ffc4e8210cb36857f7b237))
- A computer whose ssh key has a passphrase connects through its own OpenSSH again, and a run that cannot reach the machine says what ssh said ([4b2d710](https://github.com/wquguru/dsh-shisei/commit/4b2d710c69dda370c82dea58a2662570b0a64d48))
- The machine status in the sidebar updates as soon as Start accepts the keys, instead of saying the keys failed for up to three minutes ([9aea01d](https://github.com/wquguru/dsh-shisei/commit/9aea01d7eb85d6a782cc25084c26a7c065816f82))
- The machines table fits a 1280-wide window, so each machine's button is no longer hidden off to the right ([de708a4](https://github.com/wquguru/dsh-shisei/commit/de708a4ce27f36a04e4931d35335d43b7f678fa5))
- Starting a machine from a computer with no ssh key makes the plugin's key first, instead of billing the start and then failing with 'no ssh key' ([b17e36b](https://github.com/wquguru/dsh-shisei/commit/b17e36b1ff2fa120af814995900ded3aa4856968))
- A dropped ssh connection to the machine is tried again instead of failing the whole run at the last step ([8e687f4](https://github.com/wquguru/dsh-shisei/commit/8e687f4fa784b28189bd2af375340f48757074e0))
- The tunnel to the machine is opened again when ssh drops before the handshake, instead of failing the run ([7c371f2](https://github.com/wquguru/dsh-shisei/commit/7c371f26278c08e656655172db3f87c585758d43))
- Copying pictures and videos to and from the machine is tried again when ssh drops, instead of failing the run ([568df36](https://github.com/wquguru/dsh-shisei/commit/568df360c28488981caafd0731509afeffc88f9f))
- Sending work to the machine opens the tunnel again when it dropped, instead of failing the run ([f319010](https://github.com/wquguru/dsh-shisei/commit/f319010fb0b881d15f70067d06c1802784b68c29))

### Interface

#### Features

- The shoot confirmation says the computer stays awake, and the upgrade card has steps for dsh Desktop ([2779ac1](https://github.com/wquguru/dsh-shisei/commit/2779ac1d7deb938ef459aa704487490bd1b5999e))
- Rename Recast to Remake (翻拍), the second line beside short drama ([4790f4b](https://github.com/wquguru/dsh-shisei/commit/4790f4bf1596275adc6ae94ce4a29f2d96acfaff))
- Start asks for the compshare AccessKey on its own page, reopens your copy of a practice script, and the image check can use dsh's own model ([9579cb9](https://github.com/wquguru/dsh-shisei/commit/9579cb93e93b7bc6546fe2f664b3ece9f2f63917))
- The Plugins page shows Shisei with its own icon, a localized one-liner and a banner of the two lines ([24fc057](https://github.com/wquguru/dsh-shisei/commit/24fc0578e41553e385cd6c79cd6425804df907b8))

#### Fixes

- The new-series dialog is no longer cut off on the left ([0fb05dc](https://github.com/wquguru/dsh-shisei/commit/0fb05dc6bed7233f39d5b79b3150aaf0393a8830))
- The 开始 entry in the sidebar keeps its title on one line when the note beside it is long ([f0afcf0](https://github.com/wquguru/dsh-shisei/commit/f0afcf0104b8bb0aa6ea68d0b3d432c33f58cb7c))

## 0.1.2 — 2026-11-13

Windows 11 is now supported for the short-drama flow (tested end to end on Chinese Windows, including its default GBK encoding; recast and creating a machine on the "Start" page are not yet tested there). New: series of episodes that share approved characters, and a fixed voice per character. Tested with dsh 0.2.0-rc.2.

### Drama

#### Features

- Series of episodes that share characters approved once ([957c98d](https://github.com/wquguru/dsh-shisei/commit/957c98d13d4911c7e33bad4bba33f08f1c84ad08))
- Page routes for series and the recap in a new episode's first message ([337bc21](https://github.com/wquguru/dsh-shisei/commit/337bc21fabd58564244ef69a825cff330ff68b85))
- Fix a character's voice from one of its own lines ([a388f65](https://github.com/wquguru/dsh-shisei/commit/a388f656b5fc06a51aeeadab3d10782bd45776dc))
- Fix a voice from a character's line in the picked take ([256e1f0](https://github.com/wquguru/dsh-shisei/commit/256e1f086a8e0a92187ef24be412d8a9ea3180e2))

#### Fixes

- Chinese text is read and written as UTF-8 on Chinese Windows ([5431287](https://github.com/wquguru/dsh-shisei/commit/5431287d8df831d19b39f15133239519c36bbd4e))

### Machines & billing

#### Fixes

- Leases, the ssh tunnel and the director's commands work on Windows ([30f2079](https://github.com/wquguru/dsh-shisei/commit/30f2079850f793b596b070fd250653f3e470a335))
- Ssh never waits for a password, and the key bridge finds a CLI installed after dsh started ([8aaf90b](https://github.com/wquguru/dsh-shisei/commit/8aaf90ba31b911b3d750f40de856b4a39418340d))

### Interface

#### Features

- Series page with episodes, regulars and the story so far ([bc21a66](https://github.com/wquguru/dsh-shisei/commit/bc21a665d61573b1f33ef85431c7a09cfe411a53))
- Pick, play and clear a character's fixed voice ([c5e8501](https://github.com/wquguru/dsh-shisei/commit/c5e8501f8f2054362cfb16e4afad51f1e7a21783))

#### Fixes

- Fixed voice is playable on the project page and old notices clear when shooting starts ([29625bd](https://github.com/wquguru/dsh-shisei/commit/29625bda539c5e6eeec80bd9e8134284950a3739))

## 0.1.1 — 2026-11-13

### Machines & billing

#### Features

- New machines come from the free Shisei community image ([3368a1b](https://github.com/wquguru/dsh-shisei/commit/3368a1b46c55f6f820f0573c9ae14dcf90b094db))

#### Fixes

- A project's next start defaults to the machine its last run used ([5d073a2](https://github.com/wquguru/dsh-shisei/commit/5d073a214ad84c3d7fa95ef5e0790d496bb35bd7))

### Interface

#### Features

- The sign-up links point at compshare's registration page ([7d10268](https://github.com/wquguru/dsh-shisei/commit/7d10268f3cb0d1089273088e5ec8dbe4f7d89f0b))
- The Start page no longer asks which image to build the machine from ([98bad95](https://github.com/wquguru/dsh-shisei/commit/98bad950d562ea786a802f748c21dca1e84038ff))

### Other

#### Fixes

- Setup.sh skips a node already at its pin and retries GitHub over HTTP/1.1 ([a9ab68e](https://github.com/wquguru/dsh-shisei/commit/a9ab68ec7b70b2ee7be4f255e80d81d2b17022b5))

## 0.1.0 — 2026-11-13

First stable release. Since rc.1: recast a whole dance up to 5 minutes from a trial take, several GPU machines with a queue that takes whichever comes free first, compshare keys checked when saved, the director's model picked from dsh's catalog, and stricter rules for minors in prompts. Tested with dsh 0.2.0-rc.2.

### Drama

#### Features

- The Shisei library is read online only, with a versioned data contract ([da2941b](https://github.com/wquguru/dsh-shisei/commit/da2941bf930a703439b6141366c39827aeb4369a))
- No minor is drawn: children may appear in the story and lines, never in the cast or a prompt ([3348f90](https://github.com/wquguru/dsh-shisei/commit/3348f909a3fc28100d8b9f5c763546e756fee8ab))
- Pick the director's model from dsh's catalog, and when a turn stalls, switch model and resend your last message ([1a0c411](https://github.com/wquguru/dsh-shisei/commit/1a0c4118f1ca8e4ff90d72f788bae9d6834661f9))
- Block minor nudity and draw minors at their real age ([d955cbf](https://github.com/wquguru/dsh-shisei/commit/d955cbf96770d478cbf39f7df405da76e92892af))

#### Fixes

- Idle minutes before the auto-stop count in estimates, spend and the cap; sent-back pictures go to the director in one message; the spend button holds when a run would be wasted ([d1b0de7](https://github.com/wquguru/dsh-shisei/commit/d1b0de75a7df02aba1959db743bde5b8bf484937))
- The spend button shows the same "up to ¥" as the start dialog; a question in the director's last lines counts as waiting for you ([2dd1a50](https://github.com/wquguru/dsh-shisei/commit/2dd1a500366889bc8b8e57fbdff604d490b3aee8))
- Waiting for the director no longer hides a free cut; the ledger line is written before local checks so the next run can settle idle time ([69e2f1f](https://github.com/wquguru/dsh-shisei/commit/69e2f1f7a6e7e4fb41e07ac9335c23fd576c3578))
- The director can run the free drama commands without asking for wider access ([d85d3ac](https://github.com/wquguru/dsh-shisei/commit/d85d3ace7acc2bbbfc0e110dcf508332f79094b5))
- A send-back the director never got to now has a button to ask it again ([38a63a6](https://github.com/wquguru/dsh-shisei/commit/38a63a66417f6b0a69eb998158cd422cde86771a))
- The first-frame pre-check no longer flags two people not looking at each other when the shot says one looks away ([6abe52e](https://github.com/wquguru/dsh-shisei/commit/6abe52ed77425fb21a1f5c677000a61f463f8d2a))
- A shoot refused because another project holds the machine names that project and offers no stop button ([17557cd](https://github.com/wquguru/dsh-shisei/commit/17557cdf27b51d52e88c3f45f4fcda8a72c4b4fd))
- The look-away rule of the first-frame pre-check also knows 低着头 and 垂着头 ([6f7c86a](https://github.com/wquguru/dsh-shisei/commit/6f7c86a0e392cb6012749844f2c2bcb140d4dbd6))
- The director switches to the page's language when you change it mid-project ([dcb681d](https://github.com/wquguru/dsh-shisei/commit/dcb681d8ce568e8bafd07dd568b5eea3a6a3e656))
- Video and voice prompts name a minor as a minor, not as an adult ([223a902](https://github.com/wquguru/dsh-shisei/commit/223a902c58607d90d226ec249d845257b936abe8))

### Recast

#### Features

- Swap two dancers at once, or one on the original background, from the dance-swap page ([9ff40ea](https://github.com/wquguru/dsh-shisei/commit/9ff40eab152ba9f916deea90793edbbbad462df7))
- Rename the feature to 换角 (Recast) ([51ecd09](https://github.com/wquguru/dsh-shisei/commit/51ecd09aad7a55760f5aeb7e960be2a5c73346ca))
- Recast a whole dance up to 5 minutes: try the first 4.5 s, then shoot the full length from the take you pick ([9774f57](https://github.com/wquguru/dsh-shisei/commit/9774f57d303b14c61e7ec4eb6bdfd28d427420bf))

#### Fixes

- Two-person footage check puts the left and right tags on the clip itself ([3b7e91a](https://github.com/wquguru/dsh-shisei/commit/3b7e91a288b20cb9f3a29d4cb131c9f786cfa71a))
- The Recast run page no longer goes blank while a run is going ([da2e8b0](https://github.com/wquguru/dsh-shisei/commit/da2e8b022fd4c28666ea9a36b02e5fec098efa64))
- A recast refused because another project holds the machine names that project and offers no stop button ([6497687](https://github.com/wquguru/dsh-shisei/commit/649768790db95698fead52dbf064a5a66d722a6a))
- The whole dance's cap counts what its trial already spent ([f90430d](https://github.com/wquguru/dsh-shisei/commit/f90430d4900b984d03e88eb25c12cd3b0258f4b2))
- The recast page's spend for a whole dance includes its trial ([b6b7757](https://github.com/wquguru/dsh-shisei/commit/b6b775710e4c7447c015df776ac9e4bbc3924183))

### Machines & billing

#### Features

- Versioned machine environment: setup.sh pins nodes and weights, start-up warns when a machine is old or differs ([f912b59](https://github.com/wquguru/dsh-shisei/commit/f912b59647b5eb65231420524772e6bcb93bdbd7))
- Set up a GPU machine without opening a terminal ([72edb29](https://github.com/wquguru/dsh-shisei/commit/72edb299f4b6d097ac2ccc4ddeaec005b49d3c2c))
- The Machines page has 'Create a new machine', back to the start page's machine step ([8b72a3f](https://github.com/wquguru/dsh-shisei/commit/8b72a3fcdae45cce74b4b59172a774f9460303e8))
- New machines come from the Shisei community image once one is published, with a switch back to the official image ([91a998e](https://github.com/wquguru/dsh-shisei/commit/91a998ef61b1d90c8ed455300b1f0d0180990d86))
- A run can queue for a busy machine, and its price is quoted for the machine it will use ([7cf2ca5](https://github.com/wquguru/dsh-shisei/commit/7cf2ca5c8a7e5f357fc6d72f86f7b1e08877c077))
- The start confirmation names the machine it uses, and queues when every machine is busy ([aeedcda](https://github.com/wquguru/dsh-shisei/commit/aeedcda82bc96e2758091bf714b312c6329b29ba))
- Compshare keys are checked when saved; a rejected pair says why and holds the next steps ([3eb120d](https://github.com/wquguru/dsh-shisei/commit/3eb120d8514e74f3be96aa77f1ea1bdc552c08d1))

#### Fixes

- Setup.sh keeps a half-downloaded weight to resume; --stamp only with a test directory; utf-8 when reading setup.sh ([8a2526f](https://github.com/wquguru/dsh-shisei/commit/8a2526fba562374a95903b8a6d690cbb704d6bc1))
- Stop while waiting for a GPU, a failed stop says the machine still bills, one run per project ([5b5dc3d](https://github.com/wquguru/dsh-shisei/commit/5b5dc3ddcc818ed3e34bee884cc01618c37cc87f))
- The start page offers 'build another' when a machine is already picked ([504ff68](https://github.com/wquguru/dsh-shisei/commit/504ff68cec34c69b4465ca04cefd0efe1a6949a7))
- The price cap on a new machine counts both disk fields compshare quotes ([89730c5](https://github.com/wquguru/dsh-shisei/commit/89730c597626ee0d8d4724ecf98cf213a487e970))
- A new machine with a uhost- id is recognised and gets its scheduled stop ([5be8b21](https://github.com/wquguru/dsh-shisei/commit/5be8b21d0cadef219389014b29845fcf69ba00bc))
- Putting the ssh key on a new machine runs as bash -c ([8044c34](https://github.com/wquguru/dsh-shisei/commit/8044c3406ba12751d7bf99ec6f1cd19fc329fee2))
- Continuing a machine setup starts the machine without an unsupported flag ([a372e13](https://github.com/wquguru/dsh-shisei/commit/a372e13a0f7d98883d2e11f8fa5f90dfa59b8ef8))
- New-machine quote shows the disk once and caps at compshare's own total; a restarted setup gets a full window ([86159a5](https://github.com/wquguru/dsh-shisei/commit/86159a5f9d5f8ba028fd266719fd0bcd73bdfc5a))
- The machine setup ledger books a taken-over machine from its creation, at the instance's own price ([2bc8f57](https://github.com/wquguru/dsh-shisei/commit/2bc8f574965094bfed725c0147b0b7abc276f18b))
- 'continue' on a machine whose zone is out of stock says so in plain words ([fd45ed1](https://github.com/wquguru/dsh-shisei/commit/fd45ed150731301e451ec9b402a82acec16ceec9))
- The key step on a new machine retries while it boots and records why it failed ([0670264](https://github.com/wquguru/dsh-shisei/commit/06702649197818f22f51391735264260523d1884))
- A successful key step on a new machine is recognised ([e13bc95](https://github.com/wquguru/dsh-shisei/commit/e13bc957bb41d08e445c4287e93736a39c02168b))
- 'build another' from a failed setup gets a quote instead of only 'continue' ([89d9f84](https://github.com/wquguru/dsh-shisei/commit/89d9f84d8a55ca3ae2740240e91f1596ad01ae67))
- The security check on a just-booted machine waits for ComfyUI before moving it to 127.0.0.1 ([2df6397](https://github.com/wquguru/dsh-shisei/commit/2df6397bfe5a778c3e3132c46435d08bf0e24c17))
- Starting setup.sh on a new machine returns at once ([ce24017](https://github.com/wquguru/dsh-shisei/commit/ce24017b173fb4cd6c60497c8083f679d597476a))
- The machine check reads the indented env.json, and a tunnel whose local port is taken fails ([b40b20c](https://github.com/wquguru/dsh-shisei/commit/b40b20c42b1d84b274ecb76dd741c7c24c8f15ac))
- Gpu-qualify accepts a card that runs its PyTorch's same-major kernels ([18d6f71](https://github.com/wquguru/dsh-shisei/commit/18d6f71def13729411c1718252bf0673ca06e040))
- A new machine's quoted hourly price includes a community image's own fee ([8ba5935](https://github.com/wquguru/dsh-shisei/commit/8ba5935c1ed06347a0ee0e16835a059c9a1626f7))
- A shoot refused because another project holds the machine no longer cuts that machine's run short or bills idle time ([0781649](https://github.com/wquguru/dsh-shisei/commit/07816498edf8e9ca542ab092b361dd4b5d1d7bc6))
- Continuing a machine setup stops at once when compshare lists the start as failed ([b3fd668](https://github.com/wquguru/dsh-shisei/commit/b3fd66800383df54d0912aa11c82a3eaf66a345b))
- A run that never got the machine's lease no longer bills or arms a stop on a machine another project is using ([6a15a51](https://github.com/wquguru/dsh-shisei/commit/6a15a51561ab0e887e07a4561182efa9a3f7166c))
- Only your click on the Shisei page starts a paid run; the director quotes and points at the button ([1bc49dd](https://github.com/wquguru/dsh-shisei/commit/1bc49dd91c92c985e83fff71357a656c85a7cf58))
- After cancelling a queue or a stock wait, the recast page offers a new quote; a busy machine is named by the project using it ([901ba37](https://github.com/wquguru/dsh-shisei/commit/901ba3720e7a9c5b9c154a995357ef9dff676d2a))
- A queued start quotes the dearest machine and says whichever frees first is used; a new run forgets the last one's refusal ([d1c0c30](https://github.com/wquguru/dsh-shisei/commit/d1c0c3081029e2ceaed7de6c1e0902ae9040dc58))
- The queue names a recast project and shows the busy machine's price ([00f69df](https://github.com/wquguru/dsh-shisei/commit/00f69dfec22525c22f05a04c8ffe3050460b605d))
- Creating a machine says when it is sold out, and a failed second machine is not offered again ([ea5d783](https://github.com/wquguru/dsh-shisei/commit/ea5d78359a2620570d52f4d764c53d891d8c067a))
- A new machine is only created in a zone where machines were set up end to end ([084f071](https://github.com/wquguru/dsh-shisei/commit/084f07155a325a4e9ef1ee0bed1d5761d251f763))
- A project that takes over an idling machine ends the last project's idle charge ([ba694c3](https://github.com/wquguru/dsh-shisei/commit/ba694c354473ac296c4ab19433f5bc30624cbcaf))
- A queued run also takes a machine registered while it waits ([ea19a2e](https://github.com/wquguru/dsh-shisei/commit/ea19a2e226c744fa9bcfe494ca9801de2270b777))

### Interface

#### Features

- Show when a newer plugin version is out, with the command to upgrade ([6214134](https://github.com/wquguru/dsh-shisei/commit/62141349a16ef4488071028b45967e922907ac47))
- The settings page no longer shows the fixed content rules card ([d7a6df0](https://github.com/wquguru/dsh-shisei/commit/d7a6df05044cb9c32eb731b274fb9dc3c4485432))

#### Fixes

- Director bubbles show only your words, one write button, segment rows lead with lines and action, homophones are not wrong lines, one-click use of single takes, no real-person footage on the home page ([262d7fd](https://github.com/wquguru/dsh-shisei/commit/262d7fd15099978bb9841e41bad4c7a4ce2756f3))
- Clearer page wording: machine instead of instance, no internal terms or frame-count rule, compshare named 优云智算 in Chinese ([8be0e3a](https://github.com/wquguru/dsh-shisei/commit/8be0e3acaa6b0d07bd2b74bb5b75c7ec9e7bb9e0))
- Drop the AI-mark wording from the page ([703aa8d](https://github.com/wquguru/dsh-shisei/commit/703aa8d8b6343707da304f665e5b91f449281466))
- The home page links to the practice scripts once setup is done ([116fe5e](https://github.com/wquguru/dsh-shisei/commit/116fe5eac77142922dd5701dbde994fdd3090176))
- The Recast feature is called Recast again in English, Japanese and Korean ([9abc02f](https://github.com/wquguru/dsh-shisei/commit/9abc02f236bc2923b187e743b330971315dff7f0))
- The 对标 tab keeps the same side margins as the director tab ([2a22a08](https://github.com/wquguru/dsh-shisei/commit/2a22a08fb31a68eb9a6216606f251b5da29e7dba))
- The 换角 run page no longer goes blank while a run starts ([0bed73a](https://github.com/wquguru/dsh-shisei/commit/0bed73adcdfe639623ed23812f929988fa831c30))
- The take page no longer crashes when a run has stopped ([8e07e85](https://github.com/wquguru/dsh-shisei/commit/8e07e851422a5544cbba85bc1bc2240d9a5e4e01))

## 0.1.0-rc.1 — 2026-11-12

First public release. Drama: one sentence to a script, cast the characters, make first frames, shoot the shots and cut the film. Recast: put a character of your choice into a real dance video. Both run on your own Compshare GPU, priced before it starts, with a budget cap per project. Needs dsh 0.2.x.

### Drama

#### Fixes

- Idle minutes before the auto-stop count in estimates, spend and the cap; sent-back pictures go to the director in one message; the spend button holds when a run would be wasted ([d1b0de7](https://github.com/wquguru/dsh-shisei/commit/d1b0de75a7df02aba1959db743bde5b8bf484937))
- The spend button shows the same "up to ¥" as the start dialog; a question in the director's last lines counts as waiting for you ([2dd1a50](https://github.com/wquguru/dsh-shisei/commit/2dd1a500366889bc8b8e57fbdff604d490b3aee8))
- Waiting for the director no longer hides a free cut; the ledger line is written before local checks so the next run can settle idle time ([69e2f1f](https://github.com/wquguru/dsh-shisei/commit/69e2f1f7a6e7e4fb41e07ac9335c23fd576c3578))
- The director can run the free drama commands without asking for wider access ([d85d3ac](https://github.com/wquguru/dsh-shisei/commit/d85d3ace7acc2bbbfc0e110dcf508332f79094b5))
- A send-back the director never got to now has a button to ask it again ([38a63a6](https://github.com/wquguru/dsh-shisei/commit/38a63a66417f6b0a69eb998158cd422cde86771a))
- The first-frame pre-check no longer flags two people not looking at each other when the shot says one looks away ([6abe52e](https://github.com/wquguru/dsh-shisei/commit/6abe52ed77425fb21a1f5c677000a61f463f8d2a))
- A shoot refused because another project holds the machine names that project and offers no stop button ([17557cd](https://github.com/wquguru/dsh-shisei/commit/17557cdf27b51d52e88c3f45f4fcda8a72c4b4fd))
- The look-away rule of the first-frame pre-check also knows 低着头 and 垂着头 ([6f7c86a](https://github.com/wquguru/dsh-shisei/commit/6f7c86a0e392cb6012749844f2c2bcb140d4dbd6))

### Recast

#### Features

- Swap two dancers at once, or one on the original background, from the dance-swap page ([9ff40ea](https://github.com/wquguru/dsh-shisei/commit/9ff40eab152ba9f916deea90793edbbbad462df7))
- Rename the feature to 换角 (Recast) ([51ecd09](https://github.com/wquguru/dsh-shisei/commit/51ecd09aad7a55760f5aeb7e960be2a5c73346ca))

#### Fixes

- Two-person footage check puts the left and right tags on the clip itself ([3b7e91a](https://github.com/wquguru/dsh-shisei/commit/3b7e91a288b20cb9f3a29d4cb131c9f786cfa71a))
- The Recast run page no longer goes blank while a run is going ([da2e8b0](https://github.com/wquguru/dsh-shisei/commit/da2e8b022fd4c28666ea9a36b02e5fec098efa64))
- A recast refused because another project holds the machine names that project and offers no stop button ([6497687](https://github.com/wquguru/dsh-shisei/commit/649768790db95698fead52dbf064a5a66d722a6a))

### Machines & billing

#### Features

- Versioned machine environment: setup.sh pins nodes and weights, start-up warns when a machine is old or differs ([f912b59](https://github.com/wquguru/dsh-shisei/commit/f912b59647b5eb65231420524772e6bcb93bdbd7))
- Set up a GPU machine without opening a terminal ([72edb29](https://github.com/wquguru/dsh-shisei/commit/72edb299f4b6d097ac2ccc4ddeaec005b49d3c2c))
- The Machines page has 'Create a new machine', back to the start page's machine step ([8b72a3f](https://github.com/wquguru/dsh-shisei/commit/8b72a3fcdae45cce74b4b59172a774f9460303e8))

#### Fixes

- Setup.sh keeps a half-downloaded weight to resume; --stamp only with a test directory; utf-8 when reading setup.sh ([8a2526f](https://github.com/wquguru/dsh-shisei/commit/8a2526fba562374a95903b8a6d690cbb704d6bc1))
- Stop while waiting for a GPU, a failed stop says the machine still bills, one run per project ([5b5dc3d](https://github.com/wquguru/dsh-shisei/commit/5b5dc3ddcc818ed3e34bee884cc01618c37cc87f))
- The start page offers 'build another' when a machine is already picked ([504ff68](https://github.com/wquguru/dsh-shisei/commit/504ff68cec34c69b4465ca04cefd0efe1a6949a7))
- The price cap on a new machine counts both disk fields compshare quotes ([89730c5](https://github.com/wquguru/dsh-shisei/commit/89730c597626ee0d8d4724ecf98cf213a487e970))
- A new machine with a uhost- id is recognised and gets its scheduled stop ([5be8b21](https://github.com/wquguru/dsh-shisei/commit/5be8b21d0cadef219389014b29845fcf69ba00bc))
- Putting the ssh key on a new machine runs as bash -c ([8044c34](https://github.com/wquguru/dsh-shisei/commit/8044c3406ba12751d7bf99ec6f1cd19fc329fee2))
- Continuing a machine setup starts the machine without an unsupported flag ([a372e13](https://github.com/wquguru/dsh-shisei/commit/a372e13a0f7d98883d2e11f8fa5f90dfa59b8ef8))
- New-machine quote shows the disk once and caps at compshare's own total; a restarted setup gets a full window ([86159a5](https://github.com/wquguru/dsh-shisei/commit/86159a5f9d5f8ba028fd266719fd0bcd73bdfc5a))
- The machine setup ledger books a taken-over machine from its creation, at the instance's own price ([2bc8f57](https://github.com/wquguru/dsh-shisei/commit/2bc8f574965094bfed725c0147b0b7abc276f18b))
- 'continue' on a machine whose zone is out of stock says so in plain words ([fd45ed1](https://github.com/wquguru/dsh-shisei/commit/fd45ed150731301e451ec9b402a82acec16ceec9))
- The key step on a new machine retries while it boots and records why it failed ([0670264](https://github.com/wquguru/dsh-shisei/commit/06702649197818f22f51391735264260523d1884))
- A successful key step on a new machine is recognised ([e13bc95](https://github.com/wquguru/dsh-shisei/commit/e13bc957bb41d08e445c4287e93736a39c02168b))
- 'build another' from a failed setup gets a quote instead of only 'continue' ([89d9f84](https://github.com/wquguru/dsh-shisei/commit/89d9f84d8a55ca3ae2740240e91f1596ad01ae67))
- The security check on a just-booted machine waits for ComfyUI before moving it to 127.0.0.1 ([2df6397](https://github.com/wquguru/dsh-shisei/commit/2df6397bfe5a778c3e3132c46435d08bf0e24c17))
- Starting setup.sh on a new machine returns at once ([ce24017](https://github.com/wquguru/dsh-shisei/commit/ce24017b173fb4cd6c60497c8083f679d597476a))
- The machine check reads the indented env.json, and a tunnel whose local port is taken fails ([b40b20c](https://github.com/wquguru/dsh-shisei/commit/b40b20c42b1d84b274ecb76dd741c7c24c8f15ac))
- Gpu-qualify accepts a card that runs its PyTorch's same-major kernels ([18d6f71](https://github.com/wquguru/dsh-shisei/commit/18d6f71def13729411c1718252bf0673ca06e040))
- A new machine's quoted hourly price includes a community image's own fee ([8ba5935](https://github.com/wquguru/dsh-shisei/commit/8ba5935c1ed06347a0ee0e16835a059c9a1626f7))
- A shoot refused because another project holds the machine no longer cuts that machine's run short or bills idle time ([0781649](https://github.com/wquguru/dsh-shisei/commit/07816498edf8e9ca542ab092b361dd4b5d1d7bc6))
- Continuing a machine setup stops at once when compshare lists the start as failed ([b3fd668](https://github.com/wquguru/dsh-shisei/commit/b3fd66800383df54d0912aa11c82a3eaf66a345b))
- A run that never got the machine's lease no longer bills or arms a stop on a machine another project is using ([6a15a51](https://github.com/wquguru/dsh-shisei/commit/6a15a51561ab0e887e07a4561182efa9a3f7166c))
- Only your click on the Shisei page starts a paid run; the director quotes and points at the button ([1bc49dd](https://github.com/wquguru/dsh-shisei/commit/1bc49dd91c92c985e83fff71357a656c85a7cf58))

### Interface

#### Features

- Show when a newer plugin version is out, with the command to upgrade ([6214134](https://github.com/wquguru/dsh-shisei/commit/62141349a16ef4488071028b45967e922907ac47))

#### Fixes

- Director bubbles show only your words, one write button, segment rows lead with lines and action, homophones are not wrong lines, one-click use of single takes, no real-person footage on the home page ([262d7fd](https://github.com/wquguru/dsh-shisei/commit/262d7fd15099978bb9841e41bad4c7a4ce2756f3))
- Clearer page wording: machine instead of instance, no internal terms or frame-count rule, compshare named 优云智算 in Chinese ([8be0e3a](https://github.com/wquguru/dsh-shisei/commit/8be0e3acaa6b0d07bd2b74bb5b75c7ec9e7bb9e0))
- Drop the AI-mark wording from the page ([703aa8d](https://github.com/wquguru/dsh-shisei/commit/703aa8d8b6343707da304f665e5b91f449281466))
- The home page links to the practice scripts once setup is done ([116fe5e](https://github.com/wquguru/dsh-shisei/commit/116fe5eac77142922dd5701dbde994fdd3090176))
- The Recast feature is called Recast again in English, Japanese and Korean ([9abc02f](https://github.com/wquguru/dsh-shisei/commit/9abc02f236bc2923b187e743b330971315dff7f0))

