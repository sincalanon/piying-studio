#!/usr/bin/env python3
"""Piying Motion command line: dance video -> mannequin -> fictional character on your own compshare GPU.

Every command prints one JSON object on stdout ({"ok": true, ...} or {"ok": false, "error": ...}).
Progress goes to stderr. Secrets are read from the environment and never printed.

Commands that cost money (`up`) refuse to run without --confirm-billing. Nothing here deletes an
instance or a disk.
"""
import argparse
import base64
import copy
import difflib
import fnmatch
import functools
import hashlib
import contextlib
import json
import math
import os
import re
import secrets
import shlex
import threading
import traceback
import shutil
import socket
import struct
import signal
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

# Programs this script starts that are Python too (the compshare CLI, pip in its venv) read and write UTF-8 even on
# Chinese Windows (GBK). This script itself is started with -X utf8 (the plugin's host and the skill's command line).
os.environ.setdefault('PYTHONUTF8', '1')


def no_console_windows(nt=os.name == 'nt', has_console=None, popen=None):
    """Windows, when this script has no console (the plugin starts billed runs and machine setup detached): every console
    program it starts (compshare.exe, pip, ffmpeg, ssh) would get a new console window of its own, and closing one
    killed the run halfway (seen 2026-11-13). Give them none. With a console (a terminal, the director's shell) they
    share it as before. Returns whether it changed anything."""
    if not nt:
        return False
    if has_console is None:
        try:
            import ctypes
            has_console = bool(ctypes.windll.kernel32.GetConsoleWindow())
        except Exception:   # noqa: BLE001
            return False
    if has_console:
        return False
    popen = popen or subprocess.Popen
    if getattr(popen.__init__, '_shisei_no_window', False):
        return True
    init = popen.__init__
    flag = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

    def __init__(self, *a, **k):
        cf = k.get('creationflags', 0) or 0
        if not cf & 0x00000010:   # CREATE_NEW_CONSOLE asked for on purpose: leave it
            k['creationflags'] = cf | flag
        init(self, *a, **k)
    __init__._shisei_no_window = True
    popen.__init__ = __init__
    return True


no_console_windows()
HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / 'assets'
HOME = Path(os.environ.get('SHISEI_HOME', '~/.dsh/shisei')).expanduser()
CONFIG = HOME / 'config.json'
COMPSHARE_CLI_VERSION = '0.5.6'
LOCAL_PORT = 18188
REMOTE_COMFY = '/root/ComfyUI'
H3_FRAMES = 107  # default dance clip; H3 frame grid is 17n+5; 107 frames = 4.46 s at 24 fps
FPS = 24
SWAP_MAX_SECONDS = 15   # BFSShotPlanner's max_s in swap_studio.json (one generation); longer clips need splitting
# A long clip (整段换) is cut into shots the planner renders one per prompt in its queue loop, each continuing from the
# end of the one before ('history'), stored on the machine, then joined. 328 frames (13.7 s) per shot at most: with
# the 22 history frames before it, about the 345 frames measured on the 4090 (26.5 min per shot).
LONG_SHOT_FRAMES = 328
LONG_MAX_SECONDS = 300

SETUP_BUDGET_DEFAULT = 5.0
DEFAULTS = {
    'instance_id': '',
    'ssh_key': '',
    'gpu': '4090_48G',
    'keep_alive_minutes': 30,   # while working the scheduled stop is renewed to at most this far ahead
    'idle_stop_minutes': 10,
    # client_id prefixes we accept in ComfyUI history; anything else is treated as an intrusion
    'allowed_clients': ['dsh-shisei-'],
    # the image model that pre-checks keyframes and takes (OpenAI-compatible); advice only, the user approves
    'vision_base_url': 'https://maas-api.antdigital.com/v1',
    'vision_model': 'ling-3.2-flash',
    'vision_key_env': 'ANTDIGI_API_KEY',
    # which image model answers inside dsh: auto = dsh's default model when it reads images, else the one above;
    # dsh / key force one (the plugin's bridge decides, lib/vision.js)
    'vision_source': 'auto',
    'setup_budget': SETUP_BUDGET_DEFAULT,   # yuan: the most a machine setup (machine-create) may cost
    # the page asks the registry (through the profile's pnpm) once a day whether a newer dsh-shisei exists; false: never
    'update_check': True,
    # local mode (local_mode.py): the user's own ComfyUI instead of a compshare machine; empty = off
    'comfy_url': '',            # e.g. http://127.0.0.1:8188
    'comfy_dir': '',            # the ComfyUI folder (input/ output/ models/) when it is on this computer
    'comfy_model_roots': [],    # more folders holding the model files, laid out like models/
}

# minutes per stage, measured on 4090 48G in the early experiments (boot is a rough estimate)
STAGE_MINUTES = {'boot': 5, 'mocap': 1, 'mannequin': 4, 'swap_per_seed': 5, 'finish': 2}

PROMPT_TEMPLATE = (
    'Replace the white mannequin in <Video 1> with the {subject} in <Picture 1>. '
    'Keep the face, eyes, hair and makeup exactly as in <Picture 1>; <Picture 2> is a close-up of the same {subject}\'s face. '
    '{details}'
    'The expression is relaxed and natural with eyes naturally open, an occasional soft smile. '
    'Follow the mannequin\'s exact pose, head direction, timing and movement. '
    'Preserve the light grey seamless studio background, soft lighting and camera movement of <Video 1>. '
    'Do not show the reference image background.'
)


class ShiseiError(Exception):
    kind = None   # set for a compshare refusal the page explains in its own words (cs_error_kind)


LOG_HOOK = None   # set by job-run to mirror progress into progress.json


def log(msg):
    print(f'[shisei {time.strftime("%H:%M:%S")}] {msg}', file=sys.stderr, flush=True)
    if LOG_HOOK:
        LOG_HOOK(msg)


# ---------------------------------------------------------------- config

# library_dir: a local clone of the Piying library, before the page read it online (lib/library.js)
RETIRED_KEYS = ('library_dir',)


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text()))
    for k in RETIRED_KEYS:   # left in older config files; nothing reads them
        cfg.pop(k, None)
    return cfg


def save_config(cfg):
    cfg = dict(cfg)
    if '_saved_instance' in cfg:   # the machine chosen for this process stays in memory (use_machine)
        cfg['instance_id'] = cfg['_saved_instance']
    cfg = {k: v for k, v in cfg.items() if not k.startswith('_')}
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    tmp.replace(CONFIG)


def client_id():
    path = HOME / 'client_id'
    if not path.exists():
        HOME.mkdir(parents=True, exist_ok=True)
        path.write_text(f'dsh-shisei-{uuid.uuid4().hex[:12]}')
    return path.read_text().strip()


# ---------------------------------------------------------------- pure helpers (unit tested)

def swap_frames(seconds):
    """Dance clip length in frames: the longest H3 grid length (17n+5) that fits in `seconds` at 24 fps."""
    s = float(seconds)
    if not 2 <= s <= SWAP_MAX_SECONDS + 0.5:
        raise ShiseiError(f'a dance clip is 2..{SWAP_MAX_SECONDS} seconds')
    n = (min(int(round(s * FPS)), SWAP_MAX_SECONDS * FPS) - 5) // 17
    return 17 * n + 5


def check_swap_frames(frames):
    f = int(frames)
    if (f - 5) % 17 or not 39 <= f <= SWAP_MAX_SECONDS * FPS:
        raise ShiseiError(f'{f} frames is not an H3 length (17n+5, 2..{SWAP_MAX_SECONDS} s)')
    return f


def long_frames(seconds):
    """A long clip (整段换) in frames at 24 fps: longer than one generation, at most LONG_MAX_SECONDS."""
    s = float(seconds)
    if not SWAP_MAX_SECONDS < s <= LONG_MAX_SECONDS:
        raise ShiseiError(f'a whole-dance clip is {SWAP_MAX_SECONDS}..{LONG_MAX_SECONDS} seconds; shorter ones are one clip')
    return int(round(s * FPS))


def is_long(frames):
    return int(frames) > SWAP_MAX_SECONDS * FPS


def check_clip_frames(frames):
    """A prepared dance clip: one H3 length (17n+5, up to 15 s), or a long clip that is cut into shots."""
    f = int(frames)
    if is_long(f):
        if f > LONG_MAX_SECONDS * FPS:
            raise ShiseiError(f'{f} frames is longer than {LONG_MAX_SECONDS} s')
        return f
    return check_swap_frames(f)


def long_shots(frames):
    """The shots of a long clip as [start, end) frames: as few as fit LONG_SHOT_FRAMES, equal lengths."""
    f = check_clip_frames(frames)
    k = -(-f // LONG_SHOT_FRAMES)
    b = [round(f * j / k) for j in range(k + 1)]
    return [(b[j], b[j + 1]) for j in range(k)]


def swap_take_minutes(frames, per_seed=None):
    """Minutes for one take: one generation grows about k ** 1.4 with the clip; a long clip is the sum of its shots."""
    per = STAGE_MINUTES['swap_per_seed'] if per_seed is None else per_seed
    if is_long(frames):
        return sum(per * ((b - a) / H3_FRAMES) ** 1.4 for a, b in long_shots(frames))
    return per * (check_swap_frames(frames) / H3_FRAMES) ** 1.4


def start_failure(text):
    """The readable part of a failed `compshare instance start` (its JSON is long and ends cut off)."""
    if 'out of resources' in text or '226604' in text:
        return 'this GPU type is out of stock in the zone'
    m = re.search(r'"message":\s*"([^"]{1,160})"', text.split('"failed"', 1)[-1])
    return m.group(1) if m else text[:200]


def stored_shot_minutes(frames, stored, per_seed=None):
    """Minutes of the first `stored` shots of a long take: already on the machine's disk, a resumed run skips them."""
    if not stored or not is_long(frames):
        return 0
    per = STAGE_MINUTES['swap_per_seed'] if per_seed is None else per_seed
    return sum(per * ((b - a) / H3_FRAMES) ** 1.4 for a, b in long_shots(frames)[:stored])


def estimate(seeds, price_per_hour, stages=STAGE_MINUTES, running=False, frames=H3_FRAMES, mocap=True, mannequin=True,
             stored=0):
    """Minutes and cost for a run. `running=True` skips the boot step (machine already up); mocap / mannequin False
    leave out a step whose result the job folder already has (a re-run for more takes). `stored`: shots of a long take
    already on the machine (a resumed 整段换), left out of the swap.
    Measured on 107-frame clips; mocap and mannequin grow linearly with the clip, the swap faster (one H3 generation
    attends over the whole clip): 345 frames took 26.5 min per take on the 4090 (2026-11-11), about k ** 1.4 × 5 min."""
    if seeds < 1:
        raise ShiseiError('seeds must be >= 1')
    k = check_clip_frames(frames) / H3_FRAMES
    minutes = (0 if running else stages['boot']) + stages['finish'] \
        + (stages['mocap'] * mocap + stages['mannequin'] * mannequin) * k + swap_take_minutes(frames, stages['swap_per_seed']) * seeds \
        - stored_shot_minutes(frames, stored, stages['swap_per_seed'])
    minutes = int(-(-round(minutes, 3) // 1))
    out = {'seeds': seeds, 'frames': int(frames), 'seconds': round(frames / FPS, 2), 'minutes': minutes,
           'price_per_hour': price_per_hour, 'cost': round(minutes / 60 * price_per_hour, 2),
           'note': 'estimate; real charges are on the compshare bill'}
    if is_long(frames):
        out['shots'] = len(long_shots(frames))
        if stored:
            out['stored'] = int(stored)
    return out


def build_prompt(subject, details=''):
    details = details.strip()
    if details and not details.endswith('.'):
        details += '.'
    return PROMPT_TEMPLATE.format(subject=subject.strip(), details=(details + ' ') if details else '')


def _nodes(api, class_type):
    return [n for n in api.values() if n.get('class_type') == class_type]


def _assert_filled(api):
    left = [k for k, n in api.items() if '__' in json.dumps(n.get('inputs', {}))
            and any(p in json.dumps(n['inputs']) for p in ('__VIDEO__', '__PREFIX__', '__REF__', '__REF2__', '__PROMPT__'))]
    if left:
        raise ShiseiError(f'unfilled placeholders in nodes {left}')
    return api


def patch_mocap(api, video, prefix, frames=H3_FRAMES):
    api = copy.deepcopy(api)
    loads, saves, slices = _nodes(api, 'LoadVideo'), _nodes(api, 'SaveGLB'), _nodes(api, 'Video Slice')
    if len(loads) != 1 or len(saves) != 1 or len(slices) != 1:
        raise ShiseiError('mocap workflow must have exactly one LoadVideo, Video Slice and SaveGLB')
    loads[0]['inputs']['file'] = video
    # the slice decides how many frames get captured (and so the mannequin's length): the whole prepared clip
    slices[0]['inputs'].update(start_time=0, duration=check_clip_frames(frames) / FPS)
    saves[0]['inputs']['filename_prefix'] = f'3d/{prefix}'
    return _assert_filled(api)


def patch_swap(api, video, ref, ref2, prompt, seed, prefix, character_strength=None, head_strength=None, frames=H3_FRAMES):
    api = copy.deepcopy(api)
    planners = _nodes(api, 'BFSShotPlanner')
    if len(planners) != 1:
        raise ShiseiError('swap workflow must have exactly one BFSShotPlanner')
    plan = json.loads(planners[0]['inputs']['plan'])
    plan['video'] = video
    plan['end_f'] = check_clip_frames(frames)
    plan['segs'][0].update(ref=ref, ref2=ref2, prompt=prompt)
    if is_long(frames):
        long_plan(plan, frames)
    planners[0]['inputs']['plan'] = json.dumps(plan, ensure_ascii=False)
    for n in _nodes(api, 'RandomNoise'):
        n['inputs']['noise_seed'] = int(seed)
    for ct, suffix in (('VHS_VideoCombine', ''), ('SaveVideo', '_comp')):
        for n in _nodes(api, ct):
            n['inputs']['filename_prefix'] = f'{prefix}{suffix}'
    for n in _nodes(api, 'LoraLoaderModelOnly'):
        name = n['inputs'].get('lora_name', '')
        if character_strength is not None and 'character_swap' in name:
            n['inputs']['strength_model'] = float(character_strength)
        if head_strength is not None and 'head_swap' in name:
            n['inputs']['strength_model'] = float(head_strength)
    return _assert_filled(api)


def long_plan(plan, frames):
    """A long clip in the planner's queue loop: the shots by hand (long_shots), one rendered per prompt and stored in
    ComfyUI's output folder; every shot after the first continues from the end of the one before ('history'); the
    prompt that stores the last shot joins them all. A stopped run picks up at the first shot not stored."""
    shots = long_shots(frames)
    seg = dict(plan['segs'][0], chain='history')
    plan.update(run='queue', mode='manual', bounds=[a for a, _ in shots[1:]], max_parts=0, max_total_s=0,
                start_f=0, end_f=shots[-1][1], shot_cache='output', segs=[dict(seg) for _ in shots])
    return plan


# Keeping the source video's background: the guide is the source clip itself (no mocap, no mannequin) and the planner's
# per-shot mode decides how much of the frame is regenerated. Flags as BFS's planner UI writes them (crop, inpaint, paste):
#   frame = whole frame regenerated, the prompt asks to keep the background (the early hd_orig / dual-character experiments)
#   paste = whole frame generated, then only the new person is pasted onto the original frame (pixel-exact background)
#   mask  = only the SAM mask of the people is regenerated, everything else stays as it was (pixel-exact background)
KEEP_BG_MODES = {'frame': (False, False), 'paste': (False, True), 'mask': (True, False)}   # mode: (inpaint, paste)


def keep_bg_prompt(people):
    """Prompt for a swap on the source clip. `people` is a list of 1 or 2 (where, description) pairs: where is how to find
    the person in the source ('the dancer', 'the dancer on the left'), description what the new character wears.
    Person i takes <Picture i>; with one person <Picture 2> is a close-up of the same face."""
    if not 1 <= len(people) <= 2:
        raise ShiseiError('keep-background swap takes 1 or 2 people')
    parts = []
    for i, (where, desc) in enumerate(people, 1):
        desc = (desc or '').strip().rstrip('.')
        parts.append(f'Replace {where} in <Video 1> with the character in <Picture {i}>'
                     + (f', who wears {desc}' if desc else '') + '.')
    if len(people) == 1:
        parts.append('<Picture 2> is a close-up of the same character\'s face.')
    else:
        parts.append('Each person keeps their own reference: never swap faces, hair or clothes between them, '
                     'and keep these assignments through crossings and occlusions.')
    parts.append('Keep the face, hair and clothing exactly as in the reference pictures. '
                 'Follow the exact pose, head direction, timing and movement of each person in <Video 1>. '
                 'Preserve the background, lighting, framing and camera of <Video 1>. Do not show the reference image background.')
    return ' '.join(parts)


def patch_swap_keep_bg(api, video, ref, ref2, prompt, seed, prefix, mode, frames=H3_FRAMES):
    """patch_swap on the source clip with a keep-background mode (KEEP_BG_MODES). paste and mask need the people's mask:
    SAM text 'person' (every match, up to the plan's mask_cfg.max_objects)."""
    if mode not in KEEP_BG_MODES:
        raise ShiseiError(f'keep-background mode is one of {", ".join(KEEP_BG_MODES)}')
    api = patch_swap(api, video, ref, ref2, prompt, seed, prefix, frames=frames)
    planner = _nodes(api, 'BFSShotPlanner')[0]
    plan = json.loads(planner['inputs']['plan'])
    inpaint, paste = KEEP_BG_MODES[mode]
    for seg in plan['segs']:   # one per shot of a long clip
        seg.update(crop=False, inpaint=inpaint, paste=paste, target='')
        seg['mask'] = {'text': 'person'} if (inpaint or paste) else {}
        seg['paste_text'] = 'person' if paste else ''
    planner['inputs']['plan'] = json.dumps(plan, ensure_ascii=False)
    return api


KEEP_BG_JOB_MODE = 'mask'   # the one keep-background mode the page uses: two people came out right in it (frame went black)
KEEP_BG_MAX_SIDE = 1376     # the planner fits the clip to about 1 MP anyway; a 4K source is not uploaded whole


def swap_people(params):
    """1 or 2: how many people a dance-swap job replaces. Two always keep the source background."""
    return 2 if int((params or {}).get('people') or 1) == 2 else 1


def swap_keep_bg(params):
    """True when the job swaps on the source clip itself (no mocap, no white model): two people, or one person with
    the background mode `keepbg`. Jobs from before these fields are studio jobs."""
    params = params or {}
    return swap_people(params) == 2 or params.get('mode') == 'keepbg'


def swap_take_people(job, seed=None):
    """(where, description) pairs for keep_bg_prompt. A take keeps the description(s) it was asked with: seedPrompts
    holds a string for one person, a [left, right] pair for two."""
    params = job.get('params') or {}
    asked = (job.get('seedPrompts') or {}).get(str(seed)) if seed is not None else None
    if swap_people(params) == 2:
        left, right = asked if isinstance(asked, list) and len(asked) == 2 else (job.get('prompt') or '', job.get('prompt2') or '')
        return [('the dancer on the left', left), ('the dancer on the right', right)]
    return [('the dancer', asked if isinstance(asked, str) else job.get('prompt') or '')]


def ffmpeg_keep_frame_args(src, dst, start, frames=H3_FRAMES):
    """Trim to `frames` at 24 fps, keep the whole frame (no 3:4 crop), shrink only when the long side is over
    KEEP_BG_MAX_SIDE; even sizes for x264."""
    m = KEEP_BG_MAX_SIDE
    scale = f"scale='min(iw,{m})':'min(ih,{m})':force_original_aspect_ratio=decrease:force_divisible_by=2"
    return ['ffmpeg', '-y', '-ss', f'{float(start):.3f}', '-i', str(src), '-vf', f'fps={FPS},{scale}',
            '-frames:v', str(check_clip_frames(frames)), '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '14', str(dst)]


def ffmpeg_trim_args(src, dst, start, frames=H3_FRAMES):
    """Trim to `frames` at 24 fps without cropping or scaling (the planner fits it to 1 MP itself)."""
    return ['ffmpeg', '-y', '-ss', f'{float(start):.3f}', '-i', str(src), '-vf', f'fps={FPS}',
            '-frames:v', str(check_clip_frames(frames)), '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '14', str(dst)]


def ssh_endpoint(host_info):
    for fwd in host_info.get('TcpForwards') or []:
        if str(fwd.get('InternalPort')) == '23':
            return fwd['ExternalHost'], int(fwd['ExternalPort'])
    raise ShiseiError('no ssh forward (InternalPort 23) in instance info')


def public_comfy_url(host_info):
    ingress = host_info.get('IngressHost')
    return f'https://8188-{ingress}' if ingress else None


def analyze_security(history, so_files, procs, extra_files, allowed_prefixes):
    """Return a list of findings; empty means clean."""
    findings = []
    for pid, entry in (history or {}).items():
        prompt = entry.get('prompt') or []
        extra = prompt[3] if len(prompt) > 3 and isinstance(prompt[3], dict) else {}
        cid = extra.get('client_id') or ''
        if not any(cid.startswith(p) for p in allowed_prefixes):
            findings.append({'kind': 'foreign_prompt', 'detail': f'prompt {pid[:8]} client_id={cid or "<none>"}'})
    for f in so_files:
        findings.append({'kind': 'shared_object_in_input', 'detail': f})
    for p in procs:
        findings.append({'kind': 'suspicious_process', 'detail': p})
    for f in extra_files:
        findings.append({'kind': 'suspicious_file', 'detail': f})
    return findings


def ffmpeg_prep_args(src, dst, start, crop_x=None, frames=H3_FRAMES):
    """Trim to `frames` (17n+5; 107 by default) at 24 fps and crop the largest 3:4 window (portrait or landscape source).
    The window is centred; crop_x (0..1) moves it horizontally when the source is wider than 3:4."""
    fx = 0.5 if crop_x is None else float(crop_x)
    crop = f"crop=w='min(iw,ih*3/4)':h='min(ih,iw*4/3)':x='(iw-ow)*{fx}':y='(ih-oh)*0.5'"
    return ['ffmpeg', '-y', '-ss', f'{float(start):.3f}', '-i', str(src),
            '-vf', f'fps={FPS},{crop},scale=864:1152',
            '-frames:v', str(check_clip_frames(frames)), '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '14', str(dst)]


COMPOSITE_SIZE = (600, 800)                              # 3:4 video window in chrome.png
COMPOSITE_SLOTS = [(20, 109), (660, 109), (1300, 109)]   # source, mannequin, character
PLAYHEAD = {'x0': 60, 'width': 1800, 'y': 956, 'h': 118}


def composite_args(chrome, source, mannequin, character, out, frames=H3_FRAMES, loops=None):
    """1920x1080 three-panel showcase: chrome frame + three 3:4 videos + a moving playhead.
    Short clips play twice by default; clips over 8 s once."""
    w, h = COMPOSITE_SIZE
    if loops is None:
        loops = 2 if int(frames) <= 8 * FPS else 1
    n, extra = int(frames), int(loops) - 1
    parts = []
    for i in range(3):
        parts.append(f'[{i + 1}]fps={FPS},trim=end_frame={n},setpts=PTS-STARTPTS,'
                     f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},loop={extra}:{n}:0[v{i}]')
    parts.append(f'color=0xe8e9ec:s=2x{PLAYHEAD["h"]}[ph]')
    (x0, y0), (x1, y1), (x2, y2) = COMPOSITE_SLOTS
    parts.append(f'[0][v0]overlay={x0}:{y0}[a];[a][v1]overlay={x1}:{y1}[b];[b][v2]overlay={x2}:{y2}[c]')
    parts.append(f"[c][ph]overlay=x='{PLAYHEAD['x0']}+{PLAYHEAD['width']}*mod(n\\,{n})/({n}-1)':y={PLAYHEAD['y']}[out]")
    return ['ffmpeg', '-v', 'error', '-y', '-loop', '1', '-i', str(chrome), '-i', str(source), '-i', str(mannequin),
            '-i', str(character), '-filter_complex', ';'.join(parts), '-map', '[out]',
            '-frames:v', str(n * (extra + 1)), '-r', str(FPS), '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p',
            '-metadata', 'comment=AI-generated (Piying)', str(out)]


DUO_PANEL = (950, 1080)   # each half of the 1920x1080 two-panel cut (10 px margins left and right)


def composite_two_args(source, character, out, frames=H3_FRAMES, loops=None):
    """1920x1080 two-panel cut for a keep-background swap: the source clip and the swapped one side by side, each fitted
    into its half on the dark mat, any aspect ratio. Same loops and AI metadata as the three-panel cut."""
    w, h = DUO_PANEL
    if loops is None:
        loops = 2 if int(frames) <= 8 * FPS else 1
    n, extra = int(frames), int(loops) - 1
    parts = [f'[{i}]fps={FPS},trim=end_frame={n},setpts=PTS-STARTPTS,scale={w - 20}:{h - 40}:force_original_aspect_ratio=decrease,'
             f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x14171c,setsar=1,loop={extra}:{n}:0[v{i}]' for i in range(2)]
    parts.append(f'[v0][v1]hstack=inputs=2,pad=1920:1080:(ow-iw)/2:0:color=0x14171c[out]')
    return ['ffmpeg', '-v', 'error', '-y', '-i', str(source), '-i', str(character), '-filter_complex', ';'.join(parts),
            '-map', '[out]', '-frames:v', str(n * (extra + 1)), '-r', str(FPS), '-c:v', 'libx264', '-crf', '16',
            '-pix_fmt', 'yuv420p', '-metadata', 'comment=AI-generated (Piying)', str(out)]


def job_composite_args(d, job, pick, frames):
    """The showcase cut of a dance-swap job folder: three panels with the white model, two without one."""
    d = Path(d)
    if swap_keep_bg(job.get('params')):
        return composite_two_args(d / 'dance.mp4', d / f'seed_{pick}.mp4', d / 'showcase.mp4', frames=frames)
    return composite_args(ASSETS / 'composite' / 'chrome.png', d / 'dance.mp4', d / 'mannequin.mp4',
                          d / f'seed_{pick}.mp4', d / 'showcase.mp4', frames=frames)


# ---------------------------------------------------------------- compshare

VENV_BIN = 'Scripts' if os.name == 'nt' else 'bin'   # where a venv keeps its programs (Windows: Scripts\\*.exe)
EXE = '.exe' if os.name == 'nt' else ''


def venv_alive(venv):
    """Does the venv's base Python still exist? One made by a Python since uninstalled (a 32-bit one replaced by a 64-bit
    one) keeps its compshare.exe, which then fails with "did not find executable": count it as not installed."""
    try:
        cfg = (Path(venv) / 'pyvenv.cfg').read_text(errors='replace')
    except OSError:
        return False
    m = re.search(r'^\s*home\s*=\s*(.+?)\s*$', cfg, re.M)
    if not m:
        return True
    home = Path(m.group(1))
    return any((home / n).exists() for n in ('python.exe', 'python3', 'python'))


def compshare_bin():
    venv = HOME / 'venv'
    own = venv / VENV_BIN / f'compshare{EXE}'
    for c in (os.environ.get('SHISEI_COMPSHARE'), own if venv_alive(venv) else None, shutil.which('compshare')):
        if c and Path(c).exists():
            return str(c)
    raise ShiseiError('compshare-cli not found; run: shisei.py install-cli')


def keys_in_env():
    return bool(os.environ.get('COMPSHARE_PUBLIC_KEY') and os.environ.get('COMPSHARE_PRIVATE_KEY'))


def via_bridge(argv):
    """Inside dsh the model shell has no *KEY* variables; the plugin's loopback bridge runs compshare for us."""
    url = os.environ.get('DSH_SHISEI_BRIDGE', '')
    req = urllib.request.Request(url + '/compshare', json.dumps({'args': argv}).encode(), {'Content-Type': 'application/json'})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=180))
    except urllib.error.HTTPError as e:
        raise ShiseiError(f'bridge refused: {e.read().decode()[:300]}')
    except urllib.error.URLError as e:
        raise ShiseiError(f'bridge not reachable ({e.reason}); is dsh still running?')
    return r.get('stdout', ''), r.get('stderr', '')


CS_TRANSPORT_RETRIES = (5, 15, 30)   # seconds before each retry of a call that never reached compshare


def cs(*args, sensitive=False):
    """compshare-cli call. A transport error (TLS reset, timeout: the request never got an answer) is retried a few
    times, so one dropped connection does not fail a run while the machine is up and billing."""
    for wait in CS_TRANSPORT_RETRIES + (None,):
        try:
            return _cs_once(*args, sensitive=sensitive)
        except ShiseiError as e:
            if wait is None or not is_transport_error(str(e)):
                raise
            log(f'compshare {" ".join(args[:2])}: connection failed, retrying in {wait} s')
            time.sleep(wait)


def is_transport_error(text):
    return '"transport_error"' in text


def cs_error_kind(err):
    """What a compshare-cli error means for the user: 'auth' (the platform refused the key pair's signature: swapped,
    cut short or deleted keys), 'transport' (the request never got an answer) or None (anything else, shown as is).
    Only codes seen from the real platform count: ret_code 171 / Signature VerifyAC Error."""
    if not isinstance(err, dict):
        return 'transport' if is_transport_error(json.dumps(err)) else None
    if err.get('code') == 'transport_error':
        return 'transport'
    details = err.get('details') if isinstance(err.get('details'), dict) else {}
    if details.get('ret_code') == 171 or 'Signature' in str(err.get('message') or ''):
        return 'auth'
    return None


def _cs_once(*args, sensitive=False):
    argv = ['--json'] + (['--show-sensitive'] if sensitive else []) + list(args)
    if keys_in_env():
        r = subprocess.run([compshare_bin()] + argv, capture_output=True, text=True)
        stdout, stderr = r.stdout, r.stderr
    elif os.environ.get('DSH_SHISEI_BRIDGE'):
        stdout, stderr = via_bridge(argv)
    else:
        raise ShiseiError('compshare keys not available: set them in the dsh plugin page (Piying), '
                          'or export COMPSHARE_PUBLIC_KEY / COMPSHARE_PRIVATE_KEY')
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        raise ShiseiError(f'compshare {args[0]} {args[1] if len(args) > 1 else ""}: {stderr.strip()[:300] or stdout[:300]}')
    if not out.get('ok'):
        e = ShiseiError(f'compshare {" ".join(args[:2])}: {json.dumps(out.get("error"), ensure_ascii=False)[:400]}')
        e.kind = cs_error_kind(out.get('error'))
        raise e
    return out.get('data')


def need_instance(cfg):
    if not cfg.get('instance_id'):
        raise ShiseiError('instance_id not configured; run: shisei.py config set instance_id <cpod-...>')
    return cfg['instance_id']


def instance_info(cfg):
    data = cs('instance', 'show', need_instance(cfg), sensitive=True)
    return data['UHostSet'][0]


def instance_state(cfg):
    items = cs('instance', 'list')['items']
    for it in items:
        if it.get('UHostId') == cfg['instance_id']:
            return it.get('State')
    raise ShiseiError(f'instance {cfg["instance_id"]} not found in this account')


def instance_fit(info):
    """Whether an instance can run Piying (H3 needs a 48 GB card), from its GpuType such as '4090_48G'.
    Returns (suitable, why) — why is a short Chinese reason shown on the settings page."""
    gpu = str((info or {}).get('GpuType') or '')
    if not gpu:
        return False, '看不到显卡型号'
    if gpu_profile(gpu)['status'] == 'verified':
        return True, ''
    m = re.search(r'(\d+)\s*G', gpu, re.I)
    vram = GPU_VRAM_GB.get(gpu) or (int(m.group(1)) if m else None)
    if vram is not None and vram < 48:
        return False, '显存不够：H3 换人要 48G'
    return False, '这种显卡皮影还没验证过'


def pick_instance(rows, current):
    """The instance to use when none is set: the only suitable one, or None when there are zero or several."""
    if current:
        return None
    fit = [r for r in rows if r.get('suitable')]
    return fit[0]['id'] if len(fit) == 1 else None


SSH_KEY_CANDIDATES = ('~/.ssh/id_ed25519', '~/.ssh/id_rsa', '~/.ssh/id_ecdsa')
# made by the plugin (the page's 生成 button) only when none of the above exists; ~/.ssh is never written
def shisei_key():
    return HOME / 'ssh' / 'id_ed25519'


def resolve_ssh_key(cfg):
    """The private key ssh uses: config ssh_key when set, else the first default key that exists, else the plugin's
    own key (None when none)."""
    if cfg.get('ssh_key'):
        return str(Path(os.path.expanduser(cfg['ssh_key'])))   # Path: one kind of separator on Windows too
    for c in SSH_KEY_CANDIDATES + (str(shisei_key()),):
        if Path(os.path.expanduser(c)).exists():
            return str(Path(os.path.expanduser(c)))
    return None


def key_to_make(cfg):
    """Where the plugin would make its own key pair: shisei_key() when no key is set or found, else None (a user who has
    a key keeps it: machines already carry its public half)."""
    return None if resolve_ssh_key(cfg) else shisei_key()


SSH_JS = HERE.parent.parent.parent / 'lib' / 'ssh.cjs'   # ssh / scp / keygen on Node, ssh2 bundled (src/ssh/cli.js)
SSH_MODE = {'ssh': 'ssh', 'scp': 'scp', 'ssh-keygen': 'keygen'}


def node_bin(env=None, which=shutil.which, exists=os.path.exists, nt=os.name == 'nt'):
    """The Node to run lib/ssh.cjs on: the one dsh runs on (SHISEI_NODE, set by the plugin), the one dsh Desktop
    carries, or one on PATH. None when there is none (shisei.py run by hand on a computer without Node)."""
    env = os.environ if env is None else env
    home = Path(env.get('DSH_HOME') or Path.home() / '.dsh')
    roots = [r for r in (env.get('DSH_PRIMARY_RUNTIME'), home / 'dsh-runtimes' / 'dsh-primary-runtime') if r]
    # the runtime's Node is dependencies/node/bin/node(.exe) on both systems (dsh Desktop 0.2.2, seen 2026-11-13)
    cands = [env.get('SHISEI_NODE')] + [str(Path(r) / 'dependencies' / 'node' / 'bin' / ('node.exe' if nt else 'node'))
                                        for r in roots]
    for c in cands:
        if c and exists(c):
            return c
    return which('node')


def key_encrypted(path):
    """Whether a private key file has a passphrase (an OpenSSH key whose cipher is not 'none', or an encrypted PEM).
    The plugin's own ssh cannot use one; OpenSSH can, through the system keychain or ssh-agent. False when unreadable."""
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return False
    if 'ENCRYPTED' in text.split('\n\n', 1)[0] and 'BEGIN' in text:   # PEM: Proc-Type: 4,ENCRYPTED / ENCRYPTED PRIVATE KEY
        return True
    m = re.search(r'-----BEGIN OPENSSH PRIVATE KEY-----(.*?)-----END OPENSSH PRIVATE KEY-----', text, re.S)
    if not m:
        return False
    try:
        raw = base64.b64decode(''.join(m.group(1).split()))
    except ValueError:
        return False
    magic = b'openssh-key-v1\x00'
    if not raw.startswith(magic) or len(raw) < len(magic) + 4:
        return False
    n = int.from_bytes(raw[len(magic):len(magic) + 4], 'big')
    return raw[len(magic) + 4:len(magic) + 4 + n] != b'none'


def ssh_cmd(name, env=None, node=None, exists=os.path.exists, key=None):
    """How to run ssh / scp / ssh-keygen: the plugin's own (Node + lib/ssh.cjs: same arguments, no OpenSSH needed)
    when there is a Node, else this computer's OpenSSH. SHISEI_SSH=system picks OpenSSH, and so does a `key` with a
    passphrase: only OpenSSH can unlock it (keychain / ssh-agent). A Mac whose key has a passphrase never connected with
    the plugin's ssh while the machine billed (seen 2026-11-13)."""
    env = os.environ if env is None else env
    if key and name != 'ssh-keygen' and key_encrypted(key):
        try:
            return [ssh_tool(name)]
        except ShiseiError:
            raise ShiseiError(f'the ssh key {Path(key).name} has a passphrase, which the plugin\'s own ssh cannot use, and this '
                              'computer has no OpenSSH: pick a key without a passphrase in Settings, or let the Piying page '
                              'make one (开始 › GPU 机器)')
    if env.get('SHISEI_SSH') != 'system' and exists(str(SSH_JS)):
        node = node or node_bin(env)
        if node:
            return [node, str(SSH_JS), SSH_MODE[name]]
    return [ssh_tool(name)]


SSH_MISSING = ('{name} not found: this computer has no OpenSSH client. Windows: Settings > System > Optional features > '
               'add "OpenSSH Client" (or in an administrator PowerShell: '
               'Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0), then restart dsh')


def ssh_tool(name, env=None, which=shutil.which, exists=os.path.exists, nt=os.name == 'nt'):
    """Where ssh / scp / ssh-keygen is. Off PATH on Windows it is still often there: Windows' own OpenSSH (a 32-bit
    Python sees System32 through Sysnative only), or the one Git for Windows brings. Raises when there is none."""
    hit = which(name)
    if hit:
        return hit
    if nt:
        env = os.environ if env is None else env
        root = env.get('SystemRoot') or env.get('SYSTEMROOT') or r'C:\Windows'
        dirs = [rf'{root}\Sysnative\OpenSSH', rf'{root}\System32\OpenSSH']
        for pf in (env.get('ProgramFiles'), env.get('ProgramW6432'), env.get('ProgramFiles(x86)')):
            if pf:
                dirs += [rf'{pf}\OpenSSH', rf'{pf}\Git\usr\bin']
        for d in dirs:
            c = rf'{d}\{name}.exe'
            if exists(c):
                return c
    raise ShiseiError(SSH_MISSING.format(name=name))


def cmd_ssh_keygen(args, cfg):
    """Make the plugin's own ssh key pair (no passphrase, only for the user's GPU machines) when this computer has none.
    Never touches ~/.ssh and never replaces a key. The user clicks for it on the page; not from the director shell."""
    if in_agent_shell():
        raise ShiseiError('the ssh key is made by the user on the 皮影 page (开始 › GPU 机器), not from the director shell')
    path = key_to_make(cfg)
    if not path:
        key = resolve_ssh_key(cfg)
        return {'made': False, 'ssh_key': Path(key).name, 'public': bool(ssh_pubkey_path(cfg))}
    make_own_key(path)
    return {'made': True, 'ssh_key': path.name, 'public': True}


def keep_awake(nt=os.name == 'nt', platform=sys.platform, popen=subprocess.Popen, which=shutil.which):
    """Keep this computer from idle-sleeping while this process runs (a billed run: sleep would drop the ssh connection
    while the machine keeps billing). macOS: caffeinate tied to this pid; Windows: the execution state of this thread,
    released when the process ends. Never changes system settings; a closed lid or an empty battery still sleeps.
    Returns what it did, for the tests."""
    try:
        if platform == 'darwin' and which('caffeinate'):
            popen(['caffeinate', '-i', '-w', str(os.getpid())], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL)
            return 'caffeinate'
        if nt:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            return 'execution-state'
    except Exception:   # noqa: BLE001 -- best effort: a run never fails because the computer may sleep
        return None
    return None


def make_own_key(path):
    """Write the plugin's own ed25519 pair at `path` (+ .pub), no passphrase."""
    tool = ssh_cmd('ssh-keygen')
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        os.chmod(path.parent, 0o700)
    r = subprocess.run(tool + ['-q', '-t', 'ed25519', '-N', '', '-C', 'shisei', '-f', str(path)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    if r.returncode != 0 or not path.is_file():
        raise ShiseiError(f'ssh-keygen failed: {(r.stderr or r.stdout).strip()[:300]}')
    if os.name != 'nt':
        os.chmod(path, 0o600)


def cmd_instances(args, cfg):
    """The instances in this compshare account, with which can run Piying. Read-only: never starts or stops anything.
    --pick: when no instance is set and exactly one is suitable, remember it (config instance_id)."""
    rows = []
    making = setup_making()   # a machine 开始 is still setting up is not offered (nor picked) until it is ready
    for it in cs('instance', 'list')['items']:
        iid = it.get('UHostId')
        if not iid:
            continue
        try:
            info = cs('instance', 'show', iid, sensitive=True)['UHostSet'][0]
        except (ShiseiError, KeyError, IndexError):
            info = {}
        ok, why = instance_fit(info)
        if ok and making and iid == making:
            ok, why = False, '还在「开始」里准备，好了会自动选上'
        rows.append({'id': iid, 'name': it.get('Name') or info.get('Name'), 'state': it.get('State') or info.get('State'),
                     'gpu': info.get('GpuType'), 'zone': info.get('Zone'), 'price': info.get('InstancePrice'),
                     'image': info.get('CompShareImageName'), 'suitable': ok, 'why': why})
    picked = pick_instance(rows, cfg.get('instance_id')) if args.pick else None
    if picked:
        cfg['instance_id'] = picked
        save_config(cfg)
    known = {r['id']: r['suitable'] for r in rows}
    if getattr(args, 'register', None) or getattr(args, 'unregister', None):
        cfg = register_machine(cfg, known, add=getattr(args, 'register', None), remove=getattr(args, 'unregister', None))
    current = cfg.get('instance_id') or None
    reg = machine_ids(cfg)
    ports = machine_ports(reg, cfg.get('ports'))
    for r in rows:
        r['current'] = r['id'] == current
        r['registered'] = r['id'] in reg
        r['port'] = ports.get(r['id'])
        lease = lease_read(r['id']) if r['registered'] else None
        r['lease'] = {k: lease.get(k) for k in ('holder', 'state', 'until')} if lease else None
    return {'instances': rows, 'picked': picked, 'current': current, 'machines': reg}


def register_machine(cfg, known, add=None, remove=None):
    """Add or remove an instance from the machines Piying may use. Only the registry changes, never the instance."""
    disk = load_config()
    machines = [m for m in disk.get('machines') or [] if m]
    if add:
        if add not in known:
            raise ShiseiError(f'{add} is not an instance of this compshare account')
        if isinstance(known, dict) and not known[add]:
            raise ShiseiError(f'{add} cannot run Piying (H3 needs a 48 GB card); not registered')
        if add != disk.get('instance_id') and add not in machines:
            machines.append(add)
        if not disk.get('instance_id'):
            disk['instance_id'] = add
            machines = [m for m in machines if m != add]
    if remove:
        if lease_held(lease_read(remove)):
            raise MachineBusy(busy_message(remove, lease_read(remove)) + ' Unregister it once that run is over.')
        machines = [m for m in machines if m != remove]
        if disk.get('instance_id') == remove:
            disk['instance_id'] = machines.pop(0) if machines else ''
    disk['machines'] = machines
    save_config(disk)
    return dict(disk)


def wait_state(cfg, want, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = instance_state(cfg)
        if s == want:
            return s
        log(f'state {s}, waiting for {want}')
        time.sleep(10)
    raise ShiseiError(f'instance did not reach {want} in {timeout}s')


# ---------------------------------------------------------------- machines and leases
#
# Several compshare instances can be registered (config `instance_id` is the preferred one, `machines` the others).
# One process drives one machine: it is chosen once at the start of a command, then cfg['instance_id'] (in memory
# only, never saved) and LOCAL_PORT point at it. Each machine has its own local port, so tunnels never collide.
#
# A lease (~/.dsh/shisei/leases/<instance>.json) says who is using a machine: a holder (the project id, or "cli"),
# and `until`, the same moment as the platform's scheduled stop. Every renewal of the scheduled stop renews the
# lease, so a holder that died lets go by itself when its machine would have stopped anyway. A finished run keeps
# the lease as `idle` (others may take it; the same project gets it back first).

LEASE_PREBOOT_MINUTES = 90   # held while waiting for stock and booting, before the first scheduled stop is set
KEEPER_EVERY = 300           # seconds between renewals of the scheduled stop while a command works
HOLDER = None                # this process's lease holder, set by main()
KEEPER = None                # the renewal thread of this process, when it holds a lease
CMD = None                   # the command this process runs (set by main(); written into the lease it takes)
STOP_CHECK = None            # a run's check_stop while it waits for stock: raises StopRequested when the user stopped
LEASE_REFUSED = False        # this process asked for the machine's lease and did not get it (someone else holds it, or
                             # the lease could not be written): it never held the machine
UP_STARTED = False           # this process's `up` got the machine running at least once (it billed)


SOFT_HOLDERS = ('cli', 'page')   # a machine turned on by hand: a project's run may take it over


class MachineBusy(ShiseiError):
    pass


def lease_blocks(lease, holder, now=None):
    """Whether `lease` keeps `holder` off the machine: someone else holds it, unless that is a manual hold (cli, page)
    and the newcomer is a project run."""
    if not lease_held(lease, now) or lease.get('holder') == holder:
        return False
    return not (lease.get('holder') in SOFT_HOLDERS and holder not in SOFT_HOLDERS)


def keep_minutes(cfg):
    """While working, the scheduled stop is always at most this far ahead (renewed every few minutes)."""
    return int(cfg.get('keep_alive_minutes') or 30)


def working_stop(cfg):
    return f'{keep_minutes(cfg)}m'


def stop_minutes(at):
    """'30m' -> 30, '3h' -> 180 (the --at forms this file uses)."""
    m = re.fullmatch(r'(\d+)([mh])', str(at))
    if not m:
        raise ShiseiError(f'unknown stop time {at!r}')
    return int(m.group(1)) * (60 if m.group(2) == 'h' else 1)


def machine_ids(cfg):
    """Registered machines, the preferred one (config instance_id as saved) first."""
    first = cfg.get('_saved_instance', cfg.get('instance_id')) or ''
    out = [first] if first else []
    for m in cfg.get('machines') or []:
        if m and m not in out:
            out.append(m)
    return out


def machine_ports(ids, saved=None, base=18188):
    """A stable local port per machine: the ones already given keep theirs, new ones get the lowest free port."""
    ports = {k: int(v) for k, v in (saved or {}).items() if k in ids}
    for i in ids:
        if i not in ports:
            p = base
            while p in ports.values():
                p += 1
            ports[i] = p
    return ports


def machine_port(cfg, iid, save=True):
    ids = machine_ids(cfg)
    if iid not in ids:
        ids = ids + [iid]
    ports = machine_ports(ids, cfg.get('ports'))
    if save and ports != {k: int(v) for k, v in (cfg.get('ports') or {}).items()}:
        disk = load_config()            # saved without the in-memory machine choice
        disk['ports'] = ports
        save_config(disk)
        cfg['ports'] = ports
    return ports[iid]


def comfy_base():
    """Where this process talks to ComfyUI: the ssh tunnel's local port, or the user's own ComfyUI in local mode
    (local_mode.py replaces this function)."""
    return f'http://127.0.0.1:{LOCAL_PORT}'


def use_machine(cfg, iid, save=True):
    """Point this process at one machine: cfg['instance_id'] in memory and the local port of its tunnel."""
    global LOCAL_PORT, CURRENT_MACHINE
    cfg.setdefault('_saved_instance', cfg.get('instance_id') or '')
    cfg['instance_id'] = iid
    CURRENT_MACHINE = iid
    LOCAL_PORT = machine_port(cfg, iid, save)
    return iid


CURRENT_MACHINE = None   # the machine this process last pointed at (use_machine); stamped on the project's ledger lines


def last_machine(holder):
    """The machine this project's last GPU run used (its ledger's last line that names one), or None. A run with
    「拍完立即停机」 leaves no lease behind, so this is how the next quote defaults to the same machine."""
    if not holder or not re.fullmatch(r'[A-Za-z0-9_-]+', str(holder)):
        return None
    try:
        lines = json.loads((HOME / 'jobs' / holder / 'ledger.json').read_text())
    except (OSError, ValueError):
        return None
    for e in reversed(lines if isinstance(lines, list) else []):
        if isinstance(e, dict) and e.get('instance'):
            return e['instance']
    return None


def lease_path(iid):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', iid or ''):
        raise ShiseiError(f'bad instance id {iid!r}')
    return HOME / 'leases' / f'{iid}.json'


def lease_read(iid):
    try:
        return json.loads(lease_path(iid).read_text())
    except (OSError, ValueError):
        return None


def lease_held(lease, now=None):
    """Someone is using the machine: an active lease that has not run out."""
    now = time.time() if now is None else now
    return bool(lease) and lease.get('state') == 'active' and float(lease.get('until') or 0) > now


def _LeaseLock():
    """A lock on leases/.lock around every read-check-write, so two processes never both take one machine (file_lock:
    flock, or msvcrt on Windows, which has no fcntl)."""
    (HOME / 'leases').mkdir(parents=True, exist_ok=True)
    return file_lock(HOME / 'leases' / '.lock')


def _lease_write(iid, lease):
    p = lease_path(iid)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(lease, ensure_ascii=False, indent=1))
    tmp.replace(p)


def lease_take(iid, holder, port, minutes, note=''):
    """Take the machine for `holder` for `minutes`; refused (MachineBusy) while someone else holds it."""
    now = time.time()
    with _LeaseLock():
        cur = lease_read(iid)
        if lease_blocks(cur, holder, now):
            raise MachineBusy(busy_message(iid, cur, now))
        same = (cur or {}).get('holder') == holder and lease_held(cur, now)
        if same and holder not in SOFT_HOLDERS and cur.get('pid') != os.getpid() and pid_alive(cur.get('pid')):
            raise MachineBusy(other_run_message(iid, cur))
        lease = {'instance': iid, 'port': port, 'holder': holder, 'state': 'active', 'note': note,
                 'since': cur['since'] if same else now, 'started': bool(same and cur.get('started')),
                 'until': max(now + minutes * 60, float(cur['until']) if same else 0), 'pid': os.getpid(), 'cmd': CMD}
        _lease_write(iid, lease)
    handed_over(cur, holder, now)
    return lease


def handed_over(cur, holder, now):
    """Another project takes a machine the last one left idling: that project's idle stop ended now, so its ledger line
    is trimmed to the minutes actually idle (真机 2026-11-12: 画皮 kept a full ¥0.63 while 白骨精 was already shooting)."""
    prev = (cur or {}).get('holder')
    if not prev or prev == holder or (cur or {}).get('state') != 'idle' or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', prev):
        return None
    d = HOME / 'jobs' / prev
    return settle_idle(d, now) if (d / 'ledger.json').exists() else None


def lease_renew(iid, holder, minutes):
    """The scheduled stop moved: move the lease with it (only our own lease)."""
    with _LeaseLock():
        cur = lease_read(iid)
        if not cur or cur.get('holder') != holder:
            return None
        cur.update(until=time.time() + minutes * 60, state='active', started=True)
        _lease_write(iid, cur)
        return cur


def lease_release(iid, holder, idle_minutes):
    """A run finished and armed the idle stop: the machine stays up for `idle_minutes`, free for anyone."""
    with _LeaseLock():
        cur = lease_read(iid)
        if not cur or cur.get('holder') != holder:
            return None
        cur.update(state='idle', until=time.time() + idle_minutes * 60)
        _lease_write(iid, cur)
        return cur


def lease_may_stop(iid, holder, force=False):
    """Refuse (MachineBusy) to stop a machine someone else holds, unless forced (the page asked first)."""
    cur = lease_read(iid)
    if lease_held(cur) and cur.get('holder') != holder and not force:
        raise MachineBusy(busy_message(iid, cur) + ' Stopping it would cut that run off; the user can do it on '
                          'the Piying page (machines), which asks first.')
    return cur


def lease_drop(iid, holder, force=False):
    """The machine stopped: remove the lease. Someone else's live lease is only dropped with force (the page asks)."""
    with _LeaseLock():
        cur = lease_may_stop(iid, holder, force)
        lease_path(iid).unlink(missing_ok=True)
        return cur


def lease_release_dead(iid, pid, idle_minutes):
    """The process that took this lease is gone (killed, dsh closed): free the machine until its idle stop. Only while
    the lease is still that process's, so a run that took it meanwhile keeps it."""
    with _LeaseLock():
        cur = lease_read(iid)
        if not cur or cur.get('pid') != pid or pid_alive(pid):
            return None
        cur.update(state='idle', until=time.time() + idle_minutes * 60, broken=True)   # shown as 运行中断了 until it stops
        _lease_write(iid, cur)
        return cur


def other_run_message(iid, lease):
    what = {'drama-trial': 'a trial', 'drama-run': 'a run', 'job-run': 'a run', 'up': 'up'}.get(lease.get('cmd'), 'a run')
    since = time.strftime('%H:%M', time.localtime(float(lease.get('since') or time.time())))
    return (f'this project already has {what} on machine {iid} (started {since}, still going). '
            'One run per project at a time: wait for it to end, or stop it on the Piying page.')


def own_run_elsewhere(holder, ids, now=None):
    """A live lease of `holder` taken by another process that is still alive: (iid, lease), else None. A project runs
    one thing at a time, so a second run neither overwrites the first one's progress nor stops its machine."""
    now = time.time() if now is None else now
    if holder in SOFT_HOLDERS:   # manual work (cli, page) is not a project's run
        return None
    for iid in ids:
        cur = lease_read(iid)
        if lease_held(cur, now) and cur.get('holder') == holder and cur.get('pid') != os.getpid() \
                and pid_alive(cur.get('pid')):
            return iid, cur
    return None


def busy_message(iid, lease, now=None):
    now = time.time() if now is None else now
    left = max(0, int((float(lease.get('until') or 0) - now) // 60))
    return f'machine {iid} is in use by {lease.get("holder")} (lease runs {left} more min unless renewed).'


def choose_machine(rows, leases, holder, prefer=None, now=None):
    """Which registered machine a run gets. rows: [{'id', 'state'}] in registry order; leases: {id: lease or None}.
    1. one this holder already holds; 2. `prefer` (the project's last machine) when free; 3. a running free one
    (idle leases of others last); 4. a stopped free one. None free: MachineBusy naming who uses them."""
    now = time.time() if now is None else now
    held = lambda r: lease_blocks(leases.get(r['id']), holder, now)
    for r in rows:
        if lease_held(leases.get(r['id']), now) and leases[r['id']].get('holder') == holder:
            return r['id']
    free = [r for r in rows if not held(r) and r.get('state') not in ('Deleting', 'Deleted')]
    if not free:
        raise MachineBusy('every machine is in use: ' + '; '.join(busy_message(r['id'], leases[r['id']], now) for r in rows if leases.get(r['id']))
                          if rows else 'no machine registered')
    if prefer and any(r['id'] == prefer for r in free):
        return prefer
    for r in free:   # the machine this holder finished on a moment ago is still warm
        lease = leases.get(r['id']) or {}
        if lease.get('holder') == holder and lease.get('state') == 'idle' and r.get('state') == 'Running':
            return r['id']

    def rank(r):
        lease = leases.get(r['id']) or {}
        mine_idle = lease.get('state') == 'idle' and float(lease.get('until') or 0) > now
        others_idle = mine_idle and lease.get('holder') != holder
        return (r.get('state') != 'Running', others_idle)
    return sorted(free, key=rank)[0]['id']


def pick_machine(cfg, holder, explicit=None, prefer=None):
    """The machine a run of `holder` would get (no lease taken yet), and point this process at it."""
    ids = machine_ids(cfg)
    if explicit:
        iid = explicit
    elif len(ids) <= 1:
        iid = need_instance(cfg) if not ids else ids[0]
    else:
        states = {it.get('UHostId'): it.get('State') for it in cs('instance', 'list')['items']}
        rows = [{'id': i, 'state': states.get(i, 'Unknown')} for i in ids if i in states]
        iid = choose_machine(rows, {i: lease_read(i) for i in ids}, holder, prefer or last_machine(holder))
    return use_machine(cfg, iid)


def claim_machine(cfg, holder, explicit=None, prefer=None, note=''):
    """Choose a machine for `holder`, take its lease and point this process at it."""
    iid = pick_machine(cfg, holder, explicit, prefer)
    lease_take(iid, holder, LOCAL_PORT, LEASE_PREBOOT_MINUTES, note)
    return iid


def eta_minutes(progress, now=None):
    """How long a run probably still needs, from its own progress record: the time per finished piece so far × the
    pieces left (a drama run's items, a swap run's takes). None when nothing is finished yet or the record says
    nothing; a lease's `until` is the renewed scheduled stop, never an estimate of when the run ends."""
    now = time.time() if now is None else now
    if not isinstance(progress, dict) or progress.get('state') != 'running':
        return None
    pieces = progress.get('items') or progress.get('seeds') or {}
    since = progress.get('started_at') or (progress.get('billing') or {}).get('since') \
        or ((progress.get('stages') or {}).get('boot') or {}).get('started_at')
    done = sum(1 for v in pieces.values() if v == 'done')
    left = sum(1 for v in pieces.values() if v in ('pending', 'running'))
    if not done or not since or now <= float(since):
        return None
    return max(1, int(math.ceil((now - float(since)) / done * left / 60))) if left else 1


def machine_choice(rows, leases, holder, explicit=None, now=None, prefer=None):
    """Which registered machine a run of `holder` would get right now (pure; the quote and the run use this same
    rule). rows: [{'id', 'state'}] in registry order. {'pick': id or None, 'free': {id: bool}}."""
    now = time.time() if now is None else now
    free = {r['id']: not lease_blocks(leases.get(r['id']), holder, now) and r.get('state') not in ('Deleting', 'Deleted')
            for r in rows}
    if explicit:
        return {'pick': explicit if free.get(explicit) else None, 'free': free}
    try:
        return {'pick': choose_machine(rows, leases, holder, prefer, now=now), 'free': free}
    except MachineBusy:
        return {'pick': None, 'free': free}


def machine_rows(cfg):
    """The registered machines with their platform state, in registry order (one instance list, costs nothing)."""
    items = {it.get('UHostId'): it for it in cs('instance', 'list')['items']}
    return [{'id': i, 'name': items[i].get('Name'), 'state': items[i].get('State'), 'gpu': items[i].get('GpuType'),
             'zone': items[i].get('Zone'), 'price_per_hour': listed_price(i, items[i])}
            for i in machine_ids(cfg) if i in items]


def listed_price(iid, item):
    """The hourly price of a listed instance: compshare's instance list leaves InstancePrice out (真机 2026-11-12: the
    queue showed ¥…/hour), so ask instance show for it; None when the platform gives none."""
    if item.get('InstancePrice') is not None:
        return item.get('InstancePrice')
    try:
        return (cs('instance', 'show', iid)['UHostSet'][0] or {}).get('InstancePrice')
    except (ShiseiError, KeyError, IndexError, TypeError):
        return None


def holder_view(lease, now=None):
    """Who holds a machine, for the page: the holder, its project title and how long its run probably still needs
    (from that project's own progress; None when it is not a project of this home or nothing is finished yet)."""
    holder = (lease or {}).get('holder')
    out = {'holder': holder, 'title': None, 'eta_min': None, 'cmd': (lease or {}).get('cmd')}
    if holder and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', holder):
        d = HOME / 'jobs' / holder
        try:
            job = json.loads((d / 'job.json').read_text())
            # a recast project has no title: the page names it by the start of its clothes text, so does this
            out['title'] = job.get('title') or (str(job.get('prompt') or '').strip()[:24] or None)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            out['eta_min'] = eta_minutes(json.loads((d / 'progress.json').read_text()), now)
        except (OSError, ValueError):
            pass
    return out


def machine_view(cfg, holder, explicit=None, now=None):
    """The registered machines as a run of `holder` sees them: which one it would get, and for each one whether it is
    free or who holds it. Read-only: takes no lease, starts nothing."""
    now = time.time() if now is None else now
    rows = machine_rows(cfg)
    leases = {r['id']: lease_read(r['id']) for r in rows}
    ch = machine_choice(rows, leases, holder, explicit, now, prefer=last_machine(holder))
    out = []
    for r in rows:
        m = {**r, 'free': ch['free'][r['id']], 'busy': None}
        if not m['free']:
            m['busy'] = holder_view(leases.get(r['id']), now)
        out.append(m)
    return {'pick': ch['pick'], 'machines': out}


def cmd_machine_pick(args, cfg):
    """Read-only: the machine a run of this project would use now (the one the page quotes and then starts), and the
    others with who holds them. pick is None when every registered machine is held by someone else."""
    holder = holder_for(args)
    explicit = getattr(args, 'machine', None) or None
    if explicit and explicit not in machine_ids(cfg):
        raise ShiseiError(f'{explicit} is not a registered machine')
    view = machine_view(cfg, holder, explicit)
    return {'holder': holder, **view}


QUOTE_QUEUED = False   # the estimate is for a run that waits for a machine: priced as a start from cold


def quote_machine(cfg, holder, explicit=None):
    """Point an estimate at the machine the run would get: the given one, else the one this holder already leases,
    else the free one machine_choice picks. With none free the run would queue, so the quote is for the preferred
    machine started from cold (QUOTE_QUEUED)."""
    global QUOTE_QUEUED
    QUOTE_QUEUED = False
    if explicit:
        return use_machine(cfg, explicit, save=False)
    ids = machine_ids(cfg)
    for iid in ids:
        lease = lease_read(iid)
        if lease and lease.get('holder') == holder and lease_held(lease):
            return use_machine(cfg, iid, save=False)
    if not ids:
        return None
    try:
        rows = machine_rows(cfg)
    except ShiseiError:
        return use_machine(cfg, ids[0], save=False)
    ch = machine_choice(rows, {r['id']: lease_read(r['id']) for r in rows}, holder, prefer=last_machine(holder))
    if ch['pick']:
        return use_machine(cfg, ch['pick'], save=False)
    # queued: whichever machine frees first is used, so the quote is the dearest one started from cold (the most it costs)
    QUOTE_QUEUED = True
    usable = [r for r in rows if r.get('state') not in ('Deleting', 'Deleted')] or rows
    dearest = max(usable, key=lambda r: float(r.get('price_per_hour') or 0)) if usable else {'id': ids[0]}
    return use_machine(cfg, dearest['id'], save=False)


QUEUE_POLL_S = 30   # how often a queued run looks for a free machine


def refresh_registry(cfg):
    """Re-read which machines are registered (and their ports) from disk into this process's config: a queued run
    loaded it when it started (真机 2026-11-12: the second machine became ready, both queued runs kept waiting)."""
    try:
        disk = load_config()
    except (OSError, ValueError, ShiseiError):
        return cfg
    for k in ('instance_id', 'machines', 'ports'):
        if k in disk:
            cfg[k] = disk[k]
    return cfg


def wait_for_machine(cfg, minutes, show, explicit=None):
    """A run's queue: until a registered machine is free for HOLDER, look every QUEUE_POLL_S seconds; then point this
    process at it and take its lease (so nobody else gets it between the wait and the boot). show(queue) is told who
    holds the machines while it waits. Takes no lease while waiting and starts nothing. Raises StopRequested (the user
    cancelled; STOP_CHECK) or MachineBusy (nothing came free in `minutes`)."""
    global LEASE_REFUSED
    since = time.time()
    until = since + minutes * 60
    while True:
        refresh_registry(cfg)   # a machine registered while this run waits (开始 just made one) is a candidate too
        view = machine_view(cfg, HOLDER, explicit)
        if view['pick']:
            use_machine(cfg, view['pick'], save=False)
            try:
                ensure_claim(cfg)
                return view['pick']
            except MachineBusy:
                LEASE_REFUSED = False   # taken a moment before us: keep waiting
        if time.time() >= until:
            raise MachineBusy(f'queued {minutes} min and no machine came free; nothing was started.')
        show({'since': since, 'until': until,
              'busy': [{'machine': m['id'], 'name': m['name'], **(m['busy'] or {})} for m in view['machines'] if not m['free']]})
        pause(QUEUE_POLL_S)


def max_cost_check(amount, max_cost):
    """A queued run starts without asking again only at the price the user confirmed: the machine it got (or its
    state: stopped, so it boots) may cost more than the quote."""
    if max_cost is not None and amount > float(max_cost) + 0.01:
        raise MachineBusy(f'price changed while queued: confirmed ¥{float(max_cost):.2f}, now ¥{amount:.2f}; nothing was started.')


CLAIMED = None   # the machine this process took a lease on (set by ensure_claim)


def ensure_claim(cfg):
    """Take the lease on the machine this process points at, right before it is started or worked on (a command of
    LEASE_CMDS); starts the keeper. A no-op outside main() (HOLDER unset) or when already taken."""
    global CLAIMED, KEEPER
    if not HOLDER or CLAIMED:
        return CLAIMED
    global LEASE_REFUSED
    iid = need_instance(cfg)
    try:
        lease_take(iid, HOLDER, LOCAL_PORT, LEASE_PREBOOT_MINUTES, 'run')
    except MachineBusy:
        LEASE_REFUSED = True
        raise
    except OSError as e:   # e.g. a sandbox that may not write outside the project: nothing was started
        LEASE_REFUSED = True
        raise ShiseiError(f'could not write the machine lease ({e.strerror or e}); nothing was started') from e
    CLAIMED = iid
    if KEEPER is None:
        KEEPER = LeaseKeeper(cfg)
    return iid


def attach_machine(cfg, holder, explicit=None):
    """For a step of a run already going (tunnel, submit, wait, pull, down…): the machine given, else the one this
    holder leases, else the preferred one. Takes no lease."""
    if explicit:
        return use_machine(cfg, explicit, save=False)
    for iid in machine_ids(cfg):
        lease = lease_read(iid)
        if lease and lease.get('holder') == holder and (lease_held(lease) or lease.get('state') == 'idle'):
            return use_machine(cfg, iid, save=False)
    ids = machine_ids(cfg)
    return use_machine(cfg, ids[0], save=False) if ids else None


class LeaseKeeper:
    """While a command works on a running machine: every few minutes push the scheduled stop (and with it the lease)
    keep_alive_minutes ahead. Stops for good once an idle stop is armed."""
    def __init__(self, cfg):
        import threading
        self.cfg, self.on, self.done = dict(cfg), False, threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self.done.wait(KEEPER_EVERY):
            if self.on:
                try:
                    schedule_stop(self.cfg, working_stop(self.cfg), from_keeper=True)
                except ShiseiError as e:
                    log(f'could not renew the scheduled stop: {e}')

    def start(self):
        self.on = True

    def stop(self):
        self.on = False
        self.done.set()


def keeper_on():
    if KEEPER:
        KEEPER.start()


def lease_orphaned(lease, alive):
    """A held lease whose run process is gone: a project's (or setup's, qualify's) run that was killed or lost with dsh.
    Manual holds (cli, page) and the lease `up` leaves on purpose are not runs: their process ends by design."""
    return bool(alive) and lease.get('holder') not in SOFT_HOLDERS and lease.get('cmd') != 'up' \
        and bool(lease.get('pid')) and not alive(lease.get('pid'))


def patrol_plan(machines, leases, now, keep_min, idle_min, busy_jobs=0, slack=120, unleased_min=180, alive=None,
                setup_min=60):
    """What the patrol does to each machine (pure; tested). machines: [{'id', 'state', 'stop_at' (epoch or None),
    'registered'}]; leases: {id: lease or None}; busy_jobs: runs started by copies that write no lease (a job whose
    progress says running with a live pid). Rules:
    - not registered: only reported
    - running, lease held: the scheduled stop must exist and be at most keep_min ahead
    - running, lease released (idle) or run out: the scheduled stop is moved to at most idle_min ahead
    - running, lease held but its run process is gone (`alive` says so; killed, or lost with dsh): 'orphan', the lease is
      released and the stop moved to at most idle_min ahead, as for a finished run
    - running, no lease record at all (a run from an older copy or a script, the console…): only add a stop when there
      is none, unleased_min ahead (their runs can be long and nothing renews it); never shorten one
    - not registered but the machine 开始 is setting up ('setup' flag): only add a stop when there is none, setup_min ahead
      (a setup killed between create and its first scheduled stop would otherwise run on)
    Returns [{'id', 'state', 'holder', 'status', 'action' ('keep'|'idle'|None), 'minutes', 'release'}]."""
    out = []
    for m in machines:
        lease = leases.get(m['id'])
        row = {'id': m['id'], 'state': m.get('state'), 'holder': (lease or {}).get('holder'), 'action': None,
               'minutes': None, 'stop_at': m.get('stop_at')}
        stop_at = m.get('stop_at')
        if m.get('setup') and not m.get('registered'):
            row['status'] = 'setup' if m.get('state') == 'Running' else 'unmanaged'
            if m.get('state') == 'Running' and not stop_at:
                row.update(action='keep', minutes=setup_min)
        elif not m.get('registered'):
            row['status'] = 'unmanaged'
        elif m.get('state') != 'Running':
            row['status'] = 'off'
        elif lease_held(lease, now) and lease_orphaned(lease, alive):
            row.update(status='orphan', release=lease.get('pid'))
            if not stop_at or stop_at > now + idle_min * 60 + slack:
                row.update(action='idle', minutes=idle_min)
        elif lease_held(lease, now):
            row['status'] = 'working'
            if not stop_at or stop_at > now + keep_min * 60 + slack:
                row.update(action='keep', minutes=keep_min)
        elif lease:
            row['status'] = 'orphan' if lease.get('broken') else 'idle'
            if not stop_at or stop_at > now + idle_min * 60 + slack:
                row.update(action='idle', minutes=idle_min)
        else:
            row['status'] = 'busy-unleased' if busy_jobs else 'unleased'
            if not stop_at:
                row.update(action='keep', minutes=unleased_min)
        out.append(row)
    return out


def queued_runs():
    """Projects whose run waits for a machine (progress `queue`, live pid), first come first: [{'job', 'title', 'since'}]."""
    out = []
    for p in (HOME / 'jobs').glob('*/progress.json'):
        try:
            prog = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        q = prog.get('queue')
        if prog.get('state') == 'running' and isinstance(q, dict) and prog.get('pid') and pid_alive(prog['pid']):
            out.append({'job': p.parent.name, 'title': holder_view({'holder': p.parent.name})['title'], 'since': q.get('since')})
    return sorted(out, key=lambda r: r.get('since') or 0)


def running_jobs():
    """Jobs whose progress says running with a live pid (they may come from a copy that writes no lease)."""
    n = 0
    for p in (HOME / 'jobs').glob('*/progress.json'):
        try:
            prog = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        # a queued run waits for a machine: it is not working on one
        if prog.get('state') == 'running' and not prog.get('queue') and prog.get('pid') and pid_alive(prog['pid']):
            n += 1
    return n


def cmd_patrol(args, cfg):
    """Check every machine in the account and keep the rule: a registered machine that is on always has a scheduled
    stop, at most keep_alive_minutes ahead while someone works on it and idle_stop_minutes once nobody does (or its run
    process is gone). Never stops a machine itself. Of the unregistered ones it only touches the machine 开始 is setting
    up, and only to add a stop when it has none. Costs nothing (instance list / show)."""
    reg = set(machine_ids(cfg))
    now = time.time()
    machines = []
    for it in cs('instance', 'list')['items']:
        iid = it.get('UHostId')
        if not iid:
            continue
        show = {}
        if it.get('State') == 'Running':
            try:
                show = cs('instance', 'show', iid)['UHostSet'][0] or {}
            except (ShiseiError, KeyError, IndexError):
                pass
        machines.append({'id': iid, 'name': it.get('Name'), 'state': it.get('State'), 'stop_at': show.get('SchedulerStopTime') or None,
                         'registered': iid in reg, 'gpu': it.get('GpuType'), 'zone': it.get('Zone'),
                         'price': show.get('InstancePrice') or listed_price(iid, it), 'started_at': show.get('StartTime') or None})
    making = setup_making()
    sp = setup_progress() or {}
    making_name = sp.get('name') if sp.get('state') != 'done' else None
    for m in machines:   # the machine 开始 is making: by id once saved, by its unique name before (create still waiting)
        m['setup'] = bool(making and m['id'] == making) or bool(making_name and m.get('name') == making_name)
    leases = {m['id']: lease_read(m['id']) if m['registered'] else None for m in machines}
    idle_min = int(cfg.get('idle_stop_minutes') or 10)
    plan = patrol_plan(machines, leases, now, keep_minutes(cfg), idle_min, running_jobs(), alive=pid_alive,
                       setup_min=SETUP_LIMIT_MIN)
    info = {m['id']: m for m in machines}
    for row in plan:
        m = info[row['id']]
        ok, why = instance_fit({'GpuType': m['gpu']})
        if ok and row['id'] == making and not m['registered']:
            ok, why = False, '还在「开始」里准备，好了会自动选上'
        row.update(name=m['name'], gpu=m['gpu'], zone=m['zone'], price=m['price'], started_at=m['started_at'],
                   port=(cfg.get('ports') or {}).get(row['id']), suitable=ok, why=why,
                   lease_until=(leases.get(row['id']) or {}).get('until'),
                   eta_min=holder_view(leases.get(row['id']), now)['eta_min'] if lease_held(leases.get(row['id']), now) else None)
        if row.get('release') and not args.dry_run:
            lease_release_dead(row['id'], row['release'], idle_min)
        if row['action'] and not args.dry_run:
            try:
                cs('instance', 'schedule', 'set', row['id'], '--at', f'{row["minutes"]}m')
                row['stop_at'] = now + row['minutes'] * 60
                if row['action'] == 'keep' and leases.get(row['id']):
                    lease_renew(row['id'], leases[row['id']]['holder'], row['minutes'])
            except ShiseiError as e:
                row['error'] = str(e)[:200]
    return {'machines': plan, 'on': sum(1 for r in plan if r['state'] == 'Running'), 'queued': queued_runs(),
            'checked_at': now, 'dry_run': bool(args.dry_run)}


# ---------------------------------------------------------------- ssh

def ssh_base(cfg, info, scp=False):
    host, port = ssh_endpoint(info)
    key = resolve_ssh_key(cfg)
    if not key or not Path(key).exists():
        raise ShiseiError('no ssh key: none of ~/.ssh/id_ed25519, id_rsa, id_ecdsa exists; make one on the Piying page '
                          '(开始 › GPU 机器, or 设置), or run: shisei.py ssh-keygen')
    HOME.mkdir(parents=True, exist_ok=True)
    # BatchMode: never ask for a password. A machine without our key offers password login; with no terminal ssh on macOS
    # fails at once (then authorize_key adds the key), but on Windows it waited for the password for good while the
    # machine billed (seen 2026-11-13)
    opts = ['-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-i', key, '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30',
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={HOME / "known_hosts"}']
    if scp:
        return ssh_cmd('scp', key=key) + ['-q'] + opts + ['-P', str(port)], f'root@{host}'
    return ssh_cmd('ssh', key=key) + opts + ['-p', str(port), f'root@{host}']


# ssh failed before the session began (no handshake, refused, timed out, reset while connecting): the remote command
# never ran, so running it again is safe. A drop like this ended a 23-minute takes run at the last step (2026-11-14).
SSH_NEVER_RAN = re.compile(r'before handshake|kex_exchange_identification|Connection (refused|timed out|reset)|'
                           r'Operation timed out|Could not resolve hostname|Network is unreachable|No route to host', re.I)


def ssh_never_ran(returncode, stderr):
    """ssh's own failure (exit 255) before the remote side started anything: safe to try again."""
    return returncode == 255 and bool(SSH_NEVER_RAN.search(stderr or ''))


def scp_run(argv, waits=(5, 15, 30), sleep=None, timeout=None):
    """One scp copy, tried again when ssh itself failed (exit 255: the gateway dropped the connection, often before the
    handshake). A copy is safe to repeat. Raises CalledProcessError like check=True did."""
    for wait in (*waits, None):
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        rc = getattr(r, 'returncode', 0)   # a stand-in run in the tests may answer None
        if rc != 255 or wait is None:
            break
        log(f'scp did not get through ({(r.stderr or "").strip()[-120:]}); trying again in {wait} s')
        (sleep or time.sleep)(wait)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, argv, r.stdout, r.stderr)
    return r


def ssh_run(cfg, info, script, check=True, timeout=600, waits=(5, 15, 30), sleep=None):
    for wait in (*waits, None):
        r = subprocess.run(ssh_base(cfg, info) + [script], capture_output=True, text=True, timeout=timeout)
        if wait is None or not ssh_never_ran(r.returncode, r.stderr):
            break
        log(f'ssh did not connect ({r.stderr.strip()[-120:]}); trying again in {wait} s')
        (sleep or time.sleep)(wait)
    if check and r.returncode != 0:
        raise ShiseiError(f'remote command failed ({r.returncode}): {r.stderr.strip()[-400:]}')
    return r.stdout


# Right after boot ComfyUI is not listening yet: wait until it is (on any address) before deciding, or the switch to
# 127.0.0.1 is skipped and the base image's 0.0.0.0 comes up after the check (first real create, 2026-11-12).
REMOTE_LOCALHOST_ONLY = r'''
S=${SHISEI_START_D:-/start.d}/comfyui.sh
listening(){ (ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep -E '[:.]8188\b' ; }
for i in $(seq 1 60); do listening | grep -q 8188 && break; sleep 5; done
if listening | grep -qE '(0\.0\.0\.0|\*|\[::\]|:::):8188' || grep -q -- '--listen 0.0.0.0' "$S"; then
  sed -i.bak 's/--listen 0.0.0.0/--listen 127.0.0.1/' "$S" && rm -f "$S.bak"
  pkill -f '[m]ain.py' ; sleep 2
  grep -q 'listen 127.0.0.1' "$S" && (nohup bash "$S" >/tmp/comfy_restart.log 2>&1 &)
fi
for i in $(seq 1 60); do listening | grep -q '127.0.0.1:8188' && break; sleep 5; done
listening
'''

# ComfyUI started at boot and was gone a minute later with nothing in its log (2026-11-13, a resumed 整段换): start it
# again from the same start script, only when that script listens on 127.0.0.1 (REMOTE_LOCALHOST_ONLY made it so)
REMOTE_COMFY_ENSURE = r'''
S=${SHISEI_START_D:-/start.d}/comfyui.sh
listening(){ (ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep -E '[:.]8188\b' ; }
if ! pgrep -f '[m]ain.py' >/dev/null; then
  grep -q -- '--listen 127.0.0.1' "$S" || { echo 'start script does not listen on 127.0.0.1'; exit 3; }
  (nohup bash "$S" >/tmp/comfy_restart.log 2>&1 &)
  echo restarted
fi
for i in $(seq 1 36); do listening | grep -q '127.0.0.1:8188' && break; sleep 5; done
listening
'''

REMOTE_AUDIT = r'''
source /root/miniconda3/bin/activate >/dev/null 2>&1
python - <<'PY'
import json, glob, subprocess, os, urllib.request
try:
    hist = json.load(urllib.request.urlopen('http://127.0.0.1:8188/history?max_items=500', timeout=10))
except Exception as e:
    hist = {'__error__': str(e)}
so = glob.glob('/root/ComfyUI/input/**/*.so', recursive=True)
ps = subprocess.run(['ps', '-eo', 'args'], capture_output=True, text=True).stdout.splitlines()
# needles are built by concatenation so this script's own command line never matches them
bad_dir, bad_sh = '/usr/lib/' + '.systemd/', 'kp' + '.sh'
procs = [p for p in ps if bad_dir in p or bad_sh in p]
extra = [f for f in ('/etc/ssh/sshd_bd.conf', '/root/ComfyUI/output/_ev.txt') if os.path.exists(f)]
print(json.dumps({'history': hist, 'so': so, 'procs': procs, 'extra': extra}))
PY
'''


def external_port_closed(info):
    url = public_comfy_url(info)
    if not url:
        return {'checked': False, 'reason': 'no IngressHost'}
    try:
        with urllib.request.urlopen(url + '/system_stats', timeout=10) as r:
            body = r.read(2000).decode('utf8', 'replace')
            exposed = r.status == 200 and '"system"' in body
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        return {'checked': True, 'closed': True, 'detail': type(e).__name__}
    return {'checked': True, 'closed': not exposed, 'detail': 'ComfyUI answered from the public URL' if exposed else 'no ComfyUI response'}


def do_secure(cfg, info):
    listen = ssh_run(cfg, info, REMOTE_LOCALHOST_ONLY)
    if '127.0.0.1:8188' not in listen:
        raise ShiseiError(f'ComfyUI is not listening on 127.0.0.1:8188: {listen.strip()[:200]}')
    ext = external_port_closed(info)
    audit = json.loads(ssh_run(cfg, info, REMOTE_AUDIT).strip().splitlines()[-1])
    hist = audit['history']
    if '__error__' in hist:
        raise ShiseiError(f'could not read ComfyUI history: {hist["__error__"]}')
    findings = analyze_security(hist, audit['so'], audit['procs'], audit['extra'],
                                cfg.get('allowed_clients') or DEFAULTS['allowed_clients'])
    if ext.get('checked') and not ext.get('closed'):
        findings.append({'kind': 'public_port_open', 'detail': ext['detail']})
    return {'listen': listen.strip(), 'external': ext, 'findings': findings, 'clean': not findings}



# ---------- the machine environment version (docs/IMAGE.md) ----------
# setup.sh pins every custom node to a commit and every downloaded weight to a ModelScope revision and sha256, then
# writes /root/.shisei/env.json with what it actually found. The plugin reads the pins from its own setup.sh (one
# source) and compares. An old, missing or drifted environment is a warning on `up` (the machine is already billing,
# so refusing would only leave it running), a problem in gpu-qualify, and a failure at the end of machine-create.
SHISEI_ENV = 1          # must equal SHISEI_ENV in assets/image/setup.sh (test_shisei checks)
PUBLIC_IMAGES = {1: 'compshareImage-1xnjxhnxyr75'}   # env -> public community image id (docs/IMAGE.md); env 1: 「皮影 AI短剧换角 4090」, free
REMOTE_ENV = 'cat /root/.shisei/env.json 2>/dev/null || echo "{}"'


def setup_manifest(text=None):
    """The pins in setup.sh: {'env', 'nodes': {dir: commit}, 'downloads': {file: sha256}, 'blender'}."""
    if text is None:
        text = (ASSETS / 'image' / 'setup.sh').read_text(encoding='utf-8')
    block = text.split('# ---- 锁定', 1)[1].split('# ---- 锁定结束', 1)[0]   # zh-ok
    def var(name):
        m = re.search(rf"^{name}=(?:'([^']*)'|(\S+))", block, re.M)
        return m.group(1) if m.group(1) is not None else m.group(2)
    rows = lambda v: [l.split('|') for l in v.splitlines() if l.strip()]
    return {'env': int(var('SHISEI_ENV')), 'blender': var('BL_VER'),
            'nodes': {r[0]: r[2] for r in rows(var('NODES'))},
            'downloads': {r[2].rsplit('/', 1)[-1]: r[4] for r in rows(var('DOWNLOADS'))}}


def env_report(env, manifest):
    """Compare a machine's env.json with the pins. state: current / old / newer / drift / missing."""
    want = manifest['env']
    if not isinstance(env, dict) or not isinstance(env.get('env'), int):
        return {'state': 'missing', 'have': None, 'want': want, 'problems': [],
                'hint': 'This machine has no environment version (set up before versions, or setup.sh did not finish). '
                        'Update its environment (皮影 page › 机器 › 更新环境, which reruns setup.sh) to be sure it matches this plugin.'}
    have = env['env']
    if have < want:
        return {'state': 'old', 'have': have, 'want': want, 'problems': [],
                'hint': f'This machine has environment {have}; this plugin expects {want}. Update its environment (皮影 page › 机器 › 更新环境).'}
    if have > want:
        return {'state': 'newer', 'have': have, 'want': want, 'problems': [],
                'hint': f'This machine has environment {have}, newer than this plugin knows ({want}). Update the plugin.'}
    problems = []
    for kind, label in (('nodes', 'node'), ('downloads', 'weight')):
        got = env.get(kind) or {}
        problems += [f'{label} {k}: {(got.get(k) or "none")[:12]}, expected {v[:12]}'
                     for k, v in manifest[kind].items() if got.get(k) != v]
    if problems:
        return {'state': 'drift', 'have': have, 'want': want, 'problems': problems,
                'hint': 'The machine says environment %d but differs from it; update its environment (皮影 page › 机器 › 更新环境).' % have}
    return {'state': 'current', 'have': have, 'want': want, 'problems': [], 'hint': None}


def parse_env_json(text):
    """env.json as `cat` prints it: setup.sh writes it indented (many lines), maybe after login noise; {} when empty."""
    text = (text or '').strip()
    if not text:
        return {}
    i = text.find('{')
    if i < 0:
        raise ValueError('no JSON object in env.json output')
    return json.loads(text[i:])


def remember_env(iid, report, now=None):
    """The last environment read from each machine (machines-env.json), so the machines page can say which stopped
    machine is out of date. Only what was read: an unreadable machine (state unknown) keeps its last record."""
    if not iid or not report or report.get('state') == 'unknown':
        return None
    p = HOME / 'machines-env.json'
    try:
        seen = json.loads(p.read_text())
    except (OSError, ValueError):
        seen = {}
    seen[iid] = {'have': report.get('have'), 'state': report.get('state'), 'at': time.time() if now is None else now}
    HOME.mkdir(parents=True, exist_ok=True)
    save_json_atomic(p, seen)
    return seen[iid]


def read_env(cfg, info):
    """The running machine's environment against this plugin's setup.sh; never raises. Remembered per machine."""
    try:
        report = env_report(parse_env_json(ssh_run(cfg, info, REMOTE_ENV, check=False, timeout=60)), setup_manifest())
        try:
            remember_env((info or {}).get('UHostId'), report)
        except OSError:
            pass
        return report
    except (ShiseiError, subprocess.SubprocessError, OSError, ValueError, IndexError) as e:
        return {'state': 'unknown', 'want': SHISEI_ENV, 'problems': [], 'hint': f'could not read env.json: {e}'[:200]}


# ---------------------------------------------------------------- commands

def plugin_version():
    """This plugin's version (package.json two folders above skills/), or None in a copy without it."""
    try:
        return json.loads((Path(__file__).resolve().parents[3] / 'package.json').read_text(encoding='utf-8')).get('version')
    except (OSError, ValueError):
        return None


def update_note(installed, home=None):
    """The page's last update answer (lib/update.js writes update.json; doctor never asks the registry itself)."""
    try:
        d = json.loads(((home or HOME) / 'update.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    latest = (d.get('latest') or {}).get('version')
    if not latest or not installed or version_key(latest) <= version_key(installed):
        return None
    return {'version': latest, 'checked_at': d.get('checkedAt')}


def version_key(v):
    """Sort key for x.y.z[-pre]: a release sorts after its prereleases."""
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?', str(v or ''))
    if not m:
        return ((-1,),)
    pre = m.group(4)
    parts = tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in pre.split('.')) if pre else ()
    return (tuple(int(x) for x in m.groups()[:3]), (0,) if pre else (1,), parts)


def cmd_doctor(args, cfg):
    checks = {}
    checks['plugin'] = plugin_version()
    checks['env'] = SHISEI_ENV
    note = update_note(checks['plugin'])
    if note:
        checks['update'] = note['version']
    checks['python'] = sys.version.split()[0]
    # keys come from the environment (plain shell) or the dsh plugin's bridge (model shell inside dsh)
    checks['keys'] = 'env' if keys_in_env() else ('dsh-bridge' if os.environ.get('DSH_SHISEI_BRIDGE') else None)
    if checks['keys'] == 'env':
        try:
            checks['compshare_cli'] = compshare_bin()
        except ShiseiError as e:
            checks['compshare_cli'] = None
            checks['compshare_cli_hint'] = str(e)
    checks['instance_id'] = cfg.get('instance_id') or None
    key = resolve_ssh_key(cfg)
    checks['ssh_key_exists'] = bool(key) and Path(key).exists()
    checks['ssh_key'] = (('shisei:' if key == str(shisei_key()) else 'auto:') if not cfg.get('ssh_key') else '') + Path(key).name \
        if key else None
    ensure_ffmpeg_path()
    for tool in ('ffmpeg', 'blender'):
        checks[tool] = shutil.which(tool)
    for tool in ('ssh', 'scp'):
        try:
            cmd = ssh_cmd(tool, key=key if checks['ssh_key_exists'] else None)
            checks[tool] = 'plugin' if len(cmd) > 1 else cmd[0]
        except ShiseiError:
            checks[tool] = None
    checks['whisper'] = whisper_bin()
    if checks['keys'] and (checks['keys'] == 'dsh-bridge' or checks.get('compshare_cli')):
        try:
            cs('instance', 'list')
            checks['api'] = 'ok'
        except ShiseiError as e:
            checks['api'] = str(e)
    ready = all([checks['keys'], checks.get('api') == 'ok',
                 checks['instance_id'], checks['ssh_key_exists'], checks['ssh'], checks['scp']])
    return {'ready_for_gpu': ready, 'checks': checks}


INSTALL_CLI_TIMEOUT = 600
# compshare-cli and its dependencies, pure-Python wheels for Python 3.9-3.13 (scripts/vendor-compshare.sh refreshes them)
VENDOR_WHEELS = ASSETS / 'vendor' / 'wheels'


def install_step(cmd, what):
    """Run one install step, echoing its output to stderr (the page's job log) as it comes; on failure, or after
    INSTALL_CLI_TIMEOUT seconds, raise with the last lines it printed, so the page can say why."""
    tail, late = [], []
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
    timer = threading.Timer(INSTALL_CLI_TIMEOUT, lambda: (late.append(1), p.kill()))
    timer.start()
    try:
        for line in p.stdout:
            sys.stderr.write(line)
            tail = (tail + [line.rstrip()])[-12:]
        p.wait()
    finally:
        timer.cancel()
    if p.returncode:
        took = f' (stopped after {INSTALL_CLI_TIMEOUT // 60} minutes)' if late else ''
        raise ShiseiError(f'compshare-cli install failed: {what}{took}: ' + ' | '.join(x for x in tail if x.strip())[-600:])


def cmd_install_cli(args, cfg):
    venv = HOME / 'venv'
    py = venv / VENV_BIN / f'python{EXE}'
    own = venv / VENV_BIN / f'compshare{EXE}'
    if getattr(args, 'if_missing', False) and venv_alive(venv) and own.exists():
        return {'compshare_cli': str(own), 'version': COMPSHARE_CLI_VERSION, 'source': 'present'}
    # a venv left by an older Python (one made with 3.5 before the user upgraded) cannot install compshare-cli: rebuild it
    fresh = py.exists() and venv_alive(venv) and subprocess.run([str(py), '-c', 'import sys; sys.exit(sys.version_info < (3, 9))'],
                                           capture_output=True).returncode == 0
    if not fresh:
        install_step([sys.executable, '-m', 'venv', '--clear', str(venv)], 'could not make a venv')
    pip = [str(py), '-m', 'pip', 'install', '--disable-pip-version-check']
    want = f'compshare-cli=={COMPSHARE_CLI_VERSION}'
    try:   # the wheels shipped with the plugin: no network, a few seconds
        install_step(pip + ['--no-index', '--find-links', str(VENDOR_WHEELS), want], 'pip install (bundled)')
        source = 'bundled'
    except ShiseiError:   # an interpreter the bundled set does not cover: PyPI, as before
        install_step(pip + ['--timeout', '30', '--retries', '2', want], 'pip install')
        source = 'pypi'
    return {'compshare_cli': str(venv / VENV_BIN / f'compshare{EXE}'), 'version': COMPSHARE_CLI_VERSION, 'source': source}


def vendor_check(folder=None):
    """The bundled wheels are exactly the ones in SHA256SUMS (the tests and install-cli rely on it)."""
    folder = Path(folder or VENDOR_WHEELS)
    want = {}
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split(None, 1)
        want[name.strip().lstrip('*')] = digest
    have = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in folder.glob('*.whl')}
    return want == have


def cmd_config(args, cfg):
    if args.action == 'set':
        value = args.value
        if args.key == 'vision_base_url' and not re.fullmatch(r'https://[\w.-]+(:\d+)?(/[\w./-]*)?', value):
            raise ShiseiError('vision_base_url must be an https URL')
        if args.key == 'vision_source' and value not in ('auto', 'dsh', 'key'):
            raise ShiseiError('vision_source is auto, dsh or key')
        if args.key in ('idle_stop_minutes',):
            value = int(value)
        if args.key == 'update_check':
            if str(value).lower() not in ('true', 'false', '1', '0', 'on', 'off'):
                raise ShiseiError('update_check is true or false')
            value = str(value).lower() in ('true', '1', 'on')
        if args.key == 'setup_budget':
            value = float(value)
            if not 0 < value <= 100:
                raise ShiseiError('setup_budget is yuan, 0..100')
        if args.key == 'comfy_url' and value and not re.fullmatch(r'https?://[\w.-]+(:\d+)?/?', value):
            raise ShiseiError('comfy_url is like http://127.0.0.1:8188 (empty turns local mode off)')
        if args.key == 'comfy_model_roots':
            value = [v.strip() for v in value.split(',') if v.strip()]
        if args.key not in DEFAULTS:
            raise ShiseiError(f'unknown key {args.key}; known: {", ".join(DEFAULTS)}')
        cfg[args.key] = value
        save_config(cfg)
    return {'config': cfg, 'path': str(CONFIG)}


def cmd_estimate(args, cfg):
    price = args.price
    if price is None:
        price = float(instance_info(cfg)['InstancePrice'])
    keep = getattr(args, 'keep_bg', False)
    return estimate(args.seeds, price, running=args.running, frames=args.frames, mocap=not keep, mannequin=not keep)


def cmd_status(args, cfg):
    info = instance_info(cfg)
    out = {k: info.get(k) for k in ('UHostId', 'Name', 'State', 'GpuType', 'Zone', 'InstancePrice', 'SchedulerStopTime')}
    out['image'] = info.get('CompShareImageName')
    out['port'] = LOCAL_PORT
    lease = lease_read(info['UHostId'])
    out['lease'] = {k: lease.get(k) for k in ('holder', 'state', 'until')} if lease else None
    return out


def stop_check():
    """While waiting for stock: the run's stop request (StopRequested) ends the wait at once. No-op outside a run."""
    if STOP_CHECK:
        STOP_CHECK()


def pause(seconds, every=5):
    """Sleep `seconds` in short slices, checking for a stop request between them."""
    end = time.time() + seconds
    while True:
        stop_check()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(every, left))


def key_before_start(cfg, make=None):
    """Before a start bills anything: a computer with no ssh key at all gets the plugin's own pair (as machine-create
    does); up then puts its public half on the machine once it runs (authorize_key). A key of the user's is kept as it
    is. Returns the key made, else None. Without this a fresh computer started the machine and only then failed with
    'no ssh key', billing the boot and the idle minutes (seen 2026-11-14)."""
    own = key_to_make(cfg)
    if own:
        (make or make_own_key)(own)
    return own


def cmd_up(args, cfg):
    global UP_STARTED
    info = instance_info(cfg)
    plan = {'instance': info['UHostId'], 'gpu': info.get('GpuType'), 'zone': info.get('Zone'),
            'price_per_hour': info.get('InstancePrice'), 'state': info.get('State')}
    if not args.confirm_billing:
        return {'needs_confirmation': True, 'plan': plan,
                'message': 'Starting the instance bills by the second. Ask the user, then rerun with --confirm-billing.'}
    if key_before_start(cfg):
        log('made the plugin\'s own ssh key (this computer had none); it goes on the machine once it runs')
    ensure_claim(cfg)   # the lease is taken now, not before: refusals and local-only work never hold a machine
    ok = False
    for attempt in range(1, args.gpu_attempts + 1):
        if instance_state(cfg) != 'Running':
            deadline = time.time() + args.max_wait_min * 60
            while True:
                stop_check()   # stopped while waiting for stock: nothing started, nothing billed
                try:
                    data = cs('instance', 'start', cfg['instance_id'])
                    if not (data or {}).get('failed'):
                        break
                    reason = json.dumps(data.get('failed'), ensure_ascii=False)[:200]
                except ShiseiError as e:
                    reason = start_failure(str(e))
                # 「Pod is starting … pod is Running」: the earlier start went through after all; it is running (and
                # billing) now, so stop asking and go on
                if instance_state(cfg) == 'Running':
                    log('the instance is running now')
                    break
                if time.time() > deadline:
                    raise ShiseiError(f'no GPU available after {args.max_wait_min} min (nothing was billed while waiting): {reason}')
                log(f'start failed ({reason}); retrying in 120 s, waiting is not billed')
                pause(120)
                if instance_state(cfg) == 'Running':
                    log('the instance is running now')
                    break
            log('starting the instance (billing from now)')
            wait_state(cfg, 'Running', 600)
        UP_STARTED = True
        schedule_stop(cfg, working_stop(cfg))
        info = instance_info(cfg)
        key_tried = False
        said = ''
        for _ in range(30):
            r = subprocess.run(ssh_base(cfg, info) + ['true'], capture_output=True, text=True)
            if r.returncode == 0:
                break
            if r.stderr.strip() and r.stderr.strip() != said:
                said = r.stderr.strip()
                log(f'ssh: {said[-300:]}')
            # a machine made in the console has never seen this key: put it on once (we hold the lease), then retry
            if ssh_denied(r.stderr) and not key_tried:
                key_tried = True
                try:
                    log('ssh key not on the machine yet; adding it' if authorize_key(cfg, cfg['instance_id'])
                        else 'could not add the ssh key to the machine')
                except ShiseiError as e:
                    log(f'could not add the ssh key to the machine: {e}')
                continue
            time.sleep(10)
        else:
            # not a GPU fault: do not stop/restart, the user decides (machine is running and billing)
            raise ShiseiError('instance is Running but ssh never connected (key not on the instance, or network). '
                              'It is still billing: fix ssh or run `down --yes`.' + (f' Last ssh error: {said[-300:]}' if said else ''))
        cuda = ssh_run(cfg, info, 'source /root/miniconda3/bin/activate; python -c "import torch;print(torch.cuda.is_available())"',
                       check=False).strip().splitlines()
        log(f'attempt {attempt}: cuda={cuda[-1] if cuda else "?"}')
        if cuda and cuda[-1] == 'True':
            ok = True
            break
        log('GPU unusable; stopping and trying again')
        cs('instance', 'stop', cfg['instance_id'], '--yes')
        wait_state(cfg, 'Stopped', 600)
    if not ok:
        raise ShiseiError(f'GPU never usable after {args.gpu_attempts} attempts; instance stopped')
    sec = do_secure(cfg, info)
    if not sec['clean']:
        return {'secure': sec, 'alert': True,
                'message': 'Possible intrusion. Pull finished outputs, stop the instance, do not reuse it.'}
    env = read_env(cfg, info)
    if env['state'] != 'current':
        log(env['hint'])
    return {'plan': plan, 'secure': sec, 'ssh': '%s:%d' % ssh_endpoint(info), 'env': env}


def cmd_secure(args, cfg):
    sec = do_secure(cfg, instance_info(cfg))
    if not sec['clean']:
        sec['alert'] = True
    return sec


def tunnel_pidfile():
    """Where a tunnel started in the background keeps its pid (Windows, and the plugin's own ssh everywhere): one per
    local port, as each machine has its own port."""
    return HOME / 'tunnels' / f'{LOCAL_PORT}.pid'


def win_image(pid):
    """Windows: the program file a process runs, or None when it is gone (so a reused pid is not taken for our ssh)."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        return buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k.CloseHandle(h)


def tunnel_is_ours(pid, exe=None, nt=os.name == 'nt', image=None):
    """Is `pid` still the tunnel we started (not a reused pid)? Windows: the program it was started with (`exe`, kept
    in the pid file: under dsh Desktop the plugin's ssh runs on "DeepSeek Harness.exe", seen 2026-11-13), or an ssh.exe
    / node.exe; elsewhere its command line carries this port's forward."""
    if not pid_alive(pid):
        return False
    if nt:
        img = (image or win_image)(pid) or ''
        if exe and img and os.path.normcase(os.path.abspath(img)) == os.path.normcase(os.path.abspath(exe)):
            return True
        return Path(img.replace('\\', '/')).name.lower() in ('ssh.exe', 'node.exe')
    r = subprocess.run(['ps', '-o', 'command=', '-p', str(pid)], capture_output=True, text=True)
    return f'{LOCAL_PORT}:127.0.0.1:8188' in r.stdout


def tunnel_pids():
    pids = []
    try:   # the pid written when the tunnel was opened in the background
        lines = tunnel_pidfile().read_text().splitlines()   # pid, then the program it was started with
        pid = int(lines[0].strip())
        if tunnel_is_ours(pid, exe=lines[1].strip() if len(lines) > 1 else None):
            pids.append(str(pid))
    except (OSError, ValueError):
        pass
    if os.name != 'nt':   # also an OpenSSH tunnel opened with -f (no pid file); the pattern must not start with '-'
        r = subprocess.run(['pgrep', '-f', f'{LOCAL_PORT}:127.0.0.1:8188'], capture_output=True, text=True)
        pids += [p for p in r.stdout.split() if p.strip() and p not in pids]
    return pids


def open_tunnel_background(argv):
    """Start the tunnel without -f and keep its pid in tunnel_pidfile(): Windows OpenSSH says "forking to background"
    for -f but the process it was started as never exits, and the plugin's own ssh has no -f. On Windows: no window,
    its own process group (and out of dsh's job object when that is allowed, so it outlives the command that opened
    it); elsewhere its own session."""
    io = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == 'nt':
        base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(argv, creationflags=base | 0x01000000, **io)   # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            proc = subprocess.Popen(argv, creationflags=base, **io)
    else:
        proc = subprocess.Popen(argv, start_new_session=True, **io)
    tunnel_pidfile().parent.mkdir(parents=True, exist_ok=True)
    tunnel_pidfile().write_text(f'{proc.pid}\n{argv[0]}\n')
    return proc


def cmd_tunnel(args, cfg):
    for pid in tunnel_pids():
        if os.name == 'nt':
            try:
                os.kill(int(pid), signal.SIGTERM)   # TerminateProcess; tunnel_pids made sure it is our ssh
            except OSError:
                pass
        else:
            subprocess.run(['kill', pid])
    tunnel_pidfile().unlink(missing_ok=True)
    if args.close:
        return {'tunnel': 'closed'}
    info = instance_info(cfg)
    base = ssh_base(cfg, info)
    # ExitOnForwardFailure: with the local port taken (another tunnel, a local ComfyUI) ssh only warned and stayed up, and
    # every check after it talked to whatever owned the port (seen 2026-11-12)
    forward = ['-N', '-o', 'ExitOnForwardFailure=yes', '-L', f'{LOCAL_PORT}:127.0.0.1:8188']
    proc = None
    n = len(ssh_cmd('ssh', key=resolve_ssh_key(cfg)))   # the program part of base: OpenSSH's ssh, or Node + lib/ssh.cjs + 'ssh'
    def open_once():
        nonlocal proc
        if os.name == 'nt' or n > 1:
            proc = open_tunnel_background(base[:n] + forward + base[n:])
        else:
            try:
                subprocess.run(base[:n] + ['-f'] + forward + base[n:], check=True)
            except subprocess.CalledProcessError as e:
                raise ShiseiError(f'ssh tunnel exited ({e.returncode}): port {LOCAL_PORT} taken, or ssh could not connect')
    def answers(tries):
        for _ in range(tries):
            if proc is not None and proc.poll() is not None:
                raise ShiseiError(f'ssh tunnel exited ({proc.returncode}): port {LOCAL_PORT} taken, or ssh could not connect')
            try:
                urllib.request.urlopen(f'{comfy_base()}/system_stats', timeout=5)
                return True
            except Exception:
                time.sleep(2)
        return False
    # the machine's ssh gateway sometimes drops a connection before the handshake (several in a row, 2026-11-14): open
    # the tunnel again a few times before giving up; a port that is really taken fails the same way each time
    up = False
    for wait in (5, 15, 30, None):
        try:
            open_once()
            up = answers(10)
            break
        except ShiseiError as e:
            if wait is None:
                raise
            log(f'{e}; opening the tunnel again in {wait} s')
            time.sleep(wait)
    global TUNNEL_CFG
    TUNNEL_CFG = cfg
    if up:
        return {'tunnel': f'{comfy_base()}'}
    # ComfyUI not running on the machine (it died after boot): start it again, on 127.0.0.1 only, and wait for it
    out = ssh_run(cfg, info, REMOTE_COMFY_ENSURE, check=False, timeout=240)
    if 'restarted' in out:
        log('ComfyUI was not running on the machine; started it again')
    if '127.0.0.1:8188' in out and answers(15):
        return {'tunnel': f'{comfy_base()}'}
    raise ShiseiError('tunnel opened but ComfyUI does not answer')


def cmd_upload(args, cfg):
    info = instance_info(cfg)
    base, target = ssh_base(cfg, info, scp=True)
    for f in args.files:
        if not Path(f).is_file():
            raise ShiseiError(f'not a file: {f}')
        scp_run(base + [f, f'{target}:{REMOTE_COMFY}/input/'])
    return {'uploaded': [Path(f).name for f in args.files]}


TUNNEL_CFG = None   # the config of the tunnel this process opened: a send that finds it dropped opens it again


def post_prompt(api, reopen=True):
    body = json.dumps({'prompt': api, 'client_id': client_id()}).encode()
    req = urllib.request.Request(f'{comfy_base()}/prompt', body, {'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))['prompt_id']
    except urllib.error.HTTPError as e:
        raise ShiseiError(f'ComfyUI rejected the prompt ({e.code}): {e.read().decode()[:1500]}')
    except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
        # waiting already reopened a dropped tunnel, sending did not: a 整段换 failed at its first shot (2026-11-14)
        if reopen and TUNNEL_CFG is not None:
            log('tunnel dropped before sending, reopening')
            cmd_tunnel(argparse.Namespace(close=False), TUNNEL_CFG)
            return post_prompt(api, reopen=False)
        raise ShiseiError(f'ComfyUI not reachable on {comfy_base()}; open the tunnel first ({getattr(e, "reason", e)})')


def load_workflow(name):
    return json.loads((ASSETS / 'workflows' / f'{name}.json').read_text())


def cmd_submit(args, cfg):
    if args.kind == 'mocap':
        api = patch_mocap(load_workflow('mocap_glb'), args.video, args.prefix, args.frames)
        apis = [(None, api)]
    elif args.keep_bg:
        if not args.prompt:
            raise ShiseiError('--keep-bg needs --prompt (the studio prompt talks about a mannequin)')
        apis = [(s, patch_swap_keep_bg(load_workflow('swap_studio'), args.video, args.ref, args.ref2, args.prompt, s,
                                       f'{args.prefix}_s{s}', args.keep_bg, args.frames)) for s in args.seeds]
    else:
        prompt = args.prompt or build_prompt(args.subject, args.details or '')
        apis = [(s, patch_swap(load_workflow('swap_studio'), args.video, args.ref, args.ref2, prompt, s,
                               f'{args.prefix}_s{s}', args.character_strength, args.head_strength, args.frames)) for s in args.seeds]
    if args.dry_run:
        return {'dry_run': True, 'workflows': [a for _, a in apis]}
    queued = [{'seed': s, 'prompt_id': post_prompt(a)} for s, a in apis]
    # working: push the scheduled stop back so a long job is not cut off
    schedule_stop(cfg, working_stop(cfg))
    return {'queued': queued}


def never_claimed():
    """This run asked for the machine's lease and did not get it: refused (someone else holds it) or unwritable (a
    sandbox that may not write outside the project). Its failure path must not touch that machine's scheduled stop or
    tunnel, nor bill its time: the machine may be on for another project (2026-11-12, a director's run blocked by its
    sandbox booked 10 idle minutes of the machine another project was using)."""
    return LEASE_REFUSED


def arm_idle_stop(cfg):
    """After a failed run: if the machine is up, let it stop by itself after the idle time instead of billing until
    someone notices. Best effort; the failure itself is what gets reported."""
    if never_claimed():
        return
    try:
        if instance_state(cfg) == 'Running':
            idle = int(cfg.get('idle_stop_minutes') or 10)
            schedule_stop(cfg, f'{idle}m')
            log(f'run failed; the machine stops by itself in {idle} min unless you continue')
    except ShiseiError as e:
        log(f'could not arm the idle stop: {e}')


def schedule_stop(cfg, at, from_keeper=False):
    """Use the platform's scheduled stop as the idle timer: it fires even if dsh is closed. The lease of this process
    follows it: the working stop (keep_alive_minutes) renews it and starts the keeper; any other stop is the idle
    stop, which releases the lease (free for others until the machine stops) and ends the renewals."""
    iid = need_instance(cfg)
    idle = at != working_stop(cfg)
    if idle and KEEPER and not from_keeper:
        KEEPER.stop()   # first, so a renewal in flight cannot land after the idle stop
    cs('instance', 'schedule', 'set', iid, '--at', at)
    if HOLDER:
        minutes = stop_minutes(at)
        if at == working_stop(cfg):
            lease_renew(iid, HOLDER, minutes)
            if not from_keeper:
                keeper_on()
        else:
            lease_release(iid, HOLDER, minutes)
    return at


def history(pid):
    d = json.load(urllib.request.urlopen(f'{comfy_base()}/history/{pid}', timeout=20))
    return d.get(pid)


def cmd_wait(args, cfg):
    deadline = time.time() + args.timeout_min * 60
    keeper_on()   # the machine is working: keep its scheduled stop ahead while we wait
    results = {}
    while time.time() < deadline:
        for pid in args.ids:
            if pid in results:
                continue
            try:
                h = history(pid)
            except (urllib.error.URLError, ConnectionError, socket.timeout):
                log('tunnel dropped, reopening')
                cmd_tunnel(argparse.Namespace(close=False), cfg)
                break
            if h and h.get('status', {}).get('completed') is not None:
                st = h['status']
                if st.get('status_str') in ('success', 'error'):
                    errs = [m[1] for m in st.get('messages', []) if m[0] == 'execution_error']
                    results[pid] = {'status': st['status_str'],
                                    'error': (errs[0].get('exception_message', '')[:600] if errs else None)}
        if len(results) == len(args.ids):
            out = {'results': results}
            if not args.keep_alive:
                idle = int(cfg.get('idle_stop_minutes') or 10)
                out['auto_stop'] = f'instance stops in {schedule_stop(cfg, f"{idle}m")} unless new work is submitted'
            return out
        log(f'{len(results)}/{len(args.ids)} done')
        time.sleep(20)
    raise ShiseiError(f'timed out; finished so far: {json.dumps(results)[:400]}')


def safe_pattern(pattern):
    """A glob relative to ComfyUI output/: letters, digits and _ . / * ? - only, no '..', not absolute."""
    if not re.fullmatch(r'[A-Za-z0-9_./*?-]+', pattern or '') or '..' in pattern or pattern.startswith('/'):
        raise ShiseiError(f'unsafe pattern: {pattern!r}')
    return pattern


def cmd_pull(args, cfg):
    pattern = safe_pattern(args.pattern)
    info = instance_info(cfg)
    # unquoted on purpose so the remote shell expands the glob; safe_pattern() allows no shell syntax
    listing = ssh_run(cfg, info, f'cd {REMOTE_COMFY}/{args.dir} && ls -1d {pattern} 2>/dev/null; true')
    names = [n for n in listing.split() if fnmatch.fnmatch(Path(n).name, Path(args.pattern).name)]
    if not names:
        raise ShiseiError(f'nothing matches {args.pattern} in {args.dir}/')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    base, target = ssh_base(cfg, info, scp=True)
    for n in names:
        scp_run(base + [f'{target}:{REMOTE_COMFY}/{args.dir}/{n}', str(out)])
    return {'pulled': [str(out / Path(n).name) for n in names]}


def cmd_down(args, cfg):
    if not args.yes:
        return {'needs_confirmation': True, 'message': 'Pull and check outputs first, then rerun with --yes.'}
    force = bool(getattr(args, 'force_other', False))   # only the page's machine-stop, after it asked
    if force and in_agent_shell():
        raise ShiseiError('stopping a machine another project is using is done by the user on the Piying page, '
                          'not from the director shell')
    iid = need_instance(cfg)
    holder = HOLDER or 'cli'
    lease_may_stop(iid, holder, force)   # refuse before anything changes
    if KEEPER:
        KEEPER.stop()
    cmd_tunnel(argparse.Namespace(close=True), cfg)
    # the lease goes only once the machine is stopped: until then nobody else may pick it as a free, running machine
    try:
        stopped_now = instance_state(cfg) != 'Stopped'   # a run stopped while waiting for stock never started it
        if stopped_now:
            cs('instance', 'stop', iid, '--yes')
            wait_state(cfg, 'Stopped', 600)
    except ShiseiError as e:
        raise StopFailed(str(e), arm_stop_after_failure(cfg))
    cur = lease_read(iid)
    if not stopped_now and (cur or {}).get('holder') == holder:
        # nothing was on; but a start answered as failed may still go through: keep an idle lease so the patrol gives
        # such a machine the idle stop instead of treating it as nobody's
        lease_release(iid, holder, int(cfg.get('idle_stop_minutes') or 10))
    else:
        try:
            cur = lease_drop(iid, holder, force=force)
        except MachineBusy as e:   # someone took it while it was stopping: their run will find it stopped
            log(str(e))
    for h in {holder, (cur or {}).get('holder')}:
        clear_stop_failed(h)
    return {'state': 'Stopped', 'stopped': stopped_now, 'note': 'only disk is billed while stopped; nothing was deleted'}


def clear_stop_failed(holder):
    """A project whose last stop failed: the machine is stopped now, so its page stops saying it is still billing."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', holder or ''):
        return
    p = HOME / 'jobs' / holder / 'progress.json'
    try:
        prog = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    if prog.get('stop_failed') and prog.get('state') != 'running':
        prog.pop('stop_failed')
        save_json_atomic(p, prog)


class StopFailed(ShiseiError):
    """The machine did not stop. `auto_stop_at`: when its scheduled stop now fires (epoch), None if even that failed."""
    def __init__(self, reason, auto_stop_at):
        self.reason, self.auto_stop_at = reason, auto_stop_at
        when = (f'it stops by itself at {time.strftime("%H:%M", time.localtime(auto_stop_at))}' if auto_stop_at
                else 'its earlier scheduled stop still applies')
        super().__init__(f'the machine did not stop ({reason[:300]}); it is still billing, {when}')


def arm_stop_after_failure(cfg):
    """A stop that failed: the scheduled stop moves to the idle time (and our lease to idle), so the machine still
    stops soon by itself. Returns when it fires, or None."""
    idle = int(cfg.get('idle_stop_minutes') or 10)
    try:
        schedule_stop(cfg, f'{idle}m')
        return time.time() + idle * 60
    except ShiseiError as e:
        log(f'could not arm the idle stop: {e}')
        return None


def stop_failed_note(e):
    """What a run writes into its progress when its stop failed (the page shows it instead of 「已停止并停机」)."""
    return {'error': e.reason[:400], 'auto_stop_at': e.auto_stop_at}


def cmd_probe(args, cfg):
    """Duration / size / fps of a local video, plus an optional filmstrip PNG (12 frames) for the segment picker."""
    if not shutil.which('ffprobe'):
        raise ShiseiError('ffprobe not found')
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=width,height,r_frame_rate:format=duration', '-of', 'json', args.src],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ShiseiError(f'not a readable video: {r.stderr.strip()[-200:]}')
    d = json.loads(r.stdout)
    st = (d.get('streams') or [{}])[0]
    num, _, den = (st.get('r_frame_rate') or '0/1').partition('/')
    fps = round(float(num) / float(den or 1), 2) if float(den or 1) else 0
    out = {'duration': round(float(d.get('format', {}).get('duration') or 0), 3), 'width': st.get('width'),
           'height': st.get('height'), 'fps': fps, 'segment_seconds': round(H3_FRAMES / FPS, 3),
           'max_seconds': SWAP_MAX_SECONDS, 'long_max_seconds': LONG_MAX_SECONDS,
           'long_shot_seconds': round(LONG_SHOT_FRAMES / FPS, 2)}
    if args.strip:
        dur = max(out['duration'], 0.1)
        r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', args.src, '-vf',
                            f'fps={12 / dur:.5f},scale=-2:120,tile=12x1', '-frames:v', '1', args.strip],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ShiseiError(f'filmstrip failed: {r.stderr.strip()[-200:]}')
        out['strip'] = args.strip
    return out


def cmd_prep(args, cfg):
    if not shutil.which('ffmpeg'):
        raise ShiseiError('ffmpeg not found')
    if getattr(args, 'long', False):   # 整段换: the whole range, cut into shots at run time
        if not args.seconds:
            raise ShiseiError('a whole-dance clip needs --seconds')
        frames = long_frames(args.seconds)
    else:
        frames = swap_frames(args.seconds) if args.seconds else H3_FRAMES
    keep = getattr(args, 'keep_frame', False)
    r = subprocess.run(ffmpeg_keep_frame_args(args.src, args.out, args.start, frames) if keep
                       else ffmpeg_prep_args(args.src, args.out, args.start, args.crop_x, frames), capture_output=True, text=True)
    if r.returncode != 0:
        raise ShiseiError(f'ffmpeg failed: {r.stderr.strip()[-400:]}')
    got = video_frames(args.out)
    if got != frames:
        raise ShiseiError(f'the source has only {got} frames from {args.start:g} s on; {frames} are needed. '
                          'Move the start earlier or pick a shorter clip')
    out = {'video': args.out, 'frames': frames, 'seconds': round(frames / FPS, 2), 'fps': FPS,
           'size': video_size(args.out) if keep else '864x1152'}
    if is_long(frames):
        out['shots'] = [list(x) for x in long_shots(frames)]
    return out


def video_size(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height',
                        '-of', 'csv=p=0:s=x', str(path)], capture_output=True, text=True)
    return r.stdout.strip() or None


def video_frames(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets', '-show_entries',
                        'stream=nb_read_packets', '-of', 'csv=p=0', str(path)], capture_output=True, text=True)
    try:
        return int(r.stdout.strip().split(',')[0])
    except ValueError:
        raise ShiseiError(f'cannot count frames of {path}')


def cmd_composite(args, cfg):
    if not shutil.which('ffmpeg'):
        raise ShiseiError('ffmpeg not found')
    for f in (args.source, args.mannequin, args.character):
        if not Path(f).is_file():
            raise ShiseiError(f'not a file: {f}')
    r = subprocess.run(composite_args(ASSETS / 'composite' / 'chrome.png', args.source, args.mannequin,
                                      args.character, args.out, frames=video_frames(args.source), loops=args.loops),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ShiseiError(f'ffmpeg failed: {r.stderr.strip()[-400:]}')
    return {'video': args.out, 'size': '1920x1080', 'frames': video_frames(args.out)}


REMOTE_WORK = '/root/shisei'


def remote_mannequin_script(glb_name, out_name, head, shot, engine):
    """Shell run on the instance: render frames with Blender, encode with ffmpeg, into ComfyUI input/."""
    frames = f'{REMOTE_WORK}/frames_{Path(out_name).stem}'
    env = f'SHISEI_ENGINE={engine} ' if engine else ''
    return (f'set -e; rm -rf {frames}; mkdir -p {frames}; '
            f'{env}blender -b --python {REMOTE_WORK}/studio_scene.py -- {REMOTE_WORK}/{glb_name} {frames} '
            f'864 1152 all -25 0 0 {head} {shot} > {frames}.log 2>&1; '
            f"ffmpeg -v error -y -framerate {FPS} -pattern_type glob -i '{frames}/f_*.png' "
            f'-c:v libx264 -pix_fmt yuv420p -crf 14 {REMOTE_COMFY}/input/{out_name}; '
            f'ls {frames} | wc -l')


def cmd_mannequin(args, cfg):
    if args.remote:
        for v in (args.glb, args.out):
            if not re.fullmatch(r'[A-Za-z0-9_./-]+', v):
                raise ShiseiError(f'unsafe name: {v!r}')
        info = instance_info(cfg)
        base, target = ssh_base(cfg, info, scp=True)
        ssh_run(cfg, info, f'mkdir -p {REMOTE_WORK}')
        for f in (ASSETS / 'blender' / 'studio_scene.py', Path(args.glb)):
            scp_run(base + [str(f), f'{target}:{REMOTE_WORK}/'])
        glb, out = Path(args.glb).name, Path(args.out).name
        errors = []
        for engine in ('', 'CYCLES'):   # EEVEE first; Cycles+CUDA if no GPU display context
            try:
                n = ssh_run(cfg, info, remote_mannequin_script(glb, out, args.head, args.shot, engine),
                            timeout=int(getattr(args, 'timeout', None) or 1800))
                return {'video': f'input/{out}', 'frames': int(n.strip().splitlines()[-1]), 'engine': engine or 'EEVEE',
                        'note': 'rendered on the instance into ComfyUI input/, ready for submit swap'}
            except ShiseiError as e:
                errors.append(f'{engine or "EEVEE"}: {e}')
        raise ShiseiError('remote render failed: ' + ' | '.join(errors))
    blender = shutil.which('blender') or '/Applications/Blender.app/Contents/MacOS/Blender'
    if not Path(blender).exists():
        raise ShiseiError('Blender not found (GPU-side rendering is not built yet)')
    frames = Path(args.out).with_suffix('')
    frames.mkdir(parents=True, exist_ok=True)
    # curves (body reshaping) is deliberately fixed at 0 here
    subprocess.run([blender, '-b', '--python', str(ASSETS / 'blender' / 'studio_scene.py'), '--',
                    args.glb, str(frames), '864', '1152', 'all', '-25', '0', '0', args.head, args.shot],
                   check=True, capture_output=True)
    subprocess.run(['ffmpeg', '-y', '-framerate', str(FPS), '-pattern_type', 'glob', '-i', str(frames / 'f_*.png'),
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '14', args.out], check=True, capture_output=True)
    return {'video': args.out}


# ---------------------------------------------------------------- job runner (GUI pipeline)

JOB_STAGES = [('boot', '开机'), ('secure', '安全检查'), ('upload', '上传素材'), ('mocap', '动捕'),
              ('mannequin', '白模渲染'), ('swap', '换人'), ('composite', '三栏成片'), ('finish', '收尾')]


JOB_OUTPUTS = {'mocap': 'motion.glb', 'mannequin': 'mannequin.mp4', 'composite': 'showcase.mp4'}


class StopRequested(Exception):
    pass


class JobRun:
    """Progress record for one job folder. Every stage is resumable: finished artifacts are skipped."""

    def __init__(self, jobdir):
        self.dir = Path(jobdir)
        self.job = json.loads((self.dir / 'job.json').read_text())
        self.id = self.job['id']
        p = self.dir / 'progress.json'
        self.p = json.loads(p.read_text()) if p.exists() else {}
        self.p.setdefault('stages', {k: {'status': 'pending'} for k, _ in JOB_STAGES})
        self.p.update(state='running', pid=os.getpid(), error=None, updated_at=time.time())
        self.p.pop('queue', None)
        self.p.setdefault('log', [])
        for k, _ in JOB_STAGES:
            st = self.p['stages'][k]
            # a stage whose result file is gone runs again, so it is not shown as done
            if st['status'] in ('running', 'failed', 'waiting', 'queued') or \
                    (st['status'] == 'done' and k in JOB_OUTPUTS and not (self.dir / JOB_OUTPUTS[k]).exists()):
                st['status'] = 'pending'
        self.current = None
        self.save()

    def save(self):
        self.p['updated_at'] = time.time()
        tmp = self.dir / 'progress.json.tmp'
        tmp.write_text(json.dumps(self.p, ensure_ascii=False, indent=1))
        tmp.replace(self.dir / 'progress.json')

    def note(self, msg):
        self.p['log'] = (self.p['log'] + [f'{time.strftime("%H:%M:%S")} {msg}'])[-40:]
        if self.current == 'boot' and 'retrying in 120 s' in msg:
            self.stage('boot', 'waiting', '4090 48G 暂时没货，每 2 分钟重试一次，等待期间不计费', retry_at=time.time() + 120)
        elif self.current == 'boot' and msg.startswith('starting the instance'):
            self.stage('boot', 'running', '开机中', retry_at=None)
        elif self.current == 'boot' and 'GPU unusable' in msg:
            self.stage('boot', 'running', '显卡自检失败，停机后换一台')
        else:
            self.save()

    def stage(self, key, status, detail=None, **extra):
        st = self.p['stages'][key]
        if status == 'running' and st.get('status') != 'running':
            st['started_at'] = time.time()
        if status in ('done', 'failed', 'skipped'):
            st['ended_at'] = time.time()
        st['status'] = status
        if detail is not None:
            st['detail'] = detail
        st.update(extra)
        self.current = key if status in ('running', 'waiting') else self.current
        self.save()

    def check_stop(self):
        if (self.dir / 'stop').exists():
            raise StopRequested()

    def remote(self, name):
        return f'{self.id}_{name}'


def _upload_as(cfg, info, local, remote_name):
    base, target = ssh_base(cfg, info, scp=True)
    scp_run(base + [str(local), f'{target}:{REMOTE_COMFY}/input/{remote_name}'])


def _pull_one(cfg, info, remote_rel, local):
    base, target = ssh_base(cfg, info, scp=True)
    scp_run(base + [f'{target}:{REMOTE_COMFY}/{remote_rel}', str(local)])


def _remote_ls(cfg, info, folder, pattern):
    pattern = safe_pattern(pattern)
    out = ssh_run(cfg, info, f'cd {REMOTE_COMFY}/{folder} && ls -1d {pattern} 2>/dev/null; true')
    return [n for n in out.split() if n]


def _wait_prompt(run, cfg, pid, label, timeout_min=60):
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        run.check_stop()
        try:
            h = history(pid)
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            log(f'{label}: tunnel dropped, reopening')
            cmd_tunnel(argparse.Namespace(close=False), cfg)
            continue
        if h and h.get('status', {}).get('status_str') in ('success', 'error'):
            st = h['status']
            if st['status_str'] == 'error':
                errs = [m[1] for m in st.get('messages', []) if m[0] == 'execution_error']
                raise ShiseiError(f'{label} failed: ' + (errs[0].get('exception_message', '')[:400] if errs else 'unknown error'))
            return h
        time.sleep(10)
    raise ShiseiError(f'{label} timed out after {timeout_min} min')


def _thumb(video, png, at=2.0):
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(at), '-i', str(video), '-frames:v', '1', '-vf', 'scale=-2:360', str(png)],
                   capture_output=True)


SWAP_FIRST_SEED = 1001


def job_seeds(params):
    """The take seeds of a dance-swap job, in order. A new job has `seeds`, a count (1001, 1002, …). Asking for more
    takes on the result page writes `takes`, the whole list, so earlier takes keep their numbers and files."""
    takes = params.get('takes')
    if takes:
        return [int(x) for x in takes]
    return [SWAP_FIRST_SEED + i for i in range(int(params.get('seeds') or 3))]


def job_todo(d, job):
    """What job-run would make in this folder: the steps whose result files are missing, and the takes not made yet."""
    d = Path(d)
    missing = [s for s in job_seeds(job.get('params') or {}) if not (d / f'seed_{s}.mp4').exists()]
    if swap_keep_bg(job.get('params')):   # swapped on the source clip: there is no motion capture or white model to make
        return {'mocap': False, 'mannequin': False, 'seeds': missing, 'composite': bool(missing) or not (d / 'showcase.mp4').exists()}
    return {'mocap': not (d / 'motion.glb').exists(), 'mannequin': not (d / 'mannequin.mp4').exists(), 'seeds': missing,
            'composite': bool(missing) or not (d / 'showcase.mp4').exists()}


def stored_shots(progress, ledger, todo, machine_id):
    """Shots of a stopped long take that a resume on `machine_id` skips: the take is still to make, and its stored shots
    are on that machine's disk (the last run was there). Another machine makes them again."""
    lg = (progress or {}).get('long') or {}
    # a record from before takes were noted: the take not made yet (a failed resume leaves it pending, not running)
    seed = lg.get('seed') or next((k for k, v in ((progress or {}).get('seeds') or {}).items() if v != 'done'), None)
    done, shots = int(lg.get('done') or 0), int(lg.get('shots') or 0)
    if seed is None or str(seed) not in {str(s) for s in todo.get('seeds') or []} or not 0 < done < shots:
        return 0
    if not machine_id or not ledger or ledger[-1].get('instance') != machine_id:
        return 0
    return done


def job_estimate(job, todo, price, running, stored=0):
    """Minutes and cost of the next job-run for this folder: only what it will make (see job_todo)."""
    frames = (job.get('params') or {}).get('frames') or H3_FRAMES
    if not todo['seeds']:
        # every take is here: at most the three-panel cut is missing, made locally without the GPU
        return {'seeds': 0, 'frames': int(frames), 'seconds': round(frames / FPS, 2), 'minutes': 0, 'price_per_hour': price,
                'cost': 0.0, 'nothing': not todo.get('composite'), 'local': bool(todo.get('composite'))}
    return estimate(len(todo['seeds']), price, running=running, frames=frames, mocap=todo['mocap'], mannequin=todo['mannequin'],
                    stored=stored)


def swap_backfill_entry(progress):
    """A ledger line for a dance-swap run from before job-run kept a ledger, worked out from its progress.json: billed
    from billing.since to the last finished step. None when the run never billed."""
    b = (progress or {}).get('billing') or {}
    ends = [st.get('ended_at') for st in ((progress or {}).get('stages') or {}).values() if isinstance(st, dict) and st.get('ended_at')]
    try:
        since, price = float(b.get('since')), float(b.get('price_per_hour'))
    except (TypeError, ValueError):
        return None
    if not ends or not price or max(ends) <= since:
        return None
    made = sum(1 for v in (progress.get('seeds') or {}).values() if v == 'done')
    state = progress.get('state') if progress.get('state') in ('done', 'failed', 'stopped', 'alert') else 'failed'
    return {**ledger_entry('swap', made, since, max(ends), price, state), 'backfilled': True}


def backfill_job_ledger(d):
    """Write ledger.json for a dance-swap job that ran before ledgers existed. Never touches an existing ledger."""
    d = Path(d)
    if (d / 'ledger.json').exists() or not (d / 'progress.json').exists():
        return None
    try:
        progress = json.loads((d / 'progress.json').read_text())
    except json.JSONDecodeError:
        return None
    if progress.get('state') == 'running' and progress.get('pid') and pid_alive(progress['pid']):
        return None   # a run in progress writes its own line when it ends
    entry = swap_backfill_entry(progress)
    save_json_atomic(d / 'ledger.json', [entry] if entry else [])
    return entry


def _composite_local(d, job, frames):
    """The three-panel cut from takes already here (no GPU): the picked take, else the first."""
    seeds = [s for s in job_seeds(job.get('params') or {}) if (d / f'seed_{s}.mp4').exists()]
    pick = str(job.get('pick') or seeds[0])
    r = subprocess.run(job_composite_args(d, job, pick, frames), capture_output=True, text=True)
    if r.returncode != 0:
        raise ShiseiError(f'composite failed: {r.stderr[-300:]}')
    _thumb(d / 'dance.mp4', d / 'thumb_source.png')


def cmd_job_pick(args, cfg):
    """Remake the showcase cut of a dance-swap job with take `seed` (the result page's 「用第 n 条」)."""
    d = Path(args.jobdir)
    job = json.loads((d / 'job.json').read_text())
    if not (d / f'seed_{args.seed}.mp4').exists():
        raise ShiseiError(f'take {args.seed} is not here')
    frames = video_frames(d / 'dance.mp4')
    r = subprocess.run(job_composite_args(d, job, args.seed, frames), capture_output=True, text=True)
    if r.returncode != 0:
        raise ShiseiError(f'composite failed: {r.stderr[-300:]}')
    return {'video': str(d / 'showcase.mp4'), 'pick': args.seed}


def _fail_before_run(d, err):
    """A refusal before the run record exists (price, cap, compshare unreachable, a queue that ran out) still reaches
    the page. A MachineBusy one started nothing on any machine: `refused`, so the page offers no stop and bills nothing."""
    p = Path(d) / 'progress.json'
    try:
        pr = json.loads(p.read_text()) if p.exists() else {}
    except json.JSONDecodeError:
        pr = {}
    pr.setdefault('stages', {k: {'status': 'pending'} for k, _ in JOB_STAGES})
    if (pr['stages'].get('boot') or {}).get('status') == 'queued':
        pr['stages']['boot'] = {'status': 'pending'}
    pr.pop('queue', None)
    pr.update(state='failed', error=str(err)[:600], pid=None, updated_at=time.time())
    if isinstance(err, MachineBusy):
        pr['refused'] = True
    else:
        pr.pop('refused', None)
    save_json_atomic(p, pr)
    _set_job_state(d, 'failed')


def _set_job_state(d, state):
    p = Path(d) / 'job.json'
    try:
        job = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if job.get('state') != state:
        save_json_atomic(p, {**job, 'state': state})


def job_run_reset(p, d, seeds):
    """A new run's starting record. Earlier takes stay listed even when the seed list changed; a take a killed run
    left at running is pending again unless its file is here. Every step runs again except motion capture and the
    white model whose files are here: the stage list shows this run, not the last one's times."""
    old = p.get('seeds') or {}
    merged = {**old, **{str(s): old.get(str(s), 'pending') for s in seeds}}
    for s, st in merged.items():
        if st == 'running':
            merged[s] = 'done' if (Path(d) / f'seed_{s}.mp4').exists() else 'pending'
    p['seeds'] = merged
    # a new run: what the last one ended as (refused before it got a machine, a cancelled wait) says nothing about this one
    for key in ('billing', 'auto_stop_at', 'alert', 'refused', 'cancelled', 'queue'):
        p.pop(key, None)
    S = p['stages']
    for name, _ in JOB_STAGES:
        if not (name in ('mocap', 'mannequin') and S.get(name, {}).get('status') == 'done'
                and (Path(d) / JOB_OUTPUTS[name]).exists()):
            S[name] = {'status': 'pending'}
    return p


def job_swap_api(job, seed, video, ref, ref2, prefix, frames):
    """The swap workflow for one take of a dance-swap job. Studio jobs replace the white model (the mannequin prompt);
    keep-background jobs replace the person or people on the source clip in KEEP_BG_JOB_MODE, the same call as
    `submit swap --keep-bg`. Each take uses the description(s) it was asked with."""
    if swap_keep_bg(job.get('params')):
        return patch_swap_keep_bg(load_workflow('swap_studio'), video, ref, ref2, keep_bg_prompt(swap_take_people(job, seed)),
                                  seed, prefix, KEEP_BG_JOB_MODE, frames)
    asked = (job.get('seedPrompts') or {}).get(str(seed), job.get('prompt') or '')
    return patch_swap(load_workflow('swap_studio'), video, ref, ref2, build_prompt('character', asked if isinstance(asked, str) else ''),
                      seed, prefix, frames=frames)


def _queue(cfg, d, minutes, show, explicit):
    """wait_for_machine for a run in project folder `d`: the stop file cancels it (nothing started, nothing billed)."""
    global STOP_CHECK

    def check():
        if (Path(d) / 'stop').exists():
            raise StopRequested()
    STOP_CHECK = check
    try:
        return wait_for_machine(cfg, minutes, show, explicit)
    finally:
        STOP_CHECK = None


def queue_progress(pr, q, now=None):
    """A swap run's progress while it waits for a machine (pure): running, the boot stage `queued`, who holds what."""
    pr = dict(pr or {})
    pr.setdefault('stages', {k: {'status': 'pending'} for k, _ in JOB_STAGES})
    pr['stages'] = {**pr['stages'], 'boot': {'status': 'queued', 'started_at': q['since']}}
    pr.update(state='running', pid=os.getpid(), error=None, refused=None, cancelled=None, queue=q,
              updated_at=time.time() if now is None else now)
    return pr


def _job_queue(d, args, cfg):
    """Queue a swap run until a machine is free. None when it got one (its lease is taken); else what the run returns."""
    p = Path(d) / 'progress.json'

    def show(q):
        try:
            pr = json.loads(p.read_text()) if p.exists() else {}
        except (OSError, ValueError):
            pr = {}
        save_json_atomic(p, queue_progress(pr, q))
        _set_job_state(d, 'running')
    try:
        _queue(cfg, d, args.queue_minutes, show, getattr(args, 'machine', None) or os.environ.get('SHISEI_MACHINE') or None)
        return None
    except StopRequested:
        pr = json.loads(p.read_text()) if p.exists() else {}
        pr.setdefault('stages', {k: {'status': 'pending'} for k, _ in JOB_STAGES})
        pr['stages']['boot'] = {'status': 'pending'}
        pr.pop('queue', None)
        pr.update(state='stopped', cancelled=True, pid=None, error=None, updated_at=time.time())
        save_json_atomic(p, pr)
        (Path(d) / 'stop').unlink(missing_ok=True)
        _set_job_state(d, 'stopped')
        return {'state': 'stopped', 'cancelled': True}
    except MachineBusy as e:
        _fail_before_run(d, e)
        raise ShiseiError(str(e))
def long_progress(p, shots, made=None, seed=None):
    """progress.json's record of a long take: how many shots there are and how many are stored on the machine. Each
    prompt of the queue loop stores one shot; the count is never shown as complete before the joined video is here.
    The count belongs to one take: another take (再拍一条) starts from the first shot, a resumed one keeps its count."""
    lg = p.setdefault('long', {})
    # a record from before takes were noted (no seed) is kept: it can only be the take being resumed
    if lg.get('shots') != shots or (seed is not None and lg.get('seed') not in (None, seed)):
        lg.clear()
    lg['shots'] = shots
    if seed is not None:
        lg['seed'] = seed
    lg['done'] = shots if made == 'all' else min(shots - 1, int(lg.get('done') or 0) + (1 if made else 0))
    return lg


def long_detail(lg):
    if lg['done'] >= lg['shots']:
        return f'{lg["shots"]} 段都拍好了，拼成一条'
    return f'第 {lg["done"] + 1} 段 / 共 {lg["shots"]} 段'


def _swap_long(run, cfg, info, api, seed, frames):
    """One take of a long clip: the same prompt queued again and again, each run storing the next shot on the machine,
    until the one that stores the last shot also writes the joined video. Shots stored by an earlier run of this job on
    the same machine are skipped by the planner itself."""
    shots = len(long_shots(frames))
    prefix = f'{run.id}_s{seed}'
    def videos():
        return [n for n in _remote_ls(cfg, info, 'output', f'{prefix}_0*.mp4') if 'audio' not in n]
    before = set(videos())
    lg = long_progress(run.p, shots, seed=seed)
    for _ in range(shots + 1):
        run.stage('swap', 'running', long_detail(lg))
        run.check_stop()
        log(f'seed {seed}: shot {min(lg["done"] + 1, shots)} of {shots} started')   # the page's latest log line
        pid = post_prompt(api)
        schedule_stop(cfg, working_stop(cfg))
        _wait_prompt(run, cfg, pid, f'seed {seed} shot {lg["done"] + 1}', int(30 * max(1, LONG_SHOT_FRAMES / H3_FRAMES)))
        new = [n for n in videos() if n not in before]
        if new:
            long_progress(run.p, shots, 'all', seed)
            run.stage('swap', 'running', long_detail(run.p['long']))
            return new[-1]
        lg = long_progress(run.p, shots, True, seed)
        log(f'seed {seed}: shot {lg["done"]} of {shots} stored on the machine')
        run.save()
    raise ShiseiError(f'{shots} shots were queued but the joined video never appeared')


def cmd_job_run(args, cfg):
    global LOG_HOOK, STOP_CHECK
    d0 = Path(args.jobdir)
    backfill_job_ledger(d0)   # a run from before ledgers keeps its line before this run's is added
    job0 = json.loads((d0 / 'job.json').read_text())
    todo = job_todo(d0, job0)
    if not todo['seeds']:
        if not todo['composite']:
            raise ShiseiError('every take of this job is made already; ask for more takes on the result page first')
        # stopped after the last take: only the three-panel cut is missing, and it needs no GPU
        _composite_local(d0, job0, check_clip_frames((job0.get('params') or {}).get('frames') or H3_FRAMES))
        pr = json.loads((d0 / 'progress.json').read_text()) if (d0 / 'progress.json').exists() else {'stages': {}}
        pr['stages']['composite'] = {'status': 'done', 'detail': '1920×1080', 'ended_at': time.time()}
        pr.update(state='done', pid=None, error=None, updated_at=time.time())
        save_json_atomic(d0 / 'progress.json', pr)
        _set_job_state(d0, 'done')
        return {'state': 'done', 'local': True}
    if not args.confirm_billing:
        raise ShiseiError('billing was not confirmed: tell the user the price (job-estimate) and let them start it')
    queued = getattr(args, 'queue_minutes', None)
    if queued:
        out = _job_queue(d0, args, cfg)
        if out:
            return out
    try:
        before = instance_info(cfg)
        was_running = before.get('State') == 'Running'
        price = float(before.get('InstancePrice') or 0)
        # the cap is checked with the numbers job-estimate shows, before the machine starts
        pr0 = json.loads((d0 / 'progress.json').read_text()) if (d0 / 'progress.json').exists() else {}
        stored = stored_shots(pr0, load_ledger(d0), todo, before.get('UHostId'))
        amount = round(job_estimate(job0, todo, price, was_running, stored)['cost'] + idle_cost(cfg, price)[1], 2)
        if queued:
            max_cost_check(amount, getattr(args, 'max_cost', None))
        budget_check(budget_state(job0, ledger_if_started(load_ledger(d0), time.time(), was_running)), amount, swap=True)
    except ShiseiError as e:
        _fail_before_run(d0, e)
        raise
    run = JobRun(args.jobdir)
    run_started = time.time()
    LOG_HOOK = run.note
    STOP_CHECK = run.check_stop   # a stop while waiting for stock ends the wait (nothing started)
    if was_running:   # the idle stop our last run armed ends here: charge only the gap
        settle_idle(d0, run_started)
    job, d, S = run.job, run.dir, run.p['stages']
    params = job.get('params') or {}
    seeds = job_seeds(params)
    frames = check_clip_frames(params.get('frames') or H3_FRAMES)
    k = frames / H3_FRAMES   # timeouts were set for 107-frame clips
    keep_bg = swap_keep_bg(params)
    long_clip = is_long(frames)
    job_run_reset(run.p, d, seeds)
    run.save()
    made = []
    try:
        if not (d / 'dance.mp4').exists():
            raise ShiseiError('no prepared clip (dance.mp4); finish the footage check first')
        if video_frames(d / 'dance.mp4') != frames:
            raise ShiseiError(f'dance.mp4 is not {frames} frames; redo the footage check')
        # 1-2. boot + security check (cmd_up does both)
        run.stage('boot', 'running', '开机中')
        up = cmd_up(argparse.Namespace(confirm_billing=args.confirm_billing, max_wait_min=60, gpu_attempts=3), cfg)
        if up.get('needs_confirmation'):
            raise ShiseiError('billing was not confirmed')
        info = instance_info(cfg)
        # this run's own billing: a later run on the same job starts its own count (the ledger keeps the earlier ones)
        run.p['billing'] = {'since': billing_start(run_started, was_running, info.get('StartTime')),
                            'price_per_hour': info.get('InstancePrice')}
        run.stage('boot', 'done', f'{info.get("GpuType")} · {info.get("Zone")}')
        sec = up.get('secure') or {}
        if up.get('alert'):
            run.p['alert'] = sec
            run.stage('secure', 'failed', '发现不是本插件提交的任务或可疑文件')
            run.p['state'] = 'alert'
            return {'state': 'alert', 'secure': sec}
        run.stage('secure', 'done', '只监听 127.0.0.1 · 公网端口已关 · 没有陌生任务')
        vram = vram_watch_start(cfg, info)
        run.check_stop()
        # 3. upload (a white-model video made in an earlier run goes up again: the instance may not have it any more)
        run.stage('upload', 'running', '上传舞蹈片段和参考图')
        cmd_tunnel(argparse.Namespace(close=False), cfg)
        files = job.get('files') or {}
        # the second picture: the face close-up of the one character, or the right-hand person's full body
        second = 'front2' if swap_people(params) == 2 else 'face'
        if not files.get('front') or not files.get(second):
            raise ShiseiError('a reference picture is missing; add it on the Piying page first')
        refs = ['front' + Path(files['front']).suffix, second + Path(files[second]).suffix]
        ups = [(d / 'dance.mp4', 'dance.mp4'), (d / files['front'], refs[0]), (d / files[second], refs[1])]
        if not keep_bg and (d / 'mannequin.mp4').exists():
            ups.append((d / 'mannequin.mp4', 'mq.mp4'))
        for local, name in ups:
            # a long clip's stored shots belong to the uploaded video (its path and date): a resumed run keeps the
            # one already on the machine, so the shots made before the stop are not made again
            if long_clip and name in ('dance.mp4', 'mq.mp4') and _remote_ls(cfg, info, 'input', run.remote(name)):
                continue
            _upload_as(cfg, info, local, run.remote(name))
        run.stage('upload', 'done', '视频 1 段 · 参考图 2 张' + (' · 白模' if len(ups) > 3 else ''))
        if keep_bg:   # swapped on the source clip itself
            run.stage('mocap', 'skipped', '保留原背景：不需要')
            run.stage('mannequin', 'skipped', '保留原背景：不需要')
        # 4. mocap
        if keep_bg:
            pass
        elif not (d / 'motion.glb').exists():
            run.stage('mocap', 'running', 'SAM3D Body → 动画 GLB')
            pid = post_prompt(patch_mocap(load_workflow('mocap_glb'), run.remote('dance.mp4'), run.id, frames))
            _wait_prompt(run, cfg, pid, 'mocap', int(20 * max(1, k)))
            names = _remote_ls(cfg, info, 'output', f'3d/{run.id}*.glb')
            if not names:
                raise ShiseiError('mocap finished but no GLB found')
            _pull_one(cfg, info, f'output/{names[-1]}', d / 'motion.glb')
            run.stage('mocap', 'done', 'SAM3D Body → 动画 GLB')
        elif S['mocap'].get('status') != 'done':
            run.stage('mocap', 'done', '用上次的结果')
        run.check_stop()
        # 5. mannequin (on the instance when Blender is there, else locally)
        if keep_bg:
            pass
        elif not (d / 'mannequin.mp4').exists():
            run.stage('mannequin', 'running', f'Blender 影棚 · {"中性脸" if params.get("head") != "egg" else "蛋头"} · {"膝上跟拍" if params.get("shot") != "full" else "全身"}')
            try:
                r = cmd_mannequin(argparse.Namespace(glb=str(d / 'motion.glb'), out=run.remote('mq.mp4'), head=params.get('head') or 'face',
                                                     shot=params.get('shot') or 'close', remote=True,
                                                     timeout=int(1800 * max(1, k))), cfg)
                _pull_one(cfg, info, f'input/{run.remote("mq.mp4")}', d / 'mannequin.mp4')
                engine = r.get('engine')
            except ShiseiError as e:
                log(f'remote render failed ({e}); rendering locally')
                cmd_mannequin(argparse.Namespace(glb=str(d / 'motion.glb'), out=str(d / 'mannequin.mp4'), head=params.get('head') or 'face',
                                                 shot=params.get('shot') or 'close', remote=False), cfg)
                _upload_as(cfg, info, d / 'mannequin.mp4', run.remote('mq.mp4'))
                engine = 'local'
            got = video_frames(d / 'mannequin.mp4')
            if got != frames:
                (d / 'mannequin.mp4').rename(d / 'mannequin.bad.mp4')
                raise ShiseiError(f'the mannequin video has {got} frames, the clip has {frames}; motion capture did not cover the whole clip')
            _thumb(d / 'mannequin.mp4', d / 'thumb_mannequin.png')
            run.stage('mannequin', 'done', f'{frames} 帧 · {engine}')
        elif S['mannequin'].get('status') != 'done':
            run.stage('mannequin', 'done', '用上次的结果')
        run.check_stop()
        # 6. swap, one seed at a time so finished seeds are pulled early; each take uses the description it was asked with
        todo_now = [s for s in seeds if not (d / f'seed_{s}.mp4').exists()]
        run.stage('swap', 'running', f'H3 Ref2VA + 换人 LoRA · 拍 {len(todo_now)} 条')
        for s in seeds:
            if (d / f'seed_{s}.mp4').exists():
                run.p['seeds'][str(s)] = 'done'
                continue
            run.p['seeds'][str(s)] = 'running'
            run.save()
            api = job_swap_api(job, s, run.remote('dance.mp4' if keep_bg else 'mq.mp4'), run.remote(refs[0]), run.remote(refs[1]),
                               f'{run.id}_s{s}', frames)
            if long_clip:
                outs = [_swap_long(run, cfg, info, api, s, frames)]
            else:
                pid = post_prompt(api)
                schedule_stop(cfg, working_stop(cfg))
                _wait_prompt(run, cfg, pid, f'seed {s}', int(30 * max(1, k)))
                outs = [n for n in _remote_ls(cfg, info, 'output', f'{run.id}_s{s}_0*.mp4') if 'audio' not in n]
            if not outs:
                raise ShiseiError(f'seed {s} finished but no video found')
            _pull_one(cfg, info, f'output/{outs[-1]}', d / f'seed_{s}.mp4')
            _thumb(d / f'seed_{s}.mp4', d / f'thumb_seed_{s}.png')
            run.p['seeds'][str(s)] = 'done'
            made.append(s)
            run.save()
            run.check_stop()
        run.stage('swap', 'done', f'拍了 {len(made)} 条' if len(made) < len(seeds) else f'{len(seeds)} 条都完成了')
        vram_watch_collect(cfg, info, vram, 'swap', {'takes': len(made), 'frames': frames})
        # 7. composite with the picked take (the first one until the user picks)
        run.stage('composite', 'running', '原视频 | 角色' if keep_bg else '原视频 | 白模 | 角色')
        pick = str(job.get('pick') or seeds[0])
        r = subprocess.run(job_composite_args(d, job, pick, frames), capture_output=True, text=True)
        if r.returncode != 0:
            raise ShiseiError(f'composite failed: {r.stderr[-300:]}')
        _thumb(d / 'dance.mp4', d / 'thumb_source.png')
        run.stage('composite', 'done', '1920×1080')
        # 8. finish: arm the idle stop, close the tunnel
        idle = int(cfg.get('idle_stop_minutes') or 10)
        schedule_stop(cfg, f'{idle}m')
        cmd_tunnel(argparse.Namespace(close=True), cfg)
        run.stage('finish', 'done', f'空闲 {idle} 分钟后自动停机')
        run.p['state'] = 'done'
        run.p['auto_stop_at'] = time.time() + idle * 60
        run.p['billing']['ended'] = time.time()
        result = {'state': 'done'}
    except StopRequested:
        if run.current:
            run.stage(run.current, 'skipped', '已停止')
        try:
            cmd_down(argparse.Namespace(yes=True), cfg)
        except StopFailed as e:
            log(f'stop failed: {e}')
            run.p['stop_failed'] = stop_failed_note(e)
        except ShiseiError as e:
            log(f'stop failed: {e}')
            run.p['stop_failed'] = {'error': str(e)[:400], 'auto_stop_at': None}
        run.p['cancelled'] = not UP_STARTED and not run.p.get('stop_failed')   # stopped while waiting: nothing ran
        (d / 'stop').unlink(missing_ok=True)
        run.p['state'] = 'stopped'
        return {'state': 'stopped'}
    except MachineBusy as e:
        # another project holds the machine: leave its stop time and tunnel alone, and book nothing (as in drama-run)
        if run.current:
            run.stage(run.current, 'failed', str(e)[:400])
        run.p.update(state='failed', error=str(e)[:600], refused=True)
        raise ShiseiError(str(e))
    except (ShiseiError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError) as e:
        if run.current:
            run.stage(run.current, 'failed', str(e)[:400])
        run.p['state'] = 'failed'
        run.p['error'] = str(e)[:600]
        arm_idle_stop(cfg)
        raise ShiseiError(str(e))
    finally:
        run.p['pid'] = None
        if run.p.get('state') == 'running':
            run.p['state'] = 'failed'
        # one ledger line per run, whatever way it ended (same rule as drama-run)
        since = (run.p.get('billing') or {}).get('since')
        if since is None and not run.p.get('refused') and not never_claimed():   # failed inside `up`: the machine may be on all the same
            try:
                now = instance_info(cfg)
                if now.get('State') == 'Running':
                    since = billing_start(run_started, was_running, now.get('StartTime'))
            except (ShiseiError, subprocess.CalledProcessError, KeyError):
                pass
        if since is not None:
            ended = (run.p.get('billing') or {}).get('ended') or time.time()
            # every other end arms the idle stop, and so does a stop that failed
            idle = 0 if run.p['state'] == 'stopped' and not run.p.get('stop_failed') else idle_cost(cfg, price)[0]
            append_ledger(d, ledger_entry('swap', len(made), since, ended, price, run.p['state'], idle))
        run.save()
        _set_job_state(d, run.p['state'])
        LOG_HOOK = None
        STOP_CHECK = None
    # after the ledger line and the job state are written: the image model's look at the new takes (minutes per take)
    # is advice next to each take, it never picks one
    try:
        cmd_job_precheck(argparse.Namespace(jobdir=str(d), timeout=420, force=False), cfg)
    except (ShiseiError, subprocess.CalledProcessError, OSError) as e:
        log(f'take pre-check skipped: {e}')
    return result


def cmd_job_estimate(args, cfg):
    """What the next job-run would make, take and cost, against the job's cap. Read-only; prices come from here only."""
    d = Path(args.jobdir)
    job = json.loads((d / 'job.json').read_text())
    price, running, machine = args.price, args.running, None
    if price is None:
        info = instance_info(cfg)
        # queued (every machine busy): priced as a start from cold, the most the run costs once one comes free
        price, running = float(info.get('InstancePrice') or 0), running or (info.get('State') == 'Running' and not QUOTE_QUEUED)
        machine = {'id': info.get('UHostId'), 'name': info.get('Name'), 'gpu': info.get('GpuType'), 'zone': info.get('Zone'),
                   'state': info.get('State')}
    todo = job_todo(d, job)
    if args.more:   # priced before the result page writes the new takes into job.json
        seeds = job_seeds(job.get('params') or {})
        todo['seeds'] = todo['seeds'] + [max(seeds) + 1 + i for i in range(args.more)]
    backfill_job_ledger(d)
    ledger = load_ledger(d)
    budget = budget_state(job, ledger_if_started(ledger, time.time(), running))
    pr = json.loads((d / 'progress.json').read_text()) if (d / 'progress.json').exists() else {}
    stored = 0 if args.more else stored_shots(pr, ledger, todo, (machine or {}).get('id'))
    est = job_estimate(job, todo, price, running, stored)
    # the idle stop the run arms is billed too and job-run checks the cap with it: the same numbers here
    idle_min, idle_yuan = idle_cost(cfg, price) if todo['seeds'] else (0, 0.0)
    return {**est, 'todo': todo, 'running': bool(running), 'machine': machine, 'queued': QUOTE_QUEUED,
            'idle_minutes': idle_min, 'idle_cost': idle_yuan,
            'budget': {**budget, 'over': over_budget(budget, est['cost'] + idle_yuan)}}


def cmd_job_ledger(args, cfg):
    """This job's ledger and cap; a job that ran before ledgers existed gets its line from progress.json first."""
    d = Path(args.jobdir)
    job = json.loads((d / 'job.json').read_text())
    backfill_job_ledger(d)
    ledger = load_ledger(d)
    return {'budget': budget_state(job, ledger), 'ledger': ledger}


SWAP_TAKE_PROMPT = ('第一张是角色参考图，后面是同一段换人视频按时间顺序抽的帧（第 2 张起）。只输出一个 JSON 对象，不要别的文字：'
                    '{{"是同一个人":true/false,"把握":"高|中|低","衣服一致":true/false,"手和脸正常":true/false,"像未成年":true/false,'
                    '"问题":"一句话，说清第几张哪里不对；没有就写空字符串"}}。角色描述：{prompt}。'
                    '「衣服一致」看两件事：第 2 张起各帧之间前后没变，并且和角色描述写的衣服大体对得上（描述没写衣服就和参考图比）；照描述换掉、和参考图不同不算不一致。'
                    '「问题」里写出的任何不对，必须同时在对应字段上标 false，两边不要矛盾。')


SWAP_DUO_PROMPT = ('第一张是左边那个人的角色参考图，第二张是右边那个人的，后面是同一段双人换人视频按时间顺序抽的帧（第 3 张起）。'
                   '只输出一个 JSON 对象，不要别的文字：'
                   '{{"左边像第一张":true/false,"右边像第二张":true/false,"两人互换了":true/false,"把握":"高|中|低",'
                   '"衣服一致":true/false,"手和脸正常":true/false,"像未成年":true/false,'
                   '"问题":"一句话，说清第几张哪里不对；没有就写空字符串"}}。左边的人：{left}；右边的人：{right}。'
                   '「两人互换了」指某一帧里左边的人长成第二张、或衣服串到另一个人身上。'
                   '「衣服一致」看两件事：第 3 张起各帧之间前后没变，并且和左右两人描述里写的衣服大体对得上（描述没写衣服就和参考图比）；照描述换掉、和参考图不同不算不一致。'
                   '「问题」里写出的任何不对，必须同时在对应字段上标出，两边不要矛盾。')


def swap_duo_findings(r):
    """A two-person take reply as (verdict, lines), the same shape as swap_findings."""
    if not r:
        return 'unknown', []
    left, right, swapped = r.get('左边像第一张'), r.get('右边像第二张'), r.get('两人互换了')
    clothes, body = r.get('衣服一致'), r.get('手和脸正常')
    lines = [{'ok': left, 'text': ('左边像角色 A' if left is not False else '左边不像角色 A') + f'（把握{r.get("把握", "?")}）'},
             {'ok': right, 'text': '右边像角色 B' if right is not False else '右边不像角色 B'},
             {'ok': None if swapped is None else not swapped, 'text': '两人有互换或串衣服' if swapped else '两人没有互换'},
             {'ok': clothes, 'text': '衣服前后一致' if clothes is not False else '衣服中途变了'},
             {'ok': body, 'text': '手和脸正常' if body is not False else '手或脸有变形'}]
    if r.get('像未成年') is True:
        lines.append({'ok': False, 'text': '模型觉得人物像未成年'})
    return ('issue' if any(x['ok'] is False for x in lines) else 'ok'), lines


def swap_findings(r):
    """A dance-swap take reply as (verdict, lines). Underage is only shown when the model flags it."""
    if not r:
        return 'unknown', []
    same, clothes, body = r.get('是同一个人'), r.get('衣服一致'), r.get('手和脸正常')
    lines = [{'ok': same, 'text': ('像参考图里的人' if same is not False else '不像参考图里的人') + f'（把握{r.get("把握", "?")}）'},
             {'ok': clothes, 'text': '衣服前后一致' if clothes is not False else '衣服中途变了'},
             {'ok': body, 'text': '手和脸正常' if body is not False else '手或脸有变形'}]
    if r.get('像未成年') is True:
        lines.append({'ok': False, 'text': '模型觉得人物像未成年'})
    issue = any(x['ok'] is False for x in lines)
    return ('issue' if issue else 'ok'), lines


def cmd_job_precheck(args, cfg):
    """Ask the image model about each dance-swap take. Writes precheck.json only: picking a take stays with the user."""
    import tempfile
    d = Path(args.jobdir)
    job = json.loads((d / 'job.json').read_text())
    need_vision(cfg)
    p = d / 'precheck.json'
    pre = current_precheck(d)
    tmp = Path(tempfile.mkdtemp())
    prompts = job.get('seedPrompts') or {}
    files = job.get('files') or {}
    duo = swap_people(job.get('params')) == 2
    ref = d / files.get('front', 'front.png')
    checked = []
    try:
        for s in job_seeds(job.get('params') or {}):
            f = d / f'seed_{s}.mp4'
            if not f.exists():
                continue
            name = f.name
            old = pre.get(name) or {}
            if old.get('status') == 'done' and old.get('mtime') == f.stat().st_mtime and not args.force:
                continue
            checked.append(name)
            pre[name] = {'status': 'running', 'mtime': f.stat().st_mtime, 'model': vision_model(cfg), 'started_at': time.time()}
            save_json_atomic(p, pre)
            dur = ffprobe_duration(f) or 4
            imgs = [small_jpeg(ref, tmp / f'{s}_ref.jpg')]
            if duo:   # both references, then the frames: who is who is the question that matters with two people
                imgs.append(small_jpeg(d / files.get('front2', 'front2.png'), tmp / f'{s}_ref2.jpg'))
            imgs += [small_jpeg(f, tmp / f'{s}_{i}.jpg', width=320, at=round(dur * x, 2)) for i, x in enumerate((0.1, 0.4, 0.7, 0.95))]
            if duo:
                (_, left), (_, right) = swap_take_people(job, s)
                prompt = SWAP_DUO_PROMPT.format(left=left or '（没写）', right=right or '（没写）')
            else:
                asked = prompts.get(str(s), job.get('prompt') or '')
                prompt = SWAP_TAKE_PROMPT.format(prompt=(asked if isinstance(asked, str) else '') or '（没写）')
            err = None
            for attempt in range(2):
                try:
                    r, text, secs = vision_ask(cfg, prompt, imgs, timeout=args.timeout)
                    if r is None and attempt == 0:
                        err = '没看成：回复不是 JSON'
                        continue
                    verdict, lines = (swap_duo_findings if duo else swap_findings)(r)
                    pre[name].update(status='done' if r else 'unreadable', verdict=verdict, lines=lines,
                                     issue=(r or {}).get('问题', '') if r else '没看成：回复不是 JSON', raw=r, seconds=secs, at=time.time())
                    err = None
                    break
                except ShiseiError as e:
                    err = str(e)
            if err:
                pre[name].update(status='failed', error=err[:300], at=time.time())
            save_json_atomic(p, pre)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {'checked': checked, 'precheck': pre}


def new_job_id():
    return time.strftime('j%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]


def cmd_job_new(args, cfg):
    """Create a job folder like the GUI does (copy inputs, trim the clip), so agent runs show up in the Piying page."""
    people = 2 if getattr(args, 'people', 1) == 2 else 1
    second = ('front2', getattr(args, 'front2', None)) if people == 2 else ('face', args.face)
    if not second[1]:
        raise ShiseiError('two people need --front2 (the right-hand person)' if people == 2 else '--face is required')
    for f in (args.video, args.front, second[1]):
        if not Path(f).is_file():
            raise ShiseiError(f'not a file: {f}')
    keep_bg = people == 2 or getattr(args, 'mode', 'studio') == 'keepbg'
    jid = new_job_id()
    d = HOME / 'jobs' / jid
    d.mkdir(parents=True)
    files = {'source': 'source' + Path(args.video).suffix.lower(), 'front': 'front' + Path(args.front).suffix.lower(),
             second[0]: second[0] + Path(second[1]).suffix.lower()}
    for key, src in (('source', args.video), ('front', args.front), second):
        shutil.copyfile(src, d / files[key])
    pr = cmd_probe(argparse.Namespace(src=str(d / files['source']), strip=str(d / 'strip.png')), cfg)
    clip = cmd_prep(argparse.Namespace(src=str(d / files['source']), out=str(d / 'dance.mp4'), start=args.start, crop_x=args.crop_x,
                                       seconds=args.seconds, keep_frame=keep_bg), cfg)
    job = {'id': jid, 'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S'), 'state': 'draft', 'prompt': args.details or '',
           **({'prompt2': getattr(args, 'details2', '') or ''} if people == 2 else {}),
           'params': {'shot': args.shot, 'head': args.head, 'seeds': args.seeds, 'frames': clip['frames'],
                      'mode': 'keepbg' if keep_bg else 'studio', 'people': people}, 'files': files,
           'probe': {k: pr[k] for k in ('duration', 'width', 'height', 'fps')} | {'segment': pr['segment_seconds']},
           'prep': {'start': args.start, 'cropX': 0.5 if args.crop_x is None else args.crop_x, 'file': 'dance.mp4', 'at': int(time.time() * 1000)}}
    (d / 'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=1))
    return {'job': jid, 'dir': str(d), 'next': f'job-run {d} --confirm-billing (after the user confirms the price)'}


def cmd_job_status(args, cfg):
    d = Path(args.jobdir)
    p = d / 'progress.json'
    if not p.exists():
        return {'state': 'draft'}
    pr = json.loads(p.read_text())
    out = {'state': pr.get('state'), 'stages': {k: v.get('status') for k, v in pr.get('stages', {}).items()},
           'seeds': pr.get('seeds'), 'error': pr.get('error'), 'last_log': (pr.get('log') or [''])[-1]}
    out['files'] = sorted(f.name for f in d.glob('seed_*.mp4')) + (['showcase.mp4'] if (d / 'showcase.mp4').exists() else [])
    return out


# ---------------------------------------------------------------- drama: script -> cast -> keyframes -> takes -> cut
#
# One job folder per project (kind "drama"), flat file names only:
#   job.json      id, kind, title, params                      (GUI / drama-new)
#   story.json    script, cast, scenes, props, shots           (dsh, through drama-set; validated)
#   review.json   asset and keyframe verdicts, seed picks      (the user, through the GUI or drama-review)
#   progress.json the current GPU run                          (drama-run only)
#   cast_<id>_face.png  cast_<id>_body.png  scene_<id>.png  prop_<id>.png  kf_<shot>.png  take_<shot>_<seed>.mp4
#   final.mp4 / final.srt                                       (drama-cut, local)

DRAMA_SIZES = {'576p': (576, 1024), '768p': (768, 1344)}
DRAMA_IMAGE_SIZE = (864, 1536)      # keyframes and scenes, 9:16; Qwen output is downscaled by H3
DRAMA_PORTRAIT_SIZE = (1024, 1536)
DRAMA_SEEDS = (1001, 2002, 3003, 4004, 5005)
DRAMA_STAGES = ('assets', 'keyframes', 'takes')
DEFAULT_BUDGET = 5.0     # yuan per project; the user changes it on the Piying page (job.json params.budget)
MAX_UNIT_SECONDS = 12      # one H3 generation; 10 s measured on the 4090 (155 s at 576p), 12 s is the tested ceiling
LINE_KINDS = {'say': '说', 'inner': '内心独白', 'vo': '画外音'}
FRAMING_EN = [('特写', 'Extreme close-up'), ('中近景', 'Medium close-up'), ('近景', 'Close-up'), ('中景', 'Medium shot'),
              ('全景', 'Wide shot'), ('远景', 'Long shot')]
# words for what a camera cannot film: they belong in an inner monologue, an action or an expression
INNER_WORDS = ('认出', '没认出', '心里', '心想', '想起', '意识到', '觉得', '回忆', '决定', '明白', '以为')
# acting too fine for the video model: it plays them as a plain smile or a blank face (《画皮》 2-2: 「看着冷」 came out warm)
SUBTLE_WORDS = ('意味深长', '若有所思', '不易察觉', '似笑非笑', '皮笑肉不笑', '笑意不达眼底', '神色复杂', '眼神复杂', '眼神一冷',
                '眼神冰冷', '暗藏', '微妙', '欲言又止')
# a thought that judges how something looks (「这笑……为何看着冷？」): the shot it talks about has to show it
JUDGED_LOOK_RE = re.compile(r'(看着|看起来|显得|似乎|好像)[^，。？！…,.?!]{0,3}?(冷|假|怪|阴|不对|奇怪|陌生|诡异|不像)')
# a later beat changes something about a person or a prop (掀开帷帽): the first frame is drawn from the segment's first
# shot only, so that shot has to show the thing (《画皮》 1-1 had no veil, 1-2 lifted one)
CHANGE_RE = re.compile(r'(掀开|揭开|摘下|摘掉|取下|拔出|放下|戴上|脱下|脱掉|拿起|收起|穿上|解开|扔掉|丢下|合上)(?:了|起)?'
                       r'(?:一支|一张|一把|一顶|一件|那|这|她的|他的|自己的)?([\u4e00-\u9fff]{1,4})')
SHOT_STATE_MAX = 4          # `state`: what must be visible in the shot (戴着帷帽, 剑在鞘里), at most 4 short phrases
LEAD_IN_MOVES = ('push', 'pan', 'still')   # segment `lead_in`: a still of the scene picture before the segment, no GPU
LEAD_IN_SECONDS = (1.0, 3.0)
ID_RE = re.compile(r'[a-z][a-z0-9_]{0,23}')
SHOT_RE = re.compile(r'\d{1,2}-\d{1,2}')
STAGINGS = ('profile',)   # first-frame staging templates that passed on the GPU (docs/DESIGN.md ⑥); shoulder / gaze failed
# Local inputs that are not story assets. crop/: the cut of a segment's own approved first frame (drama-crop).
# wardrobe/: a clothes picture for one character (drama-wardrobe --picture). Both are subfolders, so _have(), the plans,
# the id check and the version code (all top-level) never see them.
CROP_DIR = 'crop'
WARDROBE_DIR = 'wardrobe'

# minutes, measured on the 4090 48G in the early experiments: Qwen image 10-17 s, H3 576p 5 s take 70-90 s, 768p ~142 s
# boot: real starts took 0.3–2 min (2026-11-11 walkthrough); the old 5 put every estimate 2.5–3x over
DRAMA_MINUTES = {'boot': 2.5, 'image': 0.2, 'take_576p': 1.5, 'take_768p': 2.5, 'load': 1.0, 'pull': 0.05}

DRAMA_MODELS = {
    'qwen': [('diffusion_models', 'qwen_image_2.1_int8_convrot.safetensors'),
             ('text_encoders', 'qwen3vl_8b_int8_convrot.safetensors'),
             ('vae', 'qwen_image_2.1_vae_bf16.safetensors')],
    'h3': [('diffusion_models', 'minimax_h3_ref2va_pruned_fp8_scaled.safetensors'),
           ('text_encoders', 'qwen3vl_32b_minimax_h3_int8_convrot.safetensors'),
           ('vae', 'minimax_h3_video_vae_fp16.safetensors'),
           ('vae', 'minimax_h3_audio_vae_fp32.safetensors'),
           ('loras', 'minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors')],
}
DRAMA_NODES = {
    'qwen': ['TextEncodeQwenImage21', 'QwenImage21Cache'],
    'h3': ['MiniMaxH3ReferenceToVideo', 'MiniMaxH3AddGuide', 'MiniMaxH3SigmaShift', 'LTXVSeparateAVLatent'],
}
# where the compshare public disk keeps the weights; drama-run links missing files from here into ComfyUI/models
MODEL_ROOTS = ['/model/ModelScope/Comfy-Org/Qwen-Image-2.1', '/model/ModelScope/Comfy-Org/MiniMax-H3']

TAKE_STYLE = ('Photorealistic cinematic film still style, vertical 9:16 frame. '
              'The shot starts from the given first frame and keeps its framing, room and lighting.')


def _placeholders(api):
    return sorted(set(re.findall(r'__[A-Z0-9]+__', json.dumps(api))))


def frames_for(seconds):
    """H3 wants 17n+5 frames; the shortest grid length that covers `seconds` at 24 fps."""
    s = float(seconds)
    if not 2 <= s <= MAX_UNIT_SECONDS:
        raise ShiseiError(f'a generated clip must be 2..{MAX_UNIT_SECONDS} seconds')
    n = -(-(int(round(s * FPS)) - 5) // 17)
    return 17 * n + 5


def drama_seed(key, attempt=0):
    """Stable image seed per asset, so rerunning an untouched job gives the same picture."""
    h = 0
    for ch in key:
        h = (h * 131 + ord(ch)) % 1000003
    return 100 + h + 7919 * int(attempt)


# The rule this project keeps is 未成年性化, not "no minor may be in the story" (docs/SAFETY.md): a child may be
# written in the script, a line, a scene or a prop, but never in a prompt with the body exposed. These lists are
# checked on every field that becomes an image or video prompt.
NUDITY_RE = re.compile(
    r'全裸|裸体|赤裸|赤裸上身|裸露|半裸|露点|露胸|露乳|露臀|走光|透视装|情趣内衣|'
    r'\b(?:nude|naked|topless|bottomless|undressed|nudity)\b', re.I)
SEX_RE = re.compile(
    r'性交|性行为|做爱|床戏|猥亵|色情|'
    r'\b(?:porn|pornographic|erotic|intercourse|sex scene|making love)\b', re.I)
# milder than nudity: a swimsuit or underwear is fine for an adult, not for someone young or written as a minor
REVEALING_RE = re.compile(
    r'内衣|内裤|泳装|泳衣|比基尼|吊带|抹胸|热裤|超短裙|低胸|深V|露肩|露背|露脐|'
    r'\b(?:underwear|lingerie|swimsuit|swimwear|bikini|bra|panties|thong|cleavage)\b', re.I)
# who counts as a minor when the words themselves say so: 大学生 / 研究生 / 医学生 / 留学生 are adults, and the
# lookbehinds keep them out; ages 1–17 are caught as digits and as Chinese numerals, 18 and up are not
MINOR_RE = re.compile(
    r'未成年|孩子|小孩|小孩子|孩童|儿童|少年|少女|少男|男孩|女孩|男童|女童|小童|幼童|幼儿|婴儿|宝宝|小朋友|'
    r'幼儿园|(?<!大)(?<!研究)(?<!医)(?<!留)学生|'
    r'(?<![\d零一二三四五六七八九十百千万两])(?:1[0-7]|[1-9])\s*(?:岁|周岁)|'
    r'(?<![\d零一二三四五六七八九十百千万两])(?:十[一二三四五六七]|十|[一二三四五六七八九两])\s*(?:岁|周岁)|'
    r'\b(?:children|child|kids?|toddlers?|infants?|bab(?:y|ies)|boys?|girls?|teens?|teenage|teenagers?|'
    r'adolescents?|juveniles?|underage|schoolchildren|schoolboys?|schoolgirls?|preteens?|tweens?)\b|'
    r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|'
    r'sixteen|seventeen)[- ]year[- ]old\b|'
    r'\b(?:1[0-7]|[1-9])[- ]?(?:year[- ]old|years? old|yo)\b',
    re.I)


def word_hits(regex, text):
    """The distinct words `regex` finds in `text`, in order of appearance (for the error message)."""
    out = []
    for m in regex.finditer(str(text or '')):
        w = m.group(0)
        if w not in out:
            out.append(w)
    return out


def is_young(cast):
    """A character the stricter rule applies to: marked 「看起来年轻」 on the page, or written as under 18."""
    age = (cast or {}).get('age')
    return (cast or {}).get('look_young') is True or (isinstance(age, int) and not isinstance(age, bool) and age < 18)


def exposure_problem(text, young=False):
    """Why this prompt field may not be drawn, or '' when it is fine.

    Nudity and sex are refused for every character: a Piying picture is a drama still, not adult content. The
    milder wardrobe words are refused only for someone young or in a field that itself mentions a minor — that is
    the 未成年性化 rule. Called on `look` / `outfit` / `voice` / `keyframe` / `action` / `state` only: the script
    and a line are never prompts (docs/SAFETY.md)."""
    text = str(text or '')
    hits = word_hits(NUDITY_RE, text) or word_hits(SEX_RE, text)
    if hits:
        return f'写了裸露或性（{hits[0]}）'
    if young:
        hits = word_hits(REVEALING_RE, text)
        if hits:
            return f'给未成年或显年轻的角色写了暴露衣着（{hits[0]}）'
    return ''


def validate_story(story):
    """Check what dsh wrote. Returns the story; raises ShiseiError with every problem found."""
    errs = []
    if not isinstance(story, dict):
        raise ShiseiError('story must be a JSON object')
    ids = {}
    for kind in ('cast', 'scenes', 'props'):
        items = story.get(kind) or []
        if not isinstance(items, list):
            errs.append(f'{kind} must be a list')
            continue
        ids[kind] = {}
        for it in items:
            iid = str(it.get('id', ''))
            if not ID_RE.fullmatch(iid):
                errs.append(f'{kind} id {iid!r}: use a-z, 0-9, _ (start with a letter, max 24)')
            elif iid in ids[kind]:
                errs.append(f'{kind} id {iid} is used twice')
            ids[kind][iid] = it
            if not str(it.get('look', '')).strip():
                errs.append(f'{kind} {iid}: look is empty')
            if 'origin' in it and not isinstance(it['origin'], dict):
                errs.append(f'{kind} {iid}: origin must be an object (where the picture was brought in from)')
    for c in (story.get('cast') or []):
        age = c.get('age')
        if not isinstance(age, int) or isinstance(age, bool) or not 1 <= age <= 99:
            errs.append(f'cast {c.get("id")}: age must be a whole number 1..99')
        for field in ('look', 'outfit', 'voice'):
            text = str(c.get(field) or '')
            why = exposure_problem(text, is_young(c) or bool(word_hits(MINOR_RE, text)))
            if why:
                errs.append(f'cast {c.get("id")}: {field} {why}; a minor may be in the story and lines, not in a '
                            'prompt with the body exposed (docs/SAFETY.md)')
        if not str(c.get('name', '')).strip():
            errs.append(f'cast {c.get("id")}: name is empty')
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,39}", str(c.get('name_en', ''))):
            errs.append(f'cast {c.get("id")}: name_en (the English name used in action) is missing or not plain letters')
        if 'look_young' in c and not isinstance(c['look_young'], bool):
            errs.append(f'cast {c.get("id")}: look_young is true or false (the user sets it on the Piying page)')
        if 'outfit' in c and not (isinstance(c['outfit'], str) and 0 < len(c['outfit'].strip()) <= 300):
            errs.append(f'cast {c.get("id")}: outfit is one line of at most 300 characters')
        if 'outfit_ref' in c:
            ref, cid = c['outfit_ref'], re.escape(str(c.get('id', '')))
            if not (isinstance(ref, dict) and re.fullmatch(rf'{WARDROBE_DIR}/{cid}_[0-9a-f]{{12}}\.png', str(ref.get('file', '')))
                    and re.fullmatch(rf'cast_{cid}_face\.v\d+\.png', str(ref.get('face', '')))
                    and re.fullmatch(r'library:[A-Z]\d{3}|upload:u\d{8}-\d{6}-[0-9a-f]{4}', str(ref.get('source', '')))):
                errs.append(f'cast {c.get("id")}: outfit_ref is written by drama-wardrobe --picture only (the user, on the Piying page)')
        if 'photo_ref' in c:
            ref = c['photo_ref']
            if not (isinstance(ref, dict) and ref.get('file') == photo_file(c.get('id', ''))
                    and re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha', '')))):
                errs.append(f'cast {c.get("id")}: photo_ref is written by series-cast-new only (the user, on the Piying page)')
        if 'voice_ref' in c:
            ref = c['voice_ref']
            if not (isinstance(ref, dict) and ref.get('file') == voice_file(c.get('id', ''))
                    and re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha', '')))):
                errs.append(f'cast {c.get("id")}: voice_ref is written by drama-voice only (the user, on the Piying page)')
    if not story.get('cast'):
        errs.append('cast is empty')
    if not story.get('scenes'):
        errs.append('scenes is empty')
    shots = story.get('shots') or []
    if not shots:
        errs.append('shots is empty')
    if len(shots) > 60:
        errs.append('at most 60 shots per episode')
    seen = set()
    for sh in shots:
        sid = str(sh.get('id', ''))
        if not SHOT_RE.fullmatch(sid):
            errs.append(f'shot id {sid!r}: use <scene>-<shot>, e.g. 1-3')
        if sid in seen:
            errs.append(f'shot {sid} is used twice')
        seen.add(sid)
        if sh.get('scene') not in ids.get('scenes', {}):
            errs.append(f'shot {sid}: unknown scene {sh.get("scene")!r}')
        cast = sh.get('cast') or []
        if not 0 <= len(cast) <= 2:
            errs.append(f'shot {sid}: 0, 1 or 2 characters per shot')
        for c in cast:
            if c not in ids.get('cast', {}):
                errs.append(f'shot {sid}: unknown character {c!r}')
        for p in sh.get('props') or []:
            if p not in ids.get('props', {}):
                errs.append(f'shot {sid}: unknown prop {p!r}')
        if 1 + len(cast) + len(sh.get('props') or []) > 3:
            errs.append(f'shot {sid}: scene + characters + props must be at most 3 images (drop a prop)')
        sec = sh.get('seconds', 5)
        if not isinstance(sec, (int, float)) or not 1 <= sec <= 10:
            errs.append(f'shot {sid}: seconds must be 1..10')
        young = any(is_young(ids.get('cast', {}).get(c, {})) for c in cast)
        for f in ('keyframe', 'action'):
            text = str(sh.get(f, ''))
            if not text.strip():
                errs.append(f'shot {sid}: {f} is empty')
                continue
            why = exposure_problem(text, young or bool(word_hits(MINOR_RE, text)))
            if why:
                errs.append(f'shot {sid}: {f} {why}; this field becomes the image / video prompt (docs/SAFETY.md)')
        if 'state' in sh:
            st = sh['state']
            if not (isinstance(st, list) and len(st) <= SHOT_STATE_MAX
                    and all(isinstance(x, str) and 0 < len(x.strip()) <= 40 for x in st)):
                errs.append(f'shot {sid}: state is a list of at most {SHOT_STATE_MAX} short phrases (40 characters each) '
                            'saying what must be visible, e.g. ["素娘戴着帷帽", "剑在鞘里"]')
            elif isinstance(st, list):
                for x in st:
                    why = exposure_problem(x, young or bool(word_hits(MINOR_RE, x)))
                    if why:
                        errs.append(f'shot {sid}: state {why}; this field becomes the image / video prompt (docs/SAFETY.md)')
        if 'staging' in sh:
            if sh['staging'] not in STAGINGS:
                errs.append(f'shot {sid}: staging is {" | ".join(STAGINGS)} (or leave it out)')
            elif len(cast) != 2:
                errs.append(f'shot {sid}: staging "profile" puts two people face to face; this shot shows {len(cast)}')
            elif is_close(sh):
                errs.append(f'shot {sid}: staging "profile" draws a medium two-shot; write the keyframe as 中景, not a close-up')
        if 'crop' in sh:
            cr = sh['crop']
            box = cr.get('box') if isinstance(cr, dict) else None
            ok = (isinstance(cr, dict) and re.fullmatch(rf'kf_{re.escape(sid)}\.v\d+\.png', str(cr.get('source', '')))
                  and isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)
                  and cr.get('cast') in cast and isinstance(cr.get('note', ''), str) and len(cr.get('note', '')) <= 200)
            if ok:
                try:
                    check_box(*box)
                except ShiseiError:
                    ok = False
            if not ok:
                errs.append(f'shot {sid}: crop is written by drama-crop only (the user, on the Piying page): a cut of this '
                            "segment's own earlier first frame, of someone in the shot")
        line = sh.get('line')
        if line:
            kind = line.get('kind', 'say')
            if kind not in LINE_KINDS:
                errs.append(f'shot {sid}: line kind must be say, inner or vo')
            if kind == 'vo':
                if line.get('who') not in ids.get('cast', {}):
                    errs.append(f'shot {sid}: unknown voice-over speaker {line.get("who")!r}')
            elif line.get('who') not in cast:
                errs.append(f'shot {sid}: the speaker must be one of the shot\'s characters (use kind "vo" for an off-screen voice)')
            if not str(line.get('text', '')).strip():
                errs.append(f'shot {sid}: line text is empty')
    segs = story.get('segments')
    firsts = ({str((g.get('shots') or [None])[0]) for g in segs if isinstance(g, dict)} if isinstance(segs, list) and segs
              else {str(sh.get('id')) for sh in shots})
    for sh in shots:   # one first frame per segment: only its first shot is drawn
        for f in ('staging', 'crop'):
            if f in sh and str(sh.get('id')) not in firsts:
                errs.append(f'shot {sh.get("id")}: {f} belongs on the first shot of a segment (that is the one the first frame is drawn from)')
    if segs is not None:
        order = [str(sh.get('id')) for sh in shots]
        flat = [x for g in segs if isinstance(g, dict) for x in (g.get('shots') or [])]
        if flat != order:
            errs.append('segments must list every shot once, in story order (each segment is a run of consecutive shots)')
        by = {str(sh.get('id')): sh for sh in shots}
        for i, g in enumerate(segs):
            members = [by[x] for x in (g.get('shots') or []) if x in by]
            if not members:
                continue
            total = sum(float(m.get('seconds', 5)) for m in members)
            if not 2 <= total <= MAX_UNIT_SECONDS:
                errs.append(f'segment {i + 1}: {total:g} s; one segment is one generation of 2..{MAX_UNIT_SECONDS} s')
            union = list(dict.fromkeys(c for m in members for c in (m.get('cast') or [])))
            if len(union) > 2:
                errs.append(f'segment {i + 1}: at most 2 characters across the whole segment')
            if len({m.get('scene') for m in members}) > 1:
                errs.append(f'segment {i + 1}: one segment stays in one scene')
            if 'lead_in' in g:   # a still of the scene picture before the segment (drama-cut makes it locally)
                li = g['lead_in']
                ok = (isinstance(li, dict) and set(li) <= {'seconds', 'move'} and li.get('move', 'push') in LEAD_IN_MOVES
                      and isinstance(li.get('seconds', 1.5), (int, float)) and not isinstance(li.get('seconds'), bool)
                      and LEAD_IN_SECONDS[0] <= li.get('seconds', 1.5) <= LEAD_IN_SECONDS[1])
                if not ok:
                    errs.append(f'segment {i + 1}: lead_in is {{"seconds": 1-3, "move": "push" | "pan" | "still"}}')
            if 'study' in g:   # the reference shots (对标片, study.json) this segment follows: [from, to]
                r = g['study']
                if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in r)
                        and 1 <= r[0] <= r[1] <= 999):
                    errs.append(f'segment {i + 1}: study is [from, to], reference shot numbers (drama-study-plan)')
    if errs:
        raise ShiseiError('story is not valid: ' + '; '.join(errs))
    return story


def units(story):
    """What one H3 generation covers: a segment (consecutive shots in one scene) or, without segments, one shot.
    A unit is shaped like a shot (id = its first shot's id, so files stay kf_<id>.png / take_<id>_<seed>.mp4)
    plus `beats` (its shots with start/end offsets) and `label` (A, B, C … for segments)."""
    shots = story['shots']
    by = {s['id']: s for s in shots}
    groups = [[by[x] for x in g['shots']] for g in story.get('segments') or []] or [[s] for s in shots]
    out = []
    for i, members in enumerate(groups):
        t, beats = 0.0, []
        for m in members:
            sec = float(m.get('seconds', 5))
            beats.append({**m, 't0': t, 't1': t + sec})
            t += sec
        first = members[0]
        out.append({'id': first['id'], 'label': chr(65 + i) if story.get('segments') else first['id'],
                    'scene': first['scene'], 'keyframe': first['keyframe'], 'seconds': t, 'beats': beats,
                    'cast': list(dict.fromkeys(c for m in members for c in (m.get('cast') or []))),
                    'props': list(dict.fromkeys(p for m in members for p in (m.get('props') or []))),
                    'first': first})
    return out


def unit_by_id(story, uid):
    return next(u for u in units(story) if u['id'] == uid)


def sh_lines(unit):
    """Lines in a unit with their offsets: [(t0, t1, line, beat)]."""
    return [(b['t0'], b['t1'], b['line'], b) for b in unit['beats'] if b.get('line')]


def story_warnings(story):
    """The script check: rules from the AI short-drama method (things a camera cannot film, conflict in the first
    3 seconds, lines too long for their shot, reactions, framing variety). Warnings only; dsh fixes, the user decides."""
    out = []
    shots = story['shots']
    first_line, t = None, 0.0
    for sh in shots:
        if sh.get('line'):
            first_line = t
            break
        t += float(sh.get('seconds', 5))
    if first_line is None or first_line > 3:
        out.append({'at': shots[0]['id'], 'text': '开头 3 秒内没有台词或冲突：把最抓人的一句放到第一个镜头'})
    for sh in shots:
        words = [w for w in INNER_WORDS if w in sh.get('keyframe', '') + sh.get('action', '')]
        if words and not (sh.get('line') or {}).get('kind') == 'inner':
            out.append({'at': sh['id'], 'text': f'「{words[0]}」镜头拍不出来：改成动作、表情，或者内心独白'})
        line = sh.get('line')
        if line and CJK.search(line.get('delivery') or ''):
            out.append({'at': sh['id'], 'text': 'delivery（语气）要用英文写：写中文会被当成台词念出来，现在拍视频时会被略去'})
        if line and len(norm_line(line['text'])) > float(sh.get('seconds', 5)) * 5:
            out.append({'at': sh['id'], 'text': f'台词 {len(norm_line(line["text"]))} 个字放不进 {sh.get("seconds", 5)} 秒（约每秒 5 个字）'})
    for i, sh in enumerate(shots[:-1]):
        line = sh.get('line')
        if line and line.get('kind', 'say') == 'say':
            others = [c for c in (sh.get('cast') or []) + (shots[i + 1].get('cast') or []) if c != line['who']]
            if not others:
                out.append({'at': sh['id'], 'text': '这句台词之后没有听的人的反应镜头'})
    framing = [next((zh for zh, _ in FRAMING_EN if sh.get('keyframe', '').startswith(zh)), '') for sh in shots]
    for i in range(len(framing) - 2):
        if framing[i] and framing[i] == framing[i + 1] == framing[i + 2]:
            out.append({'at': shots[i + 2]['id'], 'text': f'连续 3 个{framing[i]}：换一个景别'})
    # staging: a character who changes sides between consecutive shots of one scene (180° rule) without a reverse angle
    names = {c['id']: c['name'] for c in story['cast']}
    last_side = {}
    for sh in shots:
        k = sh.get('keyframe', '')
        for cid, nm in names.items():
            # 「她在画面左侧」 is where she stands; 「她的脸朝画面右侧」 is where she looks, not a side
            m = re.search(re.escape(nm) + r'(?:(?!朝|看向|望向|面向|对着)[^，。；])*?画面(左|右)', k)
            if not m:
                continue
            key = (sh.get('scene'), cid)
            if key in last_side and last_side[key][0] != m.group(1) and '反打' not in k:
                out.append({'at': sh['id'], 'text': f'{nm}从画面{last_side[key][0]}边换到了{m.group(1)}边（上一镜 {last_side[key][1]}）：'
                            '同一场里保持左右不变，要换就写明「反打」'})
            last_side[key] = (m.group(1), sh['id'])
    # eyelines: a line said to someone in the scene should say where the speaker looks
    first_of = {b['id']: u['first'] for u in units(story) for b in u['beats']}
    for sh in shots:
        line = sh.get('line')
        if line and line.get('kind', 'say') == 'say':
            others = [c for c in story['cast'] if c['id'] != line['who'] and any(c['id'] in (x.get('cast') or []) for x in shots
                                                                                 if x.get('scene') == sh.get('scene'))]
            k = sh.get('keyframe', '')
            first = first_of[sh['id']]
            if first.get('staging') == 'profile' and line['who'] in (first.get('cast') or []):
                continue   # the first frame already has the two of them in profile, facing each other
            if others and not re.search(r'(朝|看向|望向|面向|对着)[^，。]*画面(左|右)', k):
                w = {'at': sh['id'], 'text': f'{names[line["who"]]}说这句时看向哪里没写清：写成「脸朝画面左/右侧（对方所在方向）」，'
                     '不然模型常让人对着镜头说'}
                if len(first.get('cast') or []) == 2 and line['who'] in first['cast'] and not is_close(first):
                    # the one writing that passed on the GPU for two people looking at each other
                    w['text'] += f'。这一段首帧里是两个人，也可以用「侧脸相对」：drama-edit <目录> {first["id"]} --staging profile'
                    w['suggest'] = {'staging': 'profile', 'shot': first['id']}
                out.append(w)
    out += continuity_warnings(story)
    if not story.get('segments') and len(shots) > 2:
        out.append({'at': shots[0]['id'], 'text': '没有分段：每个镜头单独生成，切点处声音和表演会断开。把连续的 2–3 个镜头合成一段（5–12 秒）'})
    return out


def continuity_warnings(story):
    """What makes the cut jump: a thing a later beat changes that the first frame never showed, acting the video model
    cannot play, a voice whose speaker the segment never shows, and a new place entered without a look at it."""
    out = []
    names = {c['id']: c['name'] for c in story['cast']}
    us = units(story)
    for u in us:
        first = u['first']
        shown = first.get('keyframe', '') + ''.join(first.get('state') or [])
        for b in u['beats'][1:]:
            for m in CHANGE_RE.finditer(b.get('keyframe', '')):
                obj = m.group(2)[:2]
                if obj[-1] not in shown:
                    out.append({'at': first['id'], 'text': f'{b["id"]} 写了「{m.group(1)}{obj}」，但这一段的首帧照 {first["id"]} 画、'
                                f'里面没有{obj}：开头就会少了它，到 {b["id"]} 才凭空出现。在 {first["id"]} 写明要看得出它（state，如「戴着{obj}」）'})
                    break
        for _, _, line, b in sh_lines(u):
            if line.get('kind') == 'vo' and line['who'] not in u['cast']:
                nm = names.get(line['who'], line['who'])
                out.append({'at': b['id'], 'text': f'{nm}的画外音，但这一段没有拍到{nm}：观众分不清是谁在说、谁在看。'
                            f'让{nm}在这一段出镜一下（如窗外的背影），或者把这句交给画面里的人'})
    shots = story['shots']
    for i, sh in enumerate(shots):
        words = [w for w in SUBTLE_WORDS if w in sh.get('keyframe', '')]
        if words:
            out.append({'at': sh['id'], 'text': f'「{words[0]}」这种细微表情视频模型演不出来：改成看得见的动作，'
                        '如笑容慢慢收住、眼睛一眨不眨、手停在半空'})
        line = sh.get('line') or {}
        m = JUDGED_LOOK_RE.search(line.get('text', '')) if line.get('kind') in ('inner', 'vo') else None
        if m:
            seen = shots[i - 1]['id'] if i else sh['id']
            out.append({'at': seen, 'text': f'{sh["id"]} 的心里话说画面「{m.group(0)}」，这种感觉视频模型拍不出来，画面会和这句对不上：'
                        f'把 {seen} 写成看得见的样子（如笑着但眼睛一眨不眨、笑容一下子收住）'})
    seen_scenes, bare = set(), []
    for i, u in enumerate(us):
        g = (story.get('segments') or [{}] * len(us))[i]
        if i and u['scene'] != us[i - 1]['scene'] and u['scene'] not in seen_scenes and not (g or {}).get('lead_in'):
            bare.append(u['label'])
        seen_scenes.add(u['scene'])
    if bare and story.get('segments'):
        out.append({'at': us[[x['label'] for x in us].index(bare[0])]['id'],
                    'text': f'{"、".join(bare)} 段换到新地方时直接硬切：可以在这些段前加开场空镜（lead_in：1–2 秒，'
                            '用审过的场景图慢推，本机合成、不花 GPU）'})
    return out


def _cast(story, cid):
    return next(c for c in story['cast'] if c['id'] == cid)


def _by_id(story, kind, iid):
    return next(x for x in story[kind] if x['id'] == iid)


def asset_files(story):
    """Every image the assets stage makes, in dependency order (a body needs its face first)."""
    out = []
    for c in story['cast']:
        out += [f'cast_{c["id"]}_face.png', f'cast_{c["id"]}_body.png']
    out += [f'scene_{s["id"]}.png' for s in story['scenes']]
    out += [f'prop_{p["id"]}.png' for p in story.get('props') or []]
    return out


def is_minor_cast(c):
    """Written under 18: the picture must show that age, not an adult's (`look_young` is different: an adult who
    looks young, and drawn that way on purpose)."""
    age = (c or {}).get('age')
    return isinstance(age, int) and not isinstance(age, bool) and age < 18


def age_words(c):
    """How a prompt names the age: a minor is written as a minor, never as an adult (docs/SAFETY.md)."""
    return f'{c["age"]}岁的未成年人' if is_minor_cast(c) else f'{c["age"]}岁的成年人'


def age_look(c):
    """The age sentence for a prompt that already named the person."""
    if is_minor_cast(c):
        return f'必须是{c["age"]}岁的样子，穿着得体，不要画得更成熟。'
    return f'必须是{c["age"]}岁的成熟外观，不要显得更年轻。'


def age_en(c):
    """The English prompts (H3 takes, the inner voice) name the age the same way: a minor is never 'an adult'."""
    if is_minor_cast(c):
        return f'a {c["age"]}-year-old minor who must look {c["age"]}, fully clothed, never older'
    return f'a {c["age"]}-year-old adult'


def age_line(c):
    """The sentence every prompt that shows a person carries (name, real age, and the look that must not drift)."""
    return f'{c["name"]}{age_look(c)}'


def same_age_sentence(cast):
    """The tail of a prompt with several people: every age follows its own reference picture."""
    if any(is_minor_cast(c) for c in cast):
        return ('每个人的脸、年龄、五官、发型和衣服必须与对应参考图完全一致：未成年人是那个年龄的样子，穿着得体，'
                '不要画得更成熟；成年人不要显得更年轻。')
    return '每个人的脸、年龄、五官、发型和衣服必须与对应参考图完全一致，都是成年人，不要显得更年轻。'


def one_age_sentence(cast):
    """The age part of a prompt whose people were already named one by one."""
    if any(is_minor_cast(c) for c in cast):
        return '人物的衣服、肤色与参考图一致；年龄照参考图，未成年人是那个年龄的样子，穿着得体。'
    return '人物的衣服、肤色与参考图一致，是成年人。'


def asset_prompt(story, name):
    """(prompt, input images, size) for one asset image. Ages are written out so Qwen does not drift younger."""
    if name.startswith('cast_'):
        cid, part = name[5:-4].rsplit('_', 1)
        c = _cast(story, cid)
        ref = c.get('outfit_ref')
        if ref:   # clothes from a picture (drama-wardrobe --picture): the recipe that passed on the GPU, both pictures
            return (redraw_prompts(c)[part], [ref['face'], ref['file']], DRAMA_PORTRAIT_SIZE)
        who = f'{age_words(c)}{c["name"]}：{c["look"]}'
        if c.get('outfit'):   # a wardrobe change: the new clothes win over whatever `look` says they wear
            who += f'。衣服以这一条为准（不穿上面描述里的衣服）：{c["outfit"].strip()}'
        if part == 'face' and c.get('photo_ref') and not is_young(c):   # added from a photo: the face follows the photo
            return (photo_prompt(c, who), [c['photo_ref']['file']], DRAMA_PORTRAIT_SIZE)
        if part == 'face':
            return (f'真实摄影风格，影棚近景肖像，纯白背景，正面平视，拍到头部和肩膀，表情平静自然。人物：{who}。'
                    f'{age_look(c)}柔和均匀的影棚光，真实皮肤纹理，清晰对焦。画面里只有这一个人。',
                    [], DRAMA_PORTRAIT_SIZE)
        return (f'把第一张图里的人物生成一张影棚全身定妆照：纯白背景，正面平视，全身入镜从头顶到鞋底完整，人物站直，双手自然垂下，手里不拿东西。'
                f'脸、年龄、发型和衣服与第一张图完全一致。{age_line(c)}人物：{who}。真实摄影风格。',
                [f'cast_{cid}_face.png'], DRAMA_PORTRAIT_SIZE)
    if name.startswith('scene_'):
        s = _by_id(story, 'scenes', name[6:-4])
        return (f'真实摄影风格，竖构图电影剧照，{s["look"]}。画面里没有任何人。', [], DRAMA_IMAGE_SIZE)
    p = _by_id(story, 'props', name[5:-4])
    return (f'真实摄影风格，产品静物照，纯白背景，{p["look"]}。只有这一件物品，没有人。', [], (1024, 1024))


def photo_prompt(c, who):
    """A portrait drawn after the photo a regular was added from: who it is from the photo, the rest from the words.
    Not yet tried on the GPU (the wardrobe redraw, its nearest recipe, passed 2/2)."""
    return (f'照着第一张照片里的这个人画一张影棚近景肖像：纯白背景，正面平视，拍到头部和肩膀，表情平静自然。'
            f'脸型、五官、发型与照片里的人完全一致，是同一个人；照片里的背景、姿势和别的人都不要。人物：{who}。'
            f'{age_look(c)}柔和均匀的影棚光，真实皮肤纹理，清晰对焦。真实摄影风格。画面里只有这一个人。')


def prev_keyframe(story, uid):
    """The keyframe of the unit before `uid` in the same scene (staging carries over from it), or None."""
    us = units(story)
    i = next(i for i, u in enumerate(us) if u['id'] == uid)
    if i > 0 and us[i - 1]['scene'] == us[i]['scene']:
        return f'kf_{us[i - 1]["id"]}.png'
    return None


def keyframe_prompt(story, shot, prev=None):
    """(prompt, input images) for a shot's keyframe: scene (or the previous segment's keyframe, so seating and
    sides carry over) first, then each character's portrait, then props.
    Two user / director choices replace that recipe: `crop` (drama-crop: redraw a cut of this segment's own earlier
    first frame; the cut is the only input) and `staging: profile` (two people in strict profile, facing each other)."""
    crop = shot.get('crop')
    if crop:
        others = [_cast(story, x) for x in shot.get('cast') or [] if x != crop['cast']]
        return crop_prompt(_cast(story, crop['cast']), others), [crop_input(shot['id'])]
    if shot.get('staging') == 'profile' and len(shot.get('cast') or []) == 2:
        left, right = staging_sides(story, shot)
        return (eyeline_prompt(left, right, profile_sentence(left['name'], right['name']),
                               situation_text(shot['keyframe']) + ('。' + state_sentence(shot).rstrip('。') if shot.get('state') else '')),
                [f'scene_{shot["scene"]}.png', f'cast_{left["id"]}_face.png', f'cast_{right["id"]}_face.png'])
    detail = is_close(shot) and '脸' not in shot['keyframe']   # hands, a prop: the room only gets in the way
    images = [] if detail else [prev or f'scene_{shot["scene"]}.png']
    refs, people = [], []
    for cid in shot.get('cast') or []:
        c = _cast(story, cid)
        images.append(f'cast_{cid}_face.png')
        people.append(c)
        refs.append(f'第{len(images)}张图里的{c["name"]}（{age_words(c)}）')
    for pid in shot.get('props') or []:
        images.append(f'prop_{pid}.png')
        refs.append(f'第{len(images)}张图里的{_by_id(story, "props", pid)["name"]}')
    n = len(shot.get('cast') or [])
    if detail:
        sc = _by_id(story, 'scenes', shot['scene'])
        lead = f'参考{"、".join(refs)}，' if refs else ''
        return (f'{lead}生成一张竖构图电影特写剧照：{shot["keyframe"]}。{state_sentence(shot)}主体占满画面，背景是{sc["look"]}，强烈虚化。'
                f'{one_age_sentence(people) if n else ""}真实摄影风格。', images)
    head = (f'把{"、".join(refs)}放进第一张图的场景里，' if refs else '以第一张图的场景为背景，')
    if prev:
        head = ('第一张图是上一段的画面：保持同一个场景、光线和人物的位置关系（谁在画面左边、谁在右边、谁坐哪个座位）不变，'
                + (f'参考{"、".join(refs)}，' if refs else ''))
    tail = (same_age_sentence(people) if n else '')
    who = {0: '画面里没有人。', 1: '画面里只有这一个人。', 2: '画面里只有这两个人。'}[n]
    if is_close(shot) and n:
        # tested in the early experiments: the scene picture pulls a close shot back to its own framing unless the
        # prompt makes the scene a blurred background and states how much of the person is in frame
        bg = '背景是第一张图的场景，强烈虚化，只当背景用，不要照搬第一张图的取景。景别严格按描述'
        bg += '：只拍到脸和肩膀，人物的脸占画面高度的三分之一左右。' if '脸' in shot['keyframe'] else '，主体占满画面。'
    else:
        bg = '背景、光线与第一张图一致。'
    return (f'{head}生成一张竖构图电影剧照：{shot["keyframe"]}。{state_sentence(shot)}{tail}{bg}真实摄影风格。{who}', images)


def state_sentence(shot):
    """「画面里必须看得出：素娘戴着帷帽；剑在鞘里。」 from the shot's `state`, or ''."""
    st = [x.strip().rstrip('。') for x in shot.get('state') or [] if str(x).strip()]
    return f'画面里必须看得出：{"；".join(st)}。' if st else ''


def is_close(shot):
    """Close-up or extreme close-up: the framing words dsh writes at the start of `keyframe`."""
    k = str(shot.get('keyframe', ''))[:12]
    return ('近景' in k or '特写' in k) and '中近景' not in k


def crop_input(uid):
    """Local name (under the job folder) of the cut a cropped first frame is redrawn from. Made again before every
    run from crop.source + crop.box, so it never goes stale."""
    return f'{CROP_DIR}/kf_{uid}.in.png'


def remote_input(rid, name):
    """Name in ComfyUI input/ of a local input. Subfolder inputs get their folder in the name ({rid}_crop_kf_1-1.in.png),
    so they can never land on a pipeline name such as {rid}_kf_1-1.png (the take's pinned first frame)."""
    return f'{rid}_{str(name).replace("/", "_")}'


CAMERA_RE = re.compile(r'[大超极]?(?:特写|远景|全景|中近景|中景|近景)(?:镜头)?|平视|俯拍|仰拍|俯视|仰视'
                       r'|(?:侧面|正面|低|高|固定)?机位|(?:双人|单人|固定|手持)镜头')


def situation_text(keyframe):
    """What is going on in a keyframe sentence, without its leading framing / camera clauses (中景，平视，…): the
    staging template states those itself."""
    parts = [p.strip() for p in str(keyframe or '').strip().rstrip('。.').split('，') if p.strip()]
    while parts and CAMERA_RE.fullmatch(parts[0]):   # only whole clauses that are nothing but camera words
        parts.pop(0)
    return '，'.join(parts)


def staging_sides(story, shot):
    """(left, right) cast entries for a two-person staging: where the keyframe text says each one IS (「林站在画面左侧」),
    else the order they are listed in. Where someone faces or looks (「林脸朝画面右侧」) is not a position and is skipped."""
    a, b = (_cast(story, x) for x in shot['cast'][:2])
    side = {}
    for c in (a, b):
        m = re.search(re.escape(c['name']) + r'(?:(?!朝|向|对着|看|望)[^，。；])*?在画面(左|右)', shot.get('keyframe', ''))
        side[c['id']] = m.group(1) if m else None
    if (side[a['id']] == '右' and side[b['id']] != '右') or (side[b['id']] == '左' and side[a['id']] != '左'):
        return b, a
    return a, b


def frame_inputs(story, shot):
    """Everything a first frame is drawn from, exactly as the workflow gets it: the prompt, the input picture names
    and, for a cropped frame, which picture is cut and where (the cut's own name never changes)."""
    prompt, images = keyframe_prompt(story, shot)
    crop = shot.get('crop')
    return [prompt, list(images), [crop['source'], [round(float(v), 4) for v in crop['box']]] if crop else None]


def frame_hash(story, shot):
    """A short fingerprint of frame_inputs. Kept with every first frame that is made (review.json `frame_inputs`) and
    with every archived version (`version_inputs`), so drama-version can tell whether an old approval still applies."""
    return hashlib.sha256(json.dumps(frame_inputs(story, shot), ensure_ascii=False).encode()).hexdigest()[:16]


def frame_words(story, shot):
    """What the first frame should show, in words, for the image pre-check: the keyframe text, unless a crop or a
    staging template decides the framing instead."""
    cr = shot.get('crop')
    if cr:
        note = f'（{cr["note"]}）' if cr.get('note') else ''
        return f'特写，{_cast(story, cr["cast"])["name"]}的近距离特写，从这一段原来的首帧里裁出来重画的{note}'
    if shot.get('staging') == 'profile' and len(shot.get('cast') or []) == 2:
        left, right = staging_sides(story, shot)
        return profile_sentence(left['name'], right['name']) + situation_text(shot['keyframe']) + state_sentence(shot)
    return shot['keyframe'] + ('。' + state_sentence(shot) if shot.get('state') else '')


def plain_frame(story, shot):
    """The first frame's inputs as they would be without a crop: what the script itself asks for."""
    return keyframe_prompt(story, {k: v for k, v in shot.items() if k != 'crop'})


def take_refs(story, shot):
    """Reference images for H3 besides the pinned keyframe, and the sentence that names them."""
    cast = shot.get('cast') or []
    if len(cast) == 1:
        c = _cast(story, cast[0])
        return ([f'cast_{c["id"]}_body.png', f'cast_{c["id"]}_face.png'],
                f'<Picture 1> is the full-body character sheet and <Picture 2> a close-up portrait of {c["name_en"]}, '
                f'{age_en(c)}.')
    if len(cast) == 2:
        a, b = (_cast(story, x) for x in cast)
        return ([f'cast_{a["id"]}_body.png', f'cast_{b["id"]}_body.png'],
                f'<Picture 1> is the character sheet of {a["name_en"]}, {age_en(a)}. '
                f'<Picture 2> is the character sheet of {b["name_en"]}, {age_en(b)}.')
    return ([f'scene_{shot["scene"]}.png'], '<Picture 1> shows the location.')


def framing_en(keyframe):
    return next((en for zh, en in FRAMING_EN if keyframe.startswith(zh)), '')


CJK = re.compile(r'[\u3400-\u9fff\uf900-\ufaff]')


def delivery_en(line):
    """The line's delivery note, only when it is written in English: next to Chinese dialogue, H3 (and the voice pass)
    read a Chinese note aloud as part of the line (真机 2026-11-11: 「又气又酸，嗓门压低…」 spoken in 7 of 8 takes)."""
    how = (line.get('delivery') or '').strip()
    return '' if CJK.search(how) else how


def line_sentence(story, line):
    c = _cast(story, line['who'])
    how = f', {delivery_en(line)}' if delivery_en(line) else ''
    text = line['text'].strip()
    kind = line.get('kind', 'say')
    if kind == 'inner':
        # H3 makes whoever is on screen mouth any voice it hears, so the inner voice is not generated here
        return f'{c["name_en"]} says nothing; {c["name_en"]}\'s lips stay closed, only the face shows the thought. No voice.'
    if kind == 'vo':
        return f'{c["name_en"]} speaks off-screen in Mandarin Chinese{how}: "{text}"'
    return f'{c["name_en"]} says in Mandarin Chinese{how}: "{text}" Only {c["name_en"]}\'s lips move with the words.'


def inner_lines(unit):
    return [(t0, t1, line, b) for t0, t1, line, b in sh_lines(unit) if line.get('kind') == 'inner']


def photo_file(cid):
    """The photo a character's portrait is drawn after (a regular added from a photo on the series page)."""
    return f'photo_{cid}.png'


def voice_file(cid):
    """A character's fixed voice (定声) in the project folder: one reference recording, chosen by the user."""
    return f'voice_{cid}.wav'


def voice_audios(story, cast_ids):
    """[(cast entry, file)] of the characters in `cast_ids` that have a fixed voice, in that order (H3 takes up to 3)."""
    out = []
    for cid in cast_ids:
        c = next((x for x in story['cast'] if x['id'] == cid), None)
        if c and c.get('voice_ref'):
            out.append((c, c['voice_ref']['file']))
    return out[:3]


def voice_sentence(audios):
    """How the prompt names the reference recordings (<Audio n>, in the order they are wired). Tried on the GPU
    2026-11-13: the voice moves clearly toward the reference, lines and lips stay right, nothing of the recording is
    copied."""
    return ' '.join(f"<Audio {i}> is a recording of {c['name_en']}'s own voice: {c['name_en']} speaks with exactly this "
                    'voice (same timbre, pitch and accent).' for i, (c, _) in enumerate(audios, 1))


def voice_candidates(d, story, cid):
    """Recordings of this character the user can fix as its voice: the separate voice tracks of its inner / off-screen
    lines (vo_<shot>.wav: one voice, no room sound), and its spoken lines in the take the user picked for a segment
    (cut out of that take by whisper's timing when it is chosen; a character who never thinks aloud has only these)."""
    d = Path(d)
    out = []
    for sh in story.get('shots') or []:
        line = sh.get('line') or {}
        f = f'vo_{sh["id"]}.wav'
        if line.get('who') == cid and (d / f).is_file():
            out.append({'file': f, 'shot': sh['id'], 'text': line.get('text', '')})
    try:
        picks = load_review(d).get('picks') or {}
        us = units(story)
    except (OSError, ValueError, KeyError, TypeError):
        return out
    for u in us:
        seed = picks.get(u['id'])
        take = f'take_{u["id"]}_{seed}.mp4'
        if not seed or not (d / take).is_file():
            continue
        for _, _, line, b in sh_lines(u):
            if line.get('who') == cid and line.get('kind', 'say') == 'say':
                out.append({'file': take, 'shot': b['id'], 'text': line.get('text', ''), 'take': True})
    return out


def line_span(segments, text):
    """(start, end) of the whisper segment that is this line alone, or None. Whisper splits a take at the pauses
    between speakers; a segment that holds more (another person's line too) is not a clean recording of one voice."""
    want = norm_line(text)
    if not want:
        return None
    best = None
    for t0, t1, heard in segments:
        h = norm_line(heard)
        if not h:
            continue
        r = difflib.SequenceMatcher(None, h, want).ratio()
        if r >= 0.75 and len(h) <= len(want) * 1.3 + 2 and (best is None or r > best[0]):
            best = (r, t0, t1)
    return (best[1], best[2]) if best else None


def whisper_segments(wav):
    """[(start s, end s, text)] of whisper.cpp's segments of a 16 kHz mono wav."""
    wb = whisper_bin()
    if not wb:
        raise ShiseiError(f'whisper-cli or {WHISPER_MODEL} not found: needed to find the line in the take')
    r = subprocess.run([wb, '-m', str(WHISPER_MODEL), '-l', 'zh', '-np', '--prompt', '以下是普通话的简体中文句子。', '-f', str(wav)],
                       capture_output=True, text=True, timeout=300)
    out = []
    for m in re.finditer(r'\[(\d+):(\d+):([\d.]+) --> (\d+):(\d+):([\d.]+)\]\s*(.*)', r.stdout):
        h0, m0, s0, h1, m1, s1, txt = m.groups()
        out.append((int(h0) * 3600 + int(m0) * 60 + float(s0), int(h1) * 3600 + int(m1) * 60 + float(s1), txt.strip()))
    return out


def voice_source(d, story, cid, f, shot):
    """The recording a 定声 is made from: a vo_*.wav as it is, or the line cut out of the picked take (a temp wav).
    Refused when it is not one of voice_candidates or the line cannot be found alone in the take."""
    d = Path(d)
    cand = next((x for x in voice_candidates(d, story, cid) if x['file'] == f and (not x.get('take') or x['shot'] == shot)), None)
    if cand is None:
        raise ShiseiError('定声只能用这个角色自己的一句独白或画外音（vo_*.wav）')
    if not cand.get('take'):
        return d / f
    full = d / f'.voice_{cid}.full.wav'
    cut = d / f'.voice_{cid}.cut.wav'
    try:
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(d / f), '-vn', '-ar', '16000', '-ac', '1', str(full)], check=True, timeout=120)
        span = line_span(whisper_segments(full), cand['text'])
        if span is None:
            raise ShiseiError('在这一条里找不到单独说这一句的地方（可能和别人的话连在一起了）：换一条，或换一句')
        t0, t1 = max(0.0, span[0] - 0.05), span[1] + 0.1
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(d / f), '-vn', '-ss', f'{t0:.2f}', '-to', f'{t1:.2f}',
                        '-ac', '1', '-ar', '24000', str(cut)], check=True, timeout=120)
    finally:
        full.unlink(missing_ok=True)
    return cut


# 定声 from a recording the user uploads on the page (a real person's voice is the user's to clear, like a photo): the
# page saves it next to the project (or the series) as .voice_upload.<ext>; it becomes a mono 24 kHz wav here.
VOICE_UPLOAD_RE = re.compile(r'\.voice_upload\.(wav|mp3|m4a|aac|ogg|opus|flac|webm)')
VOICE_UPLOAD_SECONDS = (3.0, 30.0)


def voice_upload_problem(seconds, has_audio):
    """Why an uploaded recording cannot be a voice reference (in the user's words), or None."""
    if not has_audio:
        return '这个文件里没有声音：换一段录音'
    lo, hi = VOICE_UPLOAD_SECONDS
    if seconds < lo:
        return f'录音太短（{seconds:.1f} 秒）：要 {lo:.0f}–{hi:.0f} 秒、只有这一个人在说话'
    if seconds > hi + 30:
        return f'录音太长（{seconds:.0f} 秒）：剪成 {lo:.0f}–{hi:.0f} 秒、只有这一个人在说话的一段'
    return None


def voice_upload_args(src, out):
    """ffmpeg: the first audio track, mono 24 kHz, at most the allowed length (a little over is cut, not refused)."""
    return ['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '24000',
            '-t', f'{VOICE_UPLOAD_SECONDS[1]:.0f}', str(out)]


def voice_from_upload(src, folder, cid):
    """The uploaded recording as a temp wav in `folder` (.voice_<cid>.cut.wav), checked; the upload itself is removed."""
    src, folder = Path(src), Path(folder)
    if src.parent.resolve() != folder.resolve() or not VOICE_UPLOAD_RE.fullmatch(src.name) or not src.is_file():
        raise ShiseiError('--upload is the recording the Piying page saved (.voice_upload.<ext> in the same folder)')
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=codec_type',
                            '-of', 'csv=p=0', str(src)], capture_output=True, text=True, timeout=60)
        why = voice_upload_problem(ffprobe_duration(src), 'audio' in r.stdout)
        if why:
            raise ShiseiError(why)
        out = folder / f'.voice_{cid}.cut.wav'
        try:
            subprocess.run(voice_upload_args(src, out), check=True, capture_output=True, timeout=120)
        except subprocess.CalledProcessError:
            raise ShiseiError('读不了这段录音：换成 wav、mp3 或 m4a 再试')
        return out
    finally:
        src.unlink(missing_ok=True)


def voice_prompt(story, line):
    c = _cast(story, line['who'])
    how = f', {delivery_en(line)}' if delivery_en(line) else ', softly, as if thinking aloud'
    return (f'Close-up portrait of {c["name_en"]}, {age_en(c)}, in a quiet room. {c["name_en"]} speaks in Mandarin '
            f'Chinese{how}: "{line["text"].strip()}" Only this one voice and a quiet room tone. No music. No subtitles.'
            + (f' Voice of {c["name_en"]}: {c["voice"].strip().rstrip(".")}.' if c.get('voice') else '')
            + (' ' + voice_sentence(voice_audios(story, [c['id']])) if c.get('voice_ref') else ''))


def take_prompt(story, unit):
    """One generation for a unit: timed beats in one continuous scene, so voices, room sound and acting carry across."""
    if 'beats' not in unit:     # a bare shot (old callers): treat it as a one-beat unit
        unit = next(u for u in units({**story, 'segments': None}) if u['id'] == unit['id'])
    refs, ref_sentence = take_refs(story, unit)
    parts = [TAKE_STYLE, ref_sentence]
    beats = unit['beats']
    if len(beats) == 1:
        b = beats[0]
        parts.append(b['action'].strip().rstrip('.') + '.')
        if b.get('line'):
            parts.append(line_sentence(story, b['line']))
    else:
        parts.append(f'One continuous {unit["seconds"]:g}-second scene in {len(beats)} beats; cut to the next camera angle at each beat. '
                     'The room, the light and the sound stay the same across the cuts.')
        for b in beats:
            beat = f'{b["t0"]:g}–{b["t1"]:g} s: {("Close-up" if b.get("crop") else framing_en(b["keyframe"])) or "Shot"}. {b["action"].strip().rstrip(".")}.'
            if b.get('line'):
                beat += ' ' + line_sentence(story, b['line'])
            parts.append(beat)
    speakers = list(dict.fromkeys(l[2]['who'] for l in sh_lines(unit) if l[2].get('kind', 'say') != 'inner'))
    for cid in speakers:
        c = _cast(story, cid)
        if c.get('voice'):
            parts.append(f'Voice of {c["name_en"]}: {c["voice"].strip().rstrip(".")}.')
    if voice_audios(story, speakers):
        parts.append(voice_sentence(voice_audios(story, speakers)))
    parts.append('No music. No subtitles, no on-screen text.')
    if unit.get('cast'):
        parts.append('Everyone keeps the same face, hair and clothes as in the reference pictures in every frame.')
    n = len(unit.get('cast') or [])
    parts.append({0: 'No people in the frame.', 1: 'Only one person in the frame.', 2: 'Only these two people appear.'}[n])
    return ' '.join(parts), refs


def patch_drama_image(api, prompt, seed, prefix, images=(), size=DRAMA_IMAGE_SIZE):
    """Qwen-Image 2.1: text-to-image with no images, edit with 1..4 images (node per image)."""
    api = copy.deepcopy(api)
    enc = _nodes(api, 'TextEncodeQwenImage21')
    if len(enc) != 1:
        raise ShiseiError('image workflow must have exactly one TextEncodeQwenImage21')
    if len(images) > 4:
        raise ShiseiError('at most 4 input images')
    enc[0]['inputs']['prompt'] = prompt
    for i, img in enumerate(images, 1):
        nid = str(100 + i)
        api[nid] = {'class_type': 'LoadImage', 'inputs': {'image': img}}
        enc[0]['inputs'][f'images.image_{i}'] = [nid, 0]
    for n in _nodes(api, 'EmptyLatentImage'):
        n['inputs'].update(width=int(size[0]), height=int(size[1]))
    for n in _nodes(api, 'KSampler'):
        n['inputs']['seed'] = int(seed)
    for n in _nodes(api, 'SaveImage'):
        n['inputs']['filename_prefix'] = f'drama/{prefix}'
    left = _placeholders(api)
    if left:
        raise ShiseiError(f'unfilled placeholders {left}')
    return api


def take_audios(story, unit):
    """The fixed voices a take is given: its speakers' (inner lines are made apart, see voice_prompt)."""
    speakers = list(dict.fromkeys(l[2]['who'] for l in sh_lines(unit) if l[2].get('kind', 'say') != 'inner'))
    return [f for _, f in voice_audios(story, speakers)]


def patch_drama_take(api, prompt, seed, prefix, keyframe, refs, size, frames, audios=()):
    """MiniMax H3 Ref2V with the keyframe pinned at frame 0 (MiniMaxH3AddGuide); `audios`: reference recordings
    (ref_audios, at most 3), named <Audio n> in the prompt."""
    api = copy.deepcopy(api)
    r2v, guide = _nodes(api, 'MiniMaxH3ReferenceToVideo'), _nodes(api, 'MiniMaxH3AddGuide')
    if len(r2v) != 1 or len(guide) != 1:
        raise ShiseiError('take workflow must have one MiniMaxH3ReferenceToVideo and one MiniMaxH3AddGuide')
    if not 1 <= len(refs) <= 9:
        raise ShiseiError('1..9 reference images')
    r = r2v[0]['inputs']
    for k in [k for k in r if k.startswith('ref_images.')]:
        api.pop(r[k][0], None)
        del r[k]
    for i, img in enumerate(refs):
        nid = str(110 + i)
        api[nid] = {'class_type': 'LoadImage', 'inputs': {'image': img}}
        r[f'ref_images.ref_image_{i}'] = [nid, 0]
    if len(audios) > 3:
        raise ShiseiError('at most 3 reference recordings')
    for k in [k for k in r if k.startswith('ref_audios.')]:
        api.pop(r[k][0], None)
        del r[k]
    for i, a in enumerate(audios):
        nid = str(140 + i)
        api[nid] = {'class_type': 'LoadAudio', 'inputs': {'audio': a}}
        r[f'ref_audios.ref_audio_{i}'] = [nid, 0]
    r.update(prompt=prompt, width=int(size[0]), height=int(size[1]), length=int(frames))
    api[guide[0]['inputs']['image'][0]]['inputs']['image'] = keyframe
    for n in _nodes(api, 'RandomNoise'):
        n['inputs']['noise_seed'] = int(seed)
    for n in _nodes(api, 'SaveVideo'):
        n['inputs']['filename_prefix'] = f'drama/{prefix}'
    left = _placeholders(api)
    if left:
        raise ShiseiError(f'unfilled placeholders {left}')
    return api


_LOCKS_HELD = threading.local()


def lock_file(d, name):
    """Where the lock for one project's `name` file lives: a hidden .locks folder inside the project. Inside, because
    the director's shell may only write its own project folder (dsh's workspace-write sandbox): a lock beside the
    project folders made even the free drama-status ask for wider access. Hidden, because the page and the asset
    library list the folder by file-name patterns and skip dot entries."""
    d = Path(d).resolve()
    p = d / '.locks' / f'{name}.lock'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextlib.contextmanager
def file_lock(path):
    """One writer at a time across processes (the page, a detached run, the director's shell): read, change, write.
    Re-entrant within a thread, so a locked command may call a locked helper."""
    key = str(Path(path).resolve())
    held = _LOCKS_HELD.__dict__.setdefault('n', {})
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    try:
        import fcntl
    except ImportError:
        fcntl = None
    with open(path, 'a+') as f:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_EX)
        else:   # Windows: lock the file's first byte (msvcrt retries for about 10 s, then raises: tried again)
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
            else:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def study_lock(d):
    """One writer of study.json at a time (the page's picks, the image model's batches, a cut)."""
    return file_lock(lock_file(d, 'study'))


def review_lock(d):
    """One writer of review.json at a time: the page's verdicts and picks, a run noting what it remade, an edit."""
    return file_lock(lock_file(d, 'review'))


def locks_review(fn):
    """A drama command that reads, changes and writes review.json holds review_lock for its whole run."""
    @functools.wraps(fn)
    def run(args, cfg):
        with review_lock(drama_dir(args.jobdir)[0]):
            return fn(args, cfg)
    return run


def append_ledger(d, entry):
    """Add one line to a project's ledger.json under its lock (a run's end and a backfill can meet)."""
    if CURRENT_MACHINE and isinstance(entry, dict):
        entry = {**entry, 'instance': entry.get('instance') or CURRENT_MACHINE}   # the next quote defaults to it
    with file_lock(lock_file(d, 'ledger')):
        save_json_atomic(Path(d) / 'ledger.json', load_ledger(d) + [entry])


def load_review(d):
    p = Path(d) / 'review.json'
    r = json.loads(p.read_text()) if p.exists() else {}
    for k in ('assets', 'keyframes', 'picks'):
        r.setdefault(k, {})
    return r


def current_precheck(d, pre=None):
    """precheck.json entries whose picture or video is still the file that was judged (same mtime).

    A remade first frame (裁特写, 退回重拍, 换回版本) or a set-aside one keeps its old entry in precheck.json until the
    next pre-check; the page and drama-status must not show that verdict next to the new picture."""
    d = Path(d)
    if pre is None:
        p = d / 'precheck.json'
        try:
            pre = json.loads(p.read_text()) if p.exists() else {}
        except json.JSONDecodeError:
            pre = {}
    out = {}
    for name, e in (pre or {}).items():
        if not isinstance(e, dict) or '/' in name or '\\' in name:
            continue
        try:
            m = (d / name).stat().st_mtime
        except OSError:
            continue
        if e.get('mtime') == m:
            if e.get('status') == 'done' and isinstance(e.get('raw'), dict):
                # verdicts saved before the structured-only rule are worked out again from the reply
                judge = swap_findings if name.startswith('seed_') else take_findings if name.startswith('take_') else kf_findings
                e = {**e, 'verdict': judge(e['raw'])[0]}
            out[name] = e
    return out


def precheck_verdict(d, name):
    """The pre-check's verdict on a file that is still the one judged ('' when it was never checked)."""
    return (current_precheck(d).get(name) or {}).get('verdict') or ''


def needs_moderation(story):
    """A project with someone young or written as under 18 has to be pre-checked before the cut: its first frames
    and takes must not reach the final video before a model has looked for nudity (docs/SAFETY.md)."""
    return any(is_young(c) for c in story.get('cast') or [])


def save_json_atomic(path, obj):
    path = Path(path)
    tmp = path.with_name(f'{path.name}.{secrets.token_hex(4)}.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def verdict(review, kind, key):
    return (review.get(kind, {}).get(key) or {}).get('status', 'pending')


def in_agent_shell(env=None):
    """True inside a dsh model shell: the plugin puts DSH_SHISEI_BRIDGE there, and the page removes it for its own calls."""
    return bool((os.environ if env is None else env).get('DSH_SHISEI_BRIDGE'))


# Starting a paid run is the user's click on the Piying page: its dialog names the GPU, zone and price and takes the
# lease. From a model shell (the director, a dsh chat) --confirm-billing is refused before anything is written, so the
# model quotes and points at the button instead of starting a machine, asking for wider sandbox access, or failing
# half-way on a lease it may not write (2026-11-12).
BILLING_IS_THE_USERS = ('starting the GPU is the user\'s click on the Piying page, not a model shell\'s: nothing was started. '
                        'Tell the user the price (drama-estimate / job-estimate / up without --confirm-billing) and point them '
                        'to the button: the project page\'s bottom bar or its suggested next steps; for a recast, its '
                        '"Next: confirm GPU start"; for a new machine, the Start page.')


def user_only(kind, value, agent):
    """Approving a picture and picking a take belong to the user (they click on the Piying page). From a model shell
    these are refused; reject / pending stay open so the director can record what the user said. A guardrail, not a
    lock: whoever can edit review.json can get around it."""
    if agent and (kind == 'pick' or value == 'pass'):
        what = 'picking a take' if kind == 'pick' else 'approving a picture'
        raise ShiseiError(f'{what} is done by the user on the Piying page, not from the director shell. '
                          'To suggest a take run: drama-recommend <dir> <segment> <take number> --why "reason"')


def take_number(seed):
    """1001 -> 1: what the user sees is 「第 1 条」, never the seed."""
    return DRAMA_SEEDS.index(int(seed)) + 1


def resolve_take(value, takes):
    """A take number (1..5) or a seed (1001…) -> the seed, which must be one of the takes on disk."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ShiseiError(f'not a take: {value}')
    seed = v if v in DRAMA_SEEDS else (DRAMA_SEEDS[v - 1] if 1 <= v <= len(DRAMA_SEEDS) else None)
    if seed not in takes:
        raise ShiseiError(f'take {value} does not exist; this segment has takes ' + (', '.join(str(take_number(t)) for t in takes) or 'none'))
    return seed


def load_recommend(d):
    p = Path(d) / 'recommend.json'
    return json.loads(p.read_text()) if p.exists() else {}


# ---- money: what each GPU run cost (ledger.json) against the project's cap

def billing_start(run_started, was_running, start_time):
    """When this run began to cost money: its own start if the machine was already up, else the machine's start."""
    try:
        t = float(start_time)
    except (TypeError, ValueError):
        return run_started
    return run_started if was_running else max(run_started, t)


def ledger_entry(stage, items, began, ended, price, state, idle_minutes=0):
    """One line of the project's ledger. `run_cost` is the run itself (began..ended); `idle_minutes` is the idle stop
    this run armed (the machine stays up and billing until it stops by itself), counted in full until settle_idle
    trims it to the gap actually left before the next run. `cost` = run + idle: what the project is charged."""
    minutes = round(max(0.0, ended - began) / 60, 1)
    price = float(price or 0)
    run_cost = round(minutes / 60 * price, 2)
    e = {'at': ended, 'stage': stage, 'items': items, 'minutes': minutes, 'price_per_hour': price,
         'run_cost': run_cost, 'cost': run_cost, 'state': state}
    if idle_minutes > 0:
        e['idle_minutes'] = round(float(idle_minutes), 1)
        e['idle_cost'] = round(e['idle_minutes'] / 60 * price, 2)
        e['cost'] = round(run_cost + e['idle_cost'], 2)
    return e


def idle_cost(cfg, price, stop_after=False):
    """(minutes, ¥) of the idle stop a run arms when it ends: billed too, so estimates and the cap count it."""
    if stop_after:
        return 0, 0.0
    idle = int((cfg or {}).get('idle_stop_minutes') or 10)
    return idle, round(idle / 60 * float(price or 0), 2)


def _settled(e, now):
    """Copy of a ledger line whose idle stop ends at `now` (the next run starts on the still-running machine)."""
    gap = round(max(0.0, now - float(e.get('at') or now)) / 60, 1)
    mins = min(float(e['idle_minutes']), gap)
    price = float(e.get('price_per_hour') or 0)
    e = {**e, 'idle_minutes': mins, 'idle_cost': round(mins / 60 * price, 2), 'idle_settled': True}
    e['cost'] = round(float(e.get('run_cost') or 0) + e['idle_cost'], 2)
    return e


def ledger_if_started(ledger, now, was_running):
    """The ledger as it will be once a run starting now settles the last idle stop (for the cap check before it)."""
    if not was_running or not ledger or not ledger[-1].get('idle_minutes') or ledger[-1].get('idle_settled'):
        return ledger
    return ledger[:-1] + [_settled(ledger[-1], now)]


def settle_idle(d, now):
    """A new run of this project starts while the machine is still up: the idle stop the last run armed ended now,
    not after its full minutes. Trims that ledger line to the gap actually spent idle (never adds)."""
    with file_lock(lock_file(d, 'ledger')):
        ledger = load_ledger(d)
        e = ledger[-1] if ledger else None
        if not e or not e.get('idle_minutes') or e.get('idle_settled'):
            return None
        ledger[-1] = e = _settled(e, now)
        save_json_atomic(Path(d) / 'ledger.json', ledger)
        return e


def load_ledger(d):
    p = Path(d) / 'ledger.json'
    return json.loads(p.read_text()) if p.exists() else []


def trial_spent(job):
    """A whole-dance project (params.long.from) is one dance with the trial it was made from: what that trial project
    spent counts toward this one's cap too (真机 2026-11-12: the trial's ¥1.80 was left out, the cap started at ¥0)."""
    frm = (((job or {}).get('params') or {}).get('long') or {}).get('from')
    if not isinstance(frm, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', frm) or frm == (job or {}).get('id'):
        return 0.0
    try:
        return round(sum(float(e.get('cost') or 0) for e in load_ledger(HOME / 'jobs' / frm)), 2)
    except (OSError, ValueError):
        return 0.0


def budget_state(job, ledger):
    cap = (job.get('params') or {}).get('budget')
    cap = DEFAULT_BUDGET if cap is None else float(cap)
    trial = trial_spent(job)
    spent = round(sum(float(e.get('cost') or 0) for e in ledger) + trial, 2)
    out = {'cap': cap, 'spent': spent, 'left': round(max(0.0, cap - spent), 2)}
    if trial:
        out['trial'] = trial   # included in spent
    return out


def over_budget(budget, cost):
    """How far spent + this run would go past the cap (0 when it fits)."""
    return round(max(0.0, budget['spent'] + cost - budget['cap']), 2)


def budget_check(budget, cost, swap=False):
    over = over_budget(budget, cost)
    if over > 0:
        fewer = 'Remake: make fewer takes this time' if swap else 'Make fewer takes (--seeds 1) or fewer segments (--shots)'
        raise ShiseiError(f'over the project budget: spent ¥{budget["spent"]:.2f} + this run ≈ ¥{cost:.2f} is ¥{over:.2f} above the cap '
                          f'¥{budget["cap"]:g}. {fewer}, or ask the user to raise the cap on the Piying page (only the user changes it).')


def plan_blockers(story, review, stage, shots=None):
    """What the user still has to approve before `stage` may run: {'assets': [files], 'keyframes': [segment ids]}."""
    out = {'assets': [], 'keyframes': []}
    if stage in ('keyframes', 'takes'):
        out['assets'] = [f for f in asset_files(story) if verdict(review, 'assets', f) != 'pass']
    if stage == 'takes':
        out['keyframes'] = [u['id'] for u in units(story) if (not shots or u['id'] in shots) and verdict(review, 'keyframes', u['id']) != 'pass']
    return out


def drama_plan(story, review, stage, have, seeds=1, shots=None, gate=True):
    """What a stage would make: list of output file names, skipping files that exist and are not rejected.
    Gates: keyframes need every asset passed; takes need every keyframe (of the chosen shots) passed.
    gate=False is for a price only (drama-estimate --pending-ok): the list as if what waits for the user were approved.
    drama-run never passes it."""
    want = [u for u in units(story) if not shots or u['id'] in shots]
    if stage == 'assets':
        files = asset_files(story)
        return [f for f in files if f not in have or verdict(review, 'assets', f) == 'reject']
    if not gate:
        if stage == 'keyframes':
            return [f'kf_{s["id"]}.png' for s in want
                    if f'kf_{s["id"]}.png' not in have or verdict(review, 'keyframes', s['id']) == 'reject']
        if stage == 'takes':
            todo = [f'take_{s["id"]}_{seed}.mp4' for s in want for seed in DRAMA_SEEDS[:seeds]
                    if f'take_{s["id"]}_{seed}.mp4' not in have]
            return todo + [f'vo_{b["id"]}.wav' for s in want for _, _, _, b in inner_lines(s) if f'vo_{b["id"]}.wav' not in have]
    if stage == 'keyframes':
        blocked = [f for f in asset_files(story) if verdict(review, 'assets', f) != 'pass']
        if blocked:
            raise ShiseiError('every character, scene and prop image must be approved first: ' + ', '.join(blocked))
        return [f'kf_{s["id"]}.png' for s in want
                if f'kf_{s["id"]}.png' not in have or verdict(review, 'keyframes', s['id']) == 'reject']
    if stage == 'takes':
        blocked = [f for f in asset_files(story) if verdict(review, 'assets', f) != 'pass']
        if blocked:   # the video takes the character sheets as references, so they need the same approval
            raise ShiseiError('every character, scene and prop image must be approved first: ' + ', '.join(blocked))
        blocked = [s['id'] for s in want if verdict(review, 'keyframes', s['id']) != 'pass']
        if blocked:
            raise ShiseiError('these keyframes are not approved yet: ' + ', '.join(blocked))
        todo = [f'take_{s["id"]}_{seed}.mp4' for s in want for seed in DRAMA_SEEDS[:seeds]
                if f'take_{s["id"]}_{seed}.mp4' not in have]
        todo += [f'vo_{b["id"]}.wav' for s in want for _, _, _, b in inner_lines(s) if f'vo_{b["id"]}.wav' not in have]
        return todo
    raise ShiseiError(f'unknown stage {stage}')


def unit_inputs(story, unit):
    """What a segment is generated from, exactly as the workflows get it: `frame` is the first-frame prompt and its
    input pictures (frame_inputs of the first shot), `takes` the video prompt, its references, its length and the
    inner voices. Two stories that give the same value here make the same pictures."""
    voices = [(b['id'], voice_prompt(story, line)) for _, _, line, b in inner_lines(unit)]
    # which recording each fixed voice is (a new one under the same file name remakes the takes too)
    fixed = sorted((c['id'], c['voice_ref']['sha']) for c in story['cast'] if c.get('voice_ref') and c['id'] in unit['cast'])
    return {'frame': frame_inputs(story, unit['first']), 'takes': (take_prompt(story, unit), unit['seconds'], voices, fixed)}


def changed_units(old, new):
    """{segment id: 'frame' | 'takes'} for the segments of `new` that `old` would have generated differently.
    'frame': the first frame itself is different (so is everything after it). 'takes': the first frame still fits and
    only the video has to be remade. Segments missing from the result are untouched. A segment `old` did not have
    counts as 'frame': whatever is on disk under its id was made for something else."""
    was = {u['id']: unit_inputs(old, u) for u in units(old)} if old else {}
    out = {}
    for u in units(new):
        now, before = unit_inputs(new, u), was.get(u['id'])
        if before is None or before['frame'] != now['frame']:
            out[u['id']] = 'frame'
        elif before['takes'] != now['takes']:
            out[u['id']] = 'takes'
    return out


UNIT_STATES = {'assets': '等演员 · 场景 · 道具', 'frame': '待拍首帧', 'review': '待审首帧', 'shoot': '待拍', 'pick': '待选', 'picked': '已选'}
FILTERS = ('all', 'review', 'shoot', 'pick', 'changed')
# which filter chip a segment's state counts under (the page uses each segment's `filter`, never its own copy)
STATE_FILTER = {'review': 'review', 'frame': 'shoot', 'shoot': 'shoot', 'pick': 'pick'}


def unit_state(assets_ok, keyframe, review, takes, pick):
    """Where one segment is. A first frame that is missing or was sent back has to be made before anyone can review
    it ('frame', or 'assets' while the pictures it is drawn from are not all approved); then the user reviews it
    ('review'), the video is made ('shoot'), the user clicks a take ('pick'), and it is 'picked'."""
    if not keyframe or review == 'reject':
        return 'frame' if assets_ok else 'assets'
    if review != 'pass':
        return 'review'
    if not takes:
        return 'shoot' if assets_ok else 'assets'   # the video is drawn from those pictures too
    return 'picked' if pick else 'pick'


def unit_changed(uid, pick, changes, cut):
    """有改动: the segment was edited or swapped to another version after the last cut (or there is no cut yet), or
    the take picked now is not the one in the cut. `cut` is cut.json when final.mp4 exists, else None."""
    cut_at = float((cut or {}).get('at') or 0)
    if any(c.get('unit') == uid and float(c.get('at') or 0) > cut_at for c in changes or []):
        return True
    if cut and pick:
        return f'take_{uid}_{pick}.mp4' not in (cut.get('clips') or [])
    return False


def _labels(us):
    return '、'.join(u['label'] for u in us)


def drama_filter(us, assets, final, story=True):
    """Counts for the filter chips, the filter the page starts on, and one line saying why.
    First pass (no final cut yet): the filter follows the step the project is at, things the user can do for free
    (review, pick) before things that cost money (shoot). After the first cut it is always 全部."""
    n = {k: 0 for k in FILTERS}
    n['all'] = len(us)
    for u in us:
        key = STATE_FILTER.get(u['state'])
        if key:
            n[key] += 1
        if u.get('changed'):
            n['changed'] += 1
    miss = [f for f, a in (assets or {}).items() if not a['made'] or a['review'] == 'reject']
    pend = [f for f, a in (assets or {}).items() if a['made'] and a['review'] == 'pending']
    auto, reason = 'all', None
    if not story:
        step = 'script'
        reason = '还没有剧本。在右边告诉导演你的故事，写好后一段一段列在这里。'
    elif miss or pend:
        step = 'assets'
        reason = (f'先拍演员、场景、道具图，还差 {len(miss)} 张。' if miss
                  else f'{len(pend)} 张演员、场景、道具图等你审，都通过后才能拍首帧。')
    elif n['review']:
        step, auto = 'review', 'review'
        reason = f'{n["review"]} 张首帧拍好了，现在看「待审」。'
    elif n['pick']:
        step, auto = 'pick', 'pick'
        reason = f'{n["pick"]} 段的视频拍好了，现在看「待选」：点一条画面就选它。'
    elif n['shoot']:
        step, auto = 'shoot', 'shoot'
        frames = [u for u in us if u['state'] == 'frame']
        if frames:
            reason = (f'演员、场景、道具都通过了，现在看「待拍」：{len(frames)} 段要拍首帧。' if len(frames) == len(us)
                      else f'段 {_labels(frames)} 要重拍首帧，现在看「待拍」。')
        else:
            reason = (f'{n["shoot"]} 段首帧都通过了，现在看「待拍」。' if n['shoot'] == len(us)
                      else f'段 {_labels([u for u in us if u["state"] == "shoot"])} 的首帧通过了，现在看「待拍」。')
    else:
        step = 'cut'
        reason = None if final else '每段都选好了，可以合成成片。'
    if final:   # after the first cut the page is an editing table: everything stays in view
        auto = 'all'
        reason = None
    return {'counts': n, 'auto': auto, 'reason': reason, 'step': step}


def next_spend(us, assets):
    """The one run the spending button offers: stage, the segments it covers (drama-run --shots) and its title.
    Only what needs work is in it. None when nothing can be made right now."""
    miss = [f for f, a in (assets or {}).items() if not a['made'] or a['review'] == 'reject']
    if miss:
        # unchanged: sent back by the user, but its description has not been changed yet (the page holds the button)
        return {'stage': 'assets', 'shots': None, 'items': len(miss), 'title': f'拍 {len(miss)} 张图', 'what': '演员、场景、道具图',
                'unchanged': [f for f in miss if (assets[f] or {}).get('unchanged')]}
    if any(a['review'] != 'pass' for a in (assets or {}).values()):
        return None   # a picture waits for the user: nothing is drawn from it, first frame or video, until it passes
    frames = [u for u in us if u['state'] == 'frame']
    if frames:
        title = f'拍段 {_labels(frames)} 的首帧' if len(frames) < len(us) and len(frames) <= 3 else f'拍 {len(frames)} 张首帧'
        return {'stage': 'keyframes', 'shots': [u['id'] for u in frames], 'items': len(frames), 'title': title, 'what': '首帧',
                'unchanged': [u['label'] for u in frames if u.get('unchanged')]}
    shoot = [u for u in us if u['state'] == 'shoot']
    if shoot:
        title = f'拍段 {_labels(shoot)}' if len(shoot) < len(us) and len(shoot) <= 3 else f'拍 {len(shoot)} 段'
        return {'stage': 'takes', 'shots': [u['id'] for u in shoot], 'items': len(shoot), 'title': title, 'what': '视频'}
    return None


def drama_estimate(story, todo, stage, quality, price_per_hour, running=False):
    m = DRAMA_MINUTES
    if stage == 'takes':
        per = m[f'take_{quality}']
        secs = {u['id']: u['seconds'] for u in units(story)}
        work = sum(per * max(1.0, secs[f.split('_')[1]] / 5) if f.startswith('take_') else m['image'] * 3 for f in todo)
    else:
        work = m['image'] * len(todo)
    minutes = round((0 if running else m['boot']) + m['load'] + work + m['pull'] * len(todo), 1) if todo else 0
    return {'stage': stage, 'items': len(todo), 'minutes': minutes, 'price_per_hour': price_per_hour,
            'cost': round(minutes / 60 * price_per_hour, 2), 'note': 'estimate; real charges are on the compshare bill'}


def srt_time(t):
    ms = int(round(t * 1000))
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def build_srt(entries):
    """entries: [(start_s, end_s, text)] -> SRT text. A line spans its whole shot (no speech alignment yet)."""
    out = []
    for i, (a, b, text) in enumerate(entries, 1):
        out.append(f'{i}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n')
    return '\n'.join(out)


DIALOG_LUFS, BED_LUFS = -16, -36   # every clip levelled to -16 LUFS; one room-tone bed underneath, 20 dB lower


def cut_args(clips, label, out, width, subs=None, durs=None, bed=None, voices=(), silent=()):
    """Concatenate clips, optionally burn the AI label top-right, optionally mux soft subtitles.
    `label` None keeps the picture clean: the file still carries an AI-generated comment in its metadata.
    Sound: each clip is loudness-normalised and gets 50/80 ms fades (no clicks at cuts); with `bed` (a clip whose
    sound is just the room) its audio is looped under the whole episode so the background never drops out.
    `silent`: indexes of clips with no sound of their own (a lead-in still): not levelled (silence has no loudness)."""
    n = len(clips)
    args = ['ffmpeg', '-v', 'error', '-y']
    for c in clips:
        args += ['-i', str(c)]
    if label:
        args += ['-i', str(label)]
    if bed == 'synth':      # no clip without dialogue: a soft low room hum keeps the gaps from going dead
        args += ['-f', 'lavfi', '-i', 'anoisesrc=color=brown:amplitude=0.05:sample_rate=48000']
    elif bed:
        args += ['-i', str(bed)]
    bed_index = n + (1 if label else 0)
    vbase = bed_index + (1 if bed else 0)
    for v, _ in voices:
        args += ['-i', str(v)]
    sub_index = vbase + len(voices)
    if subs:
        args += ['-i', str(subs)]
    h = width * 16 // 9
    parts = []
    for i in range(n):
        fade = ''
        if durs:
            fade = f',afade=t=in:d=0.05,afade=t=out:st={max(0.0, durs[i] - 0.08):.3f}:d=0.08'
        level = '' if i in silent else f',loudnorm=I={DIALOG_LUFS}:TP=-1.5:LRA=11,aresample=48000'
        parts.append(f'[{i}:v]scale={width}:{h},setsar=1,fps={FPS}[v{i}];'
                     f'[{i}:a]aresample=48000{level}{fade}[a{i}]')
    parts.append(''.join(f'[v{i}][a{i}]' for i in range(n)) + f'concat=n={n}:v=1:a=1[cv][ca]')
    audio = '[ca]'
    if bed and durs:
        total = sum(durs)
        b = bed_index
        src = f'[{b}:a]lowpass=f=350,' if bed == 'synth' else f'[{b}:a]'
        parts.append(f'{src}aresample=48000,loudnorm=I={BED_LUFS}:TP=-9,aresample=48000,aloop=loop=-1:size=2000000000,'
                     f'atrim=0:{total:.3f},afade=t=in:d=0.5,afade=t=out:st={max(0.0, total - 0.8):.3f}:d=0.8[bed]')
        parts.append('[ca][bed]amix=inputs=2:normalize=0:duration=first[mix]')
        audio = '[mix]'
    if voices:
        # inner monologue: its own take's voice, levelled, a touch of room echo, placed at its beat
        for k, (_, start) in enumerate(voices):
            parts.append(f'[{vbase + k}:a]aresample=48000,loudnorm=I={DIALOG_LUFS + 1}:TP=-1.5,aresample=48000,'
                         f'aecho=0.8:0.6:40:0.25,adelay={int(start * 1000)}:all=1[vo{k}]')
        parts.append(f'{audio}' + ''.join(f'[vo{k}]' for k in range(len(voices)))
                     + f'amix=inputs={len(voices) + 1}:normalize=0:duration=first[withvo]')
        audio = '[withvo]'
    if label:
        parts.append(f'[{n}:v]scale={width * 2 // 9}:-1[lb];[cv][lb]overlay=W-w-{width // 36}:{width // 36}[out]')
        vout = '[out]'
    else:
        vout = '[cv]'
    args += ['-filter_complex', ';'.join(parts), '-map', vout, '-map', audio]
    if subs:
        args += ['-map', f'{sub_index}:s', '-c:s', 'mov_text', '-metadata:s:s:0', 'language=chi']
    args += ['-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart',
             '-metadata', 'comment=AI-generated (Piying)', str(out)]
    return args


def mean_volume(path):
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(path), '-af', 'volumedetect', '-f', 'null', '-'], capture_output=True, text=True)
    m = re.search(r'mean_volume: (-?[\d.]+) dB', r.stderr)
    return float(m.group(1)) if m else -99.0


def loudness(path):
    """Integrated loudness (LUFS) of a file's audio, for the cut report."""
    r = subprocess.run(['ffmpeg', '-hide_banner', '-i', str(path), '-af', 'ebur128', '-f', 'null', '-'], capture_output=True, text=True)
    m = re.findall(r'I:\s+(-?[\d.]+) LUFS', r.stderr)
    return float(m[-1]) if m else None


REMOTE_ENSURE_MODELS = r'''
M=/root/ComfyUI/models; missing=""
for item in {items}; do
  sub=${{item%%/*}}; name=${{item#*/}}
  if [ ! -e "$M/$sub/$name" ]; then
    src=""
    for root in {roots}; do [ -e "$root/$sub/$name" ] && src="$root/$sub/$name" && break; done
    if [ -n "$src" ]; then mkdir -p "$M/$sub" && ln -sf "$src" "$M/$sub/$name" && echo "linked $sub/$name"; else missing="$missing $sub/$name"; fi
  fi
done
echo "missing:$missing"
'''


def ensure_models_script(group):
    items = ' '.join(f'{sub}/{name}' for sub, name in DRAMA_MODELS[group])
    return REMOTE_ENSURE_MODELS.format(items=items, roots=' '.join(MODEL_ROOTS))


# ---------------------------------------------------------------- drama commands

def drama_dir(arg):
    d = Path(arg)
    if not (d / 'job.json').exists():
        raise ShiseiError(f'not a job folder: {d}')
    job = json.loads((d / 'job.json').read_text())
    if job.get('kind') != 'drama':
        raise ShiseiError('this job is a remake, not a drama')
    # a story.json written straight (a model editing the file, not drama-set) takes back what it made stale before any
    # command reads, prices, shoots, edits or cuts from it
    sync_story(d)
    return d, job


def load_story(d):
    p = Path(d) / 'story.json'
    if not p.exists():
        raise ShiseiError('no story yet: write the script and shots, then run drama-set')
    return validate_story(json.loads(p.read_text()))


def cmd_drama_new(args, cfg):
    jid = new_job_id()
    d = HOME / 'jobs' / jid
    d.mkdir(parents=True)
    t = load_template(args.template) if getattr(args, 'template', None) else None
    title = (args.title or (t and t['title']) or '').strip()[:60]
    if not title:
        raise ShiseiError('a project needs --title (or --template)')
    job = {'id': jid, 'kind': 'drama', 'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S'), 'state': 'draft',
           'title': title, 'prompt': (args.idea or (t and t['story'].get('script')) or '').strip()[:4000],
           'params': {'quality': t['quality'] if t else args.quality, 'seeds': t['seeds'] if t else args.seeds}}
    if t:
        job['template'] = t['id']
    save_json_atomic(d / 'job.json', job)
    if t:   # the script only: no pictures, no review, nothing approved; drama-set checks it like any other
        story = validate_story(strip_user_fields(copy.deepcopy(t['story'])))
        save_story(d, story)
        return {'job': jid, 'dir': str(d), 'template': t['id'], 'next': 'drama-estimate assets, then the user starts it'}
    return {'job': jid, 'dir': str(d), 'next': 'write story.json (see SKILL.md), then drama-set'}


def strip_user_fields(story):
    """Take out what only the user's picture commands write: `crop` on shots (drama-crop) and `outfit_ref` on cast
    (drama-wardrobe --picture). drama-set runs this on the director's file first, then merge_user_fields puts back the
    ones the project already had, so the director can neither add nor alter them. Changes `story` in place."""
    if isinstance(story, dict):
        for key, field in (('shots', 'crop'), ('cast', 'outfit_ref'), ('cast', 'voice_ref'), ('cast', 'photo_ref')):
            for x in story.get(key) or []:
                if isinstance(x, dict):
                    x.pop(field, None)
    return story


def merge_user_fields(old, story):
    """What the director's drama-set cannot drop: `look_young` (only the user turns it off, on the Piying page),
    `origin` (where an asset was brought in from), and the two picture instructions the user gave: a shot's `crop`
    (kept while the shot is still the first of its segment and still shows that person) and a character's `outfit_ref`
    (kept while look and outfit text are unchanged; rewriting either means new clothes from words).
    `story` must be valid; changes it in place."""
    if not old:
        return story
    for kind in ('cast', 'scenes', 'props'):
        was = {x.get('id'): x for x in old.get(kind) or []}
        for x in story.get(kind) or []:
            o = was.get(x.get('id'))
            if o is None:
                continue
            if o.get('origin') and not x.get('origin'):
                x['origin'] = o['origin']
            if kind == 'cast':
                if o.get('look_young') or x.get('look_young'):
                    x['look_young'] = True
                else:
                    x.pop('look_young', None)
                if o.get('outfit_ref') and (cast_look(o)[0], cast_look(o)[2]) == (cast_look(x)[0], cast_look(x)[2]):   # look, outfit
                    x['outfit_ref'] = o['outfit_ref']
                if o.get('voice_ref'):   # the fixed voice stays whatever the director rewrites; only the user changes it
                    x['voice_ref'] = o['voice_ref']
                if o.get('photo_ref'):   # so does the photo the user added the character from
                    x['photo_ref'] = o['photo_ref']
    # a segment's 对标 range (segments[].study) the director left out of a rewrite stays while the segment does
    spans = {(g.get('shots') or [None])[0]: g['study'] for g in old.get('segments') or [] if isinstance(g, dict) and g.get('study')}
    for g in story.get('segments') or []:
        if 'study' not in g and (g.get('shots') or [None])[0] in spans:
            g['study'] = spans[g['shots'][0]]
    was = {x.get('id'): x for x in old.get('shots') or []}
    firsts = {u['id'] for u in units(story)} if story.get('shots') else set()
    for sh in story.get('shots') or []:
        cr = (was.get(sh['id']) or {}).get('crop')
        if cr and sh['id'] in firsts and cr.get('cast') in (sh.get('cast') or []):
            sh['crop'] = cr
    return story


def drop_stale_crops(old, story):
    """A crop redraws a cut of the old first frame, so the script's own words for that frame are not in its prompt.
    When the script changes what the frame would be drawn from without the crop (its text, scene, people, props,
    staging, an age), the crop no longer shows what the script asks for: it is removed and the frame is drawn from the
    script again. Changes `story` in place; returns the ids of the segments whose crop went."""
    if not old:
        return []
    was = {x['id']: x for x in old.get('shots') or []}
    gone = []
    for u in units(story):
        sh, o = u['first'], was.get(u['id'])
        if sh.get('crop') and o is not None and o.get('crop') and plain_frame(old, o) != plain_frame(story, sh):
            del sh['crop']
            gone.append(u['id'])
    return gone


def cast_look(c):
    """What a character's pictures are drawn from besides the name: description, age, outfit, and the clothes picture
    with the portrait it keeps the face of (outfit_ref)."""
    ref = c.get('outfit_ref') or None
    return (str(c.get('look', '')).strip(), c.get('age'), str(c.get('outfit') or '').strip(),
            (ref.get('file'), ref.get('face')) if ref else None)


def asset_resets(old, new):
    """{cast id: [face, body]} for characters of `new` whose look, age or outfit differ from `old`: their pictures
    show someone else now, so the approval goes and they are drawn again."""
    was = {c.get('id'): c for c in (old or {}).get('cast') or []}
    out = {}
    for c in new.get('cast') or []:
        o = was.get(c.get('id'))
        if o is not None and cast_look(o) != cast_look(c):
            out[c['id']] = [f'cast_{c["id"]}_face.png', f'cast_{c["id"]}_body.png']
    return out


def item_resets(old, new):
    """{('props' | 'scenes', id): picture} for props and scenes of `new` whose description differs from `old`: the
    picture shows something else now (a marker pen where the script says a brush), so its approval goes too."""
    out = {}
    for kind, prefix in (('props', 'prop'), ('scenes', 'scene')):
        was = {x.get('id'): x for x in (old or {}).get(kind) or []}
        for x in new.get(kind) or []:
            o = was.get(x.get('id'))
            if o is not None and str(o.get('look', '')).strip() != str(x.get('look', '')).strip():
                out[(kind, x['id'])] = f'{prefix}_{x["id"]}.png'
    return out


STORY_APPLIED = '.story.applied.json'   # the story as it was last saved by a command (sync_story compares with it)


def story_lock(d):
    """One writer of story.json at a time, and no sync_story reading between a save and its snapshot (re-entrant)."""
    return file_lock(lock_file(d, 'story'))


def save_story(d, story):
    """Every command that writes story.json writes it here: the file and the snapshot sync_story compares with, together.
    A write that skips this (a model editing the file) is what sync_story catches."""
    with story_lock(d):
        save_json_atomic(Path(d) / 'story.json', story)
        save_json_atomic(Path(d) / STORY_APPLIED, story)


def sync_story(d):
    """story.json edited without drama-set (a model writing the file straight): take back what the edit made stale,
    exactly as drama-set would have, before anything is shown, priced or shot from it. None when nothing changed."""
    with review_lock(d), story_lock(d):
        return _sync_story(Path(d))


def _sync_story(d):
    p, a = d / 'story.json', d / STORY_APPLIED
    if not p.exists():
        return None
    try:
        now = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    if not a.exists():   # a project from before this check: start comparing from here
        save_json_atomic(a, now)
        return None
    try:
        was = validate_story(json.loads(a.read_text()))
    except (ShiseiError, json.JSONDecodeError):
        save_json_atomic(a, now)
        return None
    if was == now:
        return None
    # a command from a copy of the plugin without this check (another worktree, an older install) saves the story it
    # replaced as story.vN.json and has already taken back what changed: the newest version is the snapshot
    vs = sorted(d.glob('story.v*.json'), key=lambda f: int(re.sub(r'\D', '', f.name) or 0))
    if vs and vs[-1].stat().st_mtime >= a.stat().st_mtime:
        try:
            if json.loads(vs[-1].read_text()) == json.loads(a.read_text()):
                save_json_atomic(a, now)
                return None
        except json.JSONDecodeError:
            pass
    try:
        story = validate_story(strip_user_fields(json.loads(json.dumps(now))))
        merge_user_fields(was, story)
        validate_story(story)
    except ShiseiError:
        return None   # not a valid story: drama-status reports the error; the last valid one stays the reference
    return apply_story(d, was, story, why='story.json 被直接改动（没经过 drama-set）')


def next_asset_version(d, name):
    """The version number archive_asset gives `name` next (cast_x_face.png -> N of cast_x_face.vN.png)."""
    f = Path(d) / name
    n = 1
    while (f.parent / f'{f.stem}.v{n}{f.suffix}').exists():
        n += 1
    return n


def archive_asset(d, name, review):
    """Move an asset picture to its next version (cast_x_face.v1.png) with the verdict it had; the current name is
    then missing, so the next assets run draws it again and the user reviews the new one. None if nothing to keep."""
    d = Path(d)
    had = review['assets'].pop(name, None)
    f = d / name
    if not f.exists() and had is None:
        return None
    n = next_asset_version(d, name)
    if f.exists():
        f.replace(d / f'{f.stem}.v{n}{f.suffix}')
    review.setdefault('asset_versions', {}).setdefault(name, {})[str(n)] = had
    return n


def load_staged(d):
    """Assets brought into a project that has no story yet (adopted.json): the director writes them into story.json."""
    p = Path(d) / 'adopted.json'
    return json.loads(p.read_text()) if p.exists() else []


def apply_story(d, old, story, why=''):
    with review_lock(d), story_lock(d):   # always review first, then story (sync_story takes them in the same order)
        return _apply_story(d, old, story, why)


def _apply_story(d, old, story, why=''):
    """Save `story` and take back, in code, whatever it makes stale: segments whose generation inputs changed
    (changed_units / reset_unit) and character pictures whose description changed (asset_resets / archive_asset;
    the segments that show them are marked 有改动 and are not re-shot). A crop the script has moved away from is
    removed first (drop_stale_crops; `story` is changed in place). Used by drama-set and every page edit."""
    d = Path(d)
    uncropped = drop_stale_crops(old, story)
    if old is not None and old != story:
        n = 1
        while (d / f'story.v{n}.json').exists():
            n += 1
        save_json_atomic(d / f'story.v{n}.json', old)   # (sync_story: story.json already holds the new text)
    save_story(d, story)
    review = load_review(d)
    touched = False
    redo = []
    if old is not None:
        was = {u['id']: u for u in units(old)}
        diff = changed_units(old, story)
        for u in units(story):
            what = diff.get(u['id'])
            if not what:
                continue
            voices = [b['id'] for b in u['beats']] + [b['id'] for b in (was.get(u['id']) or {}).get('beats', [])]
            r = reset_unit(d, u, what == 'frame', review, voices)
            if r is None:
                continue   # nothing had been made or decided for it yet
            touched = True
            log_change(d, {'kind': 'set', 'unit': u['id'], 'label': u['label'], 'version': r['version'], 'shot': None,
                           'why': (why or '')[:300], 'redo': r['redo']})
            redo.append({'unit': u['id'], 'label': u['label'], 'archived_as': f'v{r["version"]}', 'redo': r['redo']})
    # character pictures: compared with the old story, or (first story) with what was brought in before it existed
    staged = load_staged(d) if old is None else []
    base = old if old is not None else {'cast': [e['entry'] for e in staged if e.get('kind') == 'cast']}
    redrawn = []
    for cid, files in asset_resets(base, story).items():
        kept = {f: archive_asset(d, f, review) for f in files}
        if not any(v is not None for v in kept.values()):
            continue
        touched = True
        marked = []
        for u in units(story):
            if cid in u['cast']:
                marked.append(u['label'])
                if not any(r['unit'] == u['id'] for r in redo):
                    log_change(d, {'kind': 'cast', 'unit': u['id'], 'label': u['label'], 'cast': cid, 'shot': None,
                                   'why': (why or '')[:300], 'redo': []})
        redrawn.append({'cast': cid, 'archived': {f: f'v{n}' for f, n in kept.items() if n is not None}, 'marked': marked})
    # props and scenes whose description changed: same as a character (archived with its verdict, drawn again)
    for (kind, iid), f in (item_resets(old, story).items() if old is not None else ()):
        n = archive_asset(d, f, review)
        if n is None:
            continue
        touched = True
        marked = [u['label'] for u in units(story) if (iid in u['props'] if kind == 'props' else u['scene'] == iid)]
        redrawn.append({kind[:-1]: iid, 'archived': {f: f'v{n}'}, 'marked': marked})
    if staged:
        left = [e for e in staged if e['entry']['id'] not in {x['id'] for x in story.get(ADOPT_KINDS[e['kind']]) or []}]
        if left:
            save_json_atomic(d / 'adopted.json', left)
        else:
            (d / 'adopted.json').unlink()
    if touched:
        save_json_atomic(d / 'review.json', review)
    return {'changed': redo, 'redrawn': redrawn, 'uncropped': uncropped}


def cmd_drama_set(args, cfg):
    d, _ = drama_dir(args.jobdir)
    # crop / outfit_ref are the user's picture instructions: whatever the file says about them is dropped before it
    # is checked, and the project's own are put back below
    story = validate_story(strip_user_fields(json.loads(Path(args.story).read_text())))
    old = None
    if (d / 'story.json').exists():
        try:
            old = load_story(d)
        except (ShiseiError, json.JSONDecodeError):
            old = None   # the saved story was not valid: there is nothing to compare with
    staged = load_staged(d) if old is None else []
    merge_user_fields(old if old is not None else {ADOPT_KINDS[k]: [e['entry'] for e in staged if e['kind'] == k] for k in ADOPT_KINDS}, story)
    validate_story(story)
    # Segments whose generation inputs changed lose the user's approval and pick, in code: their pictures and videos
    # are kept as a version. Segments that did not change keep everything. Reviews of removed shots are just ignored.
    # A character whose look, age or outfit changed loses the approval of their pictures the same way.
    r = apply_story(d, old, story, getattr(args, 'why', None) or '')
    redo = r['changed']
    notes = []
    if redo:
        notes.append('changed segments went back: their first frame is made and reviewed again where it no longer fits, '
                     'their takes are remade, and the old files are kept as a version')
    if r['redrawn']:
        notes.append('characters whose look, age or outfit changed, and drawn scenes or props whose look changed, get new '
                     'pictures (old ones kept as a version, the user reviews the new ones); the segments that show them are '
                     'marked as changed and are not re-shot')
    if r['uncropped']:
        notes.append("the user's close-up crop was removed from segments whose first frame the script changed: " + ', '.join(r['uncropped']))
    return {'shots': len(story['shots']), 'cast': len(story['cast']), 'scenes': len(story['scenes']),
            'props': len(story.get('props') or []), 'images': len(asset_files(story)), 'changed': redo,
            'redrawn': r['redrawn'], 'uncropped': r['uncropped'], 'note': '; '.join(notes) or 'no made segment changed'}


# ---------------------------------------------------------------- references: assets from elsewhere, wardrobe, placing

ADOPT_KINDS = {'cast': 'cast', 'scene': 'scenes', 'prop': 'props'}
UPLOAD_ID_RE = re.compile(r'u\d{8}-\d{6}-[0-9a-f]{4}')
LIBRARY_ID_RE = re.compile(r'[A-Z]\d{3}')


def slug_id(text, fallback):
    """A story id from a name: lower-case ascii words joined by _, at most 20 characters (room for a _N suffix).
    A longer name is cut after a whole word (acoustic_guitar, not acoustic_guitar_natu) unless its first word alone
    is too long."""
    s = re.sub(r'[^a-z0-9]+', '_', str(text or '').lower()).strip('_')
    if len(s) > 20:
        cut = s[:21].rfind('_')
        s = s[:cut] if cut > 0 else s[:20]
    return s if s and s[0].isalpha() else fallback


def fresh_id(base, taken):
    """`base`, or base_2, base_3 … : the first one no story entry and no picture on disk uses."""
    if base not in taken:
        return base
    for n in range(2, 1000):
        x = f'{base[:20]}_{n}'
        if x not in taken:
            return x
    raise ShiseiError('no free id left')


def adopt_files(kind, iid):
    return [f'cast_{iid}_face.png', f'cast_{iid}_body.png'] if kind == 'cast' else [f'{kind}_{iid}.png']


def taken_ids(d, kind, story, staged):
    """Ids a new `kind` entry must not reuse: the story's, the staged ones, and any picture name on disk (old versions
    too), so a copied picture never lands on a file that belongs to something else."""
    taken = {x.get('id') for x in (story or {}).get(ADOPT_KINDS[kind]) or []}
    taken |= {e['entry']['id'] for e in staged if e.get('kind') == kind}
    pat = re.compile(rf'^{kind}_([a-z0-9_]+?)(?:_(?:face|body))?(?:\.v\d+)?\.png$' if kind == 'cast' else rf'^{kind}_([a-z0-9_]+?)(?:\.v\d+)?\.png$')
    for f in Path(d).iterdir():
        m = pat.match(f.name)
        if m:
            taken.add(m.group(1))
    return taken


def image_to_png_args(src, dst, size, fit='cover'):
    """ffmpeg argv: one picture to a PNG of exactly `size`. cover = fill and crop the middle (scenes), contain = whole
    picture on a light grey card (props: an object must not lose its ends)."""
    w, hh = size
    vf = (f'scale={w}:{hh}:force_original_aspect_ratio=increase,crop={w}:{hh}' if fit == 'cover'
          else f'scale={w}:{hh}:force_original_aspect_ratio=decrease,pad={w}:{hh}:(ow-iw)/2:(oh-ih)/2:color=0xE6E6E6')
    return ['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-frames:v', '1', '-vf', vf, '-map_metadata', '-1', str(dst)]


ADOPT_SIZE = {'scene': (DRAMA_IMAGE_SIZE, 'cover'), 'prop': ((1024, 1024), 'contain')}


def check_entry(kind, entry):
    """Run validate_story on one entry alone (a project without a story yet), with stand-ins for the rest."""
    stub = {'cast': [{'id': 'stub', 'name': 'stub', 'name_en': 'Stub', 'age': 30, 'look': 'x'}],
            'scenes': [{'id': 'stub', 'name': 'stub', 'look': 'x'}], 'props': []}
    stub[ADOPT_KINDS[kind]] = [entry]
    scene = stub['scenes'][0]['id']
    stub['shots'] = [{'id': '1-1', 'scene': scene, 'cast': [], 'keyframe': 'x', 'action': 'x'}]
    validate_story(stub)


@locks_review
def cmd_drama_adopt(args, cfg):
    """Bring a cast member, scene or prop into this project: from another project (--from-job, its pictures are copied),
    or a picture from the Piying library or the user's uploads (--image, scene or prop only). The new entry gets an id
    nothing here uses and `origin`; its pictures arrive unreviewed, so nothing is shot from them until the user
    approves them in this project. Same source twice returns the entry already made. Without a story yet the entry
    waits in adopted.json for the director to write it into story.json."""
    d, job = drama_dir(args.jobdir)
    kind = args.kind
    plural = ADOPT_KINDS[kind]
    story = load_story(d) if (d / 'story.json').exists() else None
    staged = load_staged(d)
    if args.from_job:
        sd, sjob = drama_dir(args.from_job)
        if sd.resolve() == d.resolve():
            raise ShiseiError('that is this project')
        item = next((x for x in load_story(sd).get(plural) or [] if x.get('id') == args.id), None)
        if item is None:
            raise ShiseiError(f'《{sjob.get("title", "")}》 has no {kind} {args.id}')
        origin = {'kind': 'project', 'job': sjob['id'], 'title': str(sjob.get('title') or '')[:60], 'id': args.id}
        entry = {k: v for k, v in copy.deepcopy(item).items() if k != 'origin'}
        base = slug_id(args.id, kind)
    else:
        if kind == 'cast':
            raise ShiseiError('--image copies one picture, and a character needs two (face and body): copy a character '
                              'from another project with --from-job, or write one from a description')
        if in_agent_shell():
            # every outside picture is confirmed by the user when it is imported (rights, what it is);
            # the director can mention library items and uploads, and the page copies them in
            raise ShiseiError('pictures from the library or uploads are brought in by the Piying page (the user confirms each one); '
                              'mention them with @ instead')
        if not args.image or not (args.name or '').strip() or not (args.look or '').strip():
            raise ShiseiError('--image, --name and --look are needed (look: what the picture shows, used if it is redrawn)')
        try:
            origin = json.loads(args.origin or '{}')
        except json.JSONDecodeError:
            origin = None
        ok = isinstance(origin, dict) and (
            (origin.get('kind') == 'library' and LIBRARY_ID_RE.fullmatch(str(origin.get('item', ''))))
            or (origin.get('kind') == 'upload' and UPLOAD_ID_RE.fullmatch(str(origin.get('id', '')))))
        if not ok:
            raise ShiseiError('--origin must be {"kind": "library", "item": "E001"} or {"kind": "upload", "id": "u…"}')
        entry = {'name': args.name.strip()[:40], 'look': args.look.strip()[:600]}
        base = slug_id(args.name_en or args.name, kind)
    existing = list((story or {}).get(plural) or []) + [e['entry'] for e in staged if e.get('kind') == kind]
    again = next((x for x in existing if x.get('origin') == origin), None)
    files = adopt_files(kind, (again or {}).get('id', ''))
    if again is not None:
        return {'kind': kind, 'id': again['id'], 'name': again.get('name'), 'files': files, 'again': True,
                'staged': story is None, 'origin': origin, 'pending': True}
    nid = fresh_id(base, taken_ids(d, kind, story, staged))
    entry = {**entry, 'id': nid, 'origin': origin}
    if story is not None:
        new = copy.deepcopy(story)
        new.setdefault(plural, []).append(entry)
        validate_story(new)            # a copied character keeps its age, and must still be an adult
    else:
        check_entry(kind, entry)
    files = adopt_files(kind, nid)
    made = []
    if args.from_job:
        for old_name, new_name in zip(adopt_files(kind, args.id), files):
            if (sd / old_name).exists():
                shutil.copyfile(sd / old_name, d / new_name)
                made.append(new_name)
    else:
        size, fit = ADOPT_SIZE[kind]
        cache = Path(args.cache) if args.cache else d / f'.{nid}.import.png'
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(image_to_png_args(args.image, cache, size, fit), check=True, capture_output=True, timeout=120)
        shutil.copyfile(cache, d / files[0])
        if not args.cache:
            cache.unlink()
        made.append(files[0])
    review = load_review(d)
    if any(review['assets'].pop(f, None) is not None for f in files):
        save_json_atomic(d / 'review.json', review)   # a stale verdict under the new name is not an approval
    where = f'《{origin.get("title")}》' if origin['kind'] == 'project' else '皮影素材库' if origin['kind'] == 'library' else '我的上传'
    if story is not None:
        apply_story(d, story, new, f'从{where}带进来 {entry.get("name", nid)}')
    else:
        save_json_atomic(d / 'adopted.json', staged + [{'kind': kind, 'entry': entry, 'at': time.time()}])
    return {'kind': kind, 'id': nid, 'name': entry.get('name'), 'files': files, 'made': made, 'staged': story is None,
            'origin': origin, 'pending': True, 'label': f'沿用自{where}' if origin['kind'] == 'project' else f'来自 {where}' if where[0].isascii() else f'来自{where}',
            'note': 'the pictures are unreviewed in this project: the user approves them before anything is shot' if made
                    else 'no picture yet: the next assets run draws it from look'}


def _inside(path, roots):
    p = os.path.realpath(str(path))
    for r in roots:
        if r:
            root = os.path.realpath(str(Path(r).expanduser()))
            if p != root and os.path.commonpath([p, root]) == root:
                return True
    return False


def _read_picture(path, what):
    """(type, sha256) of a picture file of at most 15 MB, recognised by its bytes."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_REF_BYTES:
        raise ShiseiError(f'{what} is not a picture file of at most 15 MB')
    data = path.read_bytes()
    fmt = image_type(data[:16])
    if fmt is None:
        raise ShiseiError(f'{what} is not a PNG, JPEG or WebP picture (checked by its bytes, not its name)')
    return fmt, hashlib.sha256(data).hexdigest()


def wardrobe_picture(src, image, cfg, home=None):
    """The clothes picture behind --source, checked: {'path', 'type', 'sha256'}.
    upload:<id>  the file in ~/.dsh/shisei/uploads, with the confirmation the user gave for this very picture when it
                 was imported (<id>.json, written by the Piying page): it is clothes, the user may use it, and the
                 bytes still have the recorded checksum. Anything missing: refused.
    library:<id> `image` is the file the page found through the catalogue; it must lie in the configured Piying
                 library checkout (public, rating general) or the plugin's cache of it."""
    home = Path(home) if home is not None else HOME
    if src.startswith('upload:'):
        uid = src.split(':', 1)[1]
        if not UPLOAD_ID_RE.fullmatch(uid):
            raise ShiseiError('bad upload id')
        try:
            rec = json.loads((home / 'uploads' / f'{uid}.json').read_text())
        except (OSError, json.JSONDecodeError):
            rec = None
        conf = (rec or {}).get('confirmed') if isinstance(rec, dict) else None
        if not (isinstance(conf, dict) and rec.get('id') == uid and conf.get('rights') is True):
            raise ShiseiError('this upload has no confirmation record: the user imports and confirms each picture on the Piying page '
                              '(the right to use it) before it can be used')
        if rec.get('kind') != 'clothes':
            raise ShiseiError('this upload was not confirmed as clothes')
        if rec.get('ext') not in ('png', 'jpg', 'webp') or not re.fullmatch(r'[0-9a-f]{64}', str(rec.get('sha256') or '')):
            raise ShiseiError('the confirmation record of this upload is incomplete (no checksum): import the picture again')
        path = home / 'uploads' / f'{uid}.{rec["ext"]}'
        fmt, sha = _read_picture(path, 'the uploaded picture')
        if sha != rec['sha256']:
            raise ShiseiError('the uploaded picture is not the one the user confirmed (checksum differs): import it again')
        return {'path': path, 'type': fmt, 'sha256': sha}
    if not image:
        raise ShiseiError('library clothes as a picture need --image <file in the Piying library cache>')
    # the page downloads a library picture into ~/.dsh/shisei/library (lib/library.js cacheLibImage) before calling this
    if not _inside(image, [home / 'library']):
        raise ShiseiError('--image is not inside the Piying library cache (~/.dsh/shisei/library)')
    fmt, sha = _read_picture(image, '--image')
    return {'path': Path(image), 'type': fmt, 'sha256': sha}


def cmd_drama_wardrobe(args, cfg):
    """Put other clothes on a character: `outfit` is set (the new clothes win over what `look` says), the portrait and
    full-body picture become the previous version and wait to be drawn and reviewed again, and every segment that
    shows the character is marked 有改动. Nothing is re-shot. A character marked look_young only gets clothes the page
    offers from the library (it never offers more revealing ones); typed or uploaded clothes are refused here.

    --picture: the clothes come from a PICTURE (--source upload:<id> or library:<id> --image F) instead of words.
    The picture is copied to wardrobe/<cast>_<checksum>.png and recorded as the character's `outfit_ref` together with
    the portrait that says who it is (the current approved one, by the version name it is archived under). The next
    assets run then draws portrait and full body with the redraw recipe that passed on the GPU: image 1 that archived
    portrait (identity), image 2 the clothes (clothes only). Only the user starts this (refused from the director
    shell), an upload needs its confirmation record (wardrobe_picture), and it is refused for a look_young character.
    wardrobe/ is never a segment reference: only asset_prompt of that one character names the file."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    c = next((x for x in story['cast'] if x['id'] == args.cast), None)
    if c is None:
        raise ShiseiError(f'unknown character {args.cast}')
    src = args.source or 'text'
    picture = bool(getattr(args, 'picture', False))
    if not (src == 'text' or re.fullmatch(r'library:[A-Z]\d{3}', src) or re.fullmatch(r'upload:' + UPLOAD_ID_RE.pattern, src)):
        raise ShiseiError('--source is text, library:C123 or upload:<id>')
    if picture and in_agent_shell():
        raise ShiseiError('redrawing a character from a picture is started by the user on the Piying page, not from the director shell')
    if c.get('look_young') and in_agent_shell():
        raise ShiseiError(f'{c["name"]} is marked as looking young: only the user changes her clothes, on the Piying page '
                          '(which offers only library clothes that are not more revealing)')
    if c.get('look_young') and picture:
        raise ShiseiError(f'{c["name"]} is marked as looking young: no redraws from a picture for this character '
                          '(library clothes the page offers can still go on as words)')
    if c.get('look_young') and not src.startswith('library:'):
        raise ShiseiError(f'{c["name"]} is marked as looking young: only clothes the Piying page offers from the library '
                          '(never more revealing ones) can go on this character')
    if picture and src == 'text':
        raise ShiseiError('--picture needs --source upload:<id> or library:<id>')
    outfit = ' '.join(x.strip() for x in (args.outfit or '', args.note or '') if x and x.strip())
    if not outfit:
        raise ShiseiError('--outfit is empty')
    new = copy.deepcopy(story)
    nc = next(x for x in new['cast'] if x['id'] == args.cast)
    nc['outfit'] = outfit[:300]
    nc['outfit_src'] = src
    files = adopt_files('cast', c['id'])
    pic = None
    if picture:
        pic = wardrobe_picture(src, getattr(args, 'image', None), cfg)
        review = load_review(d)
        old_ref = c.get('outfit_ref') or {}
        if (d / files[0]).is_file() and verdict(review, 'assets', files[0]) == 'pass':
            face = f'cast_{c["id"]}_face.v{next_asset_version(d, files[0])}.png'   # what apply_story archives it as
        elif old_ref.get('face') and (d / old_ref['face']).is_file():
            face = old_ref['face']     # changed again before the redraw was approved: same identity portrait
        else:
            raise ShiseiError(f'{c["name"]} has no approved portrait to keep the face of: approve the portrait first')
        nc['outfit_ref'] = {'file': f'{WARDROBE_DIR}/{c["id"]}_{pic["sha256"][:12]}.png', 'source': src, 'face': face}
    else:
        nc.pop('outfit_ref', None)   # clothes from words replace clothes from a picture
    validate_story(new)
    effects = {'cast': c['id'], 'name': c['name'], 'outfit': nc['outfit'], 'outfit_ref': nc.get('outfit_ref'),
               'before': {'look': c['look'], 'outfit': c.get('outfit'), 'outfit_ref': c.get('outfit_ref')},
               'redraw': files, 'archive': [f for f in files if (d / f).exists()],
               'marked': [u['label'] for u in units(story) if c['id'] in u['cast']],
               'next_run': {'stage': 'assets', 'shots': None, 'items': len(files), 'assume_missing': files}}
    if args.dry_run or cast_look(c) == cast_look(nc):
        return {**effects, 'dry_run': bool(args.dry_run), 'same': cast_look(c) == cast_look(nc)}
    if pic:
        dst = d / nc['outfit_ref']['file']
        if not dst.exists():
            dst.parent.mkdir(exist_ok=True)
            tmp = dst.with_name(dst.name + '.part.png')
            subprocess.run(image_to_png_args(pic['path'], tmp, DRAMA_PORTRAIT_SIZE, 'contain'), check=True, capture_output=True, timeout=120)
            tmp.replace(dst)
    r = apply_story(d, story, new, f'{c["name"]}换衣服：{nc["outfit"][:60]}')
    if pic and not (d / nc['outfit_ref']['face']).is_file():
        raise ShiseiError(f'internal: the portrait was not archived as {nc["outfit_ref"]["face"]}')
    return {**effects, **r}


def cmd_drama_ref(args, cfg):
    """Put a project asset into a segment's first frame (--add cast:x | prop:x), or swap one (--swap scene:a=scene:b,
    prop:a=prop:b). The first frame is drawn from the first shot's scene + people + props (at most 3), a segment holds
    at most 2 people. Saved with the same rule as drama-set: the segment's first frame goes back to be made again."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    u = next((x for x in units(story) if args.unit in (x['id'], x['label'])), None)
    if u is None:
        raise ShiseiError(f'unknown segment {args.unit}')
    new = copy.deepcopy(story)
    shots = {s['id']: s for s in new['shots']}
    first = shots[u['id']]
    member_ids = [b['id'] for b in u['beats']]
    ref = re.compile(r'(cast|scene|prop):([a-z][a-z0-9_]{0,23})')
    has = lambda kind, iid: any(x['id'] == iid for x in new.get(ADOPT_KINDS[kind]) or [])
    name = lambda kind, iid: next((x.get('name', iid) for x in new.get(ADOPT_KINDS[kind]) or [] if x['id'] == iid), iid)
    if bool(args.add) == bool(args.swap):
        raise ShiseiError('give one of --add or --swap')
    if args.add:
        m = ref.fullmatch(args.add)
        if not m or not has(m.group(1), m.group(2)):
            raise ShiseiError(f'{args.add}: not a cast member, scene or prop of this project')
        kind, iid = m.groups()
        if kind == 'scene':   # a segment has one scene: adding one replaces it, in every shot of the segment
            if first['scene'] == iid:
                raise ShiseiError(f'{name(kind, iid)} is already the scene of segment {u["label"]}')
            was = first['scene']
            for sid in member_ids:
                shots[sid]['scene'] = iid
            what = f'段 {u["label"]} 的场景从{name(kind, was)}换成{name(kind, iid)}'
        else:
            key = 'cast' if kind == 'cast' else 'props'
            if iid in (first.get(key) or []):
                raise ShiseiError(f'{name(kind, iid)} is already in segment {u["label"]}')
            first[key] = list(first.get(key) or []) + [iid]
            what = f'把{name(kind, iid)}放进段 {u["label"]} 的首帧'
    else:
        m = re.fullmatch(r'(scene|prop):([a-z][a-z0-9_]{0,23})=(scene|prop):([a-z][a-z0-9_]{0,23})', args.swap or '')
        if not m or m.group(1) != m.group(3) or not has(m.group(3), m.group(4)):
            raise ShiseiError('--swap is scene:<old>=scene:<new> or prop:<old>=prop:<new> (people are changed by the director)')
        kind, a, _, b = m.groups()
        if kind == 'scene':
            if first['scene'] != a:
                raise ShiseiError(f'segment {u["label"]} is not in {a}')
            for sid in member_ids:     # one segment stays in one scene
                shots[sid]['scene'] = b
        else:
            if a not in (first.get('props') or []):
                raise ShiseiError(f'{name(kind, a)} is not in the first frame of segment {u["label"]}')
            first['props'] = [b if x == a else x for x in first['props']]
        what = f'段 {u["label"]} 的{name(kind, a)}换成{name(kind, b)}'
    try:
        validate_story(new)
    except ShiseiError as e:
        raise ShiseiError(f'放不进段 {u["label"]}：每段首帧最多 3 张参考图（场景 + 人 + 道具），一段最多 2 个人。({e})')
    uncropped = drop_stale_crops(story, new)   # a close-up cut of the old frame cannot show what was just put in
    diff = changed_units(story, new)
    images = 1 + len(first.get('cast') or []) + len(first.get('props') or [])
    effects = {'unit': u['id'], 'label': u['label'], 'what': what, 'images': images, 'redo': diff.get(u['id']), 'uncropped': uncropped}
    if args.dry_run:
        return {**effects, 'dry_run': True}
    r = apply_story(d, story, new, args.why or what)
    return {**effects, **r, 'uncropped': uncropped}


def cmd_drama_cast_flag(args, cfg):
    """The user marks a character as looking young (or takes the mark off) on the Piying page. It does not change any
    picture; it limits what can be done to the character: no more revealing clothes, no low angles above the knee."""
    if in_agent_shell():
        raise ShiseiError('「看起来年轻」 is set by the user on the Piying page, not from the director shell')
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    c = next((x for x in story['cast'] if x['id'] == args.cast), None)
    if c is None:
        raise ShiseiError(f'unknown character {args.cast}')
    if args.look_young == 'on':
        c['look_young'] = True
    else:
        c.pop('look_young', None)
    save_story(d, validate_story(story))
    return {'cast': c['id'], 'look_young': bool(c.get('look_young'))}


def _have(d):
    return {p.name for p in Path(d).iterdir() if p.is_file()}


def lead_of(story, uid):
    """The segment's lead_in (a still of the scene before it), with its defaults filled in, or None."""
    us = units(story)
    i = next(i for i, u in enumerate(us) if u['id'] == uid)
    segs = story.get('segments') or []
    li = (segs[i] or {}).get('lead_in') if i < len(segs) else None
    return {'seconds': float(li.get('seconds', 1.5)), 'move': li.get('move', 'push')} if li else None


def cut_differs(cut, uid, ops, lead):
    """The final cut was made with other edits or another lead-in for this segment than the ones set now."""
    if not cut:
        return False
    return ((cut.get('edits') or {}).get(uid) or []) != ops or ((cut.get('leads') or {}).get(uid) or None) != lead


def drama_summary(d, job, story, review, recommend=None, ledger=None, changes=None, cut=None):
    have = _have(d)
    assets = {f: {'made': f in have, 'review': verdict(review, 'assets', f)} for f in asset_files(story)}
    for f, a in assets.items():
        if a['review'] == 'reject' and sent_back_unchanged(story, review, 'assets', f):
            a['unchanged'] = True   # sent back, the description the director should fix is still the same
    assets_ok = all(a['made'] and a['review'] == 'pass' for a in assets.values())
    final = 'final.mp4' in have
    cut = cut if final else None
    shots = []
    for u in units(story):
        takes = [seed for seed in DRAMA_SEEDS if f'take_{u["id"]}_{seed}.mp4' in have]
        pick = review['picks'].get(u['id'])
        pick = pick if pick in takes else None   # only what the user clicked; no default
        rec = (recommend or {}).get(u['id'])
        kf = f'kf_{u["id"]}.png' in have
        rv = verdict(review, 'keyframes', u['id'])
        state = unit_state(assets_ok, kf, rv, takes, pick)
        if takes and state in ('pick', 'picked') and any(not (Path(d) / f'vo_{b["id"]}.wav').exists() for _, _, _, b in inner_lines(u)):
            state = 'shoot'   # an inner voice a cut-off run never brought back: the button makes just that (2026-11-14)
        cr = u['first'].get('crop')
        base, why_not = crop_base(d, story, review, u)
        shots.append({'id': u['id'], 'label': u['label'], 'members': [b['id'] for b in u['beats']], 'seconds': u['seconds'],
                      # crop: the user's 裁特写 for this segment (box = the 9:16 cut, as fractions of `source`), or None.
                      # crop_from: the picture a new drama-crop --box would be measured on, None with crop_refused when
                      # it cannot be done now. staging: 'profile' | None; staging_ok: this first frame shows two people.
                      'crop': {'box': cr['box'], 'source': cr['source'], 'version': int(cr['source'].rsplit('.v', 1)[1][:-4]),
                               'cast': cr['cast'], 'note': cr.get('note', ''), 'source_exists': (Path(d) / cr['source']).is_file()} if cr else None,
                      'crop_from': base, 'crop_refused': why_not,
                      # for the page's viewfinder: the cut is enlarged out_width / (box width in pixels), at most `max`
                      'crop_limit': {'max': TRIAL_MAX_UPSCALE, 'out_width': DRAMA_IMAGE_SIZE[0]},
                      'staging': u['first'].get('staging'),
                      'staging_ok': len(u['first'].get('cast') or []) == 2 and not is_close(u['first']),
                      'versions': unit_versions(Path(d), u['id']),
                      'keyframe': kf, 'review': rv,
                      'keyframe_at': int((Path(d) / f'kf_{u["id"]}.png').stat().st_mtime) if kf else None,
                      'note': (review['keyframes'].get(u['id']) or {}).get('note', ''),
                      'takes': takes, 'pick': pick,
                      'recommend': rec if rec and rec.get('seed') in takes else None,
                      'unchanged': rv == 'reject' and sent_back_unchanged(story, review, 'keyframes', u['id']),
                      'state': state, 'state_label': UNIT_STATES[state], 'filter': STATE_FILTER.get(state),
                      'edits': unit_edits(review, u['id'], pick, d),
                      'lead_in': lead_of(story, u['id']),
                      'changed': unit_changed(u['id'], pick, changes, cut) or cut_differs(cut, u['id'], unit_edits(review, u['id'], pick, d),
                                                                                         lead_of(story, u['id']))})
    if not assets_ok:
        stage = 'assets'
    elif not all(s['keyframe'] and s['review'] == 'pass' for s in shots):
        stage = 'keyframes'
    elif not all(s['takes'] and s['pick'] for s in shots):
        stage = 'takes'
    else:
        stage = 'cut'
    # earlier versions of each asset picture (a wardrobe change, a new look): cast_x_face.v1.png …
    prev = {}
    for f in asset_files(story):
        stem = f[:-4]
        vs = sorted(int(m.group(1)) for x in have for m in [re.fullmatch(re.escape(stem) + r'\.v(\d+)\.png', x)] if m)
        if vs:
            prev[f] = vs
    cast = [{'id': c['id'], 'name': c['name'], 'look_young': bool(c.get('look_young')), 'outfit': c.get('outfit'),
             'outfit_src': c.get('outfit_src'),
             # outfit_ref: clothes from a picture (file under the job folder; face = the portrait version that says who it is)
             'outfit_ref': ({**c['outfit_ref'], 'exists': all((Path(d) / c['outfit_ref'][k]).is_file() for k in ('file', 'face'))}
                            if c.get('outfit_ref') else None),
             'voice_ref': ({**c['voice_ref'], 'exists': (Path(d) / c['voice_ref']['file']).is_file()} if c.get('voice_ref') else None),
             'voice_lines': voice_candidates(d, story, c['id'])} for c in story['cast']]
    return {'stage': stage, 'assets': assets, 'asset_prev': prev, 'staged': load_staged(d), 'shots': shots, 'units': shots, 'final': final,
            'cast': cast,
            'filter': drama_filter(shots, assets, final), 'spend': next_spend(shots, assets),
            'warnings': story_warnings(story), 'budget': budget_state(job, ledger or [])}


def _load_list(p):
    return json.loads(p.read_text()) if p.exists() else []


def cmd_drama_status(args, cfg):
    d, job = drama_dir(args.jobdir)
    p = d / 'progress.json'
    prog = json.loads(p.read_text()) if p.exists() else None
    budget = budget_state(job, load_ledger(d))
    empty = {'stage': 'script', 'progress': prog, 'budget': budget, 'units': [], 'shots': [], 'final': False,
             'filter': drama_filter([], {}, False, story=False), 'spend': None, 'staged': load_staged(d), 'precheck': {}}
    if not (d / 'story.json').exists():
        return empty
    try:
        story = load_story(d)
    except (ShiseiError, json.JSONDecodeError) as e:
        return {**empty, 'story_error': str(e)}
    cut = json.loads((d / 'cut.json').read_text()) if (d / 'cut.json').exists() else None
    return {**drama_summary(d, job, story, load_review(d), load_recommend(d), load_ledger(d), _load_list(d / 'changes.json'), cut),
            'progress': prog, 'precheck': current_precheck(d)}


@locks_review
def cmd_drama_review(args, cfg):
    user_only(args.kind, args.value, in_agent_shell())
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    if args.kind == 'pick':
        if args.key not in {u['id'] for u in units(story)} or (args.value != 'none' and args.value not in {str(x) for x in DRAMA_SEEDS}):
            raise ShiseiError('unknown shot or seed')
        if args.value == 'none':   # taking a pick back (the page's undo): the segment waits for a click again
            review['picks'].pop(args.key, None)
        elif precheck_verdict(d, f'take_{args.key}_{args.value}.mp4') == 'block':
            raise ShiseiError('这一条的预审看到裸露，而且人物像未成年：不能选。换一条，或回到首帧重画。')
        else:
            review['picks'][args.key] = int(args.value)
    else:
        valid = set(asset_files(story)) if args.kind == 'assets' else {u['id'] for u in units(story)}
        if args.key not in valid or args.value not in ('pass', 'reject', 'pending'):
            raise ShiseiError(f'unknown {args.kind} item or verdict')
        if args.kind == 'keyframes' and args.value == 'pass' and precheck_verdict(d, f'kf_{args.key}.png') == 'block':
            raise ShiseiError('这张首帧的预审看到裸露，而且人物像未成年：不能通过。重画、换装或裁掉，不要点通过。')
        review[args.kind][args.key] = {'status': args.value, 'note': (args.note or '')[:300], 'at': time.time()}
        if args.value == 'reject':   # what it was drawn from: until that changes, making it again draws the same thing
            review[args.kind][args.key]['inputs'] = input_print(story, args.kind, args.key)
    save_json_atomic(d / 'review.json', review)
    return {'review': review}


def input_print(story, kind, key):
    """A short fingerprint of what a picture is drawn from (the prompt and input images the workflow gets)."""
    if kind == 'assets':
        what = asset_prompt(story, key)
    else:
        u = next(x for x in units(story) if x['id'] == key)
        what = unit_inputs(story, u)['frame']
    return hashlib.sha1(json.dumps(what, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:16]


def sent_back_unchanged(story, review, kind, key):
    """Sent back, and nothing it is drawn from has changed since: making it again would most likely give the same."""
    r = (review.get(kind) or {}).get(key) or {}
    return r.get('status') == 'reject' and bool(r.get('inputs')) and r['inputs'] == input_print(story, kind, key)


def cmd_drama_recommend(args, cfg):
    """The director's suggestion for which take to use, with a reason. Writes recommend.json only, never review.json:
    a take enters the cut when the user clicks it on the Piying page."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    u = next((x for x in units(story) if args.unit in (x['id'], x['label'])), None)
    if u is None:
        raise ShiseiError(f'unknown segment {args.unit}')
    rec = load_recommend(d)
    if args.clear:
        rec.pop(u['id'], None)
    else:
        takes = [s for s in DRAMA_SEEDS if (d / f'take_{u["id"]}_{s}.mp4').exists()]
        seed = resolve_take(args.take, takes)
        why = (args.why or '').strip()
        if not why:
            raise ShiseiError('give the reason with --why: the user reads it next to the take')
        rec[u['id']] = {'seed': seed, 'take': take_number(seed), 'why': why[:200], 'at': time.time()}
    save_json_atomic(d / 'recommend.json', rec)
    return {'unit': u['id'], 'label': u['label'], 'recommend': rec.get(u['id']),
            'note': 'a suggestion only: the take is used when the user clicks it on the Piying page'}


def cmd_drama_budget(args, cfg):
    """What this project has spent on GPU runs, against its cap. Read-only: the cap is changed by the user on the page."""
    d, job = drama_dir(args.jobdir)
    ledger = load_ledger(d)
    return {'budget': budget_state(job, ledger), 'ledger': ledger,
            'note': 'cost is GPU run time × hourly price (estimate), including the idle minutes before the automatic stop'}


def cmd_drama_estimate(args, cfg):
    d, job = drama_dir(args.jobdir)
    story = load_story(d)
    params = job.get('params') or {}
    review, have, quality = load_review(d), _have(d), params.get('quality') or '576p'
    have -= set(getattr(args, 'assume_missing', None) or [])   # the price of a change the page is about to make
    seeds = int(args.seeds or params.get('seeds') or 1)
    running, machine = args.running, None
    if args.price is not None:
        price = args.price
    else:
        info = instance_info(cfg)
        price = float(info['InstancePrice'])
        running = running or (info.get('State') == 'Running' and not QUOTE_QUEUED)   # queued: from cold
        machine = {'id': info.get('UHostId'), 'name': info.get('Name'), 'gpu': info.get('GpuType'), 'zone': info.get('Zone'),
                   'state': info.get('State')}

    # --pending-ok: the price of a run that cannot start yet (pictures wait for the user). Only a price: `blocked` says
    # what has to be approved first, and drama-run (and the page's start, which asks without this flag) still refuse.
    pending_ok = bool(getattr(args, 'pending_ok', False))

    def est(n):
        todo = drama_plan(story, review, args.stage, have, n, args.shots, gate=not pending_ok)
        return {**drama_estimate(story, todo, args.stage, quality, price, running), 'todo': todo, 'seeds': n}

    out = est(seeds)
    if pending_ok:
        b = plan_blockers(story, review, args.stage, args.shots)
        out['blocked'] = b if b['assets'] or b['keyframes'] else None
    budget = budget_state(job, ledger_if_started(load_ledger(d), time.time(), running))
    # the idle stop the run arms is billed too: `over` counts it; `over_stop` is the same run stopped right after
    idle_min, idle_yuan = idle_cost(cfg, price) if out['items'] else (0, 0.0)
    out.update(running=bool(running), machine=machine, queued=QUOTE_QUEUED, idle_minutes=idle_min, idle_cost=idle_yuan,
               budget={**budget, 'over': over_budget(budget, out['cost'] + idle_yuan), 'over_stop': over_budget(budget, out['cost'])})
    if args.stage == 'takes' and seeds > 1 and out['budget']['over'] > 0:
        c = est(1)   # the cheapest sensible run: one take per segment, more only if the first is not good enough
        if c['items']:
            out['cheaper'] = {'seeds': 1, 'items': c['items'], 'minutes': c['minutes'], 'cost': c['cost'],
                              'over': over_budget(budget, c['cost'] + idle_yuan), 'over_stop': over_budget(budget, c['cost'])}
    return out


def drama_queue_progress(stage, todo, q, now=None):
    """A drama run's progress while it waits for a machine (pure): phase `queue`, who holds what."""
    now = time.time() if now is None else now
    return {'stage': stage, 'state': 'running', 'pid': os.getpid(), 'started_at': q['since'], 'error': None,
            'phase': 'queue', 'items': {f: 'pending' for f in todo}, 'log': [], 'queue': q, 'updated_at': now}


def _drama_refused(d, stage, todo, err, **extra):
    """A drama run that never got a machine (queue ran out, price changed, cancelled): nothing started, nothing billed."""
    p = {'stage': stage, 'state': 'failed', 'pid': None, 'error': str(err)[:600], 'phase': 'boot',
         'items': {f: 'pending' for f in todo}, 'log': [], 'refused': True, 'updated_at': time.time()}
    p.update(extra)
    save_json_atomic(Path(d) / 'progress.json', p)


def _drama_queue(cfg, d, args, todo):
    """Queue a drama run until a machine is free. None when it got one (its lease is taken); else what the run returns."""
    try:
        _queue(cfg, d, args.queue_minutes, lambda q: save_json_atomic(Path(d) / 'progress.json', drama_queue_progress(args.stage, todo, q)),
               getattr(args, 'machine', None) or os.environ.get('SHISEI_MACHINE') or None)
        return None
    except StopRequested:
        _drama_refused(d, args.stage, todo, '', state='stopped', cancelled=True, error=None, refused=None)
        (Path(d) / 'stop').unlink(missing_ok=True)
        return {'state': 'stopped', 'cancelled': True}
    except MachineBusy as e:
        _drama_refused(d, args.stage, todo, e)
        raise ShiseiError(str(e))


class DramaRun:
    def __init__(self, d, stage, todo):
        self.dir, self.path = Path(d), Path(d) / 'progress.json'
        self.p = {'stage': stage, 'state': 'running', 'pid': os.getpid(), 'started_at': time.time(), 'error': None,
                  'phase': 'boot', 'items': {f: 'pending' for f in todo}, 'log': []}
        self.save()

    def save(self):
        self.p['updated_at'] = time.time()
        save_json_atomic(self.path, self.p)

    def note(self, msg):
        self.p['log'] = (self.p['log'] + [f'{time.strftime("%H:%M:%S")} {msg}'])[-40:]
        if 'retrying in 120 s' in msg:
            self.p.update(phase='waiting', retry_at=time.time() + 120)
        elif msg.startswith('starting the instance') and self.p.get('phase') == 'waiting':
            self.p.update(phase='boot', retry_at=None)   # it bills from here: the page drops 取消等待
        self.save()

    def phase(self, name):
        self.p['phase'] = name
        self.save()

    def item(self, name, status):
        self.p['items'][name] = status
        self.save()

    def check_stop(self):
        if (self.dir / 'stop').exists():
            raise StopRequested()


def _object_info_missing(classes):
    missing = []
    for c in classes:
        try:
            d = json.load(urllib.request.urlopen(f'{comfy_base()}/object_info/{c}', timeout=20))
        except (urllib.error.URLError, socket.timeout) as e:
            raise ShiseiError(f'ComfyUI not reachable for preflight: {e}')
        if c not in d:
            missing.append(c)
    return missing


def _remote_cp_to_input(cfg, info, out_rel, remote_name):
    ssh_run(cfg, info, f'cp {REMOTE_COMFY}/output/{out_rel} {REMOTE_COMFY}/input/{remote_name}')


def drama_api(story, rid, stage, f, quality, attempts):
    """The ComfyUI workflow that makes output file `f` of a stage (remote input names are prefixed with the job id)."""
    stem = f.rsplit('.', 1)[0]
    prefix = f'{rid}_{stem}'
    if stage == 'takes' and f.startswith('vo_'):
        b = next(x for x in story['shots'] if x['id'] == f[3:-4])
        line = b['line']
        refs = [f'cast_{line["who"]}_body.png', f'cast_{line["who"]}_face.png']
        secs = max(2.0, min(8.0, len(norm_line(line['text'])) / 4 + 1.5))
        return patch_drama_take(load_workflow('drama_take'), voice_prompt(story, line), 1001, prefix,
                                f'{rid}_cast_{line["who"]}_face.png', [f'{rid}_{r}' for r in refs], DRAMA_SIZES['576p'], frames_for(secs),
                                [remote_input(rid, f) for _, f in voice_audios(story, [line['who']])])
    if stage == 'takes':
        sid, seed = f.split('_')[1], int(f.rsplit('_', 1)[1][:-4])
        u = unit_by_id(story, sid)
        prompt, refs = take_prompt(story, u)
        return patch_drama_take(load_workflow('drama_take'), prompt, seed, prefix, f'{rid}_kf_{sid}.png',
                                [f'{rid}_{r}' for r in refs], DRAMA_SIZES[quality], frames_for(u['seconds']),
                                [remote_input(rid, f) for f in take_audios(story, u)])
    if stage == 'assets':
        prompt, imgs, size = asset_prompt(story, f)
    else:
        uid = f[3:-4]
        # prev_keyframe(story, uid) as image 1 was tried on GPU (2026-11-10) and is off: Qwen edits that frame instead of
        # drawing a new one (a character appeared twice, a seated man stayed standing). Sides come from the text instead.
        prompt, imgs = keyframe_prompt(story, _by_id(story, 'shots', uid))
        size = DRAMA_IMAGE_SIZE
    return patch_drama_image(load_workflow('drama_image'), prompt, drama_seed(stem, attempts.get(stem, 0)), prefix,
                             [remote_input(rid, i) for i in imgs], size)


def stage_prepare(d, story, stage, todo):
    """Local work a run needs before anything is uploaded, checked before the machine starts: for every cropped first
    frame in `todo` the ffmpeg argv that cuts crop/kf_<id>.in.png out of the archived source. Raises ShiseiError when
    a source picture is gone or the character has since been marked look_young (also for clothes pictures)."""
    d = Path(d)
    out = []
    for f in todo:
        if stage == 'keyframes':
            sh = _by_id(story, 'shots', f[3:-4])
            cr = sh.get('crop')
            if not cr:
                continue
            young = [c['name'] for c in story['cast'] if c['id'] in (sh.get('cast') or []) and c.get('look_young')]
            if young:
                raise ShiseiError(f'{"、".join(young)} is marked as looking young: no close-up cuts. The user removes the crop '
                                  f'of shot {sh["id"]} on the Piying page (drama-crop --clear)')
            src = d / cr['source']
            if not src.is_file():
                raise ShiseiError(f'the picture the close-up of shot {sh["id"]} is cut from is gone ({cr["source"]}): '
                                  'the user removes the crop (drama-crop --clear) or draws the box again')
            out.append(crop_args(src, d / crop_input(sh['id']), crop_geometry(*png_size(src), tuple(cr['box']))))
        elif stage == 'assets' and f.startswith('cast_'):
            c = _cast(story, f[5:-4].rsplit('_', 1)[0])
            ph = c.get('photo_ref')
            if ph and f.endswith('_face.png') and not c.get('outfit_ref') and not is_young(c) and not (d / ph['file']).is_file():
                raise ShiseiError(f'{c["name"]}: the photo the portrait is drawn after is gone ({ph["file"]}): add the photo again on the series page')
            ref = c.get('outfit_ref')
            if not ref:
                continue
            if c.get('look_young'):
                raise ShiseiError(f'{c["name"]} is marked as looking young: no redraws from a picture. The user changes the '
                                  'clothes again on the Piying page (library clothes, as words)')
            gone = [x for x in (ref['face'], ref['file']) if not (d / x).is_file()]
            if gone:
                raise ShiseiError(f'{c["name"]}: pictures for the redraw are gone ({", ".join(gone)}): change the clothes again')
    return out


def _arm_idle_stop(cfg, run):
    """After a failure or alert the machine may still be up: never leave it on the 3 h schedule."""
    if never_claimed():
        return
    idle = int(cfg.get('idle_stop_minutes') or 10)
    try:
        schedule_stop(cfg, f'{idle}m')
        run.p['auto_stop_at'] = time.time() + idle * 60
        cmd_tunnel(argparse.Namespace(close=True), cfg)
    except (ShiseiError, subprocess.CalledProcessError) as e:
        log(f'could not arm the idle stop: {e}')


def retire_rejected(d, stage, f, story, review):
    """A rejected picture drama-run is about to remake. Its attempt count goes up (the seed changes, and it is saved
    before the paid step, so a run that fails later never reuses it). A rejected first frame whose segment already
    has takes or a pick: those were made from the old picture, so the whole segment becomes a version (reset_unit:
    takes, inner voices, pick and suggestion move aside, nothing is deleted) and the change is logged. Anything else
    is kept beside the new one for comparison (kf_x_rN.png). Never approves or picks. Changes `review` in place."""
    d = Path(d)
    stem, ext = f.rsplit('.', 1)
    n = review.setdefault('attempts', {}).get(stem, 0) + 1
    review['attempts'][stem] = n
    if stage == 'keyframes':
        uid = f[3:-4]
        if uid in review['picks'] or any((d / f'take_{uid}_{s}.mp4').exists() for s in DRAMA_SEEDS):
            u = unit_by_id(story, uid)
            r = reset_unit(d, u, True, review)
            log_change(d, {'kind': 'remake', 'unit': uid, 'label': u['label'], 'version': r['version'], 'shot': None,
                           'why': '', 'redo': r['redo']})
            return n
    m = re.match(r'cast_(.+)_(face|body)$', stem) if stage == 'assets' else None
    if m:   # as apply_story does for a changed character: segments that show them are marked 有改动, not re-shot
        for u in units(story):
            if m.group(1) in u['cast'] and (d / f'kf_{u["id"]}.png').exists():
                log_change(d, {'kind': 'cast', 'unit': u['id'], 'label': u['label'], 'cast': m.group(1), 'shot': None,
                               'why': '', 'redo': []})
    (d / f).replace(d / f'{stem}_r{n}.{ext}')
    return n


def landed(review, story, stage, f):
    """review.json once a remade file is in place: the verdict on the old file goes (the user reviews the new one), and
    a first frame notes what it was drawn from (record_frames). Changes `review` in place."""
    if stage == 'assets':
        review['assets'].pop(f, None)
    elif stage == 'keyframes':
        review['keyframes'].pop(f[3:-4], None)
        record_frames(review, story, [f[3:-4]])
    return review


def cmd_drama_run(args, cfg):
    global LOG_HOOK, STOP_CHECK
    d, job = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    params = job.get('params') or {}
    quality = params.get('quality') or '576p'
    seeds = int(args.seeds or params.get('seeds') or 1)
    todo = drama_plan(story, review, args.stage, _have(d), seeds, args.shots)
    if not todo:
        return {'state': 'done', 'made': [], 'note': 'nothing to make'}
    for f in todo:   # every workflow is built before the machine starts, so story mistakes cost nothing
        drama_api(story, job['id'], args.stage, f, quality, review.get('attempts', {}))
    prepare = stage_prepare(d, story, args.stage, todo)
    if not args.confirm_billing:
        raise ShiseiError('billing was not confirmed: tell the user the price (drama-estimate) and let them start it')
    queued = getattr(args, 'queue_minutes', None)
    if queued:
        out = _drama_queue(cfg, d, args, todo)
        if out:
            return out
    before = instance_info(cfg)
    was_running = before.get('State') == 'Running'
    price = float(before.get('InstancePrice') or 0)
    stop_after = bool(getattr(args, 'stop_after', False))
    # the cap is checked with the same numbers drama-estimate shows (the run plus the idle stop it arms), before a run
    # record exists or the machine starts
    idle_min, idle_yuan = idle_cost(cfg, price, stop_after)
    amount = round(drama_estimate(story, todo, args.stage, quality, price, was_running)['cost'] + idle_yuan, 2)
    if queued:
        try:
            max_cost_check(amount, getattr(args, 'max_cost', None))
        except MachineBusy as e:
            _drama_refused(d, args.stage, todo, e)
            raise ShiseiError(str(e))
    budget_check(budget_state(job, ledger_if_started(load_ledger(d), time.time(), was_running)), amount)
    if prepare:   # close-up cuts, made locally before the machine starts: a failure here costs nothing
        (d / CROP_DIR).mkdir(exist_ok=True)
        for argv in prepare:
            r = subprocess.run(argv, capture_output=True, text=True)
            if r.returncode != 0:
                raise ShiseiError(f'could not cut the close-up: {r.stderr.strip()[-300:]}')
    run = DramaRun(d, args.stage, todo)
    LOG_HOOK = run.note
    STOP_CHECK = run.check_stop   # a stop while waiting for stock ends the wait (nothing started)
    ledgered = False   # the ledger line is written once: at the end of the GPU part, or in `finally`
    if was_running:   # the idle stop our last run armed ends here: charge only the gap
        settle_idle(d, run.p['started_at'])
    rid = job['id']
    group = 'h3' if args.stage == 'takes' else 'qwen'
    try:
        up = cmd_up(argparse.Namespace(confirm_billing=args.confirm_billing, max_wait_min=60, gpu_attempts=3), cfg)
        if up.get('needs_confirmation'):
            raise ShiseiError('billing was not confirmed')
        info = instance_info(cfg)
        run.p['billing'] = {'since': info.get('StartTime') or time.time(), 'price_per_hour': info.get('InstancePrice'),
                            'run_since': billing_start(run.p['started_at'], was_running, info.get('StartTime'))}
        if up.get('alert'):
            _arm_idle_stop(cfg, run)
            idle_min = idle_cost(cfg, price)[0]
            run.p.update(state='alert', alert=up.get('secure'))
            return {'state': 'alert', 'secure': up.get('secure')}
        run.phase('prepare')
        cmd_tunnel(argparse.Namespace(close=False), cfg)
        linked = ssh_run(cfg, info, ensure_models_script(group), timeout=300)
        missing = linked.strip().splitlines()[-1].split(':', 1)[1].split()
        if missing:
            raise ShiseiError('model files missing on the instance: ' + ', '.join(missing))
        nodes = _object_info_missing(DRAMA_NODES[group])
        if nodes:
            raise ShiseiError('ComfyUI on the instance lacks nodes: ' + ', '.join(nodes) + ' (update ComfyUI)')
        vram = vram_watch_start(cfg, info)
        run.check_stop()
        # inputs that already exist locally go up once; images made in this run are copied output -> input remotely
        needed = set()
        for f in todo:
            if args.stage == 'assets':
                needed |= set(asset_prompt(story, f)[1])
            elif args.stage == 'keyframes':
                needed |= set(keyframe_prompt(story, _by_id(story, 'shots', f[3:-4]))[1])
            elif f.startswith('vo_'):
                who = next(x for x in story['shots'] if x['id'] == f[3:-4])['line']['who']
                needed |= {f'cast_{who}_face.png', f'cast_{who}_body.png'} | {a for _, a in voice_audios(story, [who])}
            else:
                sh = unit_by_id(story, f.split('_')[1])
                needed |= set(take_refs(story, sh)[0]) | {f'kf_{sh["id"]}.png'} | set(take_audios(story, sh))
        for f in sorted(needed):
            if (d / f).exists():
                _upload_as(cfg, info, d / f, remote_input(rid, f))
        run.phase('generate')
        attempts = review.get('attempts', {})
        for f in todo:
            run.check_stop()
            run.item(f, 'running')
            stem = f.rsplit('.', 1)[0]
            if (d / f).exists():   # rejected: moved aside and remade with a new seed (retire_rejected)
                with review_lock(d):
                    rv = load_review(d)
                    retire_rejected(d, args.stage, f, story, rv)
                    save_json_atomic(d / 'review.json', rv)
                attempts = rv['attempts']
            prefix = f'{rid}_{stem}'
            pid = post_prompt(drama_api(story, rid, args.stage, f, quality, attempts))
            schedule_stop(cfg, working_stop(cfg))
            _wait_prompt(run, cfg, pid, f, 30)
            ext = '.mp4' if args.stage == 'takes' else '.png'
            outs = [n for n in _remote_ls(cfg, info, 'output', f'drama/{prefix}_*{ext}')]
            if not outs:
                raise ShiseiError(f'{f} finished but no output found')
            if f.startswith('vo_'):   # keep only the voice: the picture of a voice-only take is never used
                _pull_one(cfg, info, f'output/{outs[-1]}', d / f'{stem}.mp4')
                subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(d / f'{stem}.mp4'), '-vn', '-ac', '1', '-ar', '48000',
                                str(d / f)], check=True)
                (d / f'{stem}.mp4').unlink(missing_ok=True)
                run.item(f, 'done')
                continue
            _pull_one(cfg, info, f'output/{outs[-1]}', d / f)
            # the old verdict goes with the picture itself, so a run that stops later leaves no stale reject on a new file
            if args.stage in ('assets', 'keyframes'):   # takes have no verdict: review.json (the user's picks) is left alone
                with review_lock(d):
                    save_json_atomic(d / 'review.json', landed(load_review(d), story, args.stage, f))
            if ext == '.png':
                _remote_cp_to_input(cfg, info, outs[-1], f'{rid}_{f}')
            else:
                _thumb(d / f, d / f'{stem}.jpg', at=1.0)
            run.item(f, 'done')
        vram_watch_collect(cfg, info, vram, f'drama-{args.stage}', {'items': len(todo), 'quality': quality})
        if stop_after:   # the user asked for no idle minutes: stop now (only stops, deletes nothing)
            run.p['billing']['ended'] = time.time()
            try:
                cmd_down(argparse.Namespace(yes=True), cfg)
                idle_min = 0
            except ShiseiError as e:
                log(f'stop after the run failed, the idle stop stays: {e}')
                _arm_idle_stop(cfg, run)
                idle_min = idle_cost(cfg, price)[0]
            run.p.update(state='done', phase='done', auto_stop_at=None)
        else:
            idle = int(cfg.get('idle_stop_minutes') or 10)
            schedule_stop(cfg, f'{idle}m')
            cmd_tunnel(argparse.Namespace(close=True), cfg)
            run.p.update(state='done', phase='done', auto_stop_at=time.time() + idle * 60)
            run.p['billing']['ended'] = time.time()
        run.save()
        # the ledger line before the local checks (minutes of whisper and image checks): the page's 已花 is right at once,
        # and a next run started meanwhile finds this line and settles its idle stop
        bill = run.p['billing']
        append_ledger(d, ledger_entry(args.stage, sum(1 for v in run.p['items'].values() if v == 'done'),
                                      bill.get('run_since') or run.p['started_at'], bill['ended'], price, 'done', idle_min))
        ledgered = True
        # local checks run after the idle stop is armed, so they never keep the GPU billing
        if args.stage == 'takes' and whisper_bin():
            try:
                cmd_drama_check(argparse.Namespace(jobdir=str(d)), cfg)
            except (ShiseiError, subprocess.CalledProcessError) as e:
                log(f'dialogue check skipped: {e}')
        if args.stage == 'takes':
            try:
                cmd_drama_shots(argparse.Namespace(jobdir=str(d), no_look=False, force=False, timeout=120), cfg)
            except (ShiseiError, subprocess.CalledProcessError) as e:
                log(f'shot check skipped: {e}')
        if args.stage in ('keyframes', 'takes') and vision_ready(cfg):
            try:
                cmd_drama_precheck(argparse.Namespace(jobdir=str(d), kind=[args.stage], timeout=420, force=False), cfg)
            except (ShiseiError, subprocess.CalledProcessError) as e:
                log(f'image pre-check skipped: {e}')
        return {'state': 'done', 'made': todo, 'auto_stop': None if stop_after and not idle_min else f'{idle_min}m'}
    except StopRequested:
        try:
            cmd_down(argparse.Namespace(yes=True), cfg)
            idle_min = 0
        except StopFailed as e:
            log(f'stop failed: {e}')
            run.p['stop_failed'] = stop_failed_note(e)
        except ShiseiError as e:
            log(f'stop failed: {e}')
            run.p['stop_failed'] = {'error': str(e)[:400], 'auto_stop_at': None}
        run.p['cancelled'] = not UP_STARTED and not run.p.get('stop_failed')   # stopped while waiting: nothing ran
        (d / 'stop').unlink(missing_ok=True)
        run.p['state'] = 'stopped'
        return {'state': 'stopped'}
    except MachineBusy as e:
        # another project holds the machine: nothing of ours started, so no idle stop on its machine, no tunnel closed
        # under its run and no ledger line (2026-11-12: a refused start armed a 10-minute stop on the running machine)
        run.p.update(state='failed', error=str(e)[:600], refused=True)
        raise ShiseiError(str(e))
    except (ShiseiError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, StopIteration) as e:
        _arm_idle_stop(cfg, run)
        idle_min = idle_cost(cfg, price)[0]
        run.p.update(state='failed', error=str(e)[:600])
        for k, v in run.p['items'].items():
            if v == 'running':
                run.p['items'][k] = 'failed'
        raise ShiseiError(str(e))
    finally:
        run.p['pid'] = None
        if run.p['state'] == 'running':
            run.p['state'] = 'failed'
        bill = run.p.get('billing')
        if bill is None and not run.p.get('refused') and not never_claimed():   # failed somewhere inside `up`: the machine may be on all the same
            try:
                now = instance_info(cfg)
                if now.get('State') == 'Running':
                    bill = {'run_since': billing_start(run.p['started_at'], was_running, now.get('StartTime'))}
            except (ShiseiError, subprocess.CalledProcessError, KeyError):
                pass
        if bill and not ledgered:
            done = sum(1 for v in run.p['items'].values() if v == 'done')
            entry = ledger_entry(args.stage, done, bill.get('run_since') or run.p['started_at'], bill.get('ended') or time.time(),
                                 price, run.p['state'], idle_min)
            append_ledger(d, entry)
        run.save()
        LOG_HOOK = None
        STOP_CHECK = None


# ---------------------------------------------------------------- image pre-check (advice only)

KF_PROMPT = ('你在帮导演预审一张短剧关键帧。只看画面，对照分镜回答，只输出一个 JSON 对象，不要别的文字：'
             '{{"景别":"特写|近景|中景|全景","景别符合":true/false,"人数":数字,"人数符合":true/false,'
             '"看起来年龄":数字或null,"裸露":"无|部分裸露|全身裸露","性化":true/false,"道具符合":true/false,'
             '"状态符合":true/false/null,"有人看镜头":true/false,'
             '"两人互相看着":true/false/null,"像未成年":true/false,"问题":"一句话，没有就写空字符串"}}。'
             '分镜：{keyframe}。画面里应有 {n} 个人{ages}。{props}'
             '「状态符合」只看分镜里「画面里必须看得出」的那几件事（戴没戴、拿没拿、坐还是站），分镜没写就填 null。'
             '「两人互相看着」只在画面里有两个人时回答，否则填 null。'
             '「裸露」看画面里的人有没有露出不该露的部位：无；部分裸露（露点、露胸、露臀，或只穿内衣内裤）；'
             '全身裸露（全裸）。「性化」看画面是不是在突出胸、臀、腿或摆出性暗示的姿势。'
             '「问题」里写出的任何不对，必须同时在对应字段上标 false，两边不要矛盾。')
TAKE_PROMPT = ('第一张是角色定妆照，后面是同一段视频按时间抽的帧。只输出一个 JSON 对象，不要别的文字：'
               '{{"是同一个人":true/false,"把握":"高|中|低","道具一直在":true/false,'
               '"裸露":"无|部分裸露|全身裸露","性化":true/false,"看起来年龄":数字或null,"像未成年":true/false,'
               '"问题":"一句话，没有就写空字符串"}}。'
               '这一镜：{keyframe}。{props}'
               '「裸露」看帧里有没有露出不该露的部位：无；部分裸露（露点、露胸、露臀，或只穿内衣内裤）；全身裸露（全裸）。'
               '「性化」看画面是不是在突出胸、臀、腿或摆出性暗示的姿势。'
               '「问题」里写出的任何不对，必须同时在对应字段上标 false，两边不要矛盾。')
VISION_TEST_PROMPT = '只输出 JSON：{"色块数量":数字,"颜色":["..."]}。数一数白底上有几个实心色块，各是什么颜色（红/绿/蓝/黄/黑等）。'


def parse_json_reply(text):
    """The first JSON object in a model reply; None when there is none (an empty or chatty reply is not a pass)."""
    m = re.search(r'\{.*\}', text or '', re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


# the shot itself has someone look elsewhere: then "not looking at each other" is what was asked for, not a fault
LOOKS_AWAY_RE = re.compile(r'低头|低着头|低下头|垂着头|垂下头|垂眼|垂着眼|看窗外|望向窗外|望着窗外|看向车前|看着前方|看向前方|望向前方|看前方|目光移向|移开目光|背对|闭眼|闭上眼|看手机|看着自己|看向别处')


def looks_away(text):
    return bool(LOOKS_AWAY_RE.search(text or ''))


def looks_underage(r):
    """The structured sign that the person in the picture is a minor: the model's own flag, or an age under 18."""
    age = r.get('看起来年龄')
    return r.get('像未成年') is True or (isinstance(age, int) and not isinstance(age, bool) and age < 18)


def kf_findings(r):
    """Turn a keyframe reply into (verdict, lines). verdict: ok | issue | block | unknown.

    `block` is the one verdict a click cannot overrule: nudity with a person who looks underage, or full nudity
    (docs/SAFETY.md). Everything else stays advice the user judges."""
    if not r:
        return 'unknown', []
    lines, issue, block = [], False, False
    r = dict(r)
    for field in ('景别', '人数'):
        # some models answer 「景别」: true instead of naming the framing: that is the yes/no, not a value to print
        if isinstance(r.get(field), bool):
            r.setdefault(field + '符合', r.pop(field))

    def named(field):
        return f'{field}：{r[field]}' if r.get(field) not in (None, '') else field

    # `check`: which of the page's four review items (KF_CHECKS) the line is about, so the page files it there
    # the page shows no ✓ / ✗ marks, so the props line says its verdict in words
    props_said = {True: '道具对得上', False: '道具不对'}
    for key, label, check in (('景别符合', named('景别'), 0), ('人数符合', named('人数'), 0), ('道具符合', None, 2)):
        v = r.get(key)
        if v is False:
            issue = True
        lines.append({'ok': v, 'text': label if label is not None else props_said.get(v, '道具（没看出来）'), 'check': check})
    if isinstance(r.get('状态符合'), bool):   # the shot's `state`: what must be visible (戴着帷帽)
        if r['状态符合'] is False:
            issue = True
        lines.append({'ok': r['状态符合'], 'text': '分镜要求看得出的状态对得上' if r['状态符合'] else '分镜要求看得出的状态没画出来', 'check': 2})
    # two people who should be talking to each other but face the camera: the fix the page offers is 「侧脸相对」
    # (`fix` is read by the page; the model only answers yes / no, nothing here reads its free text)
    # `分镜有人看别处` is not the model's: the pre-check adds it from the shot text before judging (looks_away)
    if r.get('分镜有人看别处') and r.get('有人看镜头') is True:
        issue = True
        lines.append({'ok': False, 'text': '画面里有人看镜头', 'check': 0})
    elif r.get('分镜有人看别处'):
        pass
    elif r.get('两人互相看着') is False or (r.get('有人看镜头') is True and r.get('两人互相看着') is not True and r.get('人数') == 2):
        issue = True
        lines.append({'ok': False, 'text': '两个人没有互相看着（有人看镜头）', 'check': 0, 'fix': 'profile'})
    elif r.get('有人看镜头') is True:
        lines.append({'ok': None, 'text': '画面里有人看镜头', 'check': 0})
    if r.get('看起来年龄') is not None:
        lines.append({'ok': None, 'text': f'看起来约 {r["看起来年龄"]} 岁', 'check': 1})
    if r.get('像未成年') is True:
        issue = True
        lines.append({'ok': False, 'text': '导演觉得人物可能显小：按规则限制衣着和机位，由你判断', 'check': 3})
    young = looks_underage(r)
    nude = r.get('裸露') if r.get('裸露') in ('部分裸露', '全身裸露') else ''
    if nude:
        issue = True
        if nude == '全身裸露' or young:
            block = True
        lines.append({'ok': False, 'text': f'画面有裸露（{nude}）', 'check': 1})
    if r.get('性化') is True:
        issue = True
        if young:
            block = True
        lines.append({'ok': False, 'text': '画面有性化姿势或构图', 'check': 1})
    if young and r.get('像未成年') is not True:
        lines.append({'ok': None, 'text': '画面里的人物像未成年', 'check': 1})
    # The verdict comes only from the structured fields. 「问题」 is the model's own wording, shown beside them:
    # a free-text sentence cannot flip a frame whose fields all passed (2026-11-11).
    return ('block' if block else 'issue' if issue else 'ok'), lines


def take_findings(r):
    if not r:
        return 'unknown', []
    same, props = r.get('是同一个人'), r.get('道具一直在')
    lines = [{'ok': same, 'text': ('和定妆照是同一个人' if same is not False else '和定妆照不像同一个人') + f'（把握{r.get("把握", "?")}）'},
             {'ok': props, 'text': '道具一直在' if props is not False else '道具中途不见了'}]
    young = looks_underage(r)
    nude = r.get('裸露') if r.get('裸露') in ('部分裸露', '全身裸露') else ''
    block = nude == '全身裸露' or (young and (bool(nude) or r.get('性化') is True))
    if nude:
        lines.append({'ok': False, 'text': f'画面有裸露（{nude}）', 'check': 1})
    if r.get('性化') is True:
        lines.append({'ok': False, 'text': '画面有性化姿势或构图', 'check': 1})
    if young:
        lines.append({'ok': None, 'text': '画面里的人物像未成年', 'check': 1})
    return ('block' if block else 'issue' if any(x['ok'] is False for x in lines) else 'ok'), lines


def vision_key(cfg):
    key = os.environ.get(cfg.get('vision_key_env') or 'ANTDIGI_API_KEY', '')
    if not key:
        raise ShiseiError(f'image model key not available ({cfg.get("vision_key_env")}); set it in dsh credentials or the environment')
    return key


def vision_ready(cfg):
    """Can a picture question be asked here: through the plugin (SHISEI_VISION_URL for the page's runs, the bridge in a
    dsh model shell; the plugin picks dsh's own model or the separate one), or straight with the separate model's key."""
    return bool(os.environ.get('SHISEI_VISION_URL') or os.environ.get('DSH_SHISEI_BRIDGE')
                or os.environ.get(cfg.get('vision_key_env') or 'ANTDIGI_API_KEY'))


def need_vision(cfg):
    if not vision_ready(cfg):
        vision_key(cfg)   # raises the same message as before


def vision_model(cfg):
    """The model that answers, for the records (precheck.json, study.json): the plugin names it when it picks."""
    return os.environ.get('SHISEI_VISION_MODEL') or cfg.get('vision_model')


def small_jpeg(src, dst, width=512, at=None):
    args = ['ffmpeg', '-v', 'error', '-y'] + (['-ss', str(at)] if at is not None else []) + ['-i', str(src), '-frames:v', '1',
                                                                                          '-vf', f'scale={width}:-2', '-q:v', '4', str(dst)]
    subprocess.run(args, check=True, capture_output=True)
    return dst


def vision_ask(cfg, prompt, images, timeout=420, think=True):
    """One OpenAI-compatible chat call with images. Returns (parsed JSON or None, raw text, seconds).
    think=False asks the model not to reason first (`enable_thinking: false`): for picking fixed choices Ling-3.2-flash
    answered the same in 1.6 s instead of 80 s (2026-11-11). A server that refuses the field is asked again without it."""
    import base64
    content = [{'type': 'text', 'text': prompt}] + [
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(Path(i).read_bytes()).decode()}} for i in images]
    body = {'model': cfg.get('vision_model'), 'max_tokens': 12000 if think else 2000, 'messages': [{'role': 'user', 'content': content}]}
    if not think:
        body['enable_thinking'] = False

    def request(b):
        if os.environ.get('SHISEI_VISION_URL'):
            # the page's own runs: the plugin picks the model (dsh's default when it reads images) and adds any key
            return urllib.request.Request(os.environ['SHISEI_VISION_URL'], json.dumps(b).encode(), {'Content-Type': 'application/json'})
        if os.environ.get(cfg.get('vision_key_env') or 'ANTDIGI_API_KEY'):
            return urllib.request.Request(cfg.get('vision_base_url').rstrip('/') + '/chat/completions', json.dumps(b).encode(),
                                          {'Authorization': 'Bearer ' + vision_key(cfg), 'Content-Type': 'application/json'})
        if os.environ.get('DSH_SHISEI_BRIDGE'):
            # inside dsh the shell has no *KEY* variables; the plugin adds the key and uses the endpoint from the user's config
            return urllib.request.Request(os.environ['DSH_SHISEI_BRIDGE'] + '/vision', json.dumps(b).encode(), {'Content-Type': 'application/json'})
        vision_key(cfg)
    t0 = time.time()
    try:
        try:
            r = json.load(urllib.request.urlopen(request(body), timeout=timeout))
        except urllib.error.HTTPError as e:
            if e.code != 400 or 'enable_thinking' not in body:
                raise
            body.pop('enable_thinking')   # this server does not take the switch: ask the plain way
            r = json.load(urllib.request.urlopen(request(body), timeout=timeout))
    except urllib.error.HTTPError as e:
        raise ShiseiError(f'image model refused ({e.code}): {e.read().decode()[:200]}')
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        raise ShiseiError(f'image model did not answer in {timeout}s ({e})')
    text = ((r.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    return parse_json_reply(text), text, round(time.time() - t0, 1)


def cmd_vision_test(args, cfg):
    """Fixed test picture with known answers: three solid boxes (red, green, blue) on white. All three must be named."""
    import tempfile
    d = Path(tempfile.mkdtemp())
    img = d / 'test.jpg'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=white:s=512x512', '-frames:v', '1', '-vf',
                    'drawbox=x=40:y=60:w=120:h=120:color=red:t=fill,drawbox=x=200:y=260:w=120:h=120:color=green:t=fill,'
                    'drawbox=x=350:y=80:w=120:h=120:color=blue:t=fill', str(img)], check=True, capture_output=True)
    r, text, secs = vision_ask(cfg, VISION_TEST_PROMPT, [img], timeout=args.timeout)
    colors = ' '.join(r.get('颜色') or []) if r else ''
    ok = bool(r) and r.get('色块数量') == 3 and all(c in colors for c in ('红', '绿', '蓝'))
    return {'model': vision_model(cfg), 'sees_images': ok, 'answer': r if r else text[:200], 'seconds': secs}


def cmd_drama_precheck(args, cfg):
    """Ask the image model about keyframes and/or takes. Writes precheck.json only; never touches review.json."""
    import tempfile
    import threading
    from concurrent.futures import ThreadPoolExecutor
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    need_vision(cfg)
    p = d / 'precheck.json'
    pre = current_precheck(d)   # verdicts on pictures that have since been remade are dropped, not kept beside the new one
    lock = threading.Lock()
    tmp = Path(tempfile.mkdtemp())
    jobs = []
    for u in units(story):
        s = u['first']
        cast = [_cast(story, c) for c in s.get('cast') or []]
        props = '道具：' + '、'.join(_by_id(story, 'props', x)['name'] for x in s.get('props') or []) + '。' if s.get('props') else ''
        if 'keyframes' in args.kind and (d / f'kf_{s["id"]}.png').exists():
            ages = '（' + '、'.join(f'{c["name"]} {c["age"]} 岁' for c in cast) + '）' if cast else ''
            jobs.append((f'kf_{s["id"]}.png', 'kf', KF_PROMPT.format(keyframe=frame_words(story, s), n=1 if s.get('crop') else len(cast), ages=ages,
                                                                           props='' if s.get('crop') else props), s))
        s = {**s, 'cast': u['cast']}
        if 'takes' in args.kind and len(u['cast']) == 1:
            for seed in DRAMA_SEEDS:
                if (d / f'take_{s["id"]}_{seed}.mp4').exists():
                    jobs.append((f'take_{s["id"]}_{seed}.mp4', 'take', TAKE_PROMPT.format(keyframe=s['keyframe'], props=props), s))

    def save():
        with lock:
            save_json_atomic(p, pre)

    def one(job):
        name, kind, prompt, s = job
        f = d / name
        old = pre.get(name) or {}
        if old.get('status') == 'done' and old.get('mtime') == f.stat().st_mtime and not args.force:
            return
        with lock:
            pre[name] = {'status': 'running', 'mtime': f.stat().st_mtime, 'model': vision_model(cfg), 'started_at': time.time()}
        save()
        stem = name.rsplit('.', 1)[0]
        if kind == 'kf':
            imgs = [small_jpeg(f, tmp / f'{stem}.jpg')]
        else:
            imgs = [small_jpeg(d / f'cast_{s["cast"][0]}_face.png', tmp / f'{stem}_face.jpg')]
            dur = ffprobe_duration(f) or 5
            imgs += [small_jpeg(f, tmp / f'{stem}_{i}.jpg', width=320, at=round(dur * x, 2)) for i, x in enumerate((0.1, 0.5, 0.92))]
        err = None
        for attempt in range(2):
            try:
                # fixed-choice answers: no reasoning first (same choice, seconds instead of minutes; see vision_ask)
                r, text, secs = vision_ask(cfg, prompt, imgs, timeout=args.timeout, think=False)
                if r is None and attempt == 0:
                    err = '没看成：回复不是 JSON'
                    continue   # an empty or chatty reply is retried once, never read as a pass
                if kind == 'kf' and isinstance(r, dict) and looks_away(frame_words(story, s)):
                    r['分镜有人看别处'] = True
                verdict, lines = (kf_findings if kind == 'kf' else take_findings)(r)
                entry = {'status': 'done' if r else 'unreadable', 'verdict': verdict, 'lines': lines,
                         'issue': (r or {}).get('问题', '') if r else '没看成：回复不是 JSON', 'raw': r, 'seconds': secs}
                with lock:
                    pre[name].update(entry, at=time.time())
                save()
                return
            except ShiseiError as e:
                err = str(e)
        with lock:
            pre[name].update(status='failed', error=err[:300], at=time.time())
        save()

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, jobs))
    shutil.rmtree(tmp, ignore_errors=True)
    return {'checked': [j[0] for j in jobs], 'precheck': pre}


# ---------------------------------------------------------------- 对标片 (drama-study)
# A reference video the user brings (对标片). Only its structure is learned: where it cuts, how long each shot runs,
# framing, camera move, light, how many people, what each shot does in the story, and measured brightness and colour.
# The people in it, their looks, the words and the pictures never go into the project: the look fields are fixed
# choices (no free text), the files stay on this computer (study_*), and nothing feeds them to a generator.
STUDY_FILE = 'study.json'
STUDY_SRC = 'study_src'            # study_src.<ext>: the video as the page uploaded it (or --video copied it)
STUDY_EXTS = ('mp4', 'mov', 'webm', 'm4v')
STUDY_MAX_SECONDS = 180
STUDY_MIN_SHOT = 0.4               # cuts closer than this are flashes, not shots
STUDY_THRESHOLD = 10.0             # ffmpeg scdet score: 10 found every segment boundary of a 40 s Piying cut, 15 missed two
STUDY_LOOK = {
    'framing': ('特写', '近景', '中景', '全景', '远景'),
    'move': ('固定', '推', '拉', '摇', '跟', '手持'),
    'light': ('顺光', '侧光', '逆光', '夜景'),
    'people': ('0', '1', '2', '多'),
    'role': ('抓注意', '交代', '补信息', '转折', '给结论'),
}
STUDY_LOOK_ZH = {'framing': '景别', 'move': '运镜', 'light': '光', 'people': '人数', 'role': '作用'}
STUDY_BATCH = 6                    # shots per image-model call; without reasoning 5 sheets took 3-5 s (with it, over 10 min)
STUDY_PROMPT = ('你在帮导演拆一条对标短视频的镜头语言（只学拍法）。下面按顺序给你 {k} 张图，每张是一个镜头的首、中、尾三帧（从左到右），'
                '依次是镜头 {nums}。这条片子共 {total} 个镜头、{secs} 秒。'
                '只看拍法：不要描述人物的长相、衣着、身份，不要转述画面里的文字。每个镜头从给定选项里各选一个，只输出一个 JSON 对象，不要别的文字：'
                '{{"镜头":[{{"n":镜头号,"景别":"特写|近景|中景|全景|远景","运镜":"固定|推|拉|摇|跟|手持","光":"顺光|侧光|逆光|夜景",'
                '"人数":"0|1|2|多","作用":"抓注意|交代|补信息|转折|给结论"}}]}}。'
                '「作用」是这个镜头在整条片子里的用处：开头抓人是抓注意，交代地点和人物是交代，补充细节是补信息，情绪或局面变了是转折，结尾的落点是给结论。')


def parse_scdet(text):
    """Cut times (seconds) from ffmpeg scdet log lines."""
    return sorted({round(float(m), 3) for m in re.findall(r'lavfi\.scd\.time:\s*([0-9.]+)', text or '')})


def shots_from_cuts(cuts, duration, min_len=STUDY_MIN_SHOT):
    """[(t0, t1)] covering 0..duration; a cut closer than min_len to the one before (or to the end) is dropped."""
    edges = [0.0]
    for c in sorted(cuts):
        if c - edges[-1] >= min_len and duration - c >= min_len:
            edges.append(c)
    edges.append(float(duration))
    return [(round(a, 3), round(b, 3)) for a, b in zip(edges, edges[1:])]


def tone_words(m):
    """Mid-frame stats (y, u, v, sat on ffmpeg's 0..255 scale) -> words: 暖 / 冷 / 中性, 偏暗 / 偏亮, 低饱和 / 浓."""
    warm = (m['v'] - 128) - (m['u'] - 128)
    out = ['暖' if warm > 4 else '冷' if warm < -4 else '中性']
    if m['y'] < 80:
        out.append('偏暗')
    elif m['y'] > 165:
        out.append('偏亮')
    if m['sat'] < 12:
        out.append('低饱和')
    elif m['sat'] > 45:
        out.append('浓')
    return out


def clean_look(raw, strict=False):
    """Keep only the fixed choices of STUDY_LOOK (keys in English or Chinese). strict: refuse anything else."""
    out, bad = {}, []
    for k, opts in STUDY_LOOK.items():
        v = raw.get(k, raw.get(STUDY_LOOK_ZH[k])) if isinstance(raw, dict) else None
        if v is None or v == '':
            continue
        v = str(v).strip()
        if k == 'people' and v.isdigit() and int(v) > 2:
            v = '多'
        if v in opts:
            out[k] = v
        else:
            bad.append(f'{k} {v!r} (one of {" ".join(opts)})')
    if strict and bad:
        raise ShiseiError('not one of the choices: ' + '; '.join(bad))
    return out


def study_source(d):
    return next((f for f in sorted(Path(d).glob(STUDY_SRC + '.*')) if f.suffix.lower().lstrip('.') in STUDY_EXTS), None)


def load_study(d):
    p = Path(d) / STUDY_FILE
    if not p.exists():
        raise ShiseiError('no reference video in this project yet (drama-study <dir> --video <file>)')
    return json.loads(p.read_text())


STUDY_MIN_BEAT = 1.5   # our shortest shot when following a reference (study_beats)


def study_estimate(study, seeds, quality, price_per_hour):
    """What following the whole reference would cost before there is a script: the same minutes as drama_estimate, for
    the plan's segments and a guessed cast (people in frame, at most 2: two pictures each) plus one scene and one prop,
    each stage started from a stopped machine. A rough number; the 拍 button prices each real run."""
    m = DRAMA_MINUTES
    plan = study.get('plan') or study_plan(study.get('shots') or [])
    if not plan:
        return None
    people = (study.get('summary') or {}).get('people')
    cast = 2 if people in (None, '多') else max(1, min(2, int(people)))
    parts = {}
    for stage, n, work in (('assets', 2 * cast + 2, m['image'] * (2 * cast + 2)),
                           ('keyframes', len(plan), m['image'] * len(plan)),
                           ('takes', len(plan) * seeds, sum(m[f'take_{quality}'] * max(1.0, g['seconds'] / 5) for g in plan) * seeds)):
        minutes = round(m['boot'] + m['load'] + work + m['pull'] * n, 1)
        parts[stage] = {'items': n, 'minutes': minutes, 'cost': round(minutes / 60 * price_per_hour, 2)}
    return {'segments': len(plan), 'cast': cast, 'seeds': seeds, 'quality': quality, 'price_per_hour': price_per_hour,
            'parts': parts, 'minutes': round(sum(x['minutes'] for x in parts.values()), 1),
            'cost': round(sum(x['cost'] for x in parts.values()), 2), 'note': 'rough: before the script; each run is priced again'}


def cmd_drama_study_estimate(args, cfg):
    """The rough price of shooting the whole reference's rhythm (study_estimate) against the project's cap."""
    d, job = drama_dir(args.jobdir)
    study = load_study(d)
    params = job.get('params') or {}
    price = args.price if args.price is not None else float(instance_info(cfg)['InstancePrice'])
    est = study_estimate(study, int(params.get('seeds') or 1), params.get('quality') or '576p', price)
    if est is None:
        raise ShiseiError('the reference has no shots')
    budget = budget_state(job, load_ledger(d))
    return {**est, 'budget': {**budget, 'over': over_budget(budget, est['cost'])}}


def study_beats(shots, min_beat=STUDY_MIN_BEAT):
    """Join flashes into beats our shots can be (at least min_beat s; a shot is 1..10 s): [{'from', 'to', 'seconds'}]."""
    beats = []
    for s in shots:
        sec = s['t1'] - s['t0']
        if beats and beats[-1]['seconds'] < min_beat:
            beats[-1]['to'] = s['n']
            beats[-1]['seconds'] += sec
        else:
            beats.append({'from': s['n'], 'to': s['n'], 'seconds': sec})
    if len(beats) > 1 and beats[-1]['seconds'] < min_beat:   # a short tail joins the beat before it
        b = beats.pop()
        beats[-1]['to'] = b['to']
        beats[-1]['seconds'] += b['seconds']
    out = []
    for b in beats:   # a reference shot longer than one of ours (10 s) becomes several of the same
        k = max(1, math.ceil(b['seconds'] / 10.0 - 1e-9))
        out += [{**b, 'seconds': b['seconds'] / k} for _ in range(k)]
    return out


def study_plan(shots, max_seg=10.0, max_beats=4):
    """The reference's shots grouped into our segments (one H3 generation each, 2..MAX_UNIT_SECONDS s, at most
    `max_beats` shots), keeping its rhythm: [{'from', 'to', 'seconds', 'beats': [{'from', 'to', 'seconds'}]}].
    At most 60 shots in all (validate_story): flashes are joined more until it fits."""
    if not shots:
        return []
    min_beat = STUDY_MIN_BEAT
    beats = study_beats(shots, min_beat)
    while len(beats) > 60:
        min_beat += 0.5
        beats = study_beats(shots, min_beat)
    # fewest segments (each one is a GPU generation), up to MAX_UNIT_SECONDS but preferably max_seg, none under 2 s:
    # cost per segment 1, plus a little for every second over max_seg and for a very short one
    def cost(sec):
        return 1 + max(0.0, sec - max_seg) * 0.5 + (0.3 if sec < 4 else 0)
    n = len(beats)
    best = [(0.0, None)] + [(math.inf, None)] * n
    for j in range(1, n + 1):
        sec = 0.0
        for i in range(j - 1, max(-1, j - 1 - max_beats), -1):
            sec += beats[i]['seconds']
            if sec > MAX_UNIT_SECONDS + 1e-6 and i < j - 1:
                break
            c = best[i][0] + cost(sec) + (5 if sec < 2 else 0)
            if c < best[j][0]:
                best[j] = (c, i)
    cuts, j = [], n
    while j:
        cuts.append((best[j][1], j))
        j = best[j][1]
    segs = []
    for i, j in reversed(cuts):
        bs = beats[i:j]
        segs.append({'from': bs[0]['from'], 'to': bs[-1]['to'], 'seconds': sum(b['seconds'] for b in bs), 'beats': bs})
    for g in segs:
        for b in g['beats']:
            b['seconds'] = min(10.0, max(1.0, round(b['seconds'] * 2) / 2))   # our shots: 1..10 s in half seconds
        while sum(b['seconds'] for b in g['beats']) > MAX_UNIT_SECONDS:      # rounding up must not break the 12 s limit
            max(g['beats'], key=lambda b: b['seconds'])['seconds'] -= 0.5
        g['seconds'] = round(sum(b['seconds'] for b in g['beats']), 1)
    return segs


def study_summary(study):
    """Numbers for the page's one-line verdict and the director: hook (first 3 s), turn, ending, mix, tone, scale.
    Sentences are made by the page (in its language) and by the director."""
    shots = study.get('shots') or []
    if not shots:
        return None
    total = shots[-1]['t1']

    def lk(s, k):
        return (s.get('look') or {}).get(k)
    turn = next((s for s in shots if lk(s, 'role') == '转折'), None)
    last = shots[-1]
    framing = Counter(lk(s, 'framing') for s in shots if lk(s, 'framing'))
    tone = Counter(w for s in shots for w in s.get('tone') or [])
    people = [lk(s, 'people') for s in shots if lk(s, 'people')]
    return {'shots': len(shots), 'seconds': round(total, 1), 'avg': round(total / len(shots), 1),
            'hook': {'seconds': round(min(3.0, total), 1), 'cuts': sum(1 for s in shots if 0 < s['t0'] < 3.0),
                     'framing': [lk(s, 'framing') for s in shots if s['t0'] < 3.0 and lk(s, 'framing')]},
            'turn': {'n': turn['n'], 't': round(turn['t0'], 1)} if turn else None,
            'ending': {'n': last['n'], 'seconds': round(last['t1'] - last['t0'], 1), 'framing': lk(last, 'framing'), 'light': lk(last, 'light')},
            'framing': [k for k, _ in framing.most_common(2)], 'tone': [k for k, _ in tone.most_common(2)],
            'people': ('多' if '多' in people else max(people)) if people else None,
            'looked': sum(1 for s in shots if s.get('look')), 'segments': len(study_plan(shots)),
            # cut faster than one of our shots can be (study_beats joins anything under STUDY_MIN_BEAT): the page says so
            'fast': total / len(shots) < STUDY_MIN_BEAT}


def study_clip(study, a, b):
    """The structure of reference shots a..b, for one of our segments (drag and drop, @ 对标): shots and the plan."""
    shots = [s for s in study.get('shots') or [] if a <= s['n'] <= b]
    if not shots:
        raise ShiseiError(f'the reference has no shot {a}' + (f'..{b}' if b != a else ''))
    return {'from': a, 'to': b, 'seconds': round(shots[-1]['t1'] - shots[0]['t0'], 1),
            'shots': [{'n': s['n'], 'seconds': round(s['t1'] - s['t0'], 1), **(s.get('look') or {}), 'tone': s.get('tone') or []} for s in shots],
            'plan': study_plan([{**s, 't0': s['t0'] - shots[0]['t0'], 't1': s['t1'] - shots[0]['t0']} for s in shots], max_seg=MAX_UNIT_SECONDS)}


def save_study(d, study):
    """Write study.json with the derived parts (summary, plan) brought up to date."""
    study['summary'] = study_summary(study)
    study['plan'] = study_plan(study.get('shots') or [])
    save_json_atomic(Path(d) / STUDY_FILE, study)
    return study


def study_shot(d, src, n, t0, t1, sheet=None):
    """Contact sheet (first / middle / last frame side by side) and the middle frame's measured tone, for one shot."""
    span = t1 - t0
    at = [t0 + min(0.15 * span, 0.3), t0 + span / 2, max(t0, t1 - min(0.15 * span, 0.3) - 0.04)]
    sheet = sheet or f'study_sheet_{n:03d}.jpg'
    args = ['ffmpeg', '-hide_banner', '-nostats', '-y']
    for t in at:
        args += ['-ss', f'{t:.3f}', '-i', str(src)]
    args += ['-filter_complex', '[0:v]scale=-2:320,setsar=1[a];[1:v]scale=-2:320,setsar=1,split[b][m];[2:v]scale=-2:320,setsar=1[c];'
             '[a][b][c]hstack=inputs=3[o];[m]scale=160:-2,signalstats,metadata=mode=print[s]',
             '-map', '[o]', '-frames:v', '1', '-q:v', '4', str(Path(d) / sheet), '-map', '[s]', '-frames:v', '1', '-f', 'null', '-']
    r = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if r.returncode or not (Path(d) / sheet).exists():
        raise ShiseiError(f'could not read shot {n} of the reference video: {r.stderr.strip()[-200:]}')
    stat = {k: float(v) for k, v in re.findall(r'lavfi\.signalstats\.(YAVG|UAVG|VAVG|SATAVG)=([0-9.]+)', r.stderr)}
    m = {'y': stat.get('YAVG', 128), 'u': stat.get('UAVG', 128), 'v': stat.get('VAVG', 128), 'sat': stat.get('SATAVG', 20)}
    return {'n': n, 't0': t0, 't1': t1, 'sheet': sheet, 'measured': {k: round(v, 1) for k, v in m.items()}, 'tone': tone_words(m),
            'look': {}, 'src': {}}


def cmd_drama_study(args, cfg):
    """Read a reference video locally (ffmpeg only, no GPU, nothing uploaded): cuts, per-shot contact sheets, tone."""
    with study_lock(drama_dir(args.jobdir)[0]):
        return _drama_study(args, cfg)


def need_ffmpeg():
    """The reference is read with the local ffmpeg / ffprobe (no model is downloaded): say so plainly when they are missing."""
    missing = [x for x in ('ffmpeg', 'ffprobe') if not shutil.which(x)]
    if missing:
        raise ShiseiError(f'{" and ".join(missing)} not found on this computer: install ffmpeg 4.4 or newer '
                          '(macOS: brew install ffmpeg) to read a reference video')


# ffmpeg is looked for where an install puts it, not only on PATH: dsh started from the Dock often has no Homebrew on its
# PATH, and a winget install reaches PATH only for programs started after it. The folder found goes to the front of PATH
# for this process, so every `ffmpeg` / `ffprobe` call below finds it. SHISEI_FFMPEG_DIR names the folder outright;
# SHISEI_FFMPEG_SEARCH=path looks on PATH only (tests).
def ffmpeg_dirs(env=None):
    env = os.environ if env is None else env
    if env.get('SHISEI_FFMPEG_DIR'):
        return [Path(env['SHISEI_FFMPEG_DIR'])]
    out = [HOME / 'bin']
    if env.get('SHISEI_FFMPEG_SEARCH') == 'path':
        return out
    if os.name == 'nt':
        la = env.get('LOCALAPPDATA')
        if la:
            w = Path(la) / 'Microsoft' / 'WinGet'
            out += [w / 'Links', *sorted((w / 'Packages').glob('Gyan.FFmpeg*/*/bin'), reverse=True)]
    else:
        out += [Path('/opt/homebrew/bin'), Path('/usr/local/bin')]
    return out


def own_ffmpeg_dir(home=None):
    """The plugin's own ffmpeg (the dsh-shisei-ffmpeg-<platform>-<arch> package the plugin put in ~/.dsh/shisei/tools),
    or None."""
    for d in sorted(((home or HOME) / 'tools' / 'node_modules').glob('dsh-shisei-ffmpeg-*/bin')):
        if (d / f'ffmpeg{EXE}').exists():
            return d
    return None


def ensure_ffmpeg_path(env=None):
    """Put the folder holding ffmpeg in front of PATH when ffmpeg is not already on it (the plugin's own ffmpeg always
    goes first: it is there because the one on PATH was missing or too old). Returns that folder or None."""
    env = os.environ if env is None else env
    forced = bool(env.get('SHISEI_FFMPEG_DIR'))
    own = None if forced else own_ffmpeg_dir()
    if own:
        if os.name != 'nt':
            for f in own.iterdir():
                if not os.access(f, os.X_OK):
                    f.chmod(f.stat().st_mode | 0o111)
        if not str(env.get('PATH', '')).startswith(str(own) + os.pathsep):
            env['PATH'] = str(own) + os.pathsep + env.get('PATH', '')
        return str(own)
    if not forced and shutil.which('ffmpeg', path=env.get('PATH')):
        return None
    for d in ffmpeg_dirs(env):
        if (d / f'ffmpeg{EXE}').exists():
            env['PATH'] = str(d) + os.pathsep + env.get('PATH', '')
            return str(d)
    return None


def ffmpeg_install_plan(system, which):
    """How this computer can get ffmpeg: Homebrew on macOS, winget on Windows (both without admin rights); otherwise a
    command for the user. `which(name)` finds a program. Pure."""
    if system == 'Darwin':
        brew = which('brew') or next((b for b in ('/opt/homebrew/bin/brew', '/usr/local/bin/brew') if Path(b).exists()), None)
        if brew:
            return {'how': 'brew', 'tool': brew, 'command': 'brew install ffmpeg'}
        return {'how': 'manual', 'command': 'brew install ffmpeg', 'hint': 'install Homebrew (https://brew.sh), then: brew install ffmpeg'}
    if system == 'Windows':
        winget = which('winget')
        if winget:
            return {'how': 'winget', 'tool': winget, 'command': 'winget install -e --id Gyan.FFmpeg'}
        return {'how': 'manual', 'command': 'winget install -e --id Gyan.FFmpeg',
                'hint': 'install App Installer (winget) from the Microsoft Store, or ffmpeg from https://ffmpeg.org/download.html'}
    return {'how': 'manual', 'command': 'sudo apt install ffmpeg', 'hint': 'install ffmpeg with your package manager (Debian / Ubuntu: sudo apt install ffmpeg)'}


def ffmpeg_state():
    """Where ffmpeg is, its version, and whether it can find cuts (the scdet filter, ffmpeg 4.4+)."""
    import platform
    ensure_ffmpeg_path()
    path, probe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    version, scdet = None, False
    if path:
        r = subprocess.run([path, '-hide_banner', '-version'], capture_output=True, text=True)
        m = re.search(r'ffmpeg version (\S+)', r.stdout)
        version = m.group(1) if m else None
        scdet = ' scdet ' in subprocess.run([path, '-hide_banner', '-filters'], capture_output=True, text=True).stdout
    plan = ffmpeg_install_plan(platform.system(), shutil.which)
    # `ready`, not `ok`: the command's own answer is {ok: true, ...} and this must not overwrite it
    return {'ffmpeg': path, 'ffprobe': probe, 'version': version, 'scdet': scdet, 'ready': bool(path and probe and scdet),
            'plan': {k: v for k, v in plan.items() if k != 'tool'}}


def cmd_ffmpeg_check(args, cfg):
    return ffmpeg_state()


def cmd_ffmpeg_install(args, cfg):
    """Install (or update) ffmpeg with Homebrew / winget. The user's click on the Piying page: refused in the director
    shell. Elsewhere it only says how."""
    import platform
    if in_agent_shell():
        raise ShiseiError('installing ffmpeg is the user\'s click on the Piying page (settings, or where a reference video is added)')
    before = ffmpeg_state()
    if before['ready']:
        return {**before, 'installed': False}
    plan = ffmpeg_install_plan(platform.system(), shutil.which)
    if plan['how'] == 'brew':
        argv = [plan['tool'], 'upgrade' if before['ffmpeg'] else 'install', 'ffmpeg']
        env = {**os.environ, 'HOMEBREW_NO_AUTO_UPDATE': '1', 'HOMEBREW_NO_ENV_HINTS': '1'}
    elif plan['how'] == 'winget':
        argv = [plan['tool'], 'upgrade' if before['ffmpeg'] else 'install', '-e', '--id', 'Gyan.FFmpeg',
                '--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity']
        env = dict(os.environ)
    else:
        raise ShiseiError(f'ffmpeg cannot be installed from here: {plan["hint"]}')
    log(f'installing ffmpeg: {" ".join(argv[1:])}')
    r = subprocess.run(argv, env=env, stdout=sys.stderr, stderr=sys.stderr, timeout=3600)
    after = ffmpeg_state()
    if not after['ready']:
        raise ShiseiError(f'ffmpeg install finished (exit {r.returncode}) but ffmpeg 4.4+ is still not found; '
                          f'try in a terminal: {plan["command"]}')
    return {**after, 'installed': True}


def _drama_study(args, cfg):
    need_ffmpeg()
    d, job = drama_dir(args.jobdir)
    name = None
    if args.video:
        v = Path(args.video).expanduser()
        ext = v.suffix.lower().lstrip('.')
        if not v.is_file() or ext not in STUDY_EXTS:
            raise ShiseiError(f'not a video file ({" ".join(STUDY_EXTS)}): {v.name}')
        for old in d.glob(STUDY_SRC + '.*'):
            old.unlink()
        shutil.copyfile(v, d / f'{STUDY_SRC}.{ext}')
        name = args.name or v.name
    src = study_source(d)
    if not src:
        raise ShiseiError('no reference video: add one on the Piying page or pass --video <file>')
    dur = ffprobe_duration(src)
    if not dur:
        raise ShiseiError('could not read the reference video (is it a video file?)')
    if dur > STUDY_MAX_SECONDS:
        raise ShiseiError(f'the reference video is {dur:.0f} s; at most {STUDY_MAX_SECONDS} s')
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(src)],
                       capture_output=True, text=True)
    wh = [int(x) for x in re.findall(r'\d+', r.stdout)[:2]] or [0, 0]
    r = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(src), '-an', '-vf', f'scale=-2:360,scdet=threshold={args.threshold:g}',
                        '-f', 'null', '-'], capture_output=True, text=True, timeout=900)
    if r.returncode:
        if 'scdet' in r.stderr and ('No such filter' in r.stderr or 'Filter not found' in r.stderr):
            raise ShiseiError('this ffmpeg is too old to find cuts (the scdet filter needs ffmpeg 4.4 or newer): update ffmpeg')
        raise ShiseiError(f'could not read the reference video: {r.stderr.strip()[-200:]}')
    spans = shots_from_cuts(parse_scdet(r.stderr), dur)
    for old in d.glob('study_sheet_*.jpg'):
        old.unlink()
    old = json.loads((d / STUDY_FILE).read_text()) if (d / STUDY_FILE).exists() else {}
    study = {'v': 1, 'source': {'file': src.name, 'name': (name or args.name or (old.get('source') or {}).get('name') or src.name)[:80],
                                'seconds': round(dur, 2), 'width': wh[0], 'height': wh[-1]},
             'shots': [study_shot(d, src, i + 1, a, b) for i, (a, b) in enumerate(spans)],
             'look': {'status': 'pending'}, 'made_at': time.time()}
    save_study(d, study)
    # a different video: the segments' ranges named its shots, not these; the same one read again: they follow the time
    remap_study_links(d, None if args.video else old.get('shots'), study['shots'])
    return {'study': study, 'next': 'drama-study-look (the image model reads the contact sheets), then drama-study-plan'}


def cmd_drama_study_look(args, cfg):
    """The image model picks framing / move / light / people / role for each shot from the contact sheets (fixed choices
    only). Shots the user set by hand are kept. Without an image model: status unavailable; the page lets the user pick."""
    import tempfile
    import threading
    from concurrent.futures import ThreadPoolExecutor
    d, _ = drama_dir(args.jobdir)
    with study_lock(d):   # the running status is a write like any other
        study = load_study(d)
        if study_looking(study):
            raise ShiseiError('the image model is already looking at this reference video; wait for it to finish')
        if not cfg.get('vision_model'):
            study['look'] = {'status': 'unavailable', 'at': time.time()}
            save_study(d, study)
            return {'look': study['look'], 'note': 'no image model configured: framing and the rest stay to be picked on the Piying page'}
        try:
            need_vision(cfg)
        except ShiseiError as e:   # said in study.json too, so the page does not wait for a look that never starts
            study['look'] = {'status': 'failed', 'error': str(e)[:200], 'at': time.time()}
            save_study(d, study)
            raise
        # shots the user (or the director) picked by hand are kept; the image model only fills the rest
        shots = [s for s in study['shots'] if args.force or not (s.get('src') or {})]
        batches = [shots[i:i + STUDY_BATCH] for i in range(0, len(shots), STUDY_BATCH)]
        lock = threading.Lock()
        run = secrets.token_hex(4)   # a new video or a later look makes this run's answers stale: they are then dropped
        study['look'] = {'status': 'running', 'model': vision_model(cfg), 'started_at': time.time(), 'pid': os.getpid(),
                         'run': run, 'done': 0, 'total': len(shots)}
        save_study(d, study)
    tmp = Path(tempfile.mkdtemp())
    errors = []
    done = [0]

    def merge(batch, got):
        """Write one batch's answers into study.json as it is now (the page may have changed it meanwhile): shots are
        matched by their time span, hand-picked fields stay, and nothing is written once another run took over."""
        with study_lock(d):
            merge_now(batch, got)

    def merge_now(batch, got):
        cur = load_study(d)
        if (cur.get('look') or {}).get('run') != run:
            return
        by = {(round(s['t0'], 3), round(s['t1'], 3)): s for s in cur['shots']}
        for s in batch:
            tgt = by.get((round(s['t0'], 3), round(s['t1'], 3)))
            if tgt is None:
                continue
            look = got.get(s['n']) or {}
            mine = {k: v for k, v in (tgt.get('look') or {}).items() if (tgt.get('src') or {}).get(k) in ('user', 'director')}
            tgt['look'] = {**look, **mine}
            tgt['src'] = {**{k: 'model' for k in look}, **{k: tgt['src'][k] for k in mine}}
        done[0] += len(batch)
        cur['look']['done'] = done[0]
        save_study(d, cur)

    def one(batch):
        imgs = []
        for s in batch:
            j = tmp / f'{s["n"]:03d}.jpg'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(d / s['sheet']), '-vf', 'scale=480:-2', '-q:v', '5', str(j)], check=True)
            imgs.append(j)
        prompt = STUDY_PROMPT.format(k=len(batch), nums='、'.join(str(s['n']) for s in batch), total=len(study['shots']),
                                     secs=f'{study["shots"][-1]["t1"]:.0f}')
        for attempt in range(2):
            try:
                r, _text, _secs = vision_ask(cfg, prompt, imgs, timeout=args.timeout, think=False)
            except ShiseiError as e:
                if attempt:
                    errors.append(str(e)[:200])
                continue
            got = {}
            for x in (r or {}).get('镜头') or []:
                try:
                    got[int(x.get('n'))] = clean_look(x)
                except (TypeError, ValueError, AttributeError):
                    continue
            if not got and not attempt:
                continue   # an empty or chatty reply is asked once more, never read as an answer
            with lock:
                merge(batch, got)
            return
        if not errors:
            errors.append('the image model did not answer in the expected format')

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(one, batches))
    shutil.rmtree(tmp, ignore_errors=True)
    with lock, study_lock(d):
        study = load_study(d)
        if (study.get('look') or {}).get('run') == run:
            study['look'].update(status='done' if not errors else ('partial' if done[0] else 'failed'),
                                 error=errors[0] if errors else None, at=time.time())
            study['look'].pop('pid', None)
            save_study(d, study)
    return {'look': study['look'], 'summary': study['summary']}


def study_looking(study, now=None):
    """True while an image-model run is really going on: status running, its process alive, started under 30 min ago.
    A killed run leaves `running` behind; that is not waited for."""
    lk = study.get('look') or {}
    if lk.get('status') != 'running':
        return False
    pid = lk.get('pid')
    if pid:   # the process tells: a long reference at a slow image model can take well over half an hour
        return pid_alive(pid)
    return (now or time.time()) - float(lk.get('started_at') or 0) <= 1800


def pid_alive(pid):
    """Is this process still there? (On Windows os.kill(pid, 0) would end it, so it is asked through the Win32 API.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == 'nt':
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259   # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def remap_study_links(d, old_shots, new_shots):
    """Shot numbers moved (a cut was fixed): every segment's `study` range follows the time it covered. A new video
    (old_shots None) clears them. Returns the segments changed."""
    p = Path(d) / 'story.json'
    if not p.exists():
        return []
    story = json.loads(p.read_text())
    by = {s['n']: s for s in old_shots or []}
    changed = []
    for g in story.get('segments') or []:
        r = g.get('study')
        if not r:
            continue
        if old_shots is None or r[0] not in by or r[1] not in by:
            g.pop('study')
        else:
            t0, t1 = by[r[0]]['t0'], by[r[1]]['t1']
            hit = [s['n'] for s in new_shots if s['t1'] > t0 + 1e-3 and s['t0'] < t1 - 1e-3]
            if hit:
                g['study'] = [hit[0], hit[-1]]
            else:
                g.pop('study')
        changed.append((g.get('shots') or [None])[0])
    if changed:
        save_story(d, story)
    return changed


def cmd_drama_study_set(args, cfg):
    """Pick a shot's framing / move / light / people / role by hand (fixed choices; an empty value clears it).
    From the page it counts as the user's (kept over the image model); from the director shell as the director's."""
    with study_lock(drama_dir(args.jobdir)[0]):
        return _drama_study_set(args, cfg)


def _drama_study_set(args, cfg):
    d, _ = drama_dir(args.jobdir)
    study = load_study(d)
    s = next((x for x in study['shots'] if x['n'] == args.n), None)
    if s is None:
        raise ShiseiError(f'the reference has no shot {args.n}')
    given = {k: getattr(args, k) for k in STUDY_LOOK if getattr(args, k) is not None}
    if not given:
        raise ShiseiError('nothing to set (--framing / --move / --light / --people / --role)')
    who = 'director' if in_agent_shell() else 'user'
    look = clean_look({k: v for k, v in given.items() if v != ''}, strict=True)
    s['look'] = dict(s.get('look') or {})
    s['src'] = dict(s.get('src') or {})
    for k, v in given.items():
        if v == '':
            s['look'].pop(k, None)
            s['src'].pop(k, None)
        else:
            s['look'][k] = look[k]
            s['src'][k] = who
    save_study(d, study)
    return {'shot': s, 'summary': study['summary']}


def cmd_drama_study_cut(args, cfg):
    """Fix the cuts: --merge N joins shot N to the one before; --split N --at T cuts shot N at T seconds (into the video)."""
    with study_lock(drama_dir(args.jobdir)[0]):
        return _drama_study_cut(args, cfg)


def _drama_study_cut(args, cfg):
    d, _ = drama_dir(args.jobdir)
    study = load_study(d)
    if study_looking(study):
        raise ShiseiError('the image model is still looking at the shots; fix the cuts when it is done')
    src = d / study['source']['file']
    shots = study['shots']
    before = copy.deepcopy(shots)   # the renumbering below changes the shot dicts it keeps
    if (args.merge is None) == (args.split is None):
        raise ShiseiError('say --merge N or --split N --at T')
    tmp = lambda k: f'_study_sheet_new{k}.jpg'   # noqa: E731 - remade sheets, renamed once the numbers are known
    if args.merge is not None:
        i = args.merge - 1
        if not 1 <= i < len(shots):
            raise ShiseiError(f'shot {args.merge} cannot be joined to the one before it')
        keep = shots[i - 1]
        new = [*shots[:i - 1], {**study_shot(d, src, 0, keep['t0'], shots[i]['t1'], tmp(0)), 'look': keep.get('look') or {}, 'src': keep.get('src') or {}},
               *shots[i + 1:]]
    else:
        i = args.split - 1
        if not 0 <= i < len(shots) or args.at is None:
            raise ShiseiError('say which shot (--split N) and where (--at seconds)')
        s = shots[i]
        if not s['t0'] + STUDY_MIN_SHOT <= args.at <= s['t1'] - STUDY_MIN_SHOT:
            raise ShiseiError(f'--at must be inside shot {args.split} ({s["t0"]:.1f}..{s["t1"]:.1f} s), at least {STUDY_MIN_SHOT} s from its ends')
        at = round(args.at, 3)
        new = [*shots[:i], {**study_shot(d, src, 0, s['t0'], at, tmp(0)), 'look': s.get('look') or {}, 'src': s.get('src') or {}},
               study_shot(d, src, 0, at, s['t1'], tmp(1)), *shots[i + 1:]]
    # renumber; each contact sheet follows its shot
    for f in d.glob('study_sheet_*.jpg'):
        f.rename(f.with_name('_' + f.name))
    for n, s in enumerate(new, 1):
        old = d / (s['sheet'] if s['sheet'].startswith('_') else '_' + s['sheet'])
        s['n'], s['sheet'] = n, f'study_sheet_{n:03d}.jpg'
        if old.exists():
            old.rename(d / s['sheet'])
    for f in d.glob('_study_sheet_*.jpg'):
        f.unlink()
    study['shots'] = new
    save_study(d, study)
    moved = remap_study_links(d, before, new)
    return {'shots': len(new), 'summary': study['summary'], 'links_moved': moved}


def cmd_drama_study_plan(args, cfg):
    """The reference's structure for writing: the summary and our segments (or, with --from / --to, one stretch of it)."""
    d, _ = drama_dir(args.jobdir)
    study = load_study(d)
    end = time.time() + max(0, args.wait or 0)
    while study_looking(study) and time.time() < end:   # the image model is still looking
        time.sleep(5)
        study = load_study(d)
    if args.from_ is not None:
        return {'clip': study_clip(study, args.from_, args.to if args.to is not None else args.from_)}
    by = {s['n']: s for s in study['shots']}
    segs = [{**g, 'beats': [{**b, **(by[b['from']].get('look') or {})} for b in g['beats']]} for g in study.get('plan') or []]
    try:
        est = cmd_drama_study_estimate(argparse.Namespace(jobdir=args.jobdir, price=None), cfg)
        price = {'cost': est['cost'], 'cap': est['budget']['cap'], 'spent': est['budget']['spent'], 'over': est['budget']['over']}
    except (ShiseiError, KeyError, ValueError):
        price = None   # no machine price at hand: the director asks drama-estimate once there is a script
    return {'summary': study.get('summary'), 'look': (study.get('look') or {}).get('status'), 'segments': segs, 'price': price,
            'over_budget': 'following every segment would go past the cap: before writing, ask the user whether to follow '
                           'only the closest stretch (fewer segments) or to raise the cap' if price and price['over'] > 0 else None,
            'how': 'one of our segments per entry: write its shots in this order with these seconds and framings, and put the '
                   'reference range into the segment as "study": [from, to]. The story is the user\'s, the characters new adult '
                   'fictional ones; nothing about the people, words or pictures in the reference'}


def cmd_drama_study_link(args, cfg):
    """Note which reference shots one of our segments follows (story.json segments[].study). Metadata only: nothing is
    re-made, no review changes."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    if not story.get('segments'):
        raise ShiseiError('this story has no segments')
    by_label = {u['label']: u['id'] for u in units(story)}
    uid = by_label.get(args.unit, args.unit)
    g = next((g for g in story['segments'] if (g.get('shots') or [None])[0] == uid), None)
    if g is None:
        raise ShiseiError(f'unknown segment {args.unit}')
    if args.range == 'none':
        g.pop('study', None)
    else:
        m = re.fullmatch(r'(\d{1,3})(?:-(\d{1,3}))?', args.range)
        if not m:
            raise ShiseiError('range is like 10-12 (or 4, or none)')
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        n = len(load_study(d)['shots'])
        if not 1 <= a <= b <= n:
            raise ShiseiError(f'the reference has shots 1..{n}')
        g['study'] = [a, b]
    validate_story(story)
    save_story(d, story)
    return {'unit': uid, 'study': g.get('study')}


WHISPER_MODEL = Path('~/.cache/whisper-cpp/ggml-small.bin').expanduser()


def norm_line(text):
    """Compare dialogue ignoring punctuation and spaces (whisper output has its own punctuation)."""
    return re.sub(r'[\s\W_]+', '', text or '')


def near_line(heard, want):
    """Same number of characters and only a few swapped (寒舍 → 韩设, 回头是岸 → 回头是暗): most likely whisper picking a
    homophone, not a wrong line. Up to one swap per four characters; nothing added or dropped."""
    a, b = norm_line(heard), norm_line(want)
    if len(b) < 4 or len(a) != len(b) or a == b:
        return False   # a short line: one swapped character may well be a different word
    return sum(x != y for x, y in zip(a, b)) <= max(1, len(b) // 4)


def whisper_bin():
    return shutil.which('whisper-cli') if WHISPER_MODEL.exists() else None


def cmd_drama_check(args, cfg):
    """Transcribe every take locally with whisper.cpp and compare it with the script line. Matching text is not lip sync."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    wb = whisper_bin()
    if not wb:
        raise ShiseiError(f'whisper-cli or {WHISPER_MODEL} not found; install whisper.cpp and the small model to check dialogue')
    p = d / 'check.json'
    check = json.loads(p.read_text()) if p.exists() else {}
    inner_now = {f'vo_{b["id"]}.wav' for u in units(story) for _, _, _, b in inner_lines(u)}
    check = {k: v for k, v in check.items() if not k.startswith('vo_') or k in inner_now}   # a line no longer inner
    for u in units(story):
        spoken = [l for l in sh_lines(u) if l[2].get('kind', 'say') != 'inner']
        s = {'id': u['id'], 'line': {'text': ''.join(l[2]['text'] for l in spoken)} if spoken else None}
        # the inner voices are made on their own (vo_<shot>.wav) and mixed in at the cut: they can go wrong the same way
        files = [(d / f'take_{s["id"]}_{seed}.mp4', (s.get('line') or {}).get('text', '')) for seed in DRAMA_SEEDS]
        files += [(d / f'vo_{b["id"]}.wav', line['text']) for _, _, line, b in inner_lines(u)]
        for f, want in files:
            if not f.exists() or (f.name in check and check[f.name].get('mtime') == f.stat().st_mtime):
                continue
            wav = d / '_asr.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(f), '-ar', '16000', '-ac', '1', str(wav)], check=True)
            # the prompt keeps whisper in simplified Chinese (otherwise 再见 comes back as 再見 and never matches)
            r = subprocess.run([wb, '-m', str(WHISPER_MODEL), '-l', 'zh', '-nt', '-np', '--prompt', '以下是普通话的简体中文句子。',
                                '-f', str(wav)], capture_output=True, text=True)
            wav.unlink(missing_ok=True)
            heard = r.stdout.strip().replace('\n', ' ')
            if not want:
                heard = ''   # silent shots: whisper invents captions from the room tone; nothing to compare
            match = norm_line(heard) == norm_line(want) if want else None
            check[f.name] = {'heard': heard, 'want': want, 'match': match, 'near': bool(want) and not match and near_line(heard, want),
                             'mtime': f.stat().st_mtime}
    save_json_atomic(p, check)
    return {'check': check}


SHOTS_FILE = 'shots.json'
SHOT_CUT_THRESHOLD = 10.0   # same scdet score as the 对标片: it found every hard cut H3 makes inside a segment
SHOT_MIN = 0.3              # a piece shorter than this is a flicker, not a shot
SHOT_MATCH_TOL = 2.0        # how far (s) a found cut may sit from where the script puts it and still be that cut
SHOTS_PROMPT = ('下面 {k} 张图依次是同一段短剧视频里的 {k} 个镜头（每张取镜头中间一帧）。对照每个镜头的分镜，只看画面，'
                '只输出一个 JSON 对象，不要别的文字：{{"镜头":[{{"n":序号,"景别":"特写|近景|中景|全景","景别符合":true/false,'
                '"内容符合":true/false,"有人看镜头":true/false,"问题":"一句话，没有就写空字符串"}}]}}。「内容符合」看人物、动作、道具和分镜里'
                '「画面里必须看得出」的状态。分镜：{beats}')


def find_cuts(video, threshold=SHOT_CUT_THRESHOLD):
    """Hard cuts (seconds) in a video, found locally with ffmpeg scdet."""
    r = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(video), '-an', '-vf',
                        f'scale=-2:360,scdet=threshold={threshold:g}', '-f', 'null', '-'], capture_output=True, text=True)
    if r.returncode != 0 and 'scdet' in r.stderr:
        raise ShiseiError('this ffmpeg is too old to find cuts (the scdet filter needs ffmpeg 4.4 or newer): update ffmpeg')
    return parse_scdet(r.stderr)


def align_cuts(bounds, cuts, tol=SHOT_MATCH_TOL):
    """Pair the script's cut points (`bounds`, seconds) with the cuts found. Same count: in order (H3 keeps the order,
    not the timing: 《画皮》 B cut 1.3-1.9 s late). Otherwise nearest first within `tol`.
    Returns (matched {bound index: cut}, extra cuts, missing bound indexes)."""
    if len(bounds) == len(cuts):
        return dict(enumerate(cuts)), [], []
    pairs = sorted((abs(b - c), i, c) for i, b in enumerate(bounds) for c in cuts if abs(b - c) <= tol)
    matched, used = {}, set()
    for _, i, c in pairs:
        if i not in matched and c not in used:
            matched[i] = c
            used.add(c)
    # keep the order: a pairing that crosses another (bound 2 before bound 1's cut) is dropped
    last, ordered = -1.0, {}
    for i in sorted(matched):
        if matched[i] > last:
            ordered[i], last = matched[i], matched[i]
    extra = [c for c in cuts if c not in ordered.values()]
    return ordered, extra, [i for i in range(len(bounds)) if i not in ordered]


def shot_plan(unit, duration, cuts):
    """Each beat of a segment against a take: where the script puts it (scaled to the take's length), where it landed,
    the pieces it was cut into (an extra cut inside it splits it), and the cuts the script has no beat for."""
    beats = unit['beats']
    scale = duration / unit['seconds'] if unit['seconds'] else 1
    cuts = [c for c in cuts if SHOT_MIN <= c <= duration - SHOT_MIN]
    bounds = [b['t1'] * scale for b in beats[:-1]]
    matched, extra, missing = align_cuts(bounds, cuts)
    out = []
    for i, b in enumerate(beats):
        a = 0.0 if i == 0 else matched.get(i - 1)
        z = duration if i == len(beats) - 1 else matched.get(i)
        want = [round(b['t0'] * scale, 2), round(b['t1'] * scale, 2)]
        got = [round(a, 2), round(z, 2)] if a is not None and z is not None else None
        pieces = None
        if got:
            inner = [c for c in extra if got[0] < c < got[1]]
            edges = [got[0]] + inner + [got[1]]
            pieces = [[round(x, 2), round(y, 2)] for x, y in zip(edges, edges[1:])]
        line = b.get('line') or {}
        out.append({'id': b['id'], 'want': want, 'got': got, 'pieces': pieces,
                    'drift': round(max(abs(got[0] - want[0]), abs(got[1] - want[1])), 2) if got else None,
                    'framing': next((zh for zh, _ in FRAMING_EN if b.get('keyframe', '').startswith(zh)), ''),
                    'line': line.get('text') if line and line.get('kind', 'say') == 'say' else None})
    return {'duration': round(duration, 2), 'cuts': cuts, 'beats': out, 'extra': extra,
            'missing': [beats[i + 1]['id'] for i in missing]}


def shots_verdict(plan):
    """ok | issue: an extra or missing cut, or a shot the image model says does not match."""
    if plan['extra'] or plan['missing']:
        return 'issue'
    return 'issue' if any((b.get('look') or {}).get('ok') is False for b in plan['beats']) else 'ok'


def cmd_drama_shots(args, cfg):
    """Shot by shot against the script, for every take (local, no GPU): ffmpeg finds where H3 cut, each beat is matched
    to its piece, and (when an image model is set) one middle frame per beat is checked against the shot list.
    Writes shots.json only. Advice for the user; nothing is approved, picked or trimmed from here."""
    import tempfile
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    p = d / SHOTS_FILE
    out = json.loads(p.read_text()) if p.exists() else {}
    look = not args.no_look and vision_ready(cfg)
    tmp = Path(tempfile.mkdtemp())
    names = set()
    try:
        for u in units(story):
            for seed in DRAMA_SEEDS:
                f = d / f'take_{u["id"]}_{seed}.mp4'
                if not f.exists():
                    continue
                names.add(f.name)
                old = out.get(f.name) or {}
                if old.get('mtime') == f.stat().st_mtime and not args.force and (old.get('looked') or not look):
                    continue
                dur = ffprobe_duration(f)
                plan = shot_plan(u, dur, find_cuts(f))
                plan.update(mtime=f.stat().st_mtime, unit=u['id'], looked=False)
                if look and len(plan['beats']) <= 6:
                    imgs, texts = [], []
                    by = {b['id']: b for b in u['beats']}
                    for k, b in enumerate(plan['beats'], 1):
                        a, z = max(b['pieces'] or [b['got'] or b['want']], key=lambda r: r[1] - r[0])
                        imgs.append(small_jpeg(f, tmp / f'{f.stem}_{k}.jpg', width=320, at=round((a + z) / 2, 2)))
                        texts.append(f'{k}. {frame_words(story, by[b["id"]])}')
                    try:
                        r, _, secs = vision_ask(cfg, SHOTS_PROMPT.format(k=len(imgs), beats='；'.join(texts)), imgs,
                                                timeout=args.timeout, think=False)
                        said = {int(x.get('n', 0)): x for x in (r or {}).get('镜头') or [] if isinstance(x, dict) and str(x.get('n', '')).isdigit()}
                        for k, b in enumerate(plan['beats'], 1):
                            x = said.get(k)
                            if x:
                                bad = x.get('景别符合') is False or x.get('内容符合') is False
                                b['look'] = {'framing': x.get('景别'), 'framing_ok': x.get('景别符合'), 'content_ok': x.get('内容符合'),
                                             'camera': x.get('有人看镜头') is True,
                                             # only the yes / no fields decide; the words are shown when one of them says no
                                             'issue': str(x.get('问题') or '')[:160] if bad else '',
                                             'ok': not bad}
                        plan.update(looked=bool(said), look_seconds=secs)
                    except ShiseiError as e:
                        plan['look_error'] = str(e)[:200]
                plan['verdict'] = shots_verdict(plan)
                out[f.name] = plan
                save_json_atomic(p, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = {k: v for k, v in out.items() if k in names}   # takes that were remade or archived drop their old check
    save_json_atomic(p, out)
    return {'shots': out}


def ffprobe_duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip() or 0)


def take_file(d, review, uid, seed=None):
    takes = [s for s in DRAMA_SEEDS if (d / f'take_{uid}_{s}.mp4').exists()]
    if not takes:
        raise ShiseiError(f'{uid} has no take yet')
    pick = seed or review['picks'].get(uid)
    return d / f'take_{uid}_{pick if pick in takes else takes[0]}.mp4'


def cmd_drama_frames(args, cfg):
    """Frames the director (and the user) can look at: from a segment's take, the final cut, or both sides of a seam.
    With --ask the frames go to the image model with the question, and its answer comes back (advice only)."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    shots = []   # (source file, seconds, label)
    if args.seam:
        a, b = args.seam
        fa, fb = take_file(d, review, a), take_file(d, review, b)
        shots = [(fa, max(0.0, ffprobe_duration(fa) - 0.15), f'{a} 末帧'), (fb, 0.05, f'{b} 首帧')]
    else:
        src = d / 'final.mp4' if args.final else take_file(d, review, args.unit, args.seed)
        if not src.exists():
            raise ShiseiError('no final cut yet; run drama-cut')
        dur = ffprobe_duration(src)
        ats = [float(x) for x in args.at.split(',')] if args.at else [round(dur * x, 2) for x in (0.05, 0.35, 0.65, 0.95)]
        shots = [(src, min(max(0.0, t), max(0.0, dur - 0.05)), f'{"成片" if args.final else args.unit} {t:g}s') for t in ats]
    out = []
    for src, t, label in shots:
        name = f'fr_{src.stem}_{int(t * 1000)}.jpg'
        small_jpeg(src, d / name, width=args.width, at=t)
        out.append({'file': name, 'path': str(d / name), 'label': label})
    res = {'frames': out}
    if args.ask:
        r, text, secs = vision_ask(cfg, args.ask + '\n（这些图依次是：' + '、'.join(x['label'] for x in out) + '）只用一两句话回答。',
                                   [d / x['file'] for x in out], timeout=args.timeout, think=not args.no_think)
        res['answer'] = text.strip()[:1200]
        res['seconds'] = secs
    return res


# ---------------------------------------------------------------- point and say
# A spot the user clicked on a first frame, a take or the final cut becomes two pictures for the director: a crop with
# an amber ring and the whole frame with a small marker. They live in the job's point/ subfolder, which nothing that
# builds generation inputs looks at: the reference builders name files from story ids (cast_/scene_/prop_/kf_/take_/vo_
# at the top level), _have() lists top-level files only, and the version code globs top-level *.vN.* names.
# The one sanctioned way a picture the user framed becomes an input is drama-crop (see its section): a cut of the
# segment's own approved first frame, never a file from here.
POINT_DIR = 'point'
POINT_RGB = (255, 138, 76)     # the page's amber, #FF8A4C
POINT_CROP_WIDTH = 512
POINT_FULL_WIDTH = 720


def _even(n):
    return max(2, int(round(n)) // 2 * 2)


def point_geometry(width, height, x, y, w=0.0, h=0.0):
    """Pixels for one pointed spot. x, y (and a box's w, h) are fractions of the source frame, clamped to 0..1.
    The crop keeps its size and slides inward at the edges, so the ring is off-centre there rather than cut off."""
    W, H = int(width), int(height)
    if W < 16 or H < 16:
        raise ShiseiError('frame too small to point at')
    c = lambda v: min(1.0, max(0.0, float(v)))
    x, y = c(x), c(y)
    w, h = min(c(w), 1.0 - x), min(c(h), 1.0 - y)
    box = w >= 0.02 and h >= 0.02
    short = min(W, H)
    if box:
        bx, by, bw, bh = x * W, y * H, w * W, h * H
        px, py = bx + bw / 2, by + bh / 2
        cw = min(W, max(bw * 1.5, short * 0.3))
        ch = min(H, max(bh * 1.5, short * 0.3))
    else:
        px, py = x * W, y * H
        cw = ch = short * 0.4
    cw, ch = _even(cw), _even(ch)
    cx = int(min(max(0, px - cw / 2), W - cw))
    cy = int(min(max(0, py - ch / 2), H - ch))
    s = POINT_CROP_WIDTH / cw
    out_h = _even(ch * s)
    fw = _even(min(POINT_FULL_WIDTH, W))
    fs = fw / W
    fh = _even(H * fs)
    geo = {'width': W, 'height': H, 'box': box, 'crop': (cw, ch, cx, cy), 'crop_out': (POINT_CROP_WIDTH, out_h),
           'full_out': (fw, fh), 'centre': (px / W, py / H)}
    if box:
        geo['crop_box'] = tuple(int(round(v)) for v in ((bx - cx) * s, (by - cy) * s, bw * s, bh * s))
        geo['full_box'] = tuple(int(round(v)) for v in (bx * fs, by * fs, bw * fs, bh * fs))
    else:
        geo['crop_ring'] = (int(round((px - cx) * s)), int(round((py - cy) * s)), int(round(POINT_CROP_WIDTH * 0.12)))
        geo['full_ring'] = (int(round(px * fs)), int(round(py * fs)), max(12, int(round(fw * 0.045))))
    return geo


def _ring_filter(cx, cy, r, t, dot=0):
    """An amber ring (and optional filled dot) with a dark edge, drawn with geq: ffmpeg has no circle filter."""
    d = f'hypot(X-{cx},Y-{cy})'
    amber = f'lte(abs({d}-{r}),{t})' + (f'+lte({d},{dot})' if dot else '')
    edge = f'lte(abs({d}-{r}),{t + 2})'
    ch = lambda val, plane: f"if({amber},{val},if({edge},20,{plane}(X,Y)))"
    r_, g_, b_ = POINT_RGB
    return f"format=rgb24,geq=r='{ch(r_, 'r')}':g='{ch(g_, 'g')}':b='{ch(b_, 'b')}'"


def _box_filter(x, y, w, h, t):
    return f'drawbox=x={x}:y={y}:w={w}:h={h}:color=0x{"".join(f"{v:02X}" for v in POINT_RGB)}:t={t}'


def point_args(src, at, geo, crop_out, full_out):
    """Two ffmpeg runs: the crop with a ring (or the box) on it, and the whole frame with a small marker."""
    pre = ['ffmpeg', '-v', 'error', '-y'] + (['-ss', f'{float(at):.3f}'] if at is not None else []) + ['-i', str(src), '-frames:v', '1']
    cw, ch, cx, cy = geo['crop']
    ow, oh = geo['crop_out']
    fw, fh = geo['full_out']
    if geo['box']:
        draw_crop, draw_full = _box_filter(*geo['crop_box'], 4), _box_filter(*geo['full_box'], 3)
    else:
        rx, ry, rr = geo['crop_ring']
        draw_crop = _ring_filter(rx, ry, rr, 3)
        fx, fy, fr = geo['full_ring']
        draw_full = _ring_filter(fx, fy, fr, 2, dot=3)
    return [pre + ['-vf', f'crop={cw}:{ch}:{cx}:{cy},scale={ow}:{oh},{draw_crop}', '-q:v', '3', str(crop_out)],
            pre + ['-vf', f'scale={fw}:{fh},{draw_full}', '-q:v', '3', str(full_out)]]


def where_words(x, y):
    """Plain words for a spot: 画面右上, 画面中间 …"""
    col = 0 if x < 1 / 3 else 2 if x > 2 / 3 else 1
    row = 0 if y < 1 / 3 else 2 if y > 2 / 3 else 1
    return '画面' + [['左上', '上方', '右上'], ['左边', '中间', '右边'], ['左下', '下方', '右下']][row][col]


def point_names(source_key, at, nonce):
    """Relative paths (under the job folder) of the two pictures. Always inside point/, never a .vN. name."""
    if not re.fullmatch(r'[a-z0-9-]{1,24}', source_key) or not re.fullmatch(r'[a-z0-9]{2,8}', nonce):
        raise ShiseiError('bad point name')
    stem = f'{POINT_DIR}/pt_{source_key}_{int(round((at or 0) * 1000)):06d}_{nonce}'
    return f'{stem}_crop.jpg', f'{stem}_full.jpg'


def frame_size(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(path)],
                       capture_output=True, text=True)
    try:
        w, h = (int(v) for v in r.stdout.strip().split(',')[:2])
    except ValueError:
        raise ShiseiError(f'cannot read the picture size of {Path(path).name}')
    return w, h


def cmd_drama_point(args, cfg):
    """Turn a clicked spot into two pictures for the director (see point_geometry). Context only: never a generation input."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    us = {u['id']: u for u in units(story)}
    at = None
    if args.final:
        src, key, what = d / 'final.mp4', 'final', '成片'
    else:
        uid = args.kf or args.unit
        if uid not in us:
            raise ShiseiError(f'unknown segment {uid}')
        label = us[uid]['label']
        if args.kf:
            src, key, what = d / f'kf_{uid}.png', f'kf-{uid}', f'段 {label} 的首帧'
        else:
            if args.seed not in DRAMA_SEEDS:
                raise ShiseiError('unknown take')
            src, key, what = d / f'take_{uid}_{args.seed}.mp4', f'take-{uid}-{args.seed}', f'段 {label} 第 {take_number(args.seed)} 条'
    if not src.exists():
        raise ShiseiError(f'{what}还没有')
    if not args.kf:
        dur = ffprobe_duration(src)
        at = min(max(0.0, float(args.at or 0)), max(0.0, dur - 0.05))
    geo = point_geometry(*frame_size(src), args.x, args.y, args.w or 0, args.h or 0)
    crop, full = point_names(key, at, secrets.token_hex(2) if args.nonce is None else args.nonce)
    (d / POINT_DIR).mkdir(exist_ok=True)
    for argv in point_args(src, at, geo, d / crop, d / full):
        subprocess.run(argv, check=True, capture_output=True)
    return {'crop': str(d / crop), 'full': str(d / full), 'where': where_words(*geo['centre']), 'box': geo['box'],
            'at': at, 'source': src.name, 'what': what}


# ---------------------------------------------------------------- trials: unvalidated ideas, pictures in trial/ only
#
# Three hypotheses from docs/DESIGN.md「下一版」⑥. A trial makes comparison pictures in <job>/trial/ and nothing else:
# no story.json, review.json, kf_* or cast_*/scene_*/prop_* is written, nothing in trial/ is ever approved or read
# back as a generation input, and the command never starts the machine (it refuses unless it is already running).

TRIAL_DIR = 'trial'
TRIAL_KINDS = ('crop', 'eyeline', 'redraw')
TRIAL_MAX_UPSCALE = 6.0
MAX_REF_BYTES = 15 * 1024 * 1024


def image_type(head):
    """png | jpeg | webp from the first bytes of a file, else None. The file name is never trusted."""
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def png_size(path):
    """(width, height) from a PNG header, without ffprobe (a dry run starts no process)."""
    with open(path, 'rb') as f:
        head = f.read(24)
    if image_type(head) != 'png' or head[12:16] != b'IHDR':
        raise ShiseiError(f'{Path(path).name} is not a PNG picture')
    return struct.unpack('>II', head[16:24])


def check_box(x, y, w, h):
    if not all(v == v and 0 <= v <= 1 for v in (x, y, w, h)):
        raise ShiseiError('--box values are fractions between 0 and 1')
    if w < 0.05 or h < 0.05:
        raise ShiseiError('--box is too small: w and h are at least 0.05')
    if x + w > 1 + 1e-9 or y + h > 1 + 1e-9:
        raise ShiseiError('--box reaches outside the frame: x + w and y + h are at most 1')
    return x, y, w, h


def parse_box(text):
    """'x,y,w,h' as fractions 0..1 of the frame -> four floats. The box must lie inside the frame."""
    try:
        x, y, w, h = (float(v) for v in str(text).split(','))
    except ValueError:
        raise ShiseiError('--box is x,y,w,h: four fractions of the frame, e.g. 0.6,0.4,0.35,0.35')
    return check_box(x, y, w, h)


def crop_geometry(width, height, box, size=DRAMA_IMAGE_SIZE):
    """Pixels to cut out of a width x height frame for `box`: the smallest region of the keyframe's aspect ratio that
    holds the box, slid inward at the edges, so scaling it up to `size` never stretches the picture."""
    W, H = int(width), int(height)
    x, y, w, h = box
    r = size[0] / size[1]
    bw, bh = w * W, h * H
    cw, ch = (bh * r, bh) if bw / bh < r else (bw, bw / r)
    f = min(1.0, W / cw, H / ch)
    cw, ch = min(_even(cw * f), W // 2 * 2), min(_even(ch * f), H // 2 * 2)
    cx = int(min(max(0, x * W + bw / 2 - cw / 2), W - cw))
    cy = int(min(max(0, y * H + bh / 2 - ch / 2), H - ch))
    scale = round(size[0] / cw, 2)
    if scale > TRIAL_MAX_UPSCALE:
        raise ShiseiError(f'--box is too small: the cut would be enlarged {scale}x (at most {TRIAL_MAX_UPSCALE:g}x)')
    return {'crop': (cw, ch, cx, cy), 'out': tuple(size), 'scale': scale}


def crop_args(src, dst, geo):
    cw, ch, cx, cy = geo['crop']
    return ['ffmpeg', '-v', 'error', '-y', '-i', str(src), '-frames:v', '1',
            '-vf', f'crop={cw}:{ch}:{cx}:{cy},scale={geo["out"][0]}:{geo["out"][1]}:flags=lanczos', '-map_metadata', '-1', str(dst)]


def normal_box(width, height, geo):
    """The cut itself as fractions of the frame (x, y, w, h, 4 decimals): the 9:16 box that is stored and shown."""
    cw, ch, cx, cy = geo['crop']
    return [round(cx / width, 4), round(cy / height, 4), round(cw / width, 4), round(ch / height, 4)]


trial_crop_geometry, trial_crop_args = crop_geometry, crop_args   # the trial and the pipeline cut the same way

CROP_SAME = ('把第一张图原样重新画成一张清晰的竖构图电影特写剧照：取景、构图、人物的位置和姿势、表情、视线方向、发型、衣服、光线和背景'
             '都与第一张图完全一致，不要拉远，不要补出画面外的身体或场景，不要增加人或物。'
             '只提高清晰度和细节：真实的皮肤纹理、头发丝和布料纹理，对焦清晰。')


def crop_prompt(c, others=()):
    """The `alone` writing that passed on the GPU (4/4): the enlarged cut is the only picture. `others` are the other
    people of the frame the cut is taken from: the cut may catch them, so their age is stated too (an addition to the
    tested writing; with nobody else in the frame the prompt is exactly the tested one)."""
    more = ''.join(f'如果画面里还能看到{o["name"]}（{age_words(o)}），{age_line(o)}' for o in others)
    return f'{CROP_SAME}画面里的人是{c["name"]}（{age_words(c)}）。{age_line(c)}{more}真实摄影风格。'


def trial_crop(story, cast_id, cut):
    """Close-up cut out of a wide frame, redrawn at full size. `cut` is the enlarged crop (image 1). Two writings:
    alone = the crop is the only picture; face = the portrait comes along as image 2, for the face only."""
    c = _cast(story, cast_id)
    return [
        {'key': 'alone', 'images': [cut], 'size': DRAMA_IMAGE_SIZE, 'prompt': crop_prompt(c)},
        {'key': 'face', 'images': [cut, f'cast_{c["id"]}_face.png'], 'size': DRAMA_IMAGE_SIZE,
         'prompt': f'{CROP_SAME}第二张图是{c["name"]}（{age_words(c)}）的定妆照，只用来对照脸：五官和发型画得和第二张图是同一个人；'
                   f'不要照搬第二张图的取景、正面角度、表情和白背景，取景以第一张图为准。{age_line(c)}真实摄影风格。'},
    ]


def profile_sentence(A, B):
    """The `profile` writing that passed on the GPU (2/2): both in strict profile, facing each other."""
    return (f'中景，平视，侧面机位：{A}在画面左侧，{B}在画面右侧，两人面对面，都是正侧面入镜；'
            f'{A}脸朝画面右侧看着{B}，{B}脸朝画面左侧看着{A}，两人都不看镜头。')


def eyeline_prompt(a, b, staging, note=''):
    """A two-person first frame from scene (image 1) + a's portrait (2) + b's portrait (3), with `staging` in place of
    the shot's own framing words and `note` saying what else is going on."""
    head = (f'把第2张图里的{a["name"]}（{age_words(a)}）、第3张图里的{b["name"]}（{age_words(b)}）放进第一张图的场景里，'
            '生成一张竖构图电影剧照：')
    note = str(note or '').strip().rstrip('。.')
    tail = ((f'情境：{note}。' if note else '')
            + same_age_sentence([a, b]) + age_line(a) + age_line(b)
            + '背景、光线与第一张图一致。真实摄影风格。画面里只有这两个人。')
    return head + staging + tail


def trial_eyeline(story, unit, who=None, at=None, note=''):
    """Three writings of 「A looks at B」 for one first frame: over B's shoulder, both in profile, a two-shot with the
    gaze spelled out. Images are the ones a real first frame gets (scene, then the two portraits); the 3-image cap
    leaves no room for props, which are returned as `dropped`. The staging sentence replaces the shot's own keyframe
    text (that text may keep B out of frame); `note` says what else is going on. Only `profile` passed and is in the
    pipeline (keyframe_prompt, staging: profile); the other two stay here as the record of what was compared."""
    cast = unit.get('cast') or []
    who = who or (cast[0] if cast else None)
    at = at or next((x for x in cast if x != who), None)
    if not who or not at or who == at or who not in cast or at not in cast:
        raise ShiseiError(f'an eyeline trial needs two different characters of segment {unit["label"]} '
                          f'(--who A --at B; it has: {", ".join(cast) or "nobody"})')
    a, b = _cast(story, who), _cast(story, at)
    A, B = a['name'], b['name']
    images = [f'scene_{unit["scene"]}.png', f'cast_{a["id"]}_face.png', f'cast_{b["id"]}_face.png']
    staging = {
        'shoulder': f'过肩镜头，机位在{B}的肩膀后面：{B}背对镜头，在画面前景右侧，只露出后脑勺和一侧肩膀，虚化；'
                    f'{A}在画面中间偏左，面向{B}，脸朝画面右侧，{A}的视线落在{B}的脸上，不看镜头。',
        'profile': profile_sentence(A, B),
        'gaze': f'中景，平视，双人镜头：{A}在画面左侧，{B}在画面右侧。{A}的视线落在{B}的脸上，不看镜头。',
    }
    variants = [{'key': k, 'images': list(images), 'size': DRAMA_IMAGE_SIZE, 'prompt': eyeline_prompt(a, b, sen, note)}
                for k, sen in staging.items()]
    return variants, {'who': a['id'], 'at': b['id'], 'dropped': [f'prop_{p}.png' for p in unit['first'].get('props') or []]}


def redraw_prompts(c):
    """The redraw recipe that passed on the GPU (portrait 2/2, full body 2/2): {'face': prompt, 'body': prompt}.
    Image 1 is a portrait of the character (who it is), image 2 gives the clothes and nothing else. `look` is left
    out of the prompt on purpose: it describes the clothes being replaced."""
    keep = (f'脸、年龄、五官、发型与第一张图完全一致，是同一个人：{age_words(c)}{c["name"]}。'
            '只从第二张图取衣服：款式、颜色和材质照第二张图画，穿在这个人身上；'
            '第二张图里别的东西（背景、姿势、人台或人的脸和身材）都不要。')
    return {
        'face': f'把第一张图里的人物重新画一张影棚近景肖像：纯白背景，正面平视，拍到头部、肩膀和胸口，表情平静自然。{keep}'
                f'{age_line(c)}柔和均匀的影棚光，真实皮肤纹理，清晰对焦。真实摄影风格。画面里只有这一个人。',
        'body': f'把第一张图里的人物生成一张影棚全身定妆照：纯白背景，正面平视，全身入镜从头顶到鞋底完整，人物站直，双手自然垂下，'
                f'手里不拿东西。{keep}{age_line(c)}真实摄影风格。画面里只有这一个人。',
    }


def trial_redraw(story, cast_id, ref):
    """Redraw a character from a dropped picture: image 1 is the current portrait, image 2 (`ref`) the clothes.
    Two pictures like the normal ones: head and shoulders, and full body."""
    c = _cast(story, cast_id)
    images = [f'cast_{c["id"]}_face.png', ref]
    return [{'key': k, 'images': list(images), 'size': DRAMA_PORTRAIT_SIZE, 'prompt': p} for k, p in redraw_prompts(c).items()]


def trial_number(d, kind):
    """The next free trial number of a kind: 1 + the highest one any file in trial/ carries."""
    t = Path(d) / TRIAL_DIR
    used = [int(m.group(1)) for m in (re.match(rf'{kind}_(\d+)[._]', f.name) for f in (t.iterdir() if t.is_dir() else [])) if m]
    return max(used, default=0) + 1


def trial_name(kind, n, part, ext='png'):
    """Relative path of one trial file. Always inside trial/, never a name the pipeline or the version code looks for."""
    if kind not in TRIAL_KINDS or not re.fullmatch(r'[a-z0-9_]{1,24}', part) or int(n) < 1:
        raise ShiseiError('bad trial name')
    return f'{TRIAL_DIR}/{kind}_{int(n)}_{part}.{ext}'


def trial_remote(rid, name):
    """Remote input name of a local picture (a project file or a trial input). The `trial_` infix keeps it apart from
    the names drama-run uploads ({rid}_kf_…, {rid}_cast_…), so a trial never replaces a pipeline input on the instance."""
    return f'{rid}_trial_{Path(name).name}'


def trial_jobs(rid, kind, n, variants, seeds):
    """One workflow per variant and seed. Every variant gets the same seeds, so pictures differ by the writing only."""
    if not 1 <= int(seeds) <= len(DRAMA_SEEDS):
        raise ShiseiError(f'--seeds is 1..{len(DRAMA_SEEDS)}')
    jobs = []
    for v in variants:
        for i, seed in enumerate(DRAMA_SEEDS[:int(seeds)], 1):
            out = trial_name(kind, n, f'{v["key"]}_{i}')
            prefix = f'{rid}_trial_{kind}_{n}_{v["key"]}_{i}'
            api = patch_drama_image(load_workflow('drama_image'), v['prompt'], seed, prefix,
                                    [trial_remote(rid, x) for x in v['images']], v['size'])
            jobs.append({'file': out, 'variant': v['key'], 'seed': seed, 'prefix': prefix, 'api': api})
    return jobs


class TrialRun:
    """What _wait_prompt and _arm_idle_stop need of a run. Unlike DramaRun it writes no progress.json (the page reads that)."""
    def __init__(self):
        self.p = {}

    def check_stop(self):
        pass


def cmd_drama_trial(args, cfg):
    """Make trial pictures for one hypothesis (see the section comment). The machine must already be running."""
    d, job = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    kind, rid = args.kind, job['id']
    n = trial_number(d, kind)
    us = units(story)
    unit = next((u for u in us if args.unit in (u['id'], u['label'])), None) if args.unit else None
    if args.unit and unit is None:
        raise ShiseiError(f'unknown segment {args.unit}')
    prepare, extra = [], {}
    if kind == 'crop':
        if args.source:
            src = d / args.source
            if Path(args.source).name != args.source or not src.is_file():
                raise ShiseiError('--from is the name of a PNG directly in the project folder (not in point/ or trial/)')
        elif unit:
            src = d / f'kf_{unit["id"]}.png'
            if not src.exists() or verdict(review, 'keyframes', unit['id']) != 'pass':
                raise ShiseiError(f'segment {unit["label"]} has no approved first frame to cut from (or give --from <png>)')
        else:
            raise ShiseiError('crop needs --unit <segment> or --from <png in the project>')
        cast = (unit or {}).get('cast') or []
        cid = args.cast or (cast[0] if len(cast) == 1 else None)
        if cid is None:
            raise ShiseiError('say whose close-up this is: --cast <id>' + (f' (segment {unit["label"]} has: {", ".join(cast)})' if cast else ''))
    elif kind == 'eyeline':
        if unit is None:
            raise ShiseiError('eyeline needs --unit <segment with two characters>')
        cid = None
    else:
        if in_agent_shell():
            raise ShiseiError('redrawing a character from a picture is started by the user, not from the director shell')
        if not args.cast or not args.ref:
            raise ShiseiError('redraw needs --cast <id> and --ref <png / jpg / webp of the clothes>')
        cid = args.cast
    c = next((x for x in story['cast'] if x['id'] == cid), None) if cid else None
    if cid and c is None:
        raise ShiseiError(f'unknown character {cid}')
    if c and c.get('look_young'):
        raise ShiseiError(f'{c["name"]} is marked as looking young: no close-up cuts and no redraws from a picture for this character')

    if kind == 'crop':
        geo = trial_crop_geometry(*png_size(src), parse_box(args.box or ''))
        cut = trial_name(kind, n, 'in')
        prepare.append(trial_crop_args(src, d / cut, geo))
        variants = trial_crop(story, cid, cut)
        extra = {'source': src.name, 'box': args.box, 'cut': {'pixels': list(geo['crop']), 'enlarged': geo['scale']}, 'cast': cid}
    elif kind == 'eyeline':
        variants, extra = trial_eyeline(story, unit, args.who, args.at, args.note)
        extra['unit'] = unit['id']
    else:
        ref = Path(args.ref).expanduser()
        if not ref.is_file() or ref.stat().st_size > MAX_REF_BYTES:
            raise ShiseiError('--ref is a picture file of at most 15 MB')
        with open(ref, 'rb') as f:
            fmt = image_type(f.read(16))
        if fmt is None:
            raise ShiseiError('--ref is not a PNG, JPEG or WebP picture (checked by its bytes, not its name)')
        local = trial_name(kind, n, 'ref')
        prepare.append(image_to_png_args(ref, d / local, DRAMA_PORTRAIT_SIZE, 'contain'))
        variants = trial_redraw(story, cid, local)
        extra = {'cast': cid, 'ref': {'name': ref.name, 'type': fmt, 'sha256': hashlib.sha256(ref.read_bytes()).hexdigest()}}
    inputs = list(dict.fromkeys(x for v in variants for x in v['images']))
    made_here = {a[-1] for a in prepare}
    for x in inputs:
        if str(d / x) not in made_here and not (d / x).is_file():
            raise ShiseiError(f'{x} is not in the project yet')
    jobs = trial_jobs(rid, kind, n, variants, args.seeds)
    record = {'kind': kind, 'n': n, **extra, 'inputs': inputs,
              'variants': [{k: v[k] for k in ('key', 'prompt', 'images')} | {'size': list(v['size'])} for v in variants],
              'outputs': [{k: j[k] for k in ('file', 'variant', 'seed')} for j in jobs]}
    if args.dry_run:
        return {'dry_run': True, **record, 'record': f'{TRIAL_DIR}/{kind}_{n}.json',
                'prepare': prepare, 'uploads': {x: trial_remote(rid, x) for x in inputs},
                'workflows': [{'file': j['file'], 'workflow': j['api']} for j in jobs]}

    info = instance_info(cfg)
    if info.get('State') != 'Running':
        raise ShiseiError(f'the machine is not running (state: {info.get("State")}). drama-trial never starts it: '
                          'ask the user, start it with `up --confirm-billing`, then run this again')
    price = float(info.get('InstancePrice') or 0)
    est = drama_estimate(story, [j['file'] for j in jobs], 'trial', '576p', price, running=True)
    budget_check(budget_state(job, load_ledger(d)), est['cost'])
    ensure_claim(cfg)
    began, state, made, run = time.time(), 'failed', [], TrialRun()
    try:
        sec = do_secure(cfg, info)   # the same check `up` runs: the machine was started outside this command
        if not sec['clean']:
            raise ShiseiError('security check found something on the instance; nothing was generated. Run `secure`, '
                              'pull finished outputs, stop the instance and do not reuse it')
        cmd_tunnel(argparse.Namespace(close=False), cfg)
        linked = ssh_run(cfg, info, ensure_models_script('qwen'), timeout=300)
        missing = linked.strip().splitlines()[-1].split(':', 1)[1].split()
        if missing:
            raise ShiseiError('model files missing on the instance: ' + ', '.join(missing))
        nodes = _object_info_missing(DRAMA_NODES['qwen'])
        if nodes:
            raise ShiseiError('ComfyUI on the instance lacks nodes: ' + ', '.join(nodes) + ' (update ComfyUI)')
        (d / TRIAL_DIR).mkdir(exist_ok=True)
        for argv in prepare:
            subprocess.run(argv, check=True, capture_output=True)
        for x in inputs:
            _upload_as(cfg, info, d / x, trial_remote(rid, x))
        for j in jobs:
            pid = post_prompt(j['api'])
            schedule_stop(cfg, working_stop(cfg))
            _wait_prompt(run, cfg, pid, j['file'], 30)
            outs = _remote_ls(cfg, info, 'output', f'drama/{j["prefix"]}_*.png')
            if not outs:
                raise ShiseiError(f'{j["file"]} finished but no output found')
            _pull_one(cfg, info, f'output/{outs[-1]}', d / j['file'])   # pulled only: never copied to input/ on the instance
            made.append(j['file'])
        state = 'done'
        return {'state': 'done', 'made': made, 'record': f'{TRIAL_DIR}/{kind}_{n}.json',
                'auto_stop': f'{int(cfg.get("idle_stop_minutes") or 10)}m'}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, StopIteration, IndexError) as e:
        raise ShiseiError(str(e)[:600])
    finally:
        ended = time.time()
        _arm_idle_stop(cfg, run)   # every exit: the 3 h schedule set while working never stays
        if (d / TRIAL_DIR).is_dir():
            save_json_atomic(d / TRIAL_DIR / f'{kind}_{n}.json', {**record, 'state': state, 'made': made, 'at': ended})
        entry = ledger_entry('trial', len(made), began, ended, price, state)
        append_ledger(d, entry)


EDIT_FIELDS = ('keyframe', 'action', 'seconds', 'cast', 'props')


def next_unit_version(d, uid):
    """The version number archive_unit gives a unit's files next (kf_<uid>.png -> kf_<uid>.vN.png)."""
    d = Path(d)
    n = 1
    while any(d.glob(f'*_{uid}*.v{n}.*')) or any(d.glob(f'kf_{uid}.v{n}.*')):
        n += 1
    return n


def record_frames(review, story, uids):
    """Note what each first frame just made was drawn from (review.json `frame_inputs`: {unit id: frame_hash}). It is
    not a verdict; drama-version uses it to tell whether an archived approval still applies. Changes `review` in place."""
    by = {x['id']: x for x in story['shots']}
    for uid in uids:
        if uid in by:
            review.setdefault('frame_inputs', {})[uid] = frame_hash(story, by[uid])
    return review


def archive_unit(d, uid, story, shot_ids):
    """Move a unit's current keyframe, takes and inner voices to the next version number; nothing is deleted."""
    n = next_unit_version(d, uid)
    moved = []
    for f in [d / f'kf_{uid}.png'] + [d / f'take_{uid}_{s}.mp4' for s in DRAMA_SEEDS] + [d / f'take_{uid}_{s}.jpg' for s in DRAMA_SEEDS] \
            + [d / f'vo_{x}.wav' for x in shot_ids]:
        if f.exists():
            f.replace(f.with_name(f'{f.stem}.v{n}{f.suffix}'))
            moved.append(f.name)
    rec = load_recommend(d)
    if rec.pop(uid, None) is not None:   # the suggestion was about takes that are no longer current
        save_json_atomic(d / 'recommend.json', rec)
    return n, moved


def reset_unit(d, u, frame, review, voices=None):
    """A segment's script changed. Its current first frame, takes and inner voices become a version (nothing is
    deleted), the pick is dropped, and when the first frame no longer fits (`frame`) so is its approval: the user
    reviews the new one. Otherwise the frame is copied back with the verdict it had and only the video is remade.
    Changes `review` in place; returns None when the segment had nothing made and nothing decided."""
    uid = u['id']
    had = review['keyframes'].get(uid)
    made = any((d / f).exists() for f in [f'kf_{uid}.png'] + [f'take_{uid}_{s}.mp4' for s in DRAMA_SEEDS])
    if not made and had is None and uid not in review['picks']:
        return None
    version, moved = archive_unit(d, uid, None, voices if voices is not None else [b['id'] for b in u['beats']])
    # the archived first frame keeps the verdict the user gave it, so restoring that version never invents an approval,
    # and what it was drawn from when that is known (None: made before this was recorded), so an approval is only
    # given back to a script that still asks for the same picture
    review.setdefault('versions', {}).setdefault(uid, {})[str(version)] = had
    review.setdefault('version_inputs', {}).setdefault(uid, {})[str(version)] = (review.get('frame_inputs') or {}).get(uid)
    old = d / f'kf_{uid}.v{version}.png'
    if frame or not old.exists():
        review['keyframes'].pop(uid, None)
        (review.get('frame_inputs') or {}).pop(uid, None)
    else:   # the first frame still fits: put it back so only the video is remade
        shutil.copyfile(old, d / f'kf_{uid}.png')
    review['picks'].pop(uid, None)
    (review.get('edits') or {}).pop(uid, None)   # the user's edits were of the video now kept as a version
    return {'version': version, 'moved': moved, 'redo': ['keyframe', 'takes'] if frame else ['takes']}


def log_change(d, entry):
    log = json.loads((d / 'changes.json').read_text()) if (d / 'changes.json').exists() else []
    log.append({'at': time.time(), **entry})
    save_json_atomic(d / 'changes.json', log)


def cmd_drama_edit(args, cfg):
    """Change one shot (the director's small edit). Validates the whole story, keeps the old files of the affected
    segment as a version, logs the change, and returns before/after plus the script check."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    by_label = {u['label']: u['id'] for u in units(story)}   # a segment letter means its first shot
    sid = args.shot if any(x['id'] == args.shot for x in story['shots']) else by_label.get(args.shot)
    sh = next((x for x in story['shots'] if x['id'] == sid), None)
    if sh is None:
        raise ShiseiError(f'unknown shot {args.shot}')
    args.shot = sid
    before = json.loads(json.dumps(sh))
    staging = getattr(args, 'staging', None)
    if staging == 'none':
        sh.pop('staging', None)
    elif staging is not None:
        sh['staging'] = staging
    for f in ('keyframe', 'action'):
        if getattr(args, f) is not None:
            sh[f] = getattr(args, f).strip()
    if args.seconds is not None:
        sh['seconds'] = args.seconds
    if args.no_line:
        sh.pop('line', None)
    elif any(v is not None for v in (args.line_text, args.line_kind, args.line_who, args.line_delivery)):
        line = dict(sh.get('line') or {})
        for k, v in (('text', args.line_text), ('kind', args.line_kind), ('who', args.line_who), ('delivery', args.line_delivery)):
            if v is not None:
                line[k] = v
        sh['line'] = line
    if sh == before:
        raise ShiseiError('nothing to change')
    validate_story(story)
    old = load_story(d)
    uncropped = drop_stale_crops(old, story)   # the script moved away from the picture the user cropped
    u = next(x for x in units(story) if any(b['id'] == args.shot for b in x['beats']))
    if getattr(args, 'dry_run', False):   # what the edit would do; writes nothing
        redo = changed_units(old, story).get(u['id'])
        return {'unit': u['id'], 'label': u['label'], 'before': before, 'after': sh, 'uncropped': uncropped, 'dry_run': True,
                'redo': ['keyframe', 'takes'] if redo == 'frame' else ['takes'] if redo else [],
                'archive': [f for f in [f'kf_{u["id"]}.png'] + [f'take_{u["id"]}_{x}.mp4' for x in DRAMA_SEEDS] if (d / f).exists()] if redo else [],
                'warnings': story_warnings(story)}
    n = 1
    while (d / f'story.v{n}.json').exists():
        n += 1
    shutil.copyfile(d / 'story.json', d / f'story.v{n}.json')
    save_story(d, story)
    # same rule as drama-set: the first frame is remade only when what it is drawn from changed
    picture = changed_units(old, story).get(u['id']) == 'frame'
    review = load_review(d)
    r = reset_unit(d, u, picture, review) or {'version': None, 'moved': [], 'redo': []}
    save_json_atomic(d / 'review.json', review)
    version, moved = r['version'], r['moved']
    log_change(d, {'kind': 'edit', 'shot': args.shot, 'unit': u['id'], 'label': u['label'], 'version': version,
                   'why': (args.why or '')[:300], 'before': before, 'after': sh})
    return {'unit': u['id'], 'label': u['label'], 'before': before, 'after': sh,
            'archived_as': f'v{version}' if version else None, 'moved': moved, 'uncropped': uncropped,
            'redo': ['keyframe', 'takes'] if picture else ['takes'], 'warnings': story_warnings(story)}


def unit_versions(d, uid):
    vs = sorted({int(m.group(1)) for f in d.glob(f'*{uid}*.v*.*') for m in [re.search(r'\.v(\d+)\.[a-z0-9]+$', f.name)] if m})
    return [{'version': v, 'keyframe': f'kf_{uid}.v{v}.png' if (d / f'kf_{uid}.v{v}.png').exists() else None,
             'takes': [s for s in DRAMA_SEEDS if (d / f'take_{uid}_{s}.v{v}.mp4').exists()]} for v in vs]


def restored_verdict(was, made_from, now):
    """What a restored first frame's review is: (verdict record or None, why).
    The approval comes back only when the user had approved that version AND it is known what the version was drawn
    from AND the script asks for exactly that today. why: kept | never (was not approved) | unknown (made before
    inputs were recorded) | changed (the script asks for a different first frame now)."""
    if not (was and was.get('status') == 'pass'):
        return None, 'never'
    if not made_from:
        return None, 'unknown'
    if made_from != now:
        return None, 'changed'
    return was, 'kept'


@locks_review
def cmd_drama_version(args, cfg):
    """Swap a segment back to an older version: the current files become a new version, so nothing is lost.
    Restoring is not approving: see restored_verdict. A crop instruction survives only if the restored first frame was
    made by it; otherwise the user has chosen a picture that is not that crop, and the instruction is removed (this
    also covers restoring the very version the crop was cut from, whose file moves back to kf_<id>.png)."""
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    u = unit_by_id(story, args.unit)
    uid = u['id']
    old = [f for f in d.glob(f'*.v{args.restore}.*') if (f'_{uid}' in f.name or f.name.startswith(f'kf_{uid}.'))
           or any(f.name.startswith(f'vo_{b["id"]}.') for b in u['beats'])]
    if not old:
        raise ShiseiError(f'{u["label"]} has no version {args.restore}')
    review = load_review(d)
    current_inputs = (review.get('frame_inputs') or {}).get(uid)
    newer, moved = archive_unit(d, uid, story, [b['id'] for b in u['beats']])
    for f in old:
        f.replace(f.with_name(f.name.replace(f'.v{args.restore}.', '.')))
    saved = review.setdefault('versions', {}).setdefault(uid, {})
    saved[str(newer)] = review['keyframes'].get(uid)
    was = saved.pop(str(args.restore), None)
    made = review.setdefault('version_inputs', {}).setdefault(uid, {})
    made[str(newer)] = current_inputs
    made_from = made.pop(str(args.restore), None)
    has_frame = (d / f'kf_{uid}.png').exists()
    first = _by_id(story, 'shots', uid)
    crop_cleared = False
    if first.get('crop') and not (has_frame and made_from and made_from == frame_hash(story, first)):
        n = 1
        while (d / f'story.v{n}.json').exists():
            n += 1
        shutil.copyfile(d / 'story.json', d / f'story.v{n}.json')
        del first['crop']
        save_story(d, validate_story(story))
        crop_cleared = True
    verdict_now, why = restored_verdict(was, made_from, frame_hash(story, first)) if has_frame else (None, 'never')
    if verdict_now:
        review['keyframes'][uid] = verdict_now
    else:
        review['keyframes'].pop(uid, None)
    if has_frame and made_from:
        review.setdefault('frame_inputs', {})[uid] = made_from
    else:
        (review.get('frame_inputs') or {}).pop(uid, None)
    review['picks'].pop(uid, None)
    save_json_atomic(d / 'review.json', review)
    log_change(d, {'kind': 'restore', 'unit': uid, 'label': u['label'], 'version': newer, 'restored': args.restore,
                   'shot': None, 'why': f'换回 v{args.restore}'})
    return {'unit': uid, 'restored': args.restore, 'current_saved_as': f'v{newer}', 'saved_version': newer,
            'keyframe_review': verdict(review, 'keyframes', uid), 'approval': why, 'crop_cleared': crop_cleared,
            'versions': unit_versions(d, uid)}


# ---------------------------------------------------------------- 裁特写: a close-up cut from the segment's own first frame
#
# point/ and trial/ pictures never become generation inputs. There is ONE sanctioned exception to "a picture the user
# pointed at is context only", and it is this command, drawn narrowly:
#   - the picture that is cut is the segment's OWN first frame, in the version the user approved (kf_<id>.png with a
#     `pass` verdict when drama-crop runs; it is then archived as kf_<id>.vN.png and the story names that file).
#     validate_story accepts no other source name, so no take, no other segment, no asset, nothing in point/ or
#     trial/, and no outside file can be named;
#   - it is the user's framing request: the box comes from the page, the command is refused in the director shell,
#     and drama-set cannot add or alter a `crop` (strip_user_fields / merge_user_fields);
#   - the cut (crop/kf_<id>.in.png, made again before every run) is the only input image of that first frame and is
#     used for nothing else; the redrawn frame comes back unreviewed like any first frame;
#   - not for a segment whose first frame shows a character marked look_young (refused here and again before a run).

def crop_base(d, story, review, u):
    """(file name the next crop box is measured on, None) or (None, why not). The current first frame when the user
    has approved it; else, when a crop is already set and its redraw is not approved yet, the same source again (the
    user is adjusting the box)."""
    d = Path(d)
    first = u['first']
    cast = [_cast(story, c) for c in first.get('cast') or []]
    if not cast:
        return None, '首帧里没有人：裁特写只验证过有人的画面'
    young = [c['name'] for c in cast if c.get('look_young')]
    if young:
        return None, f'{"、".join(young)}标了「看起来年轻」：不裁特写'
    kf = f'kf_{u["id"]}.png'
    if (d / kf).is_file() and verdict(review, 'keyframes', u['id']) == 'pass':
        return kf, None
    cr = first.get('crop')
    if cr and (d / cr['source']).is_file():
        return cr['source'], None
    return None, '首帧通过后才能裁特写'


def cmd_drama_crop(args, cfg):
    """裁特写 (see the section comment for exactly what is allowed). Records on the segment's first shot
    crop = {source: kf_<id>.vN.png, box: [x, y, w, h], cast, note}: box is the 9:16 cut as fractions of the source.
    Saved with apply_story, so in code: the current first frame and takes become version N (the source is that
    archived file, which stays put), the first frame goes back to be made and reviewed, the pick is dropped. The next
    `drama-run keyframes` cuts the source locally and redraws it with the `alone` recipe; nothing is spent here.
    --clear removes the instruction (the first frame is then drawn from the script again). --dry-run writes nothing."""
    if in_agent_shell():
        raise ShiseiError('a close-up crop is framed by the user on the Piying page, not from the director shell: '
                          'tell the user which part of the first frame you would cut to')
    d, job = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    u = next((x for x in units(story) if args.unit in (x['id'], x['label'])), None)
    if u is None:
        raise ShiseiError(f'unknown segment {args.unit}')
    uid = u['id']
    new = copy.deepcopy(story)
    first = _by_id(new, 'shots', uid)
    cur = u['first'].get('crop')
    cut = None
    if args.clear:
        if not cur:
            raise ShiseiError(f'segment {u["label"]} has no crop')
        del first['crop']
        what = f'段 {u["label"]} 取消裁特写'
    else:
        base, why_not = crop_base(d, story, review, u)
        if base is None:
            raise ShiseiError(f'segment {u["label"]}: {why_not}')
        cast = u['first'].get('cast') or []
        cid = args.cast or (cast[0] if len(cast) == 1 else None)
        if cid not in cast:
            raise ShiseiError(f'say whose close-up this is: --cast <id> (the first frame of segment {u["label"]} shows: {", ".join(cast)})')
        W, H = png_size(d / base)
        geo = crop_geometry(W, H, parse_box(args.box or ''))
        source = base if base != f'kf_{uid}.png' else f'kf_{uid}.v{next_unit_version(d, uid)}.png'   # what reset_unit archives it as
        first['crop'] = {'source': source, 'box': normal_box(W, H, geo), 'cast': cid}
        if (args.note or '').strip():
            first['crop']['note'] = args.note.strip()[:200]
        cut = {'from': base, 'pixels': list(geo['crop']), 'enlarged': geo['scale']}
        what = f'段 {u["label"]} 裁特写（{_cast(story, cid)["name"]}）'
    validate_story(new)
    redo = changed_units(story, new).get(uid)
    kf, quality = f'kf_{uid}.png', (job.get('params') or {}).get('quality') or '576p'
    files = [kf] + [f'take_{uid}_{s}.mp4' for s in DRAMA_SEEDS]
    est = {k: drama_estimate(new, [kf], 'keyframes', quality, args.price or 0, running=k == 'running') for k in ('cold', 'running')}
    effects = {'unit': uid, 'label': u['label'], 'what': what, 'crop': first.get('crop'), 'before': cur, 'cut': cut,
               'same': redo is None, 'redo': ['keyframe', 'takes'] if redo else [],
               'archive': [f for f in files if (d / f).exists()] if redo else [], 'review': 'pending',
               # what the next run is, for the page's price: drama-estimate <dir> keyframes --shots <unit> --assume-missing kf_<unit>.png
               'next_run': {'stage': 'keyframes', 'shots': [uid], 'items': 1, 'assume_missing': [kf],
                            'minutes': est['cold']['minutes'], 'minutes_running': est['running']['minutes'],
                            'price_per_hour': args.price, 'cost': est['cold']['cost'] if args.price is not None else None,
                            'cost_running': est['running']['cost'] if args.price is not None else None}}
    if args.clear and cur:
        effects['source_version'] = int(cur['source'].rsplit('.v', 1)[1][:-4])   # drama-version --restore N brings the uncut frame back
    if args.dry_run or redo is None:
        return {**effects, 'dry_run': bool(args.dry_run)}
    r = apply_story(d, story, new, (args.note or '').strip() or what)
    if first.get('crop') and not (d / first['crop']['source']).is_file():
        raise ShiseiError(f'internal: the first frame was not archived as {first["crop"]["source"]}')
    return {**effects, 'dry_run': False, **r}


EDIT_MODES = ('cut', 'hold', 'swap')   # cut: picture and sound go; hold: the next piece's first frame, sound kept; swap: another take
EDIT_MIN = 0.2


def unit_edits(review, uid, pick, d=None):
    """The user's edits of a segment's picked take: [{'at': [a, b], 'mode': 'cut' | 'hold' | 'swap', 'seed'?}], sorted.
    Edits belong to the video they were made on: after another pick, or once that take is shot again (same take
    number, new file: `d` given, its time differs), they no longer apply and are not shown."""
    e = (review.get('edits') or {}).get(uid) or {}
    if e.get('seed') != pick:
        return []
    if d is not None:
        f = Path(d) / f'take_{uid}_{pick}.mp4'
        if e.get('mtime') is None or not f.exists() or abs(f.stat().st_mtime - e['mtime']) > 0.001:
            return []
    return sorted(e.get('ops') or [], key=lambda o: o['at'][0])


def lines_in(story, unit, duration, a, b):
    """Spoken lines of a segment whose time (scaled to the take) overlaps a..b by more than 0.1 s."""
    scale = duration / unit['seconds'] if unit['seconds'] else 1
    return [line['text'] for t0, t1, line, _ in sh_lines(unit)
            if line.get('kind', 'say') != 'inner' and min(b, t1 * scale) - max(a, t0 * scale) > 0.1]


def remap_time(t, ops):
    """A time in the take -> its time after the edits (only `cut` removes time). Inside a cut: where the cut lands."""
    shift = 0.0
    for o in ops:
        a, b = o['at']
        if o['mode'] != 'cut' or t <= a:
            continue
        if t < b:
            return a - shift
        shift += b - a
    return t - shift


def edited_length(duration, ops):
    return duration - sum(o['at'][1] - o['at'][0] for o in ops if o['mode'] == 'cut')


def edit_pieces(clip, duration, ops, take_of=None):
    """[(file, a, b, kind)] that make the edited segment, kind 'av' (as shot) or 'hold' (frame at b, sound a..b)."""
    out, t = [], 0.0
    for o in ops:
        a, b = o['at']
        if a > t:
            out.append((clip, t, a, 'av'))
        if o['mode'] == 'hold':
            out.append((clip, a, b, 'hold'))
        elif o['mode'] == 'swap':
            out.append((take_of(o['seed']), a, b, 'av'))
        t = b
    if t < duration:
        out.append((clip, t, duration, 'av'))
    return [p for p in out if p[2] - p[1] > 0.01]


def pieces_args(pieces, out, width):
    """ffmpeg command that joins pieces of takes into one clip (same size, frame rate and sound as a take)."""
    h = width * 16 // 9
    files = list(dict.fromkeys(str(p[0]) for p in pieces))
    args = ['ffmpeg', '-v', 'error', '-y']
    for f in files:
        args += ['-i', f]
    parts = []
    for k, (f, a, b, kind) in enumerate(pieces):
        i = files.index(str(f))
        if kind == 'hold':
            v = (f'[{i}:v]trim=start={b:.3f}:duration=0.05,setpts=PTS-STARTPTS,scale={width}:{h},setsar=1,fps={FPS},'
                 f'tpad=stop_mode=clone:stop_duration={b - a:.3f},trim=duration={b - a:.3f}[v{k}]')
        else:
            v = f'[{i}:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS,scale={width}:{h},setsar=1,fps={FPS}[v{k}]'
        parts.append(v + f';[{i}:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,aresample=48000[a{k}]')
    parts.append(''.join(f'[v{k}][a{k}]' for k in range(len(pieces))) + f'concat=n={len(pieces)}:v=1:a=1[v][a]')
    return args + ['-filter_complex', ';'.join(parts), '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-crf', '16',
                   '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', str(out)]


def lead_args(picture, seconds, move, out, width):
    """A still of the scene picture as a short shot (push: slow zoom in; pan: slow drift sideways; still), silent."""
    h = width * 16 // 9
    n = max(1, int(round(seconds * FPS)))
    if move == 'push':
        vf = (f'scale={width * 2}:{h * 2},zoompan=z=\'1+0.08*on/{n}\':x=\'iw/2-(iw/zoom/2)\':y=\'ih/2-(ih/zoom/2)\''
              f':d={n}:s={width}x{h}:fps={FPS}')
    elif move == 'pan':
        vf = (f'scale={width * 2}:{h * 2},zoompan=z=1.1:x=\'(iw-iw/zoom)*on/{n}\':y=\'ih/2-(ih/zoom/2)\''
              f':d={n}:s={width}x{h}:fps={FPS}')
    else:
        vf = f'scale={width}:{h},loop=loop={n}:size=1,fps={FPS}'
    return ['ffmpeg', '-v', 'error', '-y', '-i', str(picture), '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000',
            '-filter_complex', f'[0:v]{vf},setsar=1,trim=duration={seconds:.3f},format=yuv420p[v]', '-map', '[v]', '-map', '1:a',
            '-t', f'{seconds:.3f}', '-c:v', 'libx264', '-crf', '16', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', str(out)]


def _seconds_range(text):
    m = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*', str(text))
    if not m:
        raise argparse.ArgumentTypeError('a range of seconds such as 0-0.58')
    return float(m.group(1)), float(m.group(2))


def cmd_drama_trim(args, cfg):
    """The user's edit of a segment's picked take, applied at the cut (local, no GPU): `cut` a piece out, `hold` the
    next piece's first frame over it (the sound stays), or `swap` it for the same seconds of another take.
    From the Piying page only, like picking a take."""
    if in_agent_shell():
        raise ShiseiError('editing a take is done by the user on the Piying page, not from the director shell; '
                          'tell the user which seconds look wrong (drama-shots shows them) and they decide')
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    u = next((x for x in units(story) if x['id'] == args.segment or x['label'] == args.segment), None)
    if u is None:
        raise ShiseiError(f'unknown segment: {args.segment}')
    pick = review['picks'].get(u['id'])
    if not pick:
        raise ShiseiError(f'{u["label"]} has no take picked yet: pick one first, then edit it')
    if not (args.at or args.undo or args.clear):
        raise ShiseiError('say what to do: --at A-B [--mode cut|hold|swap], --undo A-B or --clear')
    edits = review.setdefault('edits', {})
    ops = unit_edits(review, u['id'], pick, d)
    if args.clear:
        ops = []
    elif args.undo:
        a, b = args.undo
        ops = [o for o in ops if not (abs(o['at'][0] - a) < 0.01 and abs(o['at'][1] - b) < 0.01)]
    else:
        a, b = args.at
        dur = ffprobe_duration(d / f'take_{u["id"]}_{pick}.mp4')
        if not (0 <= a < b <= dur + 0.01) or b - a < EDIT_MIN:
            raise ShiseiError(f'{a:g}-{b:g} s is not a piece of this take (0-{dur:.2f} s, at least {EDIT_MIN} s)')
        if any(min(b, o['at'][1]) > max(a, o['at'][0]) for o in ops):
            raise ShiseiError('that overlaps an edit already made: undo it first')
        op = {'at': [round(a, 3), round(b, 3)], 'mode': args.mode}
        if args.mode == 'swap':
            if not args.take:
                raise ShiseiError('swap needs --take: which take the piece comes from')
            seed = resolve_take(args.take, [x for x in DRAMA_SEEDS if (d / f'take_{u["id"]}_{x}.mp4').exists()])
            if seed == pick:
                raise ShiseiError('that is the take already picked')
            op['seed'] = seed
        elif args.mode == 'hold' and b >= dur - 0.05:
            raise ShiseiError('hold shows the next piece: there is none after the end of the take (use cut)')
        said = lines_in(story, u, dur, a, b)
        if said and args.mode == 'cut' and not args.with_line:
            raise ShiseiError(f'{a:g}-{b:g} s has the line 「{said[0]}」: cutting it also cuts the words. '
                              'Use hold (keeps the sound, shows the next shot) or add --with-line')
        ops = sorted(ops + [op], key=lambda o: o['at'][0])
    if ops:
        edits[u['id']] = {'seed': pick, 'ops': ops, 'at': time.time(),
                          'mtime': (d / f'take_{u["id"]}_{pick}.mp4').stat().st_mtime}
    else:
        edits.pop(u['id'], None)
    save_json_atomic(d / 'review.json', review)
    return {'segment': u['label'], 'take': take_number(pick), 'edits': ops}


def cmd_drama_cut(args, cfg):
    d, job = drama_dir(args.jobdir)
    story = load_story(d)
    review = load_review(d)
    pre = current_precheck(d)   # the nudity / age verdicts on the files that are on disk now
    clips, entries, t, cut_units, voices = [], [], 0.0, units(story), []
    width = DRAMA_SIZES[(job.get('params') or {}).get('quality') or '576p'][0]
    segs = story.get('segments') or []
    work = d / 'cut_parts'   # the lead-in stills and edited segments, made again at every cut
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    unit_clips, starts, leads, used_edits = [], [], {}, {}
    for i, u in enumerate(cut_units):
        pick = review['picks'].get(u['id'])
        takes = [seed for seed in DRAMA_SEEDS if (d / f'take_{u["id"]}_{seed}.mp4').exists()]
        if not takes:
            raise ShiseiError(f'{u["label"]} has no take yet')
        if pick not in takes:
            raise ShiseiError(f'{u["label"]} has no take picked: the user picks one on the Piying page '
                              '(drama-recommend only suggests)')
        if verdict(review, 'keyframes', u['id']) == 'reject':   # its takes were made from a picture the user turned down
            raise ShiseiError(f'{u["label"]}: the first frame was rejected; remake it and its takes before the cut')
        seed = pick
        kf_name = f'kf_{u["id"]}.png'
        take_name = f'take_{u["id"]}_{seed}.mp4'
        for name in (kf_name, take_name):
            if (pre.get(name) or {}).get('verdict') == 'block':
                raise ShiseiError(f'{u["label"]}：预审看到裸露，而且人物像未成年，不能进成片。'
                                  '换一条，或回到首帧重画。')
        if needs_moderation(story):
            for name in (kf_name, take_name):
                if (pre.get(name) or {}).get('status') != 'done':
                    raise ShiseiError(f'{u["label"]}：有看起来年轻或未成年的角色，成片前必须先在页面上预审首帧和选中的 take。')
        clip = d / f'take_{u["id"]}_{seed}.mp4'
        dur = ffprobe_duration(clip)
        lead = (segs[i] or {}).get('lead_in') if i < len(segs) else None
        if lead:   # the scene picture as a short silent shot before the segment
            lf = work / f'lead_{u["id"]}.mp4'
            sec = float(lead.get('seconds', 1.5))
            r = subprocess.run(lead_args(d / f'scene_{u["scene"]}.png', sec, lead.get('move', 'push'), lf, width),
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise ShiseiError(f'ffmpeg failed on the lead-in of {u["label"]}: {r.stderr.strip()[-300:]}')
            clips.append(lf)
            leads[u['id']] = {'seconds': sec, 'move': lead.get('move', 'push')}
            t += ffprobe_duration(lf)
        ops = unit_edits(review, u['id'], seed, d)
        scale = dur / u['seconds'] if u['seconds'] else 1   # the clip is a little longer than asked (17n+5 frames)
        if ops:
            ef = work / f'unit_{u["id"]}.mp4'
            r = subprocess.run(pieces_args(edit_pieces(clip, dur, ops, lambda sd, u=u: d / f'take_{u["id"]}_{sd}.mp4'), ef, width),
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise ShiseiError(f'ffmpeg failed on the edits of {u["label"]}: {r.stderr.strip()[-300:]}')
            used_edits[u['id']] = ops
        for t0, t1, line, beat in inner_lines(u):
            vo = d / f'vo_{beat["id"]}.wav'
            if not vo.exists():   # a run cut off before it brought this voice back: the cut had silence under its subtitle (2026-11-14)
                raise ShiseiError(f'{u["label"]}：心里话「{line["text"].strip()[:20]}」的声音还没拍回来（上次运行中断了）。'
                                  f'在底栏点「拍」补上，只拍缺的这一句；再合成。')
            voices.append((vo, t + remap_time(t0 * scale, ops) + 0.25))
        for t0, t1, line, beat in sh_lines(u):
            who = _cast(story, line['who'])['name']
            text = line['text'].strip()
            text = f'（{who}心里）{text}' if line.get('kind') == 'inner' else text
            a, b = remap_time(t0 * scale, ops), remap_time(t1 * scale, ops)
            if b - a > 0.2:   # a line whose seconds were all cut out has no subtitle
                entries.append((t + a, t + b, text))
        out_clip = ef if ops else clip
        clips.append(out_clip)
        unit_clips.append((out_clip, u))
        starts.append(round(t, 3))
        t += ffprobe_duration(out_clip) if ops else dur
    durs = [ffprobe_duration(c) for c in clips]
    # room-tone bed: the loudest clip that has no dialogue (its sound is just the room); none → no bed
    silent = [c for c, sh in unit_clips if not sh_lines(sh)]
    bed = max(silent, key=mean_volume) if silent and args.sound else None
    if bed is not None and mean_volume(bed) < -45:
        bed = None   # practically silent: nothing to carry across the cuts
    if bed is None and args.sound:
        bed = 'synth'
    srt = d / 'final.srt'
    srt.write_text(build_srt(entries))
    enc = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True).stdout
    soft = ' mov_text ' in enc and bool(entries)
    # the picture stays clean by default; --label burns the old corner mark back in for anyone who needs it
    burn = bool(getattr(args, 'label', False))
    label = ASSETS / 'composite' / 'ai_label.png' if burn else None
    r = subprocess.run(cut_args(clips, label, d / 'final.mp4', width, srt if soft else None,
                                durs=durs if args.sound else None, bed=bed if args.sound else None, voices=voices,
                                silent=[k for k, c in enumerate(clips) if c.name.startswith('lead_')]),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ShiseiError(f'ffmpeg failed: {r.stderr.strip()[-400:]}')
    _thumb(d / 'final.mp4', d / 'final.jpg', at=1.0)
    sound = {'clips_lufs': [loudness(c) for c, _ in unit_clips], 'final_lufs': loudness(d / 'final.mp4'),
             'inner_voices': len(voices), 'levelled': bool(args.sound), 'bed': (bed if bed == 'synth' else bed.name) if bed is not None and args.sound else None}
    takes_used = [f'take_{u["id"]}_{review["picks"][u["id"]]}.mp4' for u in cut_units]
    save_json_atomic(d / 'cut.json', {'sound': sound, 'at': time.time(), 'clips': takes_used,
                                      'label': {'burned_in': burn, 'metadata': 'AI-generated (Piying)'},
                                      # edits / leads: what the cut applied, so a later change shows as 有改动
                                      'edits': used_edits, 'leads': leads,
                                      'units': [{'id': u['id'], 'label': u['label'], 'start': st,
                                                 'seconds': round(ffprobe_duration(c), 3), 'lead': leads.get(u['id'])}
                                                for (c, u), st in zip(unit_clips, starts)]})
    return {'video': str(d / 'final.mp4'), 'seconds': round(t, 2), 'clips': len(cut_units), 'sound': sound,
            'edits': {k: len(v) for k, v in used_edits.items()}, 'leads': list(leads),
            'subtitles': 'embedded (soft) + final.srt' if soft else 'final.srt only',
            'label': 'AI 生成（烧入画面）' if burn else 'AI 生成（只在文件元数据里）'}


def local_mode_wanted(cfg):
    return bool(os.environ.get('SHISEI_COMFY_URL') or cfg.get('comfy_url'))


def main(argv=None):
    p = argparse.ArgumentParser(prog='shisei.py', description=__doc__.split('\n')[0])
    p.add_argument('--machine', help='instance id to use (default: the one this project leases, else the preferred one)')
    p.add_argument('--holder', help='who holds the lease (a project id; default SHISEI_HOLDER, else cli)')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('doctor')
    ic = sub.add_parser('install-cli')
    ic.add_argument('--if-missing', action='store_true', help='do nothing when the plugin\'s own compshare-cli is there')
    sub.add_parser('ffmpeg-check', help='where ffmpeg is, its version, whether it can find cuts, and how to install it here')
    sub.add_parser('ffmpeg-install', help="install / update ffmpeg with Homebrew (macOS) or winget (Windows); the user's click only")
    c = sub.add_parser('config')
    c.add_argument('action', choices=['show', 'set'])
    c.add_argument('key', nargs='?')
    c.add_argument('value', nargs='?')
    e = sub.add_parser('estimate')
    e.add_argument('--seeds', type=int, default=3)
    e.add_argument('--price', type=float)
    e.add_argument('--running', action='store_true', help='machine already up; no boot time')
    e.add_argument('--frames', type=int, default=H3_FRAMES, help='dance clip length in frames (job.json params.frames)')
    e.add_argument('--keep-bg', action='store_true', help='swap on the source clip: no mocap, no white model')
    sub.add_parser('status')
    ins = sub.add_parser('instances')
    ins.add_argument('--pick', action='store_true', help='remember the only suitable instance when none is set')
    ins.add_argument('--register', metavar='ID', help='add an instance of this account to the machines Piying may use')
    ins.add_argument('--unregister', metavar='ID', help='stop using an instance (nothing happens to the instance itself)')
    pa = sub.add_parser('patrol', help='keep every registered machine that is on under a scheduled stop (never stops one)')
    pa.add_argument('--dry-run', action='store_true')
    u = sub.add_parser('up')
    u.add_argument('--confirm-billing', action='store_true')
    u.add_argument('--max-wait-min', type=int, default=60)
    u.add_argument('--gpu-attempts', type=int, default=3)
    sub.add_parser('secure')
    t = sub.add_parser('tunnel')
    t.add_argument('--close', action='store_true')
    up = sub.add_parser('upload')
    up.add_argument('files', nargs='+')
    s = sub.add_parser('submit')
    s.add_argument('kind', choices=['mocap', 'swap'])
    s.add_argument('--video', required=True, help='file name inside ComfyUI input/')
    s.add_argument('--prefix', required=True)
    s.add_argument('--ref')
    s.add_argument('--ref2')
    s.add_argument('--subject', default='woman')
    s.add_argument('--details')
    s.add_argument('--prompt')
    s.add_argument('--seeds', type=int, nargs='+', default=[1001])
    s.add_argument('--character-strength', type=float)
    s.add_argument('--head-strength', type=float)
    s.add_argument('--frames', type=int, default=H3_FRAMES, help='length of the mannequin video in frames (17n+5)')
    s.add_argument('--keep-bg', choices=list(KEEP_BG_MODES),
                   help='swap on the source clip itself and keep its background (frame | paste | mask); needs --prompt')
    s.add_argument('--dry-run', action='store_true')
    w = sub.add_parser('wait')
    w.add_argument('ids', nargs='+')
    w.add_argument('--timeout-min', type=int, default=60)
    w.add_argument('--keep-alive', action='store_true', help='more work follows; do not arm the idle stop')
    pl = sub.add_parser('pull')
    pl.add_argument('--pattern', required=True)
    pl.add_argument('--out', required=True)
    pl.add_argument('--dir', choices=['output', 'input'], default='output', help='ComfyUI folder to pull from')
    d = sub.add_parser('down')
    d.add_argument('--yes', action='store_true')
    d.add_argument('--force-other', action='store_true', help='stop it although another project holds it (page only)')
    jn = sub.add_parser('job-new', help='create a job folder from local files (also visible in the Piying page)')
    jn.add_argument('--video', required=True)
    jn.add_argument('--front', required=True)
    jn.add_argument('--face', help='face close-up of the character (one person)')
    jn.add_argument('--details', default='', help='clothes and shoes (with two people: the left-hand one)')
    jn.add_argument('--people', type=int, choices=(1, 2), default=1, help='2: replace both dancers, keeps the source background')
    jn.add_argument('--front2', help='full-body picture of the right-hand character (two people)')
    jn.add_argument('--details2', default='', help="the right-hand character's clothes and shoes (two people)")
    jn.add_argument('--mode', choices=('studio', 'keepbg'), default='studio',
                    help='studio: mocap + white model (default); keepbg: swap on the source clip (always so for two people)')
    jn.add_argument('--start', type=float, default=0.0)
    jn.add_argument('--seconds', type=float, help=f'clip length, 2..{SWAP_MAX_SECONDS} s (default 4.46 s = 107 frames)')
    jn.add_argument('--crop-x', type=float)
    jn.add_argument('--shot', choices=['close', 'full'], default='close')
    jn.add_argument('--head', choices=['face', 'egg'], default='face')
    jn.add_argument('--seeds', type=int, default=3, choices=range(1, 7))
    js = sub.add_parser('job-status')
    js.add_argument('jobdir')
    jr = sub.add_parser('job-run', help='run the whole pipeline for a GUI job folder (resumable)')
    jr.add_argument('jobdir')
    jr.add_argument('--confirm-billing', action='store_true')
    jr.add_argument('--queue-minutes', type=int, choices=range(1, 241), metavar='MIN',
                    help='every machine busy: wait up to MIN minutes for one to come free (takes nothing while waiting)')
    jr.add_argument('--max-cost', type=float, help='a queued run starts only if it costs at most this (the confirmed quote)')
    sub.add_parser('machine-pick', help='read-only: the machine a run of --holder would use now, and who holds the others')
    je = sub.add_parser('job-estimate', help='minutes and cost of the next job-run (only the missing steps and takes), against the cap')
    je.add_argument('jobdir')
    je.add_argument('--price', type=float)
    je.add_argument('--running', action='store_true')
    je.add_argument('--more', type=int, choices=range(1, 7), help='price it as if this many takes were added')
    jl = sub.add_parser('job-ledger', help="the job's GPU runs and cap (read-only, except a ledger worked out once for old runs)")
    jl.add_argument('jobdir')
    jp = sub.add_parser('job-precheck', help='image-model look at each take; writes precheck.json only')
    jp.add_argument('jobdir')
    jp.add_argument('--timeout', type=int, default=420)
    jp.add_argument('--force', action='store_true')
    pb = sub.add_parser('probe')
    pb.add_argument('src')
    pb.add_argument('--strip')
    pr = sub.add_parser('prep')
    pr.add_argument('src')
    pr.add_argument('out')
    pr.add_argument('--start', type=float, default=0.0)
    pr.add_argument('--crop-x', type=float)
    pr.add_argument('--seconds', type=float, help=f'clip length, 2..{SWAP_MAX_SECONDS} s (default 4.46 s = 107 frames)')
    pr.add_argument('--keep-frame', action='store_true', help='no 3:4 crop (keep-background jobs)')
    pr.add_argument('--long', action='store_true',
                    help=f'whole dance ({SWAP_MAX_SECONDS}..{LONG_MAX_SECONDS} s): cut into shots of up to {LONG_SHOT_FRAMES} frames at run time')
    jk = sub.add_parser('job-pick', help='remake the showcase cut of a dance-swap job with one take')
    jk.add_argument('jobdir')
    jk.add_argument('--seed', required=True, type=int)
    cp = sub.add_parser('composite')
    cp.add_argument('source')
    cp.add_argument('mannequin')
    cp.add_argument('character')
    cp.add_argument('out')
    cp.add_argument('--loops', type=int, help='times the clip plays (default: 2, or 1 for clips over 8 s)')
    m = sub.add_parser('mannequin')
    m.add_argument('glb')
    m.add_argument('out')
    m.add_argument('--head', choices=['face', 'egg'], default='face')
    m.add_argument('--shot', choices=['close', 'full'], default='close')
    m.add_argument('--remote', action='store_true',
                   help='render on the instance (needs Blender from setup.sh); OUT is a file name in ComfyUI input/')
    dn = sub.add_parser('drama-new', help='create a drama project folder (also visible in the Piying page)')
    dn.add_argument('--title', help='required unless --template gives one')
    dn.add_argument('--template', help='start from a practice project (drama-templates): its script, nothing else')
    dn.add_argument('--idea', default='')
    dn.add_argument('--quality', choices=list(DRAMA_SIZES), default='576p')
    dn.add_argument('--seeds', type=int, default=2, choices=range(1, len(DRAMA_SEEDS) + 1))
    ds = sub.add_parser('drama-set', help='validate story JSON and save it into the project')
    ds.add_argument('jobdir')
    ds.add_argument('story')
    ds.add_argument('--why', help="the user's words; kept in the change log of every segment this changes")
    dst = sub.add_parser('drama-status')
    dst.add_argument('jobdir')
    dr = sub.add_parser('drama-review', help="record the user's verdict: assets <file> | keyframes <shot> | pick <shot> <seed> (pass and pick: from the Piying page only)")
    dr.add_argument('jobdir')
    dr.add_argument('kind', choices=['assets', 'keyframes', 'pick'])
    dr.add_argument('key')
    dr.add_argument('value', help='pass | reject | pending; for pick a seed, or none to take the pick back')
    dr.add_argument('--note')
    drc = sub.add_parser('drama-recommend', help="suggest a take with a reason (writes recommend.json; the user still picks)")
    drc.add_argument('jobdir')
    drc.add_argument('unit', help='segment: its first shot id (2-1) or its letter (B)')
    drc.add_argument('take', nargs='?', help='take number 1..5')
    drc.add_argument('--why', help='one line the user reads next to the take')
    drc.add_argument('--clear', action='store_true', help='withdraw the suggestion')
    db = sub.add_parser('drama-budget', help='what the project has spent on GPU runs and its cap (read-only)')
    db.add_argument('jobdir')
    de = sub.add_parser('drama-estimate')
    de.add_argument('jobdir')
    de.add_argument('stage', choices=DRAMA_STAGES)
    de.add_argument('--shots', nargs='+')
    de.add_argument('--price', type=float)
    de.add_argument('--running', action='store_true')
    de.add_argument('--seeds', type=int, choices=range(1, len(DRAMA_SEEDS) + 1), help='takes per shot (default: the project setting)')
    de.add_argument('--assume-missing', nargs='+', help='price the run as if these pictures were gone (a pending page change)')
    de.add_argument('--pending-ok', action='store_true', help='price only: do not refuse while pictures wait for approval; '
                    '`blocked` lists them (drama-run still refuses)')
    dad = sub.add_parser('drama-adopt', help='bring a cast member / scene / prop in from another project (--from-job DIR --id X), '
                         'or a library / uploaded picture as a scene or prop (--image F --name N --look L --origin JSON); arrives unreviewed')
    dad.add_argument('jobdir')
    dad.add_argument('--kind', required=True, choices=list(ADOPT_KINDS))
    dad.add_argument('--from-job')
    dad.add_argument('--id')
    dad.add_argument('--image')
    dad.add_argument('--name')
    dad.add_argument('--name-en')
    dad.add_argument('--look')
    dad.add_argument('--origin')
    dad.add_argument('--cache', help='where the converted PNG is kept (reused next time)')
    dw = sub.add_parser('drama-wardrobe', help='put other clothes on a character: portrait redrawn and reviewed again, its segments marked changed, nothing re-shot')
    dw.add_argument('jobdir')
    dw.add_argument('cast')
    dw.add_argument('--outfit', required=True)
    dw.add_argument('--note')
    dw.add_argument('--source', help='text | library:C123 | upload:<id>')
    dw.add_argument('--picture', action='store_true', help='take the clothes from the PICTURE behind --source (user only): portrait and '
                    'full body are redrawn from the current approved portrait + that picture')
    dw.add_argument('--image', help='with --picture --source library:<id>: the picture file inside the Piying library')
    dw.add_argument('--dry-run', action='store_true')
    dsd = sub.add_parser('drama-study', help='对标片: read a reference video locally (cuts, contact sheets, tone); no GPU, nothing uploaded')
    dsd.add_argument('jobdir')
    dsd.add_argument('--video', help='the video file (copied into the project as study_src.*); without it the one already there is read again')
    dsd.add_argument('--name', help='the name to show for it')
    dsd.add_argument('--threshold', type=float, default=STUDY_THRESHOLD, help='cut detection (ffmpeg scdet score); lower finds more cuts')
    dsl = sub.add_parser('drama-study-look', help='对标片: the image model picks framing / move / light / people / role per shot (fixed choices)')
    dsl.add_argument('jobdir')
    dsl.add_argument('--timeout', type=int, default=120)
    dsl.add_argument('--force', action='store_true', help='also redo shots the user set by hand')
    dss = sub.add_parser('drama-study-set', help='对标片: set one shot by hand (fixed choices; an empty value clears it)')
    dss.add_argument('jobdir')
    dss.add_argument('n', type=int)
    for k, opts in STUDY_LOOK.items():
        dss.add_argument('--' + k, help=' | '.join(opts))
    dsc = sub.add_parser('drama-study-cut', help='对标片: fix the cuts (--merge N, or --split N --at seconds)')
    dsc.add_argument('jobdir')
    dsc.add_argument('--merge', type=int)
    dsc.add_argument('--split', type=int)
    dsc.add_argument('--at', type=float)
    dsp = sub.add_parser('drama-study-plan', help="对标片: its summary and our segments to write (or --from N --to M: one stretch, for one segment)")
    dsp.add_argument('jobdir')
    dsp.add_argument('--from', dest='from_', type=int)
    dsp.add_argument('--to', type=int)
    dsp.add_argument('--wait', type=int, nargs='?', const=300, default=0,
                     help='while the image model is still looking, wait up to this many seconds (default 300); then use what is there')
    dse = sub.add_parser('drama-study-estimate', help='对标片: rough price of shooting its whole rhythm (before the script) against the cap')
    dse.add_argument('jobdir')
    dse.add_argument('--price', type=float, help='yuan per hour (otherwise the instance price)')
    dsk = sub.add_parser('drama-study-link', help='对标片: note which reference shots a segment follows (story.json segments[].study; nothing re-made)')
    dsk.add_argument('jobdir')
    dsk.add_argument('unit', help='segment: first shot id or letter')
    dsk.add_argument('range', help='10-12, 4, or none')
    dref = sub.add_parser('drama-ref', help="put a project asset into a segment's first frame (--add cast:x|prop:x|scene:x, a scene replaces the segment's) or swap one (--swap prop:a=prop:b)")
    dref.add_argument('jobdir')
    dref.add_argument('unit')
    dref.add_argument('--add')
    dref.add_argument('--swap')
    dref.add_argument('--why')
    dref.add_argument('--dry-run', action='store_true')
    dcf = sub.add_parser('drama-cast-flag', help='mark a character as looking young (Piying page only)')
    dcf.add_argument('jobdir')
    dcf.add_argument('cast')
    dcf.add_argument('--look-young', required=True, choices=['on', 'off'])
    drr = sub.add_parser('drama-run', help='make one stage on the GPU (resumable; only what is missing or rejected)')
    drr.add_argument('jobdir')
    drr.add_argument('stage', choices=DRAMA_STAGES)
    drr.add_argument('--shots', nargs='+')
    drr.add_argument('--confirm-billing', action='store_true')
    drr.add_argument('--stop-after', action='store_true', help='stop the machine as soon as the run is done (no idle minutes)')
    drr.add_argument('--queue-minutes', type=int, choices=range(1, 241), metavar='MIN',
                     help='every machine busy: wait up to MIN minutes for one to come free (takes nothing while waiting)')
    drr.add_argument('--max-cost', type=float, help='a queued run starts only if it costs at most this (the confirmed quote)')
    drr.add_argument('--seeds', type=int, choices=range(1, len(DRAMA_SEEDS) + 1), help='takes per shot (default: the project setting)')
    dp = sub.add_parser('drama-precheck', help='ask the image model about keyframes / takes (advice only; writes precheck.json)')
    dp.add_argument('jobdir')
    dp.add_argument('--kind', nargs='+', choices=['keyframes', 'takes'], default=['keyframes', 'takes'])
    dp.add_argument('--timeout', type=int, default=420)
    dp.add_argument('--force', action='store_true')
    vt = sub.add_parser('vision-test', help='check that the configured image model really reads images (fixed test picture)')
    vt.add_argument('--timeout', type=int, default=300)
    dfr = sub.add_parser('drama-frames', help='frames to look at: --unit U [--at s,s] | --final --at s | --seam U1 U2; --ask "question" asks the image model')
    dfr.add_argument('jobdir')
    dfr.add_argument('--unit')
    dfr.add_argument('--seed', type=int)
    dfr.add_argument('--final', action='store_true')
    dfr.add_argument('--seam', nargs=2)
    dfr.add_argument('--at')
    dfr.add_argument('--ask')
    dfr.add_argument('--width', type=int, default=360)
    dfr.add_argument('--timeout', type=int, default=300)
    dfr.add_argument('--no-think', action='store_true', help='ask the image model to answer without reasoning first (a plain description: seconds instead of a minute)')
    dpt = sub.add_parser('drama-point', help='a spot the user pointed at: --kf U | --unit U --seed S --at s | --final --at s, with --x --y (and --w --h for a box), fractions 0..1; writes point/pt_*_crop.jpg and _full.jpg (context for the director only)')
    dpt.add_argument('jobdir')
    g = dpt.add_mutually_exclusive_group(required=True)
    g.add_argument('--kf')
    g.add_argument('--unit')
    g.add_argument('--final', action='store_true')
    dpt.add_argument('--seed', type=int)
    dpt.add_argument('--at', type=float)
    dpt.add_argument('--x', type=float, required=True)
    dpt.add_argument('--y', type=float, required=True)
    dpt.add_argument('--w', type=float)
    dpt.add_argument('--h', type=float)
    dpt.add_argument('--nonce', help=argparse.SUPPRESS)
    dtr = sub.add_parser('drama-trial', help='trial pictures for an unvalidated idea, into trial/ only (never approved, never a '
                         'generation input). The machine must already be running: this command never starts it. '
                         'crop --unit U --box x,y,w,h [--cast C] | eyeline --unit U [--who A --at B] | redraw --cast C --ref PICTURE')
    dtr.add_argument('jobdir')
    dtr.add_argument('kind', choices=TRIAL_KINDS)
    dtr.add_argument('--unit')
    dtr.add_argument('--box', help='crop: x,y,w,h as fractions 0..1 of the frame')
    dtr.add_argument('--from', dest='source', help='crop: a PNG directly in the project folder instead of the approved first frame')
    dtr.add_argument('--cast', help='crop: whose close-up it is; redraw: the character')
    dtr.add_argument('--who', help='eyeline: the one who looks (default: first character of the segment)')
    dtr.add_argument('--at', help='eyeline: the one looked at (default: the other character)')
    dtr.add_argument('--note', help='eyeline: what is going on, one sentence (who stands, who sits, what they hold)')
    dtr.add_argument('--ref', help='redraw: a picture of the clothes (PNG / JPEG / WebP)')
    dtr.add_argument('--seeds', type=int, choices=range(1, len(DRAMA_SEEDS) + 1), default=2, help='pictures per writing (default 2)')
    dtr.add_argument('--dry-run', action='store_true', help='print the workflows and inputs; contacts nothing, writes nothing')
    ded = sub.add_parser('drama-edit', help='change one shot; the old files of its segment are kept as a version')
    ded.add_argument('jobdir')
    ded.add_argument('shot')
    ded.add_argument('--keyframe')
    ded.add_argument('--action')
    ded.add_argument('--seconds', type=float)
    ded.add_argument('--line-text')
    ded.add_argument('--line-kind', choices=list(LINE_KINDS))
    ded.add_argument('--line-who')
    ded.add_argument('--line-delivery')
    ded.add_argument('--no-line', action='store_true')
    ded.add_argument('--dry-run', action='store_true', help='print the effects; writes nothing')
    ded.add_argument('--staging', choices=list(STAGINGS) + ['none'], help='first-frame staging for a first shot with two people: '
                     'profile = both in strict profile, facing each other (none removes it)')
    ded.add_argument('--why', help="the user's words this change answers")
    dcr = sub.add_parser('drama-crop', help="裁特写 (user only): redraw a segment's first frame from a cut of its own approved first "
                         'frame. Changes the project only; the next `drama-run keyframes` draws it')
    dcr.add_argument('jobdir')
    dcr.add_argument('unit', help='segment: its first shot id (2-1) or its letter (B)')
    dcr.add_argument('--box', help='x,y,w,h as fractions 0..1 of the picture named by drama-status crop_from')
    dcr.add_argument('--cast', help='whose close-up it is (needed when the first frame shows two people)')
    dcr.add_argument('--note', help='the user\'s words, kept with the change')
    dcr.add_argument('--clear', action='store_true', help='remove the crop: the first frame is drawn from the script again')
    dcr.add_argument('--price', type=float, help='yuan per hour, to put a cost on next_run (otherwise minutes only)')
    dcr.add_argument('--dry-run', action='store_true', help='print the effects; writes nothing')
    dv = sub.add_parser('drama-version', help='restore an older version of a segment (the current one is kept too)')
    dv.add_argument('jobdir')
    dv.add_argument('unit')
    dv.add_argument('--restore', type=int, required=True)
    gq = sub.add_parser('gpu-qualify', help='run the fixed GPU test suite on the machine (must already be running) and '
                        'suggest a GPU_PROFILES entry; without --confirm-billing prints the plan only and contacts nothing')
    gq.add_argument('--gpu', help='GPU type for the plan (default: the machine\'s, read when the suite runs)')
    gq.add_argument('--price', type=float, help='yuan per hour for the plan (default: the machine\'s)')
    gq.add_argument('--swap-job', help='also run the heaviest recast with this finished job (its mannequin.mp4 and references)')
    gq.add_argument('--budget', type=float, default=QUALIFY_BUDGET, help=f'refuse when the plan costs more (default ¥{QUALIFY_BUDGET:g})')
    gq.add_argument('--confirm-billing', action='store_true')
    sub.add_parser('drama-templates', help='the practice projects: finished scripts drama-new --template copies')
    srn = sub.add_parser('series-new', help='a series: drama projects (episodes) that share characters approved once')
    srn.add_argument('--title', required=True)
    srn.add_argument('--logline')
    sub.add_parser('series-list', help='every series with its episodes and regulars')
    srs = sub.add_parser('series-show', help='one series, with the recap the director reads (--before JOB: up to that episode)')
    srs.add_argument('series')
    srs.add_argument('--before')
    sre = sub.add_parser('series-set', help='title, one-line story, and the rules (names, what never changes)')
    sre.add_argument('series')
    sre.add_argument('--title')
    sre.add_argument('--logline')
    sre.add_argument('--rules')
    sra = sub.add_parser('series-add', help='make a drama project the next episode')
    sra.add_argument('series')
    sra.add_argument('jobdir')
    srr = sub.add_parser('series-remove', help='take a project out of the series (it keeps everything)')
    srr.add_argument('series')
    srr.add_argument('jobdir')
    sro = sub.add_parser('series-order', help='the user\'s episode order: --order JOB,JOB,…')
    sro.add_argument('series')
    sro.add_argument('--order', required=True)
    src = sub.add_parser('series-counts', help='whether an episode is part of the story the recap tells')
    src.add_argument('series')
    src.add_argument('job')
    src.add_argument('value', choices=['yes', 'no'])
    srm = sub.add_parser('series-summary', help='an episode\'s one-line summary; the director writes drafts, the user fixes them')
    srm.add_argument('series')
    srm.add_argument('job')
    srm.add_argument('--text', default='')
    srm.add_argument('--state', choices=['draft', 'set'], default='draft')
    src2 = sub.add_parser('series-cast-add', help='make a character of an episode (both pictures approved) a regular')
    src2.add_argument('series')
    src2.add_argument('jobdir')
    src2.add_argument('cast')
    scu = sub.add_parser('series-cast-update', help='换定妆照: a regular takes the pictures the user approved for it in an episode')
    scu.add_argument('series')
    scu.add_argument('cast')
    scu.add_argument('--job', dest='jobdir', required=True)
    scn = sub.add_parser('series-cast-new', help='add a regular from a description (and optionally a photo); pictures come from the next episode')
    scn.add_argument('series')
    scn.add_argument('--name', required=True)
    scn.add_argument('--name-en', required=True)
    scn.add_argument('--age', required=True)
    scn.add_argument('--look', required=True)
    scn.add_argument('--outfit')
    scn.add_argument('--voice')
    scn.add_argument('--look-young', action='store_true')
    scn.add_argument('--photo', help='the photo the page saved in the series folder (.photo_upload.<ext>)')
    srt = sub.add_parser('series-cast-retire', help='a regular leaves the series (--back: returns); refused while still in an unfinished episode')
    srt.add_argument('series')
    srt.add_argument('cast')
    srt.add_argument('--back', action='store_true')
    svo = sub.add_parser('series-voice', help='定声 for a regular: one of its voice tracks (--job DIR --file vo_x.wav) or --clear')
    svo.add_argument('series')
    svo.add_argument('cast')
    svo.add_argument('--job', dest='jobdir')
    svo.add_argument('--file')
    svo.add_argument('--shot', help='with a take: the shot whose line to cut out')
    svo.add_argument('--clear', action='store_true')
    svo.add_argument('--upload', help='a recording the page saved in the series folder (.voice_upload.<ext>)')
    dvo = sub.add_parser('drama-voice', help='定声 in one project: the character\'s own voice track (--file vo_x.wav) or --clear')
    dvo.add_argument('jobdir')
    dvo.add_argument('cast')
    dvo.add_argument('--file')
    dvo.add_argument('--shot', help='with a take: the shot whose line to cut out')
    dvo.add_argument('--clear', action='store_true')
    dvo.add_argument('--upload', help='a recording the page saved in the project folder (.voice_upload.<ext>)')
    sar = sub.add_parser('series-archive', help='archive a series and its episodes (--restore: back); nothing is deleted')
    sar.add_argument('series')
    sar.add_argument('--restore', action='store_true')
    srw = sub.add_parser('series-episode-new', help='a new episode with the regulars (--cast a,b; default all) and their approved pictures')
    srw.add_argument('series')
    srw.add_argument('--title')
    srw.add_argument('--idea', default='')
    srw.add_argument('--cast')
    srw.add_argument('--quality', choices=list(DRAMA_SIZES), default='576p')
    srw.add_argument('--seeds', type=int, default=2, choices=range(1, len(DRAMA_SEEDS) + 1))
    mp = sub.add_parser('machine-plan', help='read-only: stock and price of a new machine in every zone (dry-run create)')
    mp.add_argument('--gpu')
    mp.add_argument('--zone')
    mp.add_argument('--base', action='store_true', help='quote the official base image even when a Piying image exists')
    mc = sub.add_parser('machine-create', help='create ONE machine from the base image, set it up with setup.sh and stop '
                        'it (never deletes). Without --confirm-billing: the plan only. A re-run resumes the last one')
    mc.add_argument('--gpu')
    mc.add_argument('--zone')
    mc.add_argument('--new', action='store_true', help='make another machine instead of resuming the last setup')
    mc.add_argument('--base', action='store_true', help='create from the official base image even when a Piying image exists')
    mc.add_argument('--instance', help='rerun the setup on this registered machine (stopped) instead of creating one: '
                    'the 机器 page\'s 更新环境 after the plugin moved to a newer environment')
    mc.add_argument('--confirm-billing', action='store_true')
    sub.add_parser('ssh-keygen', help="make the plugin's own ssh key pair (~/.dsh/shisei/ssh) when this computer has "
                   'none; never touches ~/.ssh or replaces a key')
    dk = sub.add_parser('drama-check', help='transcribe takes locally (whisper.cpp) and compare with the script lines')
    dk.add_argument('jobdir')
    dtr = sub.add_parser('drama-trim', help="the user's edit of a picked take, applied at the cut: cut / hold / swap a piece "
                         '(from the Piying page only)')
    dtr.add_argument('jobdir')
    dtr.add_argument('segment', help='segment label (A) or its first shot id')
    dtr.add_argument('--at', type=_seconds_range, help='the piece, in seconds of the take: 0-0.58')
    dtr.add_argument('--mode', choices=EDIT_MODES, default='cut')
    dtr.add_argument('--take', help='swap: the take number the piece comes from')
    dtr.add_argument('--with-line', action='store_true', help='cut even though a spoken line is in the piece')
    dtr.add_argument('--undo', type=_seconds_range, help='take back the edit of that piece')
    dtr.add_argument('--clear', action='store_true', help='take back every edit of this segment')
    dsh_ = sub.add_parser('drama-shots', help='check every take shot by shot against the script: cuts found locally, '
                          'one frame per shot to the image model (writes shots.json only)')
    dsh_.add_argument('jobdir')
    dsh_.add_argument('--no-look', action='store_true', help='only find the cuts; do not ask the image model')
    dsh_.add_argument('--force', action='store_true')
    dsh_.add_argument('--timeout', type=int, default=120)
    dc = sub.add_parser('drama-cut', help='assemble the picked takes into final.mp4 locally (no GPU)')
    dc.add_argument('jobdir')
    dc.add_argument('--no-sound', dest='sound', action='store_false', help='plain concatenation: no levelling, no room-tone bed')
    dc.add_argument('--label', action='store_true',
                    help='burn the AI label into the picture (default: clean picture; the file metadata still says AI-generated)')
    args = p.parse_args(argv)

    if args.cmd == 'submit' and args.kind == 'swap' and not (args.ref and args.ref2):
        p.error('swap needs --ref and --ref2')
    if args.cmd == 'config' and args.action == 'set' and (args.key is None or args.value is None):
        p.error('config set needs KEY VALUE')
    handler = globals()['cmd_' + args.cmd.replace('-', '_')]
    global HOLDER, KEEPER, CLAIMED, CMD, STOP_CHECK, LEASE_REFUSED, UP_STARTED
    HOLDER, KEEPER, CLAIMED, CMD, STOP_CHECK, LEASE_REFUSED, UP_STARTED = None, None, None, args.cmd, None, False, False
    ensure_ffmpeg_path()   # ffmpeg where an install left it, even when it is not on this process's PATH
    try:
        if getattr(args, 'confirm_billing', False) and in_agent_shell():
            raise ShiseiError(BILLING_IS_THE_USERS)
        if getattr(args, 'confirm_billing', False):
            keep_awake()   # a billed run: the computer sleeping would cut the connection while the machine bills
        cfg = load_config()
        if local_mode_wanted(cfg):   # the user's own ComfyUI instead of a compshare machine (local_mode.py)
            import importlib.util
            spec = importlib.util.spec_from_file_location('shisei_local_mode', HERE / 'local_mode.py')
            lm = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(lm)
            lm.install(globals(), cfg)
            handler = globals()['cmd_' + args.cmd.replace('-', '_')]   # the local version of the command
        # a qualify dry run is a plan from numbers alone: it takes no machine and contacts nothing
        dry_qualify = args.cmd == 'gpu-qualify' and not args.confirm_billing
        if (args.cmd in LEASE_CMDS or args.cmd in ATTACH_CMDS) and not dry_qualify:
            HOLDER = holder_for(args)
            explicit = args.machine or os.environ.get('SHISEI_MACHINE') or None
            if explicit and explicit not in machine_ids(cfg):
                raise ShiseiError(f'{explicit} is not a registered machine (instances --register {explicit})')
            if args.cmd in LEASE_CMDS:
                if archived_at(getattr(args, 'jobdir', None)):
                    raise ShiseiError(ARCHIVED_SAY)
                # before the handler writes any progress: a second run of the same project would overwrite the first
                # one's, and its failure path would arm the idle stop on the machine the first one is using
                other = own_run_elsewhere(HOLDER, machine_ids(cfg))
                if other:
                    raise MachineBusy(other_run_message(*other))
                try:
                    pick_machine(cfg, HOLDER, explicit)   # the lease itself is taken right before the machine is used
                except MachineBusy:
                    if not getattr(args, 'queue_minutes', None):
                        raise
                    use_machine(cfg, explicit or machine_ids(cfg)[0], save=False)   # the run queues (wait_for_machine)
            elif args.cmd in QUOTE_CMDS:
                quote_machine(cfg, HOLDER, explicit)   # the machine the run would get, so the price is that one's
            else:
                attach_machine(cfg, HOLDER, explicit)
                if args.cmd == 'wait':
                    KEEPER = LeaseKeeper(cfg)
        out = handler(args, cfg)
        print(json.dumps({'ok': True, **out}, ensure_ascii=False, indent=1))
        return 0
    except (ShiseiError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        kind = getattr(e, 'kind', None)
        print(json.dumps({'ok': False, 'error': str(e), **({'error_kind': kind} if kind else {})}, ensure_ascii=False))
        return 1
    except Exception as e:   # a bug or an OS error: still one JSON line the page can show, the traceback on stderr
        traceback.print_exc()
        print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}, ensure_ascii=False))
        return 1
    finally:
        if KEEPER:
            KEEPER.stop()
        if CLAIMED:
            lease = lease_read(CLAIMED)
            if lease and lease.get('holder') == HOLDER and lease.get('state') == 'active':
                if not lease.get('started'):     # never started (refused, failed to boot…): let it go
                    lease_drop(CLAIMED, HOLDER)
                elif args.cmd != 'up':           # a run is over, whatever way it ended: free for others until it stops
                    lease_release(CLAIMED, HOLDER, int(load_config().get('idle_stop_minutes') or 10))


# ---------- GPU types: which cards Piying is known to run on, and how a new one gets on the list ----------
# Only a type that passed the whole gpu-qualify suite on that GpuType is listed; nothing else is ever offered to a user
# as a replacement. Only type names, numbers and dates go in here; raw results (instance, zone, timings) stay in
# ~/.dsh/shisei/qualify. Candidates seen in the read-only compshare listing (2026-11-11, not verified):
# RTX6000D 84 GB ¥8.48/h, A800 80 GB ¥6.99/h, H20 96 GB ¥7.12/h, A100 80 GB ¥10.21/h, 5090 32 GB ¥3.83/h.
GPU_PROFILES = {
    # every stage run end to end on this card (2026-11-09 .. 11-11); the reference for `speed`
    '4090_48G': {'status': 'verified', 'vram_gb': 48, 'speed': 1.0, 'tested': '2026-11-11'},
}


def gpu_profile(gpu):
    """What Piying knows about a GpuType: its GPU_PROFILES entry, or status 'unverified'."""
    p = GPU_PROFILES.get(str(gpu or ''))
    return {'gpu': gpu, **p} if p else {'gpu': gpu, 'status': 'unverified'}


# ---- VRAM: sampled on the instance while work runs. A peak on a 48 GB card is NOT what the work needs (ComfyUI keeps
# models resident while there is room); it only says the work fit. Below 32 GB is a hint for 32 GB cards, no more.
VRAM_LOG = f'{REMOTE_WORK}/vram.log'
VRAM_EVERY = 2   # seconds between samples


def remote_vram_start_script(every=VRAM_EVERY):
    """Shell for the instance: (re)start one background sampler appending `epoch,name,used MiB,total MiB` lines."""
    pid = f'{REMOTE_WORK}/vram.pid'
    loop = (f'while :; do printf "%s," "$(date +%s)"; nvidia-smi --query-gpu=name,memory.used,memory.total '
            f'--format=csv,noheader,nounits | head -1; sleep {int(every)}; done')
    return (f'mkdir -p {REMOTE_WORK}; [ -f {pid} ] && kill "$(cat {pid})" 2>/dev/null; '
            f"nohup sh -c '{loop}' >> {VRAM_LOG} 2>/dev/null < /dev/null & echo $! > {pid}; date +%s")


def parse_vram_log(text):
    """[(epoch, name, used MiB, total MiB)] from the sampler's lines; malformed lines are skipped."""
    out = []
    for line in (text or '').splitlines():
        parts = [x.strip() for x in line.split(',')]
        if len(parts) != 4:
            continue
        try:
            out.append((int(parts[0]), parts[1], int(float(parts[2])), int(float(parts[3]))))
        except ValueError:
            continue
    return out


def vram_peak(samples, since=None, until=None):
    """Highest use between two instance times (inclusive), or None when no sample falls in the window."""
    win = [s for s in samples if (since is None or s[0] >= since) and (until is None or s[0] <= until)]
    if not win:
        return None
    top = max(win, key=lambda s: s[2])
    return {'gpu_name': top[1], 'peak_mib': top[2], 'total_mib': top[3], 'peak_gb': round(top[2] / 1024, 1),
            'samples': len(win), 'seconds': win[-1][0] - win[0][0]}


def remote_now(cfg, info):
    return int(ssh_run(cfg, info, 'date +%s', timeout=60).strip().splitlines()[-1])


def vram_watch_start(cfg, info):
    """Start the sampler; returns the instance time it started, or None. Never fails the run that calls it."""
    try:
        return int(ssh_run(cfg, info, remote_vram_start_script(), timeout=60).strip().splitlines()[-1])
    except (ShiseiError, subprocess.SubprocessError, ValueError, IndexError) as e:
        log(f'VRAM sampler not started: {e}')
        return None


def vram_read(cfg, info):
    return parse_vram_log(ssh_run(cfg, info, f'cat {VRAM_LOG} 2>/dev/null; true', timeout=60))


def vram_watch_collect(cfg, info, since, label, extra=None):
    """Peak since `since`, appended to ~/.dsh/shisei/gpu_runs.jsonl with the GpuType. Never fails the run."""
    if since is None:
        return None
    try:
        peak = vram_peak(vram_read(cfg, info), since)
        if not peak:
            return None
        row = {'at': time.time(), 'gpu': info.get('GpuType'), 'label': label, **peak, **(extra or {})}
        HOME.mkdir(parents=True, exist_ok=True)
        with open(HOME / 'gpu_runs.jsonl', 'a') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        return row
    except (ShiseiError, subprocess.SubprocessError, OSError) as e:
        log(f'VRAM peak not recorded: {e}')
        return None


# ---- the suite. Scenes only, no people: it needs no character picture and no real clip (the optional swap step uses a
# finished job of the user's own). The heaviest drama load is a 768p take of MAX_UNIT_SECONDS.
QUALIFY_DIR = HOME / 'qualify'
QUALIFY_BUDGET = 8.0
QUALIFY_SEED = 4242
QUALIFY_SCENE = ('Photorealistic cinematic film still, vertical 9:16 frame. An empty rainy city street at night, neon shop '
                 'signs reflected in puddles, parked bicycles along the kerb, no people. Natural colours, sharp focus.')
QUALIFY_MOTION = ('The camera slowly pushes forward along the empty rainy street at night. Rain falls, neon signs flicker, '
                  'puddles ripple. No people appear. Ambient sound: steady rain and distant traffic.')
QUALIFY_MINUTES = {'secure': 1.0, 'probe': 0.5, 'blender': 1.0, 'load': DRAMA_MINUTES['load'] * 2,
                   'image': DRAMA_MINUTES['image'], 'take': DRAMA_MINUTES['take_768p'] * MAX_UNIT_SECONDS / 10, 'pull': 0.5}

# The probe runs in ComfyUI's own interpreter: argv[0] of the running main.py, looked up on that process's PATH (a venv
# python resolved through /proc/<pid>/exe would lose the venv). '[m]ain.py' keeps pgrep from matching this shell.
REMOTE_PROBE = r'''
PID=$(pgrep -f '[m]ain.py' | head -1)
C=""
if [ -n "$PID" ]; then
  A0=$(tr '\0' '\n' < /proc/$PID/cmdline | head -1)
  P=$(tr '\0' '\n' < /proc/$PID/environ 2>/dev/null | sed -n 's/^PATH=//p')
  C="$A0 $(PATH="$P:$PATH" command -v "$A0" 2>/dev/null)"
fi
PY=python3
for X in $C python3; do
  case "$(basename "$X")" in python*) ;; *) continue ;; esac
  command -v "$X" >/dev/null 2>&1 || continue
  PY=$X
  "$X" -c 'import torch' 2>/dev/null && break
done
"$PY" - <<'EOF'
import json, subprocess
d = {}
try:
    import torch
    d.update(torch=torch.__version__, cuda=torch.version.cuda, available=torch.cuda.is_available())
    if d['available']:
        p = torch.cuda.get_device_properties(0)
        d.update(name=p.name, vram_mib=p.total_memory // 2 ** 20, capability=list(torch.cuda.get_device_capability(0)),
                 arch_list=torch.cuda.get_arch_list(), bf16=bool(torch.cuda.is_bf16_supported()),
                 fp8_dtype=hasattr(torch, 'float8_e4m3fn'))
        try:
            x = torch.ones(64, 64, device='cuda', dtype=torch.bfloat16)
            d['bf16_matmul'] = float((x @ x).sum().item()) == 64.0 ** 3
        except Exception as e:
            d['bf16_matmul'] = str(e)[:200]
except Exception as e:
    d['error'] = str(e)[:300]
r = subprocess.run(['nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader'], capture_output=True, text=True)
d['driver'] = r.stdout.strip()
print('PROBE ' + json.dumps(d))
EOF
'''

REMOTE_EEVEE = (f'mkdir -p {REMOTE_WORK}; rm -f {REMOTE_WORK}/qualify_eevee.png; '
                "blender -b --factory-startup --python-expr \""
                "import bpy; s = bpy.context.scene; s.render.engine = 'BLENDER_EEVEE'; "
                "s.render.resolution_x = 320; s.render.resolution_y = 240; "
                f"s.render.filepath = '{REMOTE_WORK}/qualify_eevee.png'; bpy.ops.render.render(write_still=True)\" "
                f"> {REMOTE_WORK}/qualify_eevee.log 2>&1; ls -l {REMOTE_WORK}/qualify_eevee.png 2>/dev/null | wc -l; "
                f"tail -3 {REMOTE_WORK}/qualify_eevee.log")


def parse_probe(text):
    for line in (text or '').splitlines():
        if line.startswith('PROBE '):
            return json.loads(line[6:])
    raise ShiseiError('the GPU probe printed nothing: ' + (text or '').strip()[-300:])


def probe_problems(p):
    """(fatal problems, notes) about a card and its PyTorch, from the probe. Fatal ones stop the suite before any model
    loads; notes go into the result (speed questions are answered by the timed steps, not here)."""
    fatal, notes = [], []
    if p.get('error') or not p.get('available'):
        return [f'PyTorch sees no CUDA GPU ({p.get("error") or "cuda not available"})'], notes
    cc = tuple(p.get('capability') or (0, 0))
    if cc < (8, 0):
        fatal.append(f'compute capability {cc[0]}.{cc[1]}: no bf16 (needs 8.0+)')
    sm = f'sm_{cc[0]}{cc[1]}'
    arch = p.get('arch_list') or []
    # an sm_XY build also runs on X.Z with Z >= Y (same major: the 4090's 8.9 runs sm_86 code; PyTorch 2.10+cu130 ships
    # sm_80 / sm_86 and no sm_89, and its bf16 matmul works there, 2026-11-12); PTX (compute_NN) JITs forward
    def runs(a):
        if a.startswith('sm_') and a[3:].isdigit():
            n = int(a[3:])
            return n // 10 == cc[0] and n % 10 <= cc[1]
        return a.startswith('compute_') and a[8:].isdigit() and int(a[8:]) <= cc[0] * 10 + cc[1]
    if arch and sm not in arch and not any(runs(a) for a in arch):
        fatal.append(f'this PyTorch ({p.get("torch")}) has no kernels for {sm}')
    if p.get('bf16_matmul') is not True:
        fatal.append(f'a bf16 matmul failed: {p.get("bf16_matmul")}')
    if cc < (8, 9):
        notes.append('no fp8 compute on this architecture: fp8 weights are cast up; the timed take shows what that costs')
    if (p.get('vram_mib') or 0) < 47 * 1024:
        notes.append(f'{(p.get("vram_mib") or 0) / 1024:.0f} GB: under the 48 GB Piying was built on; the take step decides')
    return fatal, notes


def qualify_plan(gpu, price, swap_frames=None):
    """Steps, minutes and cost of the suite on a running machine (no boot). Numbers come from the 4090 timings."""
    steps = [('secure', QUALIFY_MINUTES['secure']), ('probe', QUALIFY_MINUTES['probe']), ('blender', QUALIFY_MINUTES['blender']),
             ('image', QUALIFY_MINUTES['load'] / 2 + QUALIFY_MINUTES['image']), ('take', QUALIFY_MINUTES['load'] / 2 + QUALIFY_MINUTES['take'])]
    if swap_frames:
        k = check_swap_frames(swap_frames) / H3_FRAMES
        steps.append(('swap', STAGE_MINUTES['swap_per_seed'] * k ** 1.4))
    steps.append(('pull', QUALIFY_MINUTES['pull']))
    minutes = int(-(-round(sum(m for _, m in steps), 3) // 1))
    price = float(price or 0)
    return {'gpu': gpu, 'profile': gpu_profile(gpu)['status'] if gpu else None, 'price_per_hour': price,
            'steps': [{'step': s, 'minutes': round(m, 1)} for s, m in steps], 'minutes': minutes,
            'cost': round(minutes / 60 * price, 2),
            'note': 'minutes are estimates from the 4090 48G drama timings; a slower card takes longer. The machine must '
                    'already be running and set up (setup.sh, or the Piying image): this command never starts, creates or '
                    'stops one, and the idle stop is armed at the end.'}


def baseline_take_minutes(results, ref='4090_48G'):
    """Take minutes of the latest passed suite on the reference card (the result.json dicts), or None. `speed` is only
    ever measured against this same suite on the 4090, never against estimates."""
    runs = [r for r in results if r.get('gpu') == ref and (r.get('verdict') or {}).get('passed')]
    if not runs:
        return None
    last = max(runs, key=lambda r: r.get('ended') or 0)
    return next((s['minutes'] for s in last.get('steps') or [] if s.get('step') == 'take' and s.get('ok')), None)


def qualify_verdict(gpu, probe, steps, baseline=None, today=None):
    """Whether the card passed, and the GPU_PROFILES entry to add by hand after reading the result. Blender is not part
    of the verdict (drama never uses it; the swap's mannequin can fall back to Cycles): it is reported as `eevee`."""
    core = [s for s in steps if s['step'] != 'blender']
    ok = bool(core) and all(s.get('ok') for s in core) and {'image', 'take'} <= {s['step'] for s in core}
    peaks = [s['vram']['peak_gb'] for s in steps if s.get('vram')]
    take = next((s for s in steps if s['step'] == 'take' and s.get('ok')), None)
    blender = next((s for s in steps if s['step'] == 'blender'), None)
    out = {'gpu': gpu, 'passed': ok, 'failed': [s['step'] for s in core if not s.get('ok')],
           'peak_gb': max(peaks) if peaks else None, 'take_minutes': take['minutes'] if take else None,
           'eevee': blender.get('eevee') if blender else None}
    if ok:
        speed = round(baseline / max(take['minutes'], 0.01), 2) if baseline and take else None
        out['profile'] = {gpu: {'status': 'verified', 'vram_gb': round((probe.get('vram_mib') or 0) / 1024), 'speed': speed,
                                'eevee': out['eevee'], 'tested': today or time.strftime('%Y-%m-%d')}}
        if speed is None:
            out['note'] = 'speed left empty: run this suite on a 4090_48G first, it is the only reference'
    return out


def video_luma_frames(path):
    """(frames, mean luma 0..255) of a local video: ffmpeg scales every frame to one grey pixel."""
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', 'scale=1:1,format=gray', '-f', 'rawvideo', '-'],
                       capture_output=True, timeout=300)
    if r.returncode != 0:
        raise ShiseiError(f'could not read {Path(path).name}: {r.stderr.decode(errors="replace")[-200:]}')
    px = r.stdout
    return len(px), (sum(px) / len(px) if px else 0.0)


def check_take(frames, luma, want_frames):
    """Problems with a pulled video (empty when it looks like a real take)."""
    bad = []
    if frames != want_frames:
        bad.append(f'{frames} frames, expected {want_frames}')
    if luma < 8:
        bad.append(f'nearly black (mean luma {luma:.1f})')
    return bad


def cmd_gpu_qualify(args, cfg):
    """The fixed suite on the running machine: probe, Blender EEVEE, a Qwen picture, a 768p H3 take of the longest
    segment, optionally the job's recast. Writes ~/.dsh/shisei/qualify/<gpu>-<time>.json and a ledger line."""
    swap_job = None
    if args.swap_job:
        sd = Path(args.swap_job).expanduser()
        sjob = json.loads((sd / 'job.json').read_text()) if (sd / 'job.json').exists() else None
        files = (sjob or {}).get('files') or {}
        if not sjob or not (sd / 'mannequin.mp4').exists() or not files.get('front') or not files.get('face'):
            raise ShiseiError('--swap-job is a finished dance-swap job folder (job.json, mannequin.mp4 and its two references)')
        swap_job = (sd, sjob, check_swap_frames((sjob.get('params') or {}).get('frames') or H3_FRAMES))
    if not args.confirm_billing:
        plan = qualify_plan(args.gpu, args.price, swap_job and swap_job[2])
        plan['over_budget'] = plan['cost'] > args.budget
        return {'dry_run': True, **plan, 'budget': args.budget}
    info = instance_info(cfg)
    if info.get('State') != 'Running':
        raise ShiseiError(f'the machine is not running (state: {info.get("State")}). gpu-qualify never starts it: '
                          'ask the user, start it with `up --confirm-billing`, then run this again')
    gpu, price = info.get('GpuType'), float(info.get('InstancePrice') or 0)
    plan = qualify_plan(gpu, price, swap_job and swap_job[2])
    if plan['cost'] > args.budget:
        raise ShiseiError(f'the suite is priced at ¥{plan["cost"]} ({plan["minutes"]} min at ¥{price}/h), over the '
                          f'¥{args.budget:g} budget: raise --budget after telling the user')
    stamp = time.strftime('%Y%m%d-%H%M%S')
    out_dir = QUALIFY_DIR / f'{gpu}-{stamp}'
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = f'q{stamp}'
    began, run, steps, probe, state = time.time(), TrialRun(), [], {}, 'failed'

    def step(name, fn):
        t0, w0 = time.time(), remote_now(cfg, info)
        rec = {'step': name, 'ok': False}
        try:
            rec.update(fn() or {})
            rec['ok'] = not rec.get('problems')
        except (ShiseiError, subprocess.SubprocessError) as e:
            rec['error'] = str(e)[:600]
        rec['minutes'] = round((time.time() - t0) / 60, 2)
        try:
            rec['vram'] = vram_peak(vram_read(cfg, info), w0, remote_now(cfg, info))
        except (ShiseiError, subprocess.SubprocessError):
            rec['vram'] = None
        steps.append(rec)
        log(f'{name}: {"ok" if rec["ok"] else "FAILED"} in {rec["minutes"]} min')
        return rec['ok']

    def generate(api, pattern, local):
        pid = post_prompt(api)
        schedule_stop(cfg, working_stop(cfg))
        _wait_prompt(run, cfg, pid, local.name, 45)
        outs = [n for n in _remote_ls(cfg, info, 'output', pattern) if 'audio' not in n]
        if not outs:
            raise ShiseiError(f'{local.name}: finished but no output found')
        _pull_one(cfg, info, f'output/{outs[-1]}', local)
        return outs[-1]

    try:
        sec = do_secure(cfg, info)
        if not sec['clean']:
            raise ShiseiError('security check found something on the instance; nothing was run')
        vram_watch_start(cfg, info)

        def do_probe():
            probe.update(parse_probe(ssh_run(cfg, info, REMOTE_PROBE, timeout=180)))
            fatal, notes = probe_problems(probe)
            return {'probe': probe, 'problems': fatal, 'notes': notes}

        def do_blender():   # never fails the step: an EEVEE failure is a note for the swap (Cycles is the fallback)
            r = ssh_run(cfg, info, REMOTE_EEVEE, check=False, timeout=300)
            made = r.strip().splitlines()[0:1] == ['1']
            return {'eevee': made, 'notes': [] if made else ['EEVEE rendered nothing (no GL/EGL?); the mannequin would '
                                                               'need SHISEI_ENGINE=CYCLES: ' + r.strip()[-300:]]}

        def prepare():   # models and nodes: what setup.sh (or the Piying image) puts on the machine
            cmd_tunnel(argparse.Namespace(close=False), cfg)
            for group in ('qwen', 'h3'):
                linked = ssh_run(cfg, info, ensure_models_script(group), timeout=300)
                missing = linked.strip().splitlines()[-1].split(':', 1)[1].split()
                if missing:
                    return {'problems': ['model files missing (was setup.sh run?): ' + ', '.join(missing)]}
            nodes = _object_info_missing(DRAMA_NODES['qwen'] + DRAMA_NODES['h3'])
            env = read_env(cfg, info)   # a result on another environment says little about this card
            problems = ['ComfyUI lacks nodes (was setup.sh run?): ' + ', '.join(nodes)] if nodes else []
            problems += [env['hint']] + env['problems'] if env['state'] in ('old', 'newer', 'drift') else []
            return {'problems': problems, 'env': {k: env.get(k) for k in ('state', 'have', 'want')},
                    'notes': [env['hint']] if env['state'] in ('missing', 'unknown') else []}

        def do_image():
            api = patch_drama_image(load_workflow('drama_image'), QUALIFY_SCENE, QUALIFY_SEED, f'{rid}_scene')
            name = generate(api, f'drama/{rid}_scene_*.png', out_dir / 'scene.png')
            _remote_cp_to_input(cfg, info, name, f'{rid}_scene.png')
            size = png_size(out_dir / 'scene.png')
            return {'problems': [] if tuple(size) == tuple(DRAMA_IMAGE_SIZE) else [f'picture is {size}, expected {DRAMA_IMAGE_SIZE}']}

        def do_take():
            want = frames_for(MAX_UNIT_SECONDS)
            api = patch_drama_take(load_workflow('drama_take'), QUALIFY_MOTION, QUALIFY_SEED, f'{rid}_take',
                                   f'{rid}_scene.png', [f'{rid}_scene.png'], DRAMA_SIZES['768p'], want)
            generate(api, f'drama/{rid}_take_*.mp4', out_dir / 'take.mp4')
            n, luma = video_luma_frames(out_dir / 'take.mp4')
            return {'frames': n, 'luma': round(luma, 1), 'problems': check_take(n, luma, want)}

        def do_swap():
            sd, sjob, frames = swap_job
            files = sjob['files']
            ups = [(sd / 'mannequin.mp4', f'{rid}_mq.mp4'), (sd / files['front'], f'{rid}_front{Path(files["front"]).suffix}'),
                   (sd / files['face'], f'{rid}_face{Path(files["face"]).suffix}')]
            for local, name in ups:
                _upload_as(cfg, info, local, name)
            api = patch_swap(load_workflow('swap_studio'), ups[0][1], ups[1][1], ups[2][1],
                             build_prompt('character', sjob.get('prompt') or ''), SWAP_FIRST_SEED, f'{rid}_swap', frames=frames)
            generate(api, f'{rid}_swap_0*.mp4', out_dir / 'swap.mp4')
            n, luma = video_luma_frames(out_dir / 'swap.mp4')
            return {'frames': n, 'luma': round(luma, 1), 'problems': check_take(n, luma, frames)}

        if step('probe', do_probe) and step('prepare', prepare):   # a card PyTorch cannot use loads no model
            step('blender', do_blender)
            if step('image', do_image):
                step('take', do_take)
            if swap_job:
                step('swap', do_swap)
        state = 'done'
    finally:
        ended = time.time()
        _arm_idle_stop(cfg, run)
        past = []
        for f in QUALIFY_DIR.glob('4090_48G-*/result.json'):
            try:
                past.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                pass
        verdict = qualify_verdict(gpu, probe, steps, baseline_take_minutes(past))
        result = {'gpu': gpu, 'zone': info.get('Zone'), 'instance': info.get('UHostId'), 'price_per_hour': price,
                  'began': began, 'ended': ended, 'state': state, 'plan': plan, 'steps': steps, 'verdict': verdict}
        save_json_atomic(out_dir / 'result.json', result)
        ledger = QUALIFY_DIR / 'ledger.json'
        save_json_atomic(ledger, (json.loads(ledger.read_text()) if ledger.exists() else [])
                         + [{**ledger_entry('qualify', len(steps), began, ended, price, state), 'gpu': gpu}])
    return {'state': state, 'result': str(out_dir / 'result.json'), 'verdict': verdict,
            'steps': [{k: s.get(k) for k in ('step', 'ok', 'minutes', 'problems', 'notes', 'error')} | {'peak_gb': (s.get('vram') or {}).get('peak_gb')}
                      for s in steps],
            'auto_stop': f'{int(cfg.get("idle_stop_minutes") or 10)}m'}


# ---------- a machine of the user's own, made by the plugin (the Piying page's 开始 › GPU 机器) ----------
# machine-plan is read-only (stock, sizes and prices of every zone; `instance create --dry-run` is free).
# machine-create creates ONE instance from the official ComfyUI base image, then: a scheduled stop at the hard limit
# (first thing, so a lost process still stops it), the user's ssh public key (through compshare-cli's own password
# ssh), the security check (closes the public ComfyUI port the base image opens), setup.sh, the security check again,
# a model / node check, and stop. Every exit leaves the machine stopped or under a scheduled stop and writes one
# ledger line; nothing is ever deleted. A re-run resumes with the instance it already made instead of making another.
SETUP_DIR = HOME / 'setup'
BASE_IMAGE = 'compshareImage-1x98m25rrumc'   # official 「ComfyUI基础镜像纯净版0.43.0」: 100 GB, CUDA 13.2
SETUP_DISK_GB = 200
SETUP_LIMIT_MIN = 60     # the scheduled stop set right after create; the quote's ceiling
SETUP_EST_MIN = 60       # from the base image: setup.sh downloads ~14 GB (measured ~60 min at 4-5 MB/s, 2026-11-12)
IMAGE_EST_MIN = 10       # from the Piying community image: boot, checks, and setup.sh fetching only the LoRAs (~4 min)
SETUP_BUDGET = SETUP_BUDGET_DEFAULT   # refuse when the ceiling is higher (config setup_budget)
DEFAULT_GPU = '4090_48G'
SETUP_STEPS = ('create', 'key', 'secure', 'setup', 'check', 'stop')
CPOD_RE = re.compile(r'(?:cpod|uhost)-[a-z0-9]+')   # instance ids: cpod-… and, for some zones (cn-wlcb-01, 2026-11-12), uhost-…
# VRAM of the GpuTypes compshare lists whose names carry no size (instance families, 2026-11-11)
GPU_VRAM_GB = {'RTX6000D': 84, 'A800': 80, 'A100': 80, 'H20': 96, '5090': 32, '4090': 24, '3090': 24, 'V100S': 32,
               '3080Ti': 12, '2080Ti': 11, '2080': 8, 'P40': 24}


def search_sizes(data, gpu):
    """{cpu, memory_gb, stock} of one 1-GPU machine from `instance search --image` data, or None when the zone has
    no such type. The largest memory of the first CPU size (what the existing 4090 48G machines use)."""
    types = (data or {}).get('AvailableInstanceTypes') or []
    t = next((x for x in types if x.get('Name') == gpu), None)
    one = next((m for m in (t or {}).get('MachineSizes') or [] if m.get('Gpu') == 1 and m.get('Collection')), None)
    if not one:
        return None
    col = one['Collection'][0]
    mem = max(col.get('Memory') or [0])
    inv = ((data or {}).get('Inventory') or {}).get(gpu) or []
    stock = any(i.get('Gpu') == 1 and i.get('Cpu') == col.get('Cpu') and i.get('Mem') == mem and i.get('ResourceEnough')
                for i in inv)
    return {'cpu': int(col['Cpu']), 'memory_gb': int(mem), 'stock': stock}


def setup_image(base=False):
    """(image id, from the Piying image?) for a new machine: the public community image of this plugin's env when there
    is one (docs/IMAGE.md), unless the user asked for the official base image (no image fee, about an hour longer)."""
    public = PUBLIC_IMAGES.get(SHISEI_ENV)
    return (BASE_IMAGE, False) if base or not public else (public, True)


def create_args(gpu, zone, size, name, image=BASE_IMAGE):
    return ['instance', 'create', '--gpu', gpu, '--count', '1', '--cpu', str(size['cpu']), '--memory', f'{size["memory_gb"]}GiB',
            '--image', image, '--region', zone.rsplit('-', 1)[0], '--zone', zone, '--charge', 'Postpay',
            '--disk', f'{SETUP_DISK_GB}GB', '--name', name]


def parse_dry_create(data):
    cap = (data or {}).get('capacity') or {}
    det = ((((data or {}).get('price') or {}).get('PriceDetails')) or [{}])[0]
    # `total` is what compshare-cli checks --max-price against (its _price_total, 0.5.6): Instance + Disks + SystemDisks
    # + CompShareImage. Disks and SystemDisks are the same one boot disk (`instance show` lists one), so the disk price
    # shown is SystemDisks alone; the cap is the CLI's sum, or every create is refused (2026-11-12: 3.94 over 3.87)
    total = sum(float(det.get(k) or 0) for k in ('Instance', 'Disks', 'SystemDisks', 'CompShareImage')
                if not isinstance(det.get(k), bool))
    # `price` is what the machine costs while it runs: the instance plus a community image's own hourly fee (a paid image
    # bills it on top; the official base image has none), so the page never quotes less than compshare charges
    running = float(det.get('Instance') or 0) + float(det.get('CompShareImage') or 0)
    return {'stock': bool(cap.get('ResourceEnough')), 'price': round(running, 4),
            'disk_per_hour': float(det.get('SystemDisks') or 0), 'total': round(total, 4)}


def pick_zone(rows):
    """The cheapest zone with stock and a price (ties: the platform's zone order), or None."""
    ok = [r for r in rows if r.get('stock') and r.get('price')]
    return min(ok, key=lambda r: (r['price'], r.get('order', 0))) if ok else None


def setup_quote(price, disk_per_hour, budget, minutes=SETUP_EST_MIN):
    """What the page shows on the create button: an estimate, the ceiling the scheduled stop enforces, the disk."""
    ceiling = round(SETUP_LIMIT_MIN / 60 * price, 2)
    return {'minutes': minutes, 'limit_minutes': SETUP_LIMIT_MIN, 'cost': round(minutes / 60 * price, 2),
            'ceiling': ceiling, 'disk_gb': SETUP_DISK_GB, 'disk_per_day': round(disk_per_hour * 24, 2),
            'budget': float(budget), 'over_budget': ceiling > float(budget)}


# Zones a machine is created in: only those where a created machine was set up end to end. Some zones make uhost-…
# instances with a public IP and no ssh port forward (cn-sh2-02, cn-wlcb-01: 真机 2026-11-12, the security check could
# not reach it), which the plugin cannot use yet; quoting them would bill for a machine that is never usable.
SETUP_ZONES = ('cn-sh2-03',)


def zone_rows(gpu, image=BASE_IMAGE):
    """Every usable zone's size and stock for one GPU type on one image, priced (dry-run create) where there is stock.
    Only SETUP_ZONES are asked."""
    from concurrent.futures import ThreadPoolExecutor
    zones = [z['Zone'] for z in cs('instance', 'zones')['items'] if z.get('Zone') in SETUP_ZONES]

    def one(i_zone):
        i, zone = i_zone
        row = {'zone': zone, 'order': i, 'stock': False}
        try:
            size = search_sizes(cs('instance', 'search', '--gpu', gpu, '--image', image,
                                   '--region', zone.rsplit('-', 1)[0], '--zone', zone), gpu)
        except ShiseiError:
            return row
        if not size:
            return None
        row.update(size)
        if size['stock']:
            try:
                row.update(parse_dry_create(cs(*create_args(gpu, zone, size, 'shisei-plan', image), '--dry-run')))
            except ShiseiError:
                row['stock'] = False
        return row

    with ThreadPoolExecutor(max_workers=6) as ex:
        return [r for r in ex.map(one, enumerate(zones)) if r]


def machine_plan(cfg, gpu=None, zone=None, base=False):
    gpu = gpu or DEFAULT_GPU
    if gpu_profile(gpu)['status'] != 'verified':
        raise ShiseiError(f'{gpu} has not passed gpu-qualify; only verified GPU types are created for users')
    image, from_image = setup_image(base)
    rows = zone_rows(gpu, image)
    pick = next((r for r in rows if r['zone'] == zone and r.get('stock')), None) if zone else pick_zone(rows)
    budget = float(cfg.get('setup_budget') or SETUP_BUDGET)
    plan = {'gpu': gpu, 'image': image, 'from_image': from_image, 'image_available': bool(PUBLIC_IMAGES.get(SHISEI_ENV)),
            'zones': [{k: r.get(k) for k in ('zone', 'stock', 'price')} for r in rows],
            'pick': pick, 'in_stock': bool(pick), 'checked_at': time.time()}
    if pick:
        plan['quote'] = setup_quote(pick['price'], pick.get('disk_per_hour') or 0, budget,
                                    IMAGE_EST_MIN if from_image else SETUP_EST_MIN)
    key = ssh_pubkey_path(cfg)
    plan['ssh_key'] = key.name if key else None   # the page refuses to create a machine nobody could log in to
    # other verified types with stock, for 换一种显卡 (empty while only the 4090 48G is verified)
    plan['alternatives'] = []
    if not pick:
        for other in [g for g, p in GPU_PROFILES.items() if g != gpu and p.get('status') == 'verified']:
            alt = pick_zone(zone_rows(other, image))
            if alt:
                plan['alternatives'].append({'gpu': other, 'zone': alt['zone'], 'price': alt['price'],
                                             'speed': GPU_PROFILES[other].get('speed')})
    return plan


def cmd_machine_plan(args, cfg):
    return machine_plan(cfg, args.gpu, args.zone, getattr(args, 'base', False))


def setup_progress():
    p = SETUP_DIR / 'progress.json'
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def setup_making():
    """The instance 开始 is still setting up (or failed to set up), else None: it is not offered for registering."""
    sp = setup_progress()   # an environment update is on a machine already in use: it stays offered
    return sp.get('instance') if sp and sp.get('state') != 'done' and not sp.get('update') else None


class SetupRun:
    """progress.json of the machine setup (the page reads it). Has the `p` dict _arm_idle_stop expects."""
    def __init__(self, old=None):
        SETUP_DIR.mkdir(parents=True, exist_ok=True)
        keep = {k: old[k] for k in ('instance', 'name', 'gpu', 'zone', 'price_per_hour', 'began', 'update') if old and old.get(k)}
        done = {s: 'done' for s, v in ((old or {}).get('steps') or {}).items() if v == 'done' and s == 'create'}
        self.p = {'state': 'running', 'pid': os.getpid(), 'started_at': time.time(), 'error': None, 'log': [],
                  'steps': {s: done.get(s, 'pending') for s in SETUP_STEPS}, **keep}
        self.save()

    def save(self):
        self.p['updated_at'] = time.time()
        save_json_atomic(SETUP_DIR / 'progress.json', self.p)

    def step(self, name, status, detail=None):
        self.p['steps'][name] = status
        self.p['current'] = name
        if detail is not None:
            self.p['detail'] = detail
        self.save()

    def note(self, msg):
        self.p['log'] = (self.p['log'] + [f'{time.strftime("%H:%M:%S")} {msg}'])[-40:]
        self.save()

    def check_stop(self):
        pass


def ssh_pubkey_path(cfg):
    """The public half of the ssh key Piying logs in with, or None (checked before any machine is made)."""
    key = resolve_ssh_key(cfg)
    pub = Path(str(key) + '.pub') if key else None
    return pub if pub and pub.is_file() else None


def pubkey_line(path):
    """`type base64` of an ssh public key, without the comment (it often carries an e-mail address)."""
    parts = Path(path).read_text().split()
    if len(parts) < 2 or not parts[0].startswith(('ssh-', 'ecdsa-')) or not re.fullmatch(r'[A-Za-z0-9+/=]+', parts[1]):
        raise ShiseiError(f'{Path(path).name} is not an ssh public key')
    return f'{parts[0]} {parts[1]}'


def authorize_key_script(line):
    if not re.fullmatch(r'(ssh-[a-z0-9-]+|ecdsa-[a-z0-9-]+) [A-Za-z0-9+/=]+', line):
        raise ShiseiError('unexpected public key format')
    return (f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF '{line}' ~/.ssh/authorized_keys || echo '{line}' >> ~/.ssh/authorized_keys) && "
            f"chmod 600 ~/.ssh/authorized_keys && echo key-ok")


def authorize_key(cfg, iid):
    """Put the public half of the ssh key on the machine, through compshare-cli's own password ssh (put_key: only
    appends to authorized_keys, retried while the machine boots)."""
    pub = ssh_pubkey_path(cfg)
    if not pub:
        return False
    return key_put(put_key(iid, authorize_key_script(pubkey_line(pub))))


def ssh_denied(stderr):
    """ssh reached the machine and it refused the key (as opposed to a network problem or a machine still booting)."""
    return 'Permission denied' in (stderr or '')


def created_id(data, name=None, listing=None):
    """The instance a create made: the one instance id in its answer, else the instance of this account with our unique
    name (an answer lost on the way back may still have created it). None when neither says."""
    ids = sorted(set(CPOD_RE.findall(json.dumps(data or {}))))
    if len(ids) == 1:
        return ids[0]
    named = [it.get('UHostId') for it in listing or [] if name and it.get('Name') == name]
    return named[0] if len(named) == 1 else None


def key_put(r):
    """Whether `instance ssh` put the key. compshare-cli 0.5.6 drops `ok` from a successful answer (Renderer.data pops
    it), so success is: not marked failed, exit 0, and our script's key-ok on stdout (seen 2026-11-12)."""
    r = r or {}
    return r.get('ok') is not False and r.get('exit_code', 0) == 0 and 'key-ok' in str(r.get('stdout'))


def key_failure(r):
    """Why the key step failed, short: the CLI's phase and message (never the key itself: it is not in stdout/stderr)."""
    r = r or {}
    err = r.get('error') or {}
    msg = (err.get('message') if isinstance(err, dict) else str(err)) or r.get('stderr') or ''
    msg = ' '.join(l for l in str(msg).splitlines() if not l.startswith('Warning: Permanently added'))
    return f'{(err.get("phase") if isinstance(err, dict) else None) or r.get("phase") or "?"}: {msg.strip()[:160] or "exit " + str(r.get("exit_code"))}'


KEY_TRIES = (15, 30, 45)   # a machine that just turned Running may not take ssh yet (seen 2026-11-12)


def decode_password(value):
    """An API password field: base64 of the password (compshare-cli's decode_password), or a legacy plain one."""
    if value is None:
        return None
    try:
        decoded = base64.b64decode(str(value), validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        return str(value)
    return decoded if decoded and decoded.isprintable() and base64.b64encode(decoded.encode()).decode() == str(value) else str(value)


def login_target(command):
    """(user@host, port) from the API's SshLoginCommand ("ssh -p 23456 root@host")."""
    argv = shlex.split(command or '')
    if not argv or Path(argv[0]).stem != 'ssh':
        raise ShiseiError('the instance has no ssh login command')
    port = argv[argv.index('-p') + 1] if '-p' in argv[:-1] else '22'
    dest = next((a for a in argv[1:] if '@' in a and not a.startswith('-')), None)
    if not dest or not port.isdigit():
        raise ShiseiError('the instance ssh login command is not one this plugin reads')
    return dest, int(port)


def put_key_own(iid, script):
    """put_key with the plugin's own ssh: the API's login command and password (the password through a file that
    ssh.cjs reads and removes, never argv or the environment). Answers in compshare-cli's shape for key_put."""
    info = cs('instance', 'show', iid, sensitive=True)['UHostSet'][0]
    dest, port = login_target(info.get('SshLoginCommand'))
    password = decode_password(info.get('Password'))
    if not password:
        return {'ok': False, 'error': {'phase': 'cli', 'message': 'the API gave no password for this instance'}}
    HOME.mkdir(parents=True, exist_ok=True)
    fd, pwfile = tempfile.mkstemp(prefix='pw-', dir=str(HOME))
    with os.fdopen(fd, 'w') as f:
        f.write(password)
    try:
        r = subprocess.run(ssh_cmd('ssh') + ['-o', f'PasswordFile={pwfile}', '-o', 'ConnectTimeout=30',
                                             '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={HOME / "known_hosts"}',
                                             '-p', str(port), dest, script],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180)
    finally:
        Path(pwfile).unlink(missing_ok=True)
    return {'ok': r.returncode == 0, 'exit_code': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr,
            'phase': 'ssh' if r.returncode != 255 else 'connection'}


def put_key(iid, script):
    """Put the user's public key through compshare-cli's own password ssh; retried while the new machine boots.
    compshare-cli shlex.joins the words after --: one word would reach the remote shell as a single quoted command name,
    so the script goes in as `bash -c <script>` (seen on the first real create, 2026-11-12)."""
    r = None
    for wait in KEY_TRIES + (None,):
        try:
            own = len(ssh_cmd('ssh')) > 1   # the plugin's own ssh: no OpenSSH needed for the password login either
            r = put_key_own(iid, script) if own else cs('instance', 'ssh', iid, '--', 'bash', '-c', script)
        except ShiseiError as e:   # an answer the CLI itself marks failed (remote_exit_nonzero and the like)
            r = {'ok': False, 'error': {'phase': 'cli', 'message': str(e)[:300]}}
        if key_put(r) or wait is None:
            return r
        log(f'putting the ssh key: {key_failure(r)}; trying again in {wait} s')
        time.sleep(wait)
    return r


def adopt_unsaved_create(old):
    """A setup that ended (dsh closed, computer asleep) while the create was still waiting has the machine's unique name
    but not its id. Look it up by that name, so continuing uses that machine instead of creating (and billing) a
    second one. Returns `old`, with `instance` filled in when the name is found."""
    if not old or old.get('instance') or not old.get('name') or old.get('state') == 'done':
        return old
    iid = created_id(None, old['name'], cs('instance', 'list')['items'])
    return {**old, 'instance': iid} if iid else old


# `;` before nohup, not `&&`: in `a && b && nohup … &` the `&` backgrounds the whole list, whose subshell keeps the ssh
# session's stdout open until setup.sh ends, so ssh never returned (first real create, 2026-11-12)
REMOTE_SETUP_START = (f'mkdir -p {REMOTE_WORK} && cd {REMOTE_WORK} && rm -f setup.rc || exit 1; '
                      "nohup sh -c 'bash setup.sh > setup.log 2>&1; echo $? > setup.rc' > /dev/null 2>&1 < /dev/null & echo started")
REMOTE_SETUP_POLL = f'cd {REMOTE_WORK}; cat setup.rc 2>/dev/null || echo running; tail -1 setup.log 2>/dev/null; true'


def parse_setup_poll(text):
    """(exit code or None while it runs, last log line)."""
    lines = (text or '').strip().splitlines()
    first = lines[0].strip() if lines else 'running'
    rc = int(first) if first.isdigit() else None
    return rc, (lines[1] if len(lines) > 1 else '')[:200]


def update_target(cfg, iid):
    """The machine 更新环境 reruns the setup on, as a resumed setup: a registered machine of this account that is
    stopped (a running one may be in use by something the plugin cannot see, and the setup ends by stopping it)."""
    if iid not in machine_ids(cfg):
        raise ShiseiError(f'{iid} is not a registered machine; only registered machines are updated')
    if lease_held(lease_read(iid)):
        raise MachineBusy(busy_message(iid, lease_read(iid)))
    row = next((it for it in cs('instance', 'list')['items'] if it.get('UHostId') == iid), None)
    if not row:
        raise ShiseiError(f'{iid} is not an instance of this compshare account')
    if row.get('State') != 'Stopped':
        raise ShiseiError(f'{iid} must be stopped before its environment is updated (it is {row.get("State")})')
    price = row.get('InstancePrice') or (cs('instance', 'show', iid)['UHostSet'][0] or {}).get('InstancePrice')
    if not price:
        raise ShiseiError(f'compshare did not give a price for {iid}; nothing was started')
    return {'instance': iid, 'name': row.get('Name') or iid, 'gpu': row.get('GpuType'), 'zone': row.get('Zone'),
            'price_per_hour': float(price), 'steps': {'create': 'done'}, 'update': True}


def cmd_machine_create(args, cfg):
    """Create and set up one machine (see the section comment). Without --confirm-billing: the plan only."""
    if args.confirm_billing and in_agent_shell():
        raise ShiseiError('making a machine is started by the user on the 皮影 page (开始), not from the director shell')
    old = setup_progress()
    if old and old.get('state') == 'running' and old.get('pid') and pid_alive(old['pid']):
        raise ShiseiError('a machine is already being set up')
    target = getattr(args, 'instance', None)
    if not target and old and old.get('update') and old.get('state') != 'done' and not args.new:
        target = old['instance']   # continuing a failed update: the same checks as starting one
    elif target and old and old.get('instance') and old.get('state') != 'done' and not old.get('update'):
        raise ShiseiError('a machine from 开始 is not finished setting up; continue it there first')
    adopted = False
    if target:   # 更新环境: the same setup on a machine the user already has; nothing is created
        old, plan = update_target(cfg, target), None
        resume = True
        quote = setup_quote(old['price_per_hour'], 0, float(cfg.get('setup_budget') or SETUP_BUDGET))
    else:
        had = bool(old and old.get('instance'))
        old = adopt_unsaved_create(old)   # also with --new: a machine our last create made but never wrote down is that one
        adopted = bool(old and old.get('instance')) and not had
        resume = bool(old and old.get('instance') and old.get('state') != 'done' and (adopted or not args.new))
        plan = None if resume else machine_plan(cfg, args.gpu, args.zone, getattr(args, 'base', False))
        if plan is not None and not plan['pick']:
            raise ShiseiError(f'{plan["gpu"]} is out of stock in every zone right now; nothing was created')
        quote = plan['quote'] if plan is not None else None
    if not args.confirm_billing:
        return {'needs_confirmation': True, 'resume': resume, 'instance': old.get('instance') if resume else None, 'plan': plan,
                'update': bool(target), 'quote': quote, 'ssh_key': bool(ssh_pubkey_path(cfg)),
                'machine': {k: old.get(k) for k in ('name', 'gpu', 'zone', 'price_per_hour')} if target else None}
    if not ssh_pubkey_path(cfg):   # before anything is created: a machine without the key could not be used
        own = key_to_make(cfg)
        if not own:   # a private key of the user's own without its .pub: theirs to fix, never replaced
            raise ShiseiError('no ssh key pair yet: make one on the 皮影 page (开始 › GPU 机器), then continue')
        make_own_key(own)   # nothing for the user to do: the plugin's own pair, only for these machines
    if quote is not None and quote['over_budget']:
        raise ShiseiError(f'the setup is priced up to ¥{quote["ceiling"]} (the {SETUP_LIMIT_MIN}-minute limit), over '
                          f'the ¥{quote["budget"]:g} setup budget')
    run = SetupRun(old if resume else None)
    began = run.p.setdefault('began', time.time())
    iid, state, info = run.p.get('instance'), 'failed', {}
    ours = False   # the machine is this run's to stop: made by it, or leased by it (never someone else's)
    try:
        if not iid:
            pick, gpu = plan['pick'], plan['gpu']
            name = f'shisei-{time.strftime("%m%d-%H%M%S")}'
            run.p.update(name=name, gpu=gpu, zone=pick['zone'], price_per_hour=pick['price'], image=plan['image'])
            run.step('create', 'running', f'{gpu} · {pick["zone"]}')
            max_price = f'{pick.get("total") or pick["price"] + (pick.get("disk_per_hour") or 0):.2f}'
            err = None
            try:   # exactly once: _cs_once, never the retrying cs(); a lost answer is looked up by the unique name
                data = _cs_once(*create_args(gpu, pick['zone'], pick, name, plan['image']), '--max-price', max_price,
                                '--wait', '--timeout', '900', '--yes')
            except ShiseiError as e:
                data, err = None, e
            iid = created_id(data, name)
            if not iid:   # no id we recognise in the answer (or no answer): it may still exist; look it up by its unique name
                iid = created_id(None, name, cs('instance', 'list')['items'])
            if not iid:
                raise ShiseiError(f'the machine was not created: {err or "no instance id in the answer"}')
            run.p['instance'] = iid
            run.save()
            ours = True
        use_machine(cfg, iid, save=False)   # registered only once it is ready (end of this function)
        lease_take(iid, 'setup', machine_port(cfg, iid), SETUP_LIMIT_MIN, 'setup')
        ours = True
        left = max(5, SETUP_LIMIT_MIN - int((time.time() - began) / 60))
        cs('instance', 'schedule', 'set', iid, '--at', f'{left}m')   # the hard limit, before anything else runs
        run.step('create', 'done')
        info = instance_info(cfg)
        # the ledger's price: the instance's own price when compshare states a higher one than the quote
        # (2026-11-12, cn-wlcb-01: quoted 3.61, `instance show` 3.8); the bill decides, the ledger does not undercount
        try:
            run.p['price_per_hour'] = max(float(run.p.get('price_per_hour') or 0), float(info.get('InstancePrice') or 0))
        except (TypeError, ValueError):
            pass
        if adopted and info.get('State') == 'Running' and info.get('CreateTime'):
            # made by an earlier run that never wrote it down (so never booked it): it has billed since it was made
            run.p['bill_from'] = float(info['CreateTime'])
        if info.get('State') != 'Running':
            try:
                started = cs('instance', 'start', iid)   # compshare-cli 0.5.6: start has no --yes (stop does)
            except ShiseiError as e:   # the CLI's answer is long JSON that ends cut off; say the readable part
                raise ShiseiError(f'could not start the machine: {start_failure(str(e))}') from e
            if (started or {}).get('failed'):   # an answer that lists the start as failed (as `up` also reads it)
                raise ShiseiError('could not start the machine: '
                                  + start_failure(json.dumps(started, ensure_ascii=False)))
            wait_state(cfg, 'Running', 900)   # returns the state string, not the instance: read the instance after
            info = instance_info(cfg)
            # a stopped machine started again is a new start the user clicked: its own window from now. Counting from
            # the first create would cut a later 「继续」 short mid-setup.sh, or leave it the 5-minute floor
            began = run.p['began'] = time.time()
            run.save()
            cs('instance', 'schedule', 'set', iid, '--at', f'{SETUP_LIMIT_MIN}m')
        run.step('key', 'running')
        pub = ssh_pubkey_path(cfg)
        r = put_key(iid, authorize_key_script(pubkey_line(pub)))
        if not key_put(r):
            raise ShiseiError('could not put your ssh key on the machine: add ' + str(pub) + ' in the '
                              f'compshare console (instance › SSH key), then continue ({key_failure(r)})')
        run.step('key', 'done')
        run.step('secure', 'running')
        if not do_secure(cfg, info)['clean']:
            raise ShiseiError('security check found something on the new machine; it was stopped and not used')
        run.step('secure', 'done')
        run.step('setup', 'running')
        scp, host = ssh_base(cfg, info, scp=True)
        ssh_run(cfg, info, f'mkdir -p {REMOTE_WORK}', timeout=60)
        scp_run(scp + [str(ASSETS / 'image' / 'setup.sh'), f'{host}:{REMOTE_WORK}/setup.sh'], timeout=120)
        ssh_run(cfg, info, REMOTE_SETUP_START, timeout=60)
        deadline = began + (SETUP_LIMIT_MIN - 6) * 60
        while True:
            time.sleep(20)
            rc, last = parse_setup_poll(ssh_run(cfg, info, REMOTE_SETUP_POLL, check=False, timeout=60))
            run.step('setup', 'running', last)
            if rc is not None:
                if rc != 0:
                    raise ShiseiError(f'setup.sh failed ({rc}): {last}')
                break
            if time.time() > deadline:
                raise ShiseiError(f'setup.sh did not finish within {SETUP_LIMIT_MIN} minutes: {last}')
        run.step('setup', 'done')
        run.step('check', 'running')
        if not do_secure(cfg, info)['clean']:   # setup.sh restarts ComfyUI: check the port again
            raise ShiseiError('security check after setup found something; the machine was stopped and not used')
        cmd_tunnel(argparse.Namespace(close=False), cfg)
        for group in ('qwen', 'h3'):
            missing = ssh_run(cfg, info, ensure_models_script(group), timeout=300).strip().splitlines()[-1].split(':', 1)[1].split()
            if missing:
                raise ShiseiError('model files missing after setup: ' + ', '.join(missing))
        nodes = _object_info_missing(DRAMA_NODES['qwen'] + DRAMA_NODES['h3'])
        if nodes:
            raise ShiseiError('ComfyUI lacks nodes after setup: ' + ', '.join(nodes))
        env = read_env(cfg, info)
        if env['state'] != 'current':
            raise ShiseiError(f'environment after setup is {env["state"]}: ' + '; '.join(env['problems'] or [env['hint'] or '']))
        run.step('check', 'done')
        run.step('stop', 'running')
        cmd_tunnel(argparse.Namespace(close=True), cfg)
        cs('instance', 'stop', iid, '--yes')
        wait_state(cfg, 'Stopped', 600)
        known = {it.get('UHostId') for it in cs('instance', 'list')['items']} | {iid}
        register_machine(load_config(), known, add=iid)   # preferred if none was; else next to the existing one
        run.step('stop', 'done')
        state = 'done'
        return {'state': 'done', 'instance': iid, 'gpu': run.p.get('gpu'), 'zone': run.p.get('zone')}
    except Exception as e:   # every exit, a bug included, leaves the machine stopped (or under its scheduled stop)
        run.p['error'] = str(e)[:600]
        cur = run.p.get('current')
        if cur and run.p['steps'].get(cur) == 'running':
            run.p['steps'][cur] = 'failed'
        if iid and ours:   # never left running: stop it, or at least keep a scheduled stop; never delete it
            try:
                cmd_tunnel(argparse.Namespace(close=True), cfg)
                cs('instance', 'stop', iid, '--yes')
            except (ShiseiError, subprocess.SubprocessError) as e2:
                log(f'could not stop the new machine ({e2}); its scheduled stop still applies')
                try:
                    cs('instance', 'schedule', 'set', iid, '--at', f'{int(cfg.get("idle_stop_minutes") or 10)}m')
                except ShiseiError:
                    pass
        raise ShiseiError(str(e)[:600])
    finally:
        ended = time.time()
        if iid and ours:
            lease_drop(iid, 'setup', force=True)
        run.p.update(state=state, ended=ended)
        run.save()
        if iid and ours:   # nothing ran (and nothing was billed) when the machine was never ours
            ledger = SETUP_DIR / 'ledger.json'
            prev = json.loads(ledger.read_text()) if ledger.exists() else []
            start = run.p.get('bill_from') or run.p.get('started_at') or began
            save_json_atomic(ledger, prev + [{**ledger_entry('setup', 1, start, ended, run.p.get('price_per_hour'), state),
                                              'gpu': run.p.get('gpu'), 'instance': iid}])


# ---------- series: episodes (drama projects) that share characters, approved once (docs/SERIES.md) ----------
# ~/.dsh/shisei/series/<id>/series.json, plus the shared characters' pictures (cast_<id>_face.png / _body.png).
# A project joins a series (job.json `series`); a character becomes a regular of the series from a project where the
# user approved both of its pictures, and the bytes are recorded. A new episode gets the regulars staged in
# adopted.json with those very pictures: their approval carries over only while the bytes are the ones the user
# approved and the description is unchanged (drama-set takes it back like any changed character). Nothing is deleted:
# leaving a series, archiving and retiring only change state.
SERIES_DIR = HOME / 'series'
SERIES_ID_RE = re.compile(r's\d{8}-\d{6}-[0-9a-f]{4}')
SERIES_CAST_FIELDS = ('id', 'name', 'name_en', 'age', 'look', 'voice', 'outfit', 'look_young')


def series_path(sid):
    if not SERIES_ID_RE.fullmatch(str(sid or '')):
        raise ShiseiError(f'bad series id {sid!r}')
    p = SERIES_DIR / sid
    if not (p / 'series.json').is_file():
        raise ShiseiError(f'no series {sid}')
    return p


def load_series(sid):
    s = json.loads((series_path(sid) / 'series.json').read_text())
    s.setdefault('episodes', [])
    s.setdefault('cast', [])
    s.setdefault('summaries', {})
    return s


def series_lock(sid):
    return file_lock(lock_file(series_path(sid), 'series'))


def save_series(sid, s):
    save_json_atomic(series_path(sid) / 'series.json', s)


def series_refuse_archived(s):
    if s.get('archivedAt'):
        raise ShiseiError('这个系列已归档，只能看：先在页面上点「恢复并继续」')


def series_user_only(what):
    if in_agent_shell():
        raise ShiseiError(f'{what} is done by the user on the Piying page, not from the director shell')


def file_sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def job_of(jobdir):
    d = Path(jobdir)
    try:
        job = json.loads((d / 'job.json').read_text())
    except (OSError, ValueError):
        raise ShiseiError(f'not a job folder: {d}')
    if job.get('kind') != 'drama':
        raise ShiseiError('only a drama project can be an episode')
    return d, job


def set_job_series(d, sid):
    with file_lock(lock_file(d, 'job')):
        job = json.loads((Path(d) / 'job.json').read_text())
        if sid:
            job['series'] = sid
        else:
            job.pop('series', None)
        save_json_atomic(Path(d) / 'job.json', job)


def episode_story(d):
    p = Path(d) / 'story.json'
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except ValueError:
        return None


def series_cast_users(sid, s, cid):
    """Episodes (not archived, no final cut yet) that still show the regular `cid`: [(n, title, [segment labels])]."""
    out = []
    for e in s['episodes']:
        d = HOME / 'jobs' / e['job']
        try:
            job = json.loads((d / 'job.json').read_text())
        except (OSError, ValueError):
            continue
        if job.get('archivedAt') or (d / 'final.mp4').exists():
            continue
        story = episode_story(d)
        mine = [c['id'] for c in (story or {}).get('cast') or []
                if (c.get('origin') or {}).get('kind') == 'series' and c['origin'].get('series') == sid and c['origin'].get('id') == cid]
        staged = [x for x in load_staged(d) if x.get('kind') == 'cast' and (x['entry'].get('origin') or {}).get('id') == cid
                  and x['entry']['origin'].get('series') == sid]
        if mine or staged:
            labels = []
            if story and mine:
                try:
                    labels = [u['label'] for u in units(story) if set(u['cast']) & set(mine)]
                except (KeyError, TypeError, ValueError):
                    labels = []
            out.append({'n': e['n'], 'job': e['job'], 'title': job.get('title') or '', 'segments': labels or (['剧本'] if story else ['还没剧本'])})
    return out


def series_recap(s, before_job=None):
    """What the director reads before writing an episode: the series, its regulars, and the summaries the user fixed
    (draft summaries and episodes marked as not part of the story are left out). Chinese: the director's own words."""
    lines = [f'【系列前情】{s["title"]}']   # zh-ok
    if s.get('logline'):
        lines.append(f'一句话：{s["logline"]}')   # zh-ok
    if s.get('rules'):
        lines.append(f'称呼和不能改的事：{s["rules"]}')   # zh-ok
    regs = [c for c in s['cast'] if not c.get('retired')]
    if regs:
        lines.append('常驻角色（id 和名字照用，长相已经审过，不要改 look）：' + '；'.join(   # zh-ok
            f'{c["name"]}（{c["id"]}，{c["age"]} 岁{"，还没有定妆照，按 look 画" if not c.get("sha") else ""}）' for c in regs))   # zh-ok
    for e in sorted(s['episodes'], key=lambda e: e['n']):
        if e['job'] == before_job:
            break
        sm = s['summaries'].get(e['job']) or {}
        if e.get('counts', True) and sm.get('state') == 'set' and sm.get('text'):
            lines.append(f'第 {e["n"]} 集：{sm["text"]}')   # zh-ok
    return '\n'.join(lines)


def series_view(sid, s):
    eps = []
    for e in sorted(s['episodes'], key=lambda e: e['n']):
        d = HOME / 'jobs' / e['job']
        try:
            job = json.loads((d / 'job.json').read_text())
        except (OSError, ValueError):
            job = {}
        story = episode_story(d)
        used = sorted({(c.get('origin') or {}).get('id') for c in (story or {}).get('cast') or []
                       if (c.get('origin') or {}).get('series') == sid} - {None})
        if story is None:
            used = sorted({(x['entry'].get('origin') or {}).get('id') for x in load_staged(d) if x.get('kind') == 'cast'} - {None})
        # characters of this episode that could become regulars: not one already, both pictures approved here
        review = load_review(d) if story is not None else {'assets': {}}
        ready = [{'id': c['id'], 'name': c.get('name'), 'age': c.get('age')} for c in (story or {}).get('cast') or []
                 if (c.get('origin') or {}).get('kind') != 'series'
                 and all((d / f).exists() and verdict(review, 'assets', f) == 'pass' for f in adopt_files('cast', c['id']))]
        eps.append({**e, 'counts': e.get('counts', True), 'title': job.get('title') or '', 'archivedAt': job.get('archivedAt'),
                    'final': (d / 'final.mp4').exists(), 'hasStory': story is not None, 'cast': used, 'candidates': ready,
                    'summary': s['summaries'].get(e['job'])})
    # recordings each regular could be fixed with: its own voice tracks in the episodes
    lines = {}
    for e in s['episodes']:
        d = HOME / 'jobs' / e['job']
        story = episode_story(d)
        for c in (story or {}).get('cast') or []:
            o = c.get('origin') or {}
            if o.get('kind') == 'series' and o.get('series') == sid:
                try:
                    cands = voice_candidates(d, story, c['id'])
                except (KeyError, TypeError):
                    cands = []
                lines.setdefault(o.get('id'), []).extend({**x, 'job': e['job'], 'n': e['n']} for x in cands)
    newer = series_newer_pictures(sid, s)
    return {'id': sid, 'title': s['title'], 'logline': s.get('logline') or '', 'rules': s.get('rules') or '',
            'createdAt': s.get('createdAt'), 'archivedAt': s.get('archivedAt'), 'episodes': eps,
            'cast': [{**c, 'files': {k: f'cast_{c["id"]}_{k}.png' for k in ('face', 'body')},
                      'voiceFile': voice_file(c['id']) if c.get('voice_fixed') else None,
                      'voiceLines': lines.get(c['id'], []),
                      'pictures': bool(c.get('sha')), 'photo': photo_file(c['id']) if c.get('photo_ref') else None,
                      'voiceFrom': 'upload' if (c.get('voice_fixed') or {}).get('from', {}).get('kind') == 'upload' else None,
                      'newer': newer.get(c['id'], []),
                      'dir': str(series_path(sid))} for c in s['cast']],
            'nextN': max([e['n'] for e in s['episodes']] + [s.get('lastN', 0)]) + 1}


def series_newer_pictures(sid, s):
    """{regular: [{job, n, title, files}]}: episodes (not archived) where the user approved both pictures of a regular
    and they are not the series' ones: a new look to take over, or the first pictures of a regular added without any."""
    out = {}
    regs = {c['id']: c for c in s['cast']}
    for e in sorted(s['episodes'], key=lambda e: e['n']):
        d = HOME / 'jobs' / e['job']
        try:
            job = json.loads((d / 'job.json').read_text())
        except (OSError, ValueError):
            continue
        story = episode_story(d)
        if job.get('archivedAt') or story is None:
            continue
        review = load_review(d)
        for c in story.get('cast') or []:
            o = c.get('origin') or {}
            reg = regs.get(o.get('id')) if o.get('kind') == 'series' and o.get('series') == sid else None
            if reg is None or reg.get('retired'):
                continue
            files = adopt_files('cast', c['id'])
            if not all((d / f).is_file() and verdict(review, 'assets', f) == 'pass' for f in files):
                continue
            sha = {k: file_sha(d / f) for k, f in zip(('face', 'body'), files)}
            if sha == reg.get('sha') or sha in [v.get('sha') for v in reg.get('versions') or []]:
                continue   # the series' pictures, now or before (an episode that shot with the old ones keeps them)
            out.setdefault(reg['id'], []).append({'job': e['job'], 'n': e['n'], 'title': job.get('title') or '',
                                                  'files': files, 'look': c.get('look') or ''})
    return out


def put_regular_pictures(ed, sid, reg, cid, story, why):
    """Give an episode that has not shot this character yet the regular's current pictures and description: the old
    pictures are kept as a version, the new ones carry the series' approval, and the segments showing the character
    are marked changed (their first frames are not redrawn by themselves)."""
    sp = series_path(sid)
    new = copy.deepcopy(story)
    c = next(x for x in new['cast'] if x['id'] == cid)
    for k in SERIES_CAST_FIELDS:
        if k == 'id':
            continue
        if k in reg:
            c[k] = reg[k]
        else:
            c.pop(k, None)
    if new != story:
        apply_story(ed, story, new, why)
    with review_lock(ed):
        review = load_review(ed)
        for k, f in zip(('face', 'body'), adopt_files('cast', cid)):
            archive_asset(ed, f, review)
            shutil.copyfile(sp / f'cast_{reg["id"]}_{k}.png', ed / f)
            review['assets'][f] = {'status': 'pass', 'note': '系列里审过', 'at': time.time(), 'series': sid}
        save_json_atomic(ed / 'review.json', review)
        for u in units(new):
            if cid in u['cast']:
                log_change(ed, {'kind': 'cast', 'unit': u['id'], 'label': u['label'], 'cast': cid, 'shot': None,
                                'why': why[:300], 'redo': []})


def cmd_series_cast_update(args, cfg):
    """换定妆照: the regular takes the pictures (and description) the user approved for it in one episode. The series'
    old pictures are kept as cast_<id>_face.vN.png. Episodes with no take of the character yet, and new episodes, use
    the new ones; episodes with takes keep what they shot (to match there, the user changes it in that episode).
    Also how a regular added without pictures gets its first ones."""
    series_user_only("changing a regular's pictures")
    d, job = job_of(args.jobdir)
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        reg = next((x for x in s['cast'] if x['id'] == args.cast), None)
        if reg is None:
            raise ShiseiError(f'no regular {args.cast}')
        if reg.get('retired'):
            raise ShiseiError(f'{reg["name"]}已经退场：先请回来')
        if job.get('series') != args.series:
            raise ShiseiError('this project is not an episode of this series')
        story = load_story(d)
        c = next((x for x in story['cast'] if (x.get('origin') or {}).get('series') == args.series
                  and x['origin'].get('id') == args.cast), None)
        if c is None:
            raise ShiseiError(f'{reg["name"]} is not in this episode')
        review = load_review(d)
        files = adopt_files('cast', c['id'])
        missing = [f for f in files if not (d / f).is_file() or verdict(review, 'assets', f) != 'pass']
        if missing:
            raise ShiseiError(f'这一集里{reg["name"]}的定妆照和全身照都审过才能换上（还差 {", ".join(missing)}）')
        sha = {k: file_sha(d / f) for k, f in zip(('face', 'body'), files)}
        if sha == reg.get('sha'):
            raise ShiseiError('系列里已经是这两张了')
        sp = series_path(args.series)
        first = not reg.get('sha')
        for f, k in zip(files, ('face', 'body')):
            keep_aside(sp / f'cast_{args.cast}_{k}.png')
            shutil.copyfile(d / f, sp / f'cast_{args.cast}_{k}.png')
        for k in SERIES_CAST_FIELDS:
            if k != 'id' and k in c:
                reg[k] = c[k]
        if reg.get('sha'):
            reg.setdefault('versions', []).append({'sha': reg['sha'], 'from': reg.get('from'), 'until': time.time()})
        reg['sha'] = sha
        reg['from'] = {'job': d.name, 'id': c['id']}
        save_series(args.series, s)
        episodes = list(s['episodes'])
    why = f'{reg["name"]}的定妆照用第 {next((e["n"] for e in episodes if e["job"] == d.name), "?")} 集审过的'
    applied, skipped = [], []
    for e in episodes:
        ed = HOME / 'jobs' / e['job']
        if e['job'] == d.name:
            continue
        try:
            ej = json.loads((ed / 'job.json').read_text())
        except (OSError, ValueError):
            continue
        if ej.get('archivedAt'):
            continue
        st = episode_story(ed)
        if st is None:   # no script yet: the staged regular is staged again with the new pictures
            if any(x.get('kind') == 'cast' and (x['entry'].get('origin') or {}).get('series') == args.series
                   and x['entry']['origin'].get('id') == args.cast for x in load_staged(ed)):
                with review_lock(ed):
                    stage_regular(ed, args.series, reg)
                applied.append(e['n'])
            continue
        cid = next((x['id'] for x in st['cast'] if (x.get('origin') or {}).get('series') == args.series
                    and x['origin'].get('id') == args.cast), None)
        if cid is None:
            continue
        if episode_has_takes(ed, st, cid):
            skipped.append(e['n'])
            continue
        put_regular_pictures(ed, args.series, reg, cid, st, why)
        applied.append(e['n'])
    return {**series_view(args.series, load_series(args.series)), 'applied': applied, 'skipped': skipped, 'first': first}


def cmd_series_cast_new(args, cfg):
    """A regular added on the series page, before any episode has it: a description (and optionally a photo the user
    uploaded, which the portrait is drawn after). No pictures yet: the next episode draws them, the user approves them
    there, and series-cast-update makes them the series' ones. Age and the wardrobe rules are checked like any script."""
    series_user_only('adding a regular')
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        sp = series_path(args.series)
        try:
            age = int(args.age)
        except (TypeError, ValueError):
            raise ShiseiError('写上年龄（1–99 的整数）')
        name_en = (args.name_en or '').strip()
        entry = {'name': (args.name or '').strip()[:40], 'name_en': name_en, 'age': age, 'look': (args.look or '').strip()[:600]}
        if (args.outfit or '').strip():
            entry['outfit'] = args.outfit.strip()[:300]
        if (args.voice or '').strip():
            entry['voice'] = args.voice.strip()[:200]
        if args.look_young:
            entry['look_young'] = True
        if not entry['name'] or not entry['look']:
            raise ShiseiError('名字和长相描述都要写（长相描述是画定妆照用的）')
        taken = {x['id'] for x in s['cast']} | {m.group(1) for m in (re.match(r'(?:cast|photo)_(.+?)(?:_(?:face|body))?(?:\.v\d+)?\.png', f.name) for f in sp.glob('*.png')) if m}
        # the user's words first, in the user's words (check_entry below says the same to the director's way)
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,39}", name_en):
            raise ShiseiError('英文名只用英文字母（动作描述里用）')
        if not 1 <= age <= 99:
            raise ShiseiError('写上年龄（1–99 的整数）')
        for field, label in (('look', '长相'), ('outfit', '衣服'), ('voice', '声音')):
            text = entry.get(field) or ''
            why = exposure_problem(text, is_young(entry) or bool(word_hits(MINOR_RE, text)))
            if why:
                raise ShiseiError(f'{label}里{why}：改掉再加')
        rid = fresh_id(slug_id(name_en or entry['name'], 'cast'), taken)
        entry['id'] = rid
        check_entry('cast', entry)
        if args.photo:
            if is_young(entry):
                raise ShiseiError('看起来年轻或未成年的角色不照照片画：只写长相描述')
            src = Path(args.photo)
            if src.parent.resolve() != sp.resolve() or not re.fullmatch(r'\.photo_upload\.(png|jpe?g|webp)', src.name) or not src.is_file():
                raise ShiseiError('--photo is the photo the Piying page saved (.photo_upload.<ext> in the series folder)')
            try:
                _read_picture(src, 'the photo')
                subprocess.run(image_to_png_args(src, sp / photo_file(rid), DRAMA_PORTRAIT_SIZE, 'contain'), check=True, capture_output=True, timeout=120)
            except subprocess.CalledProcessError:
                raise ShiseiError('读不了这张照片：换成 PNG 或 JPEG 再试')
            finally:
                src.unlink(missing_ok=True)
            entry['photo_ref'] = {'file': photo_file(rid), 'sha': file_sha(sp / photo_file(rid))}
        entry['from'] = {'kind': 'photo' if args.photo else 'words'}
        entry['at'] = time.time()
        s['cast'].append(entry)
        save_series(args.series, s)
    return {**series_view(args.series, s), 'added': rid}


def cmd_series_new(args, cfg):
    title = (args.title or '').strip()[:60]
    if not title:
        raise ShiseiError('a series needs --title')
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    sid = time.strftime('s%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
    (SERIES_DIR / sid).mkdir()
    s = {'id': sid, 'title': title, 'logline': (args.logline or '').strip()[:400], 'rules': '',
         'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S'), 'episodes': [], 'cast': [], 'summaries': {}, 'lastN': 0}
    save_json_atomic(SERIES_DIR / sid / 'series.json', s)
    return series_view(sid, s)


def cmd_series_list(args, cfg):
    out = []
    for p in sorted(SERIES_DIR.glob('s*/series.json')) if SERIES_DIR.is_dir() else []:
        try:
            sid = p.parent.name
            out.append(series_view(sid, load_series(sid)))
        except (ShiseiError, OSError, ValueError, KeyError):
            continue
    return {'series': out}


def cmd_series_show(args, cfg):
    s = load_series(args.series)
    v = series_view(args.series, s)
    v['recap'] = series_recap(s, args.before)
    return v


def cmd_series_set(args, cfg):
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        for k, n in (('title', 60), ('logline', 400), ('rules', 600)):
            v = getattr(args, k)
            if v is not None:
                if k == 'title' and not v.strip():
                    raise ShiseiError('a series needs a title')
                s[k] = v.strip()[:n]
        save_series(args.series, s)
    return series_view(args.series, s)


def cmd_series_add(args, cfg):
    """Make a drama project the next episode. What it already made is untouched; its characters stay its own until the
    user makes one a regular (series-cast-add) or maps it to one."""
    d, job = job_of(args.jobdir)
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        if job.get('series') and job['series'] != args.series:
            raise ShiseiError('this project is already an episode of another series: take it out there first')
        if any(e['job'] == d.name for e in s['episodes']):
            return series_view(args.series, s)
        n = max([e['n'] for e in s['episodes']] + [s.get('lastN', 0)]) + 1
        s['episodes'].append({'job': d.name, 'n': n, 'counts': True, 'at': time.time()})
        s['lastN'] = n
        save_series(args.series, s)
        set_job_series(d, args.series)
    return series_view(args.series, s)


def cmd_series_remove(args, cfg):
    """Take a project out of the series: it becomes a single project again, with everything it made. Its number is not
    given to anyone else, so the numbers in the recap never shift."""
    d, _ = job_of(args.jobdir)
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        s['episodes'] = [e for e in s['episodes'] if e['job'] != d.name]
        s['summaries'].pop(d.name, None)
        save_series(args.series, s)
        set_job_series(d, None)
    return series_view(args.series, s)


def cmd_series_order(args, cfg):
    """The user's order of the episodes (drag on the page): numbers follow it; nothing else changes."""
    series_user_only('reordering episodes')
    order = [x for x in (args.order or '').split(',') if x]
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        if sorted(order) != sorted(e['job'] for e in s['episodes']):
            raise ShiseiError('the order must name every episode once')
        by = {e['job']: e for e in s['episodes']}
        s['episodes'] = [{**by[j], 'n': i + 1} for i, j in enumerate(order)]
        s['lastN'] = max(s.get('lastN', 0), len(order))
        save_series(args.series, s)
    return series_view(args.series, s)


def cmd_series_counts(args, cfg):
    """Whether an episode is part of the story (an archived draft may not be): the recap skips the ones that are not."""
    series_user_only('deciding what counts as the story')
    with series_lock(args.series):
        s = load_series(args.series)
        e = next((e for e in s['episodes'] if e['job'] == args.job), None)
        if e is None:
            raise ShiseiError('not an episode of this series')
        e['counts'] = args.value == 'yes'
        save_series(args.series, s)
    return series_view(args.series, s)


def cmd_series_summary(args, cfg):
    """An episode's one-line summary for the recap. The director may write a draft; only the user fixes it ('set'),
    and only fixed summaries reach the director."""
    if args.state == 'set':
        series_user_only('fixing a summary')
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        if not any(e['job'] == args.job for e in s['episodes']):
            raise ShiseiError('not an episode of this series')
        text = (args.text or '').strip()[:300]
        if not text:
            s['summaries'].pop(args.job, None)
        else:
            s['summaries'][args.job] = {'text': text, 'state': args.state, 'at': time.time()}
        save_series(args.series, s)
    return series_view(args.series, s)


def cmd_series_cast_add(args, cfg):
    """Make a character of an episode a regular of the series: its entry and both pictures, which the user must have
    approved in that episode, are copied in with their checksums; the episode's entry then points at the regular."""
    series_user_only('making a character a regular')
    d, job = job_of(args.jobdir)
    with series_lock(args.series), review_lock(d):
        s = load_series(args.series)
        series_refuse_archived(s)
        if job.get('series') != args.series:
            raise ShiseiError('this project is not an episode of this series')
        story = load_story(d)
        c = next((x for x in story['cast'] if x['id'] == args.cast), None)
        if c is None:
            raise ShiseiError(f'no character {args.cast} in this episode')
        if (c.get('origin') or {}).get('kind') == 'series':
            raise ShiseiError(f'{c["name"]} is already a regular of a series')
        review = load_review(d)
        files = adopt_files('cast', c['id'])
        missing = [f for f in files if not (d / f).exists() or verdict(review, 'assets', f) != 'pass']
        if missing:
            raise ShiseiError(f'{c["name"]} 的定妆照和全身照都审过才能成为常驻角色（还差 {", ".join(missing)}）')
        sp = series_path(args.series)
        rid = fresh_id(c['id'], {x['id'] for x in s['cast']} | {m.group(1) for m in (re.match(r'cast_(.+)_(?:face|body)', f.name) for f in sp.glob('cast_*.png')) if m})
        entry = {k: c[k] for k in SERIES_CAST_FIELDS if k in c}
        entry['id'] = rid
        for f, k in zip(files, ('face', 'body')):
            shutil.copyfile(d / f, sp / f'cast_{rid}_{k}.png')
        entry['sha'] = {k: file_sha(sp / f'cast_{rid}_{k}.png') for k in ('face', 'body')}
        entry['from'] = {'job': d.name, 'id': c['id']}
        entry['at'] = time.time()
        s['cast'].append(entry)
        save_series(args.series, s)
        new = copy.deepcopy(story)
        for x in new['cast']:
            if x['id'] == c['id']:
                x['origin'] = {'kind': 'series', 'series': args.series, 'id': rid}
    apply_story(d, story, new, f'{c["name"]} 成为系列的常驻角色')
    return series_view(args.series, s)


def cmd_series_cast_retire(args, cfg):
    """A regular leaves (or comes back with --back): no longer offered for new episodes; every picture stays. Refused
    while an unfinished episode still shows the character."""
    series_user_only('retiring a character')
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        c = next((x for x in s['cast'] if x['id'] == args.cast), None)
        if c is None:
            raise ShiseiError(f'no regular {args.cast}')
        if args.back:
            c.pop('retired', None)
        else:
            users = series_cast_users(args.series, s, args.cast)
            if users:
                where = '；'.join(f'第 {u["n"]} 集 ' + '、'.join(u['segments']) for u in users)
                raise ShiseiError(f'{c["name"]}还在没拍完的集里（{where}）：先从这些地方拿掉，或拍完再退场')
            c['retired'] = time.time()
        save_series(args.series, s)
    return series_view(args.series, s)


def cmd_series_archive(args, cfg):
    """Archive a whole series (every episode read-only, like a project's 归档), or --restore it. Only state changes:
    nothing is deleted. Episodes archived on their own before stay archived on restore. The page checks first that no
    episode is being shot."""
    series_user_only('archiving a series')
    with series_lock(args.series):
        s = load_series(args.series)
        at = time.strftime('%Y-%m-%dT%H:%M:%S')
        for e in s['episodes']:
            d = HOME / 'jobs' / e['job']
            if not (d / 'job.json').exists():
                continue
            with file_lock(lock_file(d, 'job')):
                job = json.loads((d / 'job.json').read_text())
                if args.restore:
                    if job.get('archivedBy') == 'series':
                        job.pop('archivedAt', None)
                        job.pop('archivedBy', None)
                elif not job.get('archivedAt'):
                    job['archivedAt'] = at
                    job['archivedBy'] = 'series'
                save_json_atomic(d / 'job.json', job)
        if args.restore:
            s.pop('archivedAt', None)
        else:
            s['archivedAt'] = s.get('archivedAt') or at
        save_series(args.series, s)
    return series_view(args.series, s)


def keep_aside(p):
    """Move an existing file to its next free .vN name (nothing is deleted); returns that name or None."""
    p = Path(p)
    if not p.exists():
        return None
    n = 1
    while p.with_name(f'{p.stem}.v{n}{p.suffix}').exists():
        n += 1
    dst = p.with_name(f'{p.stem}.v{n}{p.suffix}')
    p.replace(dst)
    return dst.name


def set_voice(d, cid, src, why):
    """Fix (src: a recording) or clear (src None) a character's voice in one project. The old recording is kept as
    voice_<id>.vN.wav; segments whose takes used the old voice are marked changed like any other edit."""
    d = Path(d)
    story = load_story(d)
    if not any(c['id'] == cid for c in story['cast']):
        raise ShiseiError(f'no character {cid} in this project')
    new = copy.deepcopy(story)
    c = next(x for x in new['cast'] if x['id'] == cid)
    keep_aside(d / voice_file(cid))
    if src is None:
        c.pop('voice_ref', None)
    else:
        shutil.copyfile(src, d / voice_file(cid))
        c['voice_ref'] = {'file': voice_file(cid), 'sha': file_sha(d / voice_file(cid))}
    return apply_story(d, story, new, why)


def cmd_drama_voice(args, cfg):
    """定声 for one project: one of the character's own voice tracks (vo_<shot>.wav) becomes the reference every take and
    voice track of that character is made with. The user's choice, on the Piying page."""
    series_user_only('fixing a voice')
    d, _ = drama_dir(args.jobdir)
    story = load_story(d)
    if args.clear:
        return {'cast': args.cast, 'voice_ref': None, **set_voice(d, args.cast, None, '取消定声')}
    if getattr(args, 'upload', None):
        if not any(c['id'] == args.cast for c in story['cast']):
            raise ShiseiError(f'no character {args.cast} in this project')
        src = voice_from_upload(args.upload, d, args.cast)
    else:
        src = voice_source(d, story, args.cast, args.file, getattr(args, 'shot', None))
    try:
        r = set_voice(d, args.cast, src, '定声：上传的录音' if getattr(args, 'upload', None) else f'定声：{args.file}')
    finally:
        if src.name.startswith('.voice_'):
            src.unlink(missing_ok=True)
    return {'cast': args.cast, 'voice_ref': next(c for c in load_story(d)['cast'] if c['id'] == args.cast)['voice_ref'], **r}


def episode_has_takes(d, story, cid):
    """Any take already made for a segment that shows this character (fixing the voice then would mean re-shooting)."""
    try:
        us = [u for u in units(story) if cid in u['cast']]
    except (KeyError, TypeError, ValueError):
        return False
    return any(next(Path(d).glob(f'take_{u["id"]}_*.mp4'), None) is not None for u in us if u['id'])


def cmd_series_voice(args, cfg):
    """定声 for a regular: one of its voice tracks in an episode becomes the series' recording. Episodes that have not
    shot this character yet use it at once; ones with takes keep their sound (drama-voice there re-shoots); new
    episodes get it with the pictures."""
    series_user_only('fixing a voice')
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        reg = next((x for x in s['cast'] if x['id'] == args.cast), None)
        if reg is None:
            raise ShiseiError(f'no regular {args.cast}')
        sp = series_path(args.series)
        dst = sp / voice_file(args.cast)
        if args.clear:
            keep_aside(dst)
            reg.pop('voice_fixed', None)
            save_series(args.series, s)
            return {**series_view(args.series, s), 'applied': [], 'skipped': []}
        if getattr(args, 'upload', None):   # a recording the user uploaded (saved by the page in the series folder)
            src = voice_from_upload(args.upload, sp, args.cast)
            frm = {'kind': 'upload'}
        else:
            d, job = job_of(args.jobdir)
            if job.get('series') != args.series:
                raise ShiseiError('this project is not an episode of this series')
            story = load_story(d)
            local = next((c['id'] for c in story['cast'] if (c.get('origin') or {}).get('series') == args.series
                          and c['origin'].get('id') == args.cast), None)
            if local is None:
                raise ShiseiError('定声只能用这个角色自己的一句独白或画外音（vo_*.wav）')
            src = voice_source(d, story, local, args.file, getattr(args, 'shot', None))
            # the shot too: several lines are cut out of one take, so the file alone does not say which line it was
            frm = {'job': d.name, 'file': args.file, **({'shot': args.shot} if getattr(args, 'shot', None) else {})}
        keep_aside(dst)
        shutil.copyfile(src, dst)
        if src.name.startswith('.voice_'):
            src.unlink(missing_ok=True)
        reg['voice_fixed'] = {'sha': file_sha(dst), 'from': frm, 'at': time.time()}
        save_series(args.series, s)
        episodes = list(s['episodes'])
    applied, skipped = [], []
    for e in episodes:
        ed = HOME / 'jobs' / e['job']
        try:
            ej = json.loads((ed / 'job.json').read_text())
        except (OSError, ValueError):
            continue
        if ej.get('archivedAt'):
            continue
        with review_lock(ed):
            st = episode_story(ed)
            if st is None:   # no script yet: the staged entry gets the recording
                staged = load_staged(ed)
                hit = False
                for x in staged:
                    o = x['entry'].get('origin') or {}
                    if x.get('kind') == 'cast' and o.get('series') == args.series and o.get('id') == args.cast:
                        shutil.copyfile(dst, ed / voice_file(x['entry']['id']))
                        x['entry']['voice_ref'] = {'file': voice_file(x['entry']['id']), 'sha': reg['voice_fixed']['sha']}
                        hit = True
                if hit:
                    save_json_atomic(ed / 'adopted.json', staged)
                    applied.append(e['n'])
                continue
            cid = next((c['id'] for c in st['cast'] if (c.get('origin') or {}).get('series') == args.series
                        and c['origin'].get('id') == args.cast), None)
            if cid is None:
                continue
            if episode_has_takes(ed, st, cid):
                skipped.append(e['n'])
                continue
            set_voice(ed, cid, dst, '系列定声')
            applied.append(e['n'])
    return {**series_view(args.series, load_series(args.series)), 'applied': applied, 'skipped': skipped}


def stage_regular(d, sid, c):
    """Bring a regular into a new episode: entry staged in adopted.json, pictures copied, and the approval the user gave
    carried over only for pictures whose bytes are the ones recorded."""
    sp = series_path(sid)
    entry = {k: c[k] for k in SERIES_CAST_FIELDS if k in c}
    entry['origin'] = {'kind': 'series', 'series': sid, 'id': c['id']}
    vsrc = sp / voice_file(c['id'])
    psrc = sp / photo_file(c['id'])
    if c.get('photo_ref') and psrc.is_file() and file_sha(psrc) == c['photo_ref'].get('sha'):   # the portrait is drawn after it
        shutil.copyfile(psrc, d / photo_file(c['id']))
        entry['photo_ref'] = dict(c['photo_ref'])
    if c.get('voice_fixed') and vsrc.is_file() and file_sha(vsrc) == c['voice_fixed'].get('sha'):   # the fixed voice comes along
        shutil.copyfile(vsrc, d / voice_file(c['id']))
        entry['voice_ref'] = {'file': voice_file(c['id']), 'sha': c['voice_fixed']['sha']}
    check_entry('cast', entry)
    review = load_review(d)
    carried = []
    for k, f in zip(('face', 'body'), adopt_files('cast', c['id'])):
        src = sp / f'cast_{c["id"]}_{k}.png'
        if not src.exists():
            continue
        shutil.copyfile(src, d / f)
        if file_sha(d / f) == (c.get('sha') or {}).get(k):
            review['assets'][f] = {'status': 'pass', 'note': '系列里审过', 'at': time.time(), 'series': sid}
            carried.append(f)
    save_json_atomic(d / 'review.json', review)
    staged = [x for x in load_staged(d) if x['entry']['id'] != c['id']]
    save_json_atomic(d / 'adopted.json', staged + [{'kind': 'cast', 'entry': entry, 'at': time.time()}])
    return carried


def cmd_series_episode_new(args, cfg):
    """A new episode: a drama project in the series with the chosen regulars (default: every regular not retired)
    already in it, their approved pictures carried over. The director then writes the script from the recap."""
    with series_lock(args.series):
        s = load_series(args.series)
        series_refuse_archived(s)
        regs = {c['id']: c for c in s['cast'] if not c.get('retired')}
        want = [x for x in (args.cast or '').split(',') if x] if args.cast is not None else list(regs)
        bad = [x for x in want if x not in regs]
        if bad:
            raise ShiseiError(f'not a regular of this series (or retired): {", ".join(bad)}')
        n = max([e['n'] for e in s['episodes']] + [s.get('lastN', 0)]) + 1
        title = (args.title or '').strip() or f'{s["title"]} 第 {n} 集'
        made = cmd_drama_new(argparse.Namespace(title=title, idea=args.idea or '', template=None,
                                                quality=args.quality, seeds=args.seeds), cfg)
        d = Path(made['dir'])
        set_job_series(d, args.series)
        s['episodes'].append({'job': d.name, 'n': n, 'counts': True, 'at': time.time()})
        s['lastN'] = n
        save_series(args.series, s)
    carried = {}
    with review_lock(d):
        for cid in want:
            carried[cid] = stage_regular(d, args.series, regs[cid])
    return {**made, 'series': args.series, 'n': n, 'cast': want, 'carried': carried,
            'next': 'the director writes story.json with these regulars (same ids, names, ages and look), then drama-set'}


# ---------- practice projects: finished scripts, copied into a new project (no pictures, nothing approved) ----------
TEMPLATES = ASSETS / 'templates'


def load_template(tid):
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', str(tid or '')) or not (TEMPLATES / f'{tid}.json').is_file():
        raise ShiseiError(f'unknown practice project {tid}')
    t = json.loads((TEMPLATES / f'{tid}.json').read_text())
    validate_story(strip_user_fields(copy.deepcopy(t['story'])))
    return t


def cmd_drama_templates(args, cfg):
    out = []
    for f in sorted(TEMPLATES.glob('*.json')):
        t = load_template(f.stem)
        st, us = t['story'], units(t['story'])
        out.append({'id': t['id'], 'title': t['title'], 'quality': t['quality'], 'seeds': t['seeds'],
                    'cast': len(st['cast']), 'segments': len(us), 'shots': len(st['shots']),
                    'seconds': round(sum(u['seconds'] for u in us), 1), 'script': (st.get('script') or '')[:200]})
    return {'templates': sorted(out, key=lambda x: (x['seconds'], x['id']))}


# commands that take a machine for themselves, and steps that work on the machine already taken
LEASE_CMDS = {'up', 'job-run', 'drama-run', 'drama-trial', 'gpu-qualify'}
QUOTE_CMDS = {'job-estimate', 'drama-estimate'}
ATTACH_CMDS = {'status', 'secure', 'tunnel', 'upload', 'submit', 'wait', 'pull', 'down', 'mannequin', 'estimate',
               'job-estimate', 'drama-estimate'}


ARCHIVED_SAY = '这个项目已归档，只能看：先在页面上点「恢复并继续」'


def archived_at(jobdir):
    """When the project in `jobdir` was archived on the page (job.json archivedAt), else None. An archived project
    takes no machine; only the user restores it, on the page."""
    if not jobdir:
        return None
    try:
        return json.loads((Path(jobdir) / 'job.json').read_text()).get('archivedAt') or None
    except (OSError, ValueError, AttributeError):
        return None


def holder_for(args):
    """Who leases the machine: the project (its folder name) for a run, else --holder, else SHISEI_HOLDER, else 'cli'."""
    if args.cmd == 'gpu-qualify':
        return 'qualify'
    jd = getattr(args, 'jobdir', None)
    if jd:
        return Path(jd).resolve().name
    h = getattr(args, 'holder', None) or os.environ.get('SHISEI_HOLDER') or 'cli'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', h):
        raise ShiseiError(f'bad holder {h!r}')
    return h


if __name__ == '__main__':
    sys.exit(main())
