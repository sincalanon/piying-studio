#!/bin/bash
# 皮影 Motion 机器环境搭建。在优云智算官方「ComfyUI基础镜像纯净版0.43.0」（compshareImage-1x98m25rrumc）上运行。
# 可重复执行：已有的节点会切到锁定的 commit，已下载的权重会核对 sha256。
# 权重优先软链公共盘 /model，缺的从 ModelScope 按锁定的 revision 下载。
# 安全：ComfyUI 只监听 127.0.0.1，本机经 ssh -L 访问。
# 第 5 步装 headless Blender 和 ffmpeg，让白模渲染、合成也在实例上跑（shisei.py mannequin --remote）。
# 版本规范见 docs/IMAGE.md：改了下面「锁定」一节的任何一项，SHISEI_ENV 加一，并同步 shisei.py 的 SHISEI_ENV。
set -u

# ---- 锁定（shisei.py 读这一节核对机器；格式：每行一项，字段用 | 分隔）
SHISEI_ENV=1
NODES='
ComfyUI-BFSNodes|https://github.com/alisson-anjos/ComfyUI-BFSNodes.git|9dcb9e4d0827864d75fe2d94c7e48fe36735112b
comfyui-kjnodes|https://github.com/kijai/ComfyUI-KJNodes.git|ab72eda7ffad6280422d71ab7d54d3fb87c0ba18
comfyui-videohelpersuite|https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git|4d907bee61e92c2e65af3bd6383a4e4d356126d1
'
DOWNLOADS='
Alissonerdx/Minimax-H3-ComfyUI|78c9677ac150bc5fb6f99ddf3856807f99716559|loras/minimax_h3_character_swap_v1.0_ref2va_r64_gl.safetensors|loras|643a3fe63646fee5aebb181a7e1c9f8a1feb5f472709f37e22eeec290cbb75e3
Alissonerdx/Minimax-H3-ComfyUI|c80883943d2a7d554ea49db993d4ff12f1a0c3a4|loras/minimax_h3_head_swap_v1.0_r32.safetensors|loras|ea97bd1a8178460661055784d63b4cb560732196fd15d5d7f33bfd214b65ab00
Comfy-Org/sam-3d-body|33cdc580e3a4985fa8058737fb8062acd0f22d78|detection/sam_3d_body_dinov3_bf16.safetensors|detection|59fa45200c504c5b56625004d7d3385daf48c616613e88099e43bf83b3e249cf
Comfy-Org/gemma4|31f79f4da5cd3235a260c661022fb4e006739a11|text_encoders/gemma4_e2b_it_bf16.safetensors|text_encoders|dcc3c8d27be2db2ef38b78d564ebfa1e92eb24665ed2751f2983275f03a067c8
'
BL_VER=5.2.3
# ---- 锁定结束

CU=${SHISEI_CU:-/root/ComfyUI}
M=$CU/models
P=/model/ModelScope
STAMP_DIR=${SHISEI_STAMP_DIR:-/root/.shisei}
FAILED=''
fail(){ echo "FAIL $*"; FAILED="$FAILED $1"; }

