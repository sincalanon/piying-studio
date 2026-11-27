"""Local mode: run Piying against the user's own ComfyUI instead of a rented compshare machine.

Turned on by config `comfy_url` (or env SHISEI_COMFY_URL), e.g.

    python3 -I shisei.py config set comfy_url http://127.0.0.1:8188
    python3 -I shisei.py config set comfy_dir "D:/ComfyUI"          # the folder holding input/ output/ models/

What changes (shisei.py's own functions are replaced for this process only; nothing else is touched):
  machine     one machine called "local": no compshare keys, no boot, no scheduled stop, price 0 (budgets stay 0)
  ComfyUI     comfy_url instead of the ssh tunnel on 127.0.0.1:18188
  files       with comfy_dir: plain copies into <comfy_dir>/input and out of <comfy_dir>/output
              without it (ComfyUI on another computer): POST /upload/image and GET /view, outputs found through
              /history of the prompts this process queued
  models      the drama/remake model check looks in <comfy_dir>/models (and extra_model_paths you add with
              comfy_model_roots); missing files are reported, never downloaded
  remote sh   steps that ran a shell on the machine run locally with bash when comfy_dir is set (macOS/Linux,
              Git Bash on Windows); without bash they fail with a clear message
Leases still work, so two projects never drive the same ComfyUI at once.
"""
import fnmatch, json, mimetypes, os, re, shutil, subprocess, time, urllib.parse, urllib.request, uuid
from pathlib import Path

LOCAL_ID = 'local'


