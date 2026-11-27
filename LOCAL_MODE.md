# 本地 ComfyUI 模式 + 魔搭

不租优云智算，直接用你自己的 ComfyUI；导演看图用魔搭 API Key。改动在
`skills/shisei-motion/scripts/local_mode.py`，`shisei.py` 只加了开关。

## 打开本地模式

```bash
S=skills/shisei-motion/scripts/shisei.py
python3 -I $S config set comfy_url http://127.0.0.1:8188      # 打开；设成空字符串就关掉
python3 -I $S config set comfy_dir "D:/ComfyUI"               # 可选：ComfyUI 在本机时填它的文件夹
python3 -I $S config set comfy_model_roots "E:/models,F:/more" # 可选：模型放在别处时
python3 -I $S tunnel                                           # 检查能否连上
```

也可以用环境变量 `SHISEI_COMFY_URL` / `SHISEI_COMFY_DIR`。

| | 优云智算（原版） | 本地模式 |
|---|---|---|
| 机器 | 租 4090 48G，开关机、计费、定时停机 | 一台叫 `local` 的机器，不开关、价格 0 |
| 连接 | ssh 隧道 127.0.0.1:18188 | 直接连 `comfy_url`（不走系统代理） |
| 传文件 | scp | 填了 `comfy_dir`：直接复制 input/ output/；没填：HTTP `/upload/image` 和 `/view` |
| 模型检查 | 机器上自动链接公共盘 | 只检查 `comfy_dir/models`，缺了就报出来，不下载 |
| 需要 bash 的步骤 | 在机器上跑 | 本机 bash（Windows 用 Git Bash），只有翻拍的部分步骤用到 |

租约照旧，两个项目不会同时占用同一个 ComfyUI。

## 要在 ComfyUI 里装的东西

短剧定妆和首帧（`drama_image.json`）：
- 节点：`TextEncodeQwenImage21`、`QwenImage21Cache`（需要新版 ComfyUI）
- 模型：`diffusion_models/qwen_image_2.1_int8_convrot.safetensors`、`text_encoders/qwen3vl_8b_int8_convrot.safetensors`、`vae/qwen_image_2.1_vae_bf16.safetensors`

短剧拍摄（`drama_take.json`）和翻拍（`swap_studio.json`、`mocap_glb.json`）用 MiniMax-H3，作者测下来要 48G 显存。
8G 显卡加 32G 内存跑不动 H3。Qwen-Image 也只能靠内存卸载慢慢跑，能不能跑通要自己实测。

## 导演看图换成魔搭

```bash
python3 -I $S config set vision_base_url https://api-inference.modelscope.cn/v1
python3 -I $S config set vision_model Qwen/Qwen3.5-35B-A3B
python3 -I $S config set vision_key_env MODELSCOPE_API_KEY
python3 -I $S config set vision_source key
export MODELSCOPE_API_KEY=ms-xxxx
python3 -I $S vision-test
```

## 已知限制

- dsh 网页里的「GPU 机器」页仍按优云智算设计，可能还会提示填密钥。本地模式下，命令行的 `drama-run` 等命令不需要它。
- 魔搭的 MiniMax-H3 视频接口只收 1 张首帧，而且图片要放在魔搭自己的图床上；本地文件和外站链接都会被拒绝。真人照片会被内容审核拦下。