# 第 7 步：只记实际读到的值（节点的 HEAD、下载文件的 sha256），不记期望值；有一项不对就不写，退出码非零
stamp(){
  echo "== 7. 写环境版本"
  if [ -n "$FAILED" ]; then
    echo "not stamping env $SHISEI_ENV, failed:$FAILED"; return 1
  fi
  mkdir -p "$STAMP_DIR"
  ACTUAL=''
  while IFS='|' read -r name url sha; do
    [ -n "$name" ] || continue
    ACTUAL="$ACTUAL${name}|$(git -C "$CU/custom_nodes/$name" rev-parse HEAD 2>/dev/null)
"
  done <<< "$NODES"
  ACTUAL_DL=''
  while IFS='|' read -r repo rev path dir sum; do
    [ -n "$repo" ] || continue
    ACTUAL_DL="$ACTUAL_DL$(basename "$path")|$(cat "$M/$dir/$(basename "$path").sha256" 2>/dev/null)
"
  done <<< "$DOWNLOADS"
  pip freeze 2>/dev/null > "$STAMP_DIR/pip-freeze.txt" || true
  SHISEI_ENV=$SHISEI_ENV ACTUAL=$ACTUAL ACTUAL_DL=$ACTUAL_DL CU=$CU STAMP_DIR=$STAMP_DIR BL_VER=$BL_VER python3 - <<'PY'
import hashlib, json, os, re, subprocess, time
def rows(s):
    return dict(l.split('|', 1) for l in s.splitlines() if '|' in l)
def first(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.splitlines()[0].strip()
    except Exception:
        return None
cu, d = os.environ['CU'], os.environ['STAMP_DIR']
comfy = None
try:
    comfy = re.search(r"['\"]([\d.]+)['\"]", open(os.path.join(cu, 'comfyui_version.py')).read()).group(1)
except Exception:
    pass
freeze = os.path.join(d, 'pip-freeze.txt')
env = {'env': int(os.environ['SHISEI_ENV']), 'built': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'comfyui': comfy,
       'nodes': rows(os.environ['ACTUAL']), 'downloads': rows(os.environ['ACTUAL_DL']),
       'blender': first(['blender', '--version']), 'blender_wanted': os.environ['BL_VER'],
       'ffmpeg': first(['ffmpeg', '-version']),
       'pip_freeze_sha256': hashlib.sha256(open(freeze, 'rb').read()).hexdigest() if os.path.exists(freeze) else None}
tmp = os.path.join(d, 'env.json.tmp')
json.dump(env, open(tmp, 'w'), indent=1, sort_keys=True)
os.replace(tmp, os.path.join(d, 'env.json'))
print('stamped env', env['env'])
PY
}

if [ "${1:-}" = --stamp ]; then   # 只给单测用：不核对就写，所以必须指向别的目录，不能写进真机的 /root/.shisei
  [ -n "${SHISEI_STAMP_DIR:-}" ] || { echo "--stamp needs SHISEI_STAMP_DIR (tests only)"; exit 2; }
  stamp; exit $?
fi

source /root/miniconda3/bin/activate
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
rm -f "$STAMP_DIR/env.json"   # 跑到一半失败的机器不能留着旧版本号

echo "== 1. ComfyUI 只监听本机"
sed -i "s/--listen 0.0.0.0/--listen 127.0.0.1/" /start.d/comfyui.sh
pkill -f "[m]ain.py --listen 0.0.0.0" || true

echo "== 2. 自定义节点（锁 commit）"
cd $CU/custom_nodes
PIP="pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple"
while IFS='|' read -r name url sha; do
  [ -n "$name" ] || continue
  if [ ! -d "$name/.git" ]; then git init -q "$name" && git -C "$name" remote add origin "$url"; fi
  # already at the pin (a machine from the Piying image): no GitHub round trip. Otherwise retry: github.com from the
  # instances drops connections (HTTP/2 framing errors, 443 timeouts, 2026-11-13), HTTP/1.1 fails less
  if [ "$(git -C "$name" rev-parse HEAD 2>/dev/null)" != "$sha" ]; then
    for i in 1 2 3 4; do
      git -c http.version=HTTP/1.1 -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 -C "$name" fetch -q --depth 1 origin "$sha" && break
      echo "  retry $name ($i)"; sleep $((i * 10))
    done
    git -C "$name" checkout -q --force "$sha" 2>/dev/null
  fi
  [ "$(git -C "$name" rev-parse HEAD 2>/dev/null)" = "$sha" ] || { fail "$name" "not at $sha"; continue; }
  [ -f "$name/requirements.txt" ] && { $PIP -r "$name/requirements.txt" || fail "$name" "pip install"; }
done <<< "$NODES"

echo "== 3. 软链公共权重"
ln_m(){ mkdir -p "$M/$2"; [ -e "$M/$2/$(basename $1)" ] || ln -s "$1" "$M/$2/"; [ -e "$M/$2/$(basename $1)" ] || fail "$(basename $1)" "public disk"; }
H3=$P/Comfy-Org/MiniMax-H3
ln_m $H3/diffusion_models/minimax_h3_ref2va_pruned_fp8_scaled.safetensors diffusion_models
ln_m $H3/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors text_encoders
ln_m $H3/vae/minimax_h3_video_vae_fp16.safetensors vae
ln_m $H3/vae/minimax_h3_audio_vae_fp32.safetensors vae
ln_m $P/lightx2v/Minimax-h3-Turbo/minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors loras
ln_m $P/akatz-ai/MiniMax-H3-Character-Swap-LoRA/h3_character_swap_v1.5.safetensors loras
ln_m $P/Comfy-Org/sam3.1/checkpoints/sam3.1_multiplex_fp16.safetensors checkpoints
ln_m $P/Comfy-Org/MoGe/geometry_estimation/moge_2_vitl_normal_fp16.safetensors geometry_estimation
ln_m $P/Comfy-Org/SDPose/diffusion_models/rt_detr_v4-x-hgnet_fp32.safetensors diffusion_models
ln_m /model/comfyui/models/background_removal/birefnet.safetensors background_removal

echo "== 4. 下载公共盘没有的（锁 revision，核 sha256）"
while IFS='|' read -r repo rev path dir sum; do
  [ -n "$repo" ] || continue
  out=$M/$dir/$(basename "$path"); mkdir -p "$M/$dir"
  # 核对过的文件旁边留一个 .sha256；文件被换过（大小或时间变了）就重新核
  if [ -s "$out.sha256" ] && [ "$out.sha256" -nt "$out" ] && [ "$(cat "$out.sha256")" = "$sum" ]; then continue; fi
  rm -f "$out.sha256"
  # 断了就留着半截文件，下次 -C - 接着下；下完整了哈希还不对才删
  if ! curl -sL --fail --retry 3 -C - -o "$out" "https://modelscope.cn/models/$repo/resolve/$rev/$path"; then
    [ "$(sha256sum "$out" 2>/dev/null | cut -d' ' -f1)" = "$sum" ] || { fail "$(basename "$path")" "download interrupted, rerun to resume"; continue; }
  fi
  got=$(sha256sum "$out" 2>/dev/null | cut -d' ' -f1)
  if [ "$got" = "$sum" ]; then echo "$got" > "$out.sha256"; echo "  ok $(basename "$path") $(du -h "$out" | cut -f1)"
  else rm -f "$out"; fail "$(basename "$path")" "sha256 $got"; fi
done <<< "$DOWNLOADS"

echo "== 5. headless Blender + ffmpeg（白模渲染、合成在实例上跑）"
# 不用 apt：基础镜像的 apt 处于 unmet dependencies 状态，--fix-broken 会装 NVIDIA 驱动用户态库，和宿主驱动冲突。
# Blender 用官方 tarball（X11/GL/EGL 库基础镜像已有）；ffmpeg 用 imageio-ffmpeg 自带的静态版；curl 来自 conda。
source /root/miniconda3/bin/activate >/dev/null 2>&1
BL_DIR=/opt/blender-$BL_VER
if [ ! -x $BL_DIR/blender ]; then
  curl -sL --retry 3 -C - -o /tmp/blender.tar.xz \
    "https://mirrors.aliyun.com/blender/release/Blender${BL_VER%.*}/blender-$BL_VER-linux-x64.tar.xz"
  mkdir -p $BL_DIR && tar -xJf /tmp/blender.tar.xz -C $BL_DIR --strip-components=1 && rm -f /tmp/blender.tar.xz
fi
ln -sf $BL_DIR/blender /usr/local/bin/blender
python -c "import imageio_ffmpeg" 2>/dev/null || pip install -q imageio-ffmpeg
FF=$(python -c "import imageio_ffmpeg as i;print(i.get_ffmpeg_exe())")
ln -sf "$FF" /usr/local/bin/ffmpeg
missing=$(ldd $BL_DIR/blender | grep 'not found')
[ -n "$missing" ] && echo "WARN blender missing libs: $missing"
blender --version | head -1 | grep -q "Blender $BL_VER" || fail blender "not $BL_VER"
ffmpeg -version | head -1
command -v ffmpeg >/dev/null || fail ffmpeg "missing"

echo "== 6. 重启 ComfyUI（127.0.0.1）"
pkill -f "[m]ain.py" || true; sleep 2
bash /start.d/comfyui.sh
for i in $(seq 1 60); do curl -s -m 2 http://127.0.0.1:8188/system_stats >/dev/null && { echo "comfyui up"; break; }; sleep 5; done
(ss -tlnp 2>/dev/null || netstat -tlnp) | grep 8188 || fail comfyui "not listening on 8188"

stamp