def install(g, cfg):
    """Swap shisei.py's machine, file and tunnel functions (module globals `g`) for local ones."""
    ShiseiError = g['ShiseiError']
    log = g['log']
    url = (os.environ.get('SHISEI_COMFY_URL') or cfg.get('comfy_url') or 'http://127.0.0.1:8188').rstrip('/')
    cdir = os.environ.get('SHISEI_COMFY_DIR') or cfg.get('comfy_dir') or ''
    cdir = Path(cdir).expanduser() if cdir else None
    if cdir is not None and not (cdir / 'input').is_dir():
        raise ShiseiError(f'comfy_dir {cdir} has no input/ folder; point it at the ComfyUI folder (the one with main.py)')
    roots = [Path(r).expanduser() for r in (cfg.get('comfy_model_roots') or [])]
    queued = []   # prompt ids this process sent, for finding outputs without comfy_dir
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never send localhost through HTTP_PROXY

    cfg['instance_id'] = LOCAL_ID
    cfg['_saved_instance'] = LOCAL_ID
    g['LOCAL_PORT'] = 0
    g['CURRENT_MACHINE'] = LOCAL_ID
    g['REMOTE_COMFY'] = str(cdir) if cdir else '/ComfyUI'

    info = {'UHostId': LOCAL_ID, 'Name': 'local ComfyUI', 'State': 'Running', 'InstancePrice': 0, 'GpuType': 'local',
            'Zone': 'local', 'StartTime': int(time.time())}

    def get(path, timeout=20):
        return opener.open(url + path, timeout=timeout)

    # ---- machine ----------------------------------------------------------------------------------------------
    def comfy_base():
        return url

    def machine_ids(cfg_):
        return [LOCAL_ID]

    def use_machine(cfg_, iid, save=True):
        cfg_['instance_id'] = LOCAL_ID
        g['CURRENT_MACHINE'] = LOCAL_ID
        return LOCAL_ID

    def pick_machine(cfg_, holder, explicit=None, prefer=None):
        return use_machine(cfg_, LOCAL_ID)

    def wait_for_machine(cfg_, minutes, show, explicit=None):
        use_machine(cfg_, LOCAL_ID)
        g['ensure_claim'](cfg_)
        return LOCAL_ID

    def need_instance(cfg_):
        return LOCAL_ID

    def instance_info(cfg_):
        return dict(info)

    def instance_state(cfg_):
        return 'Running'

    def cs(*args, sensitive=False):
        raise ShiseiError(f'local mode: compshare is not used (asked for: {" ".join(map(str, args[:2]))})')

    def schedule_stop(cfg_, at, from_keeper=False):
        return at   # nothing bills, nothing to stop; leases are released when the run ends

    def cmd_up(args, cfg_):
        tunnel(None, cfg_)
        return {'state': 'Running', 'local': True, 'comfy_url': url}

    def cmd_down(args, cfg_):
        return {'stopped': False, 'local': True, 'note': 'local ComfyUI is left running'}

    def tunnel(args, cfg_):
        if args is not None and getattr(args, 'close', False):
            return {'tunnel': 'closed'}
        try:
            get('/system_stats', timeout=8).read()
        except Exception as e:
            raise ShiseiError(f'local ComfyUI does not answer at {url} ({getattr(e, "reason", e)}); start it first')
        return {'tunnel': url}

    def vram_watch_start(cfg_, info_):
        return None

    # ---- files ------------------------------------------------------------------------------------------------
    def upload(local, remote_name):
        local = Path(local)
        if cdir:
            dst = cdir / 'input' / remote_name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, dst)
            return
        sub, name = (remote_name.rsplit('/', 1) if '/' in remote_name else ('', remote_name))
        b = uuid.uuid4().hex
        mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
        body = b''.join([
            f'--{b}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="subfolder"\r\n\r\n{sub}\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),
            local.read_bytes(), f'\r\n--{b}--\r\n'.encode()])
        req = urllib.request.Request(url + '/upload/image', body, {'Content-Type': f'multipart/form-data; boundary={b}'})
        try:
            opener.open(req, timeout=300).read()
        except Exception as e:
            raise ShiseiError(f'upload of {local.name} to {url} failed ({getattr(e, "reason", e)})')

    def outputs_via_history():
        """[(folder, subfolder/filename)] for every file the prompts of this process wrote."""
        out = []
        for pid in queued:
            try:
                h = json.load(get(f'/history/{pid}')).get(pid) or {}
            except Exception:
                continue
            for node in (h.get('outputs') or {}).values():
                for key in ('images', 'gifs', 'videos', 'audio', 'files'):
                    for f in node.get(key) or []:
                        if isinstance(f, dict) and f.get('filename') and f.get('type', 'output') == 'output':
                            out.append('/'.join(x for x in (f.get('subfolder'), f['filename']) if x))
        return out

    def remote_ls(cfg_, info_, folder, pattern):
        pattern = g['safe_pattern'](pattern)
        if cdir:
            base = cdir / folder
            return sorted(str(p.relative_to(base)).replace(os.sep, '/') for p in base.glob(pattern))
        if folder != 'output':
            raise ShiseiError('local mode without comfy_dir can only list output/ (set comfy_dir)')
        return [n for n in outputs_via_history() if fnmatch.fnmatch(n, pattern)]

    def pull_one(cfg_, info_, remote_rel, local):
        local = Path(local)
        if local.is_dir():
            local = local / Path(remote_rel).name
        if cdir:
            shutil.copyfile(cdir / remote_rel, local)
            return
        folder, _, rel = remote_rel.partition('/')
        sub, _, name = rel.rpartition('/')
        q = urllib.parse.urlencode({'filename': name, 'subfolder': sub, 'type': folder})
        with get(f'/view?{q}', timeout=600) as r, open(local, 'wb') as f:
            shutil.copyfileobj(r, f)

    def cp_to_input(cfg_, info_, out_rel, remote_name):
        if cdir:
            dst = cdir / 'input' / remote_name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cdir / 'output' / out_rel, dst)
            return
        tmp = Path(g['HOME']) / 'tmp'
        tmp.mkdir(parents=True, exist_ok=True)
        t = tmp / Path(out_rel).name
        pull_one(cfg_, info_, f'output/{out_rel}', t)
        upload(t, remote_name)
        t.unlink(missing_ok=True)

    def cmd_upload(args, cfg_):
        for f in args.files:
            if not Path(f).is_file():
                raise ShiseiError(f'not a file: {f}')
            upload(f, Path(f).name)
        return {'uploaded': [Path(f).name for f in args.files]}

    def cmd_pull(args, cfg_):
        names = remote_ls(cfg_, info, args.dir, args.pattern)
        if not names:
            raise ShiseiError(f'nothing matches {args.pattern} in {args.dir}/')
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for n in names:
            pull_one(cfg_, info, f'{args.dir}/{n}', out / Path(n).name)
        return {'pulled': [str(out / Path(n).name) for n in names]}

    # ---- prompts ------------------------------------------------------------------------------------------------
    orig_post = g['post_prompt']

    def post_prompt(api, reopen=True):
        body = json.dumps({'prompt': api, 'client_id': g['client_id']()}).encode()
        req = urllib.request.Request(url + '/prompt', body, {'Content-Type': 'application/json'})
        try:
            pid = json.load(opener.open(req, timeout=30))['prompt_id']
        except urllib.error.HTTPError as e:
            raise ShiseiError(f'ComfyUI rejected the prompt ({e.code}): {e.read().decode()[:1500]}')
        except Exception as e:
            raise ShiseiError(f'ComfyUI not reachable on {url} ({getattr(e, "reason", e)})')
        queued.append(pid)
        return pid

    def history(pid):
        return json.load(get(f'/history/{pid}')).get(pid)

    def object_info_missing(classes):
        missing = []
        for c in classes:
            try:
                d = json.load(get(f'/object_info/{c}'))
            except Exception as e:
                raise ShiseiError(f'ComfyUI not reachable for preflight: {e}')
            if c not in d:
                missing.append(c)
        return missing

    # ---- shell steps ----------------------------------------------------------------------------------------------
    def models_report(items):
        missing = []
        bases = ([cdir / 'models'] if cdir else []) + roots
        for item in items:
            sub, name = item.split('/', 1)
            if not any((b / sub / name).exists() for b in bases):
                missing.append(item)
        return 'missing:' + ''.join(' ' + m for m in missing)

    def ssh_run(cfg_, info_, script, check=True, timeout=600, waits=(5, 15, 30), sleep=None):
        m = re.search(r'for item in (.*?); do', script)
        if 'missing:$missing' in script and m:   # REMOTE_ENSURE_MODELS
            if not cdir and not roots:
                log('local mode: model files not checked (set comfy_dir)')
                return 'missing:'
            return models_report(m.group(1).split())
        if not cdir:
            raise ShiseiError('local mode: this step runs a shell next to ComfyUI; set comfy_dir (same computer)')
        bash = shutil.which('bash')
        if not bash:
            raise ShiseiError('local mode: this step needs bash (macOS/Linux, or Git Bash on Windows)')
        script = script.replace('/root/ComfyUI', str(cdir).replace('\\', '/'))
        r = subprocess.run([bash, '-c', script], capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise ShiseiError(f'local shell step failed ({r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}')
        return r.stdout

    def scp_run(argv, *a, **k):
        raise ShiseiError('local mode: scp is not used')

    def in_patrol(*a, **k):
        return {'machines': [{'id': LOCAL_ID, 'name': 'local ComfyUI', 'state': 'Running', 'free': True}]}

    for name, fn in {
        'comfy_base': comfy_base, 'machine_ids': machine_ids, 'use_machine': use_machine, 'pick_machine': pick_machine,
        'attach_machine': lambda cfg_, holder, explicit=None: use_machine(cfg_, LOCAL_ID),
        'quote_machine': lambda cfg_, holder, explicit=None: use_machine(cfg_, LOCAL_ID),
        'wait_for_machine': wait_for_machine, 'need_instance': need_instance, 'instance_info': instance_info,
        'instance_state': instance_state, 'cs': cs, 'schedule_stop': schedule_stop, 'cmd_up': cmd_up, 'cmd_down': cmd_down,
        'cmd_tunnel': tunnel, 'vram_watch_start': vram_watch_start, '_upload_as': lambda cfg_, info_, local, rn: upload(local, rn),
        '_pull_one': pull_one, '_remote_ls': remote_ls, '_remote_cp_to_input': cp_to_input, 'cmd_upload': cmd_upload,
        'cmd_pull': cmd_pull, 'post_prompt': post_prompt, 'history': history, '_object_info_missing': object_info_missing,
        'ssh_run': ssh_run, 'scp_run': scp_run, 'refresh_registry': lambda cfg_: None,
    }.items():
        if name in g or name in ('comfy_base',):
            g[name] = fn
    g['LOCAL_MODE'] = {'comfy_url': url, 'comfy_dir': str(cdir) if cdir else None}
    log(f'local mode: ComfyUI at {url}' + (f', folder {cdir}' if cdir else ' (files over HTTP)'))
    del orig_post, in_patrol
