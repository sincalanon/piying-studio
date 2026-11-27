#!/usr/bin/env python3
"""皮影 Piying Studio, standalone: the short-drama workflow in your browser, no dsh needed.

    python3 studio/app.py                     # opens http://127.0.0.1:7860
    python3 studio/app.py --port 7861 --no-browser

Needs Python 3.9+ and ffmpeg. Pictures and takes come from one of two backends, chosen on the Settings page:
  modelscope  ModelScope API-Inference with your access token (no GPU): Qwen-Image pictures, MiniMax-H3 takes
  comfyui     your own ComfyUI (comfy_url, optional comfy_dir); the takes need a 48 GB card
The director (script writing and revisions) is an OpenAI-compatible chat model, ModelScope by default.
Everything runs through skills/shisei-motion/scripts/shisei.py; projects live in ~/.dsh/shisei/jobs like the plugin's.
Standard library only.
"""
import argparse, json, mimetypes, os, re, subprocess, sys, threading, time, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / 'skills' / 'shisei-motion' / 'scripts' / 'shisei.py'
BRAND = ROOT / 'assets' / 'brand'
HOME = Path(os.environ.get('SHISEI_HOME', '~/.dsh/shisei')).expanduser()
CONF = HOME / 'piying_studio.json'
sys.path.insert(0, str(HERE))
import director as D  # noqa: E402
import ms_comfy  # noqa: E402

DEFAULTS = {'backend': 'modelscope', 'comfy_url': 'http://127.0.0.1:8188', 'comfy_dir': '',
            'base_url': 'https://api-inference.modelscope.cn/v1', 'director_model': 'Qwen/Qwen3.5-122B-A10B',
            'vision_model': 'Qwen/Qwen3.5-35B-A3B', 'key': '', 'shim_port': 8190}
TASKS = {}          # job id -> {kind, running, log, error, started, ended}
LOCK = threading.Lock()
SHIM = {'srv': None}


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, flush=True)


def load_conf():
    c = dict(DEFAULTS)
    if CONF.exists():
        c.update(json.loads(CONF.read_text(encoding='utf-8')))
    return c


def save_conf(c):
    HOME.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding='utf-8')
    try:
        os.chmod(CONF, 0o600)
    except OSError:
        pass


def api_key(c):
    return os.environ.get('MODELSCOPE_API_KEY') or c.get('key') or ''


def env_for(c):
    e = dict(os.environ, SHISEI_HOME=str(HOME), PYTHONIOENCODING='utf-8')
    e.pop('DSH_SHISEI_BRIDGE', None)   # this page is the user's own hands, like the plugin page
    if api_key(c):
        e['MODELSCOPE_API_KEY'] = api_key(c)
    return e


def shisei(args, c=None, timeout=600):
    c = c or load_conf()
    r = subprocess.run([sys.executable, '-I', str(SCRIPT)] + [str(a) for a in args], capture_output=True, text=True,
                       encoding='utf-8', env=env_for(c), timeout=timeout)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {'ok': False, 'error': (r.stdout + r.stderr).strip()[-800:] or f'exit {r.returncode}'}


def apply_backend(c):
    """Point shisei.py at the chosen backend (its local mode) and the vision model at the same key."""
    if c['backend'] == 'modelscope':
        if api_key(c) and SHIM['srv'] is None:
            SHIM['srv'] = ms_comfy.serve(HOME / 'modelscope', int(c['shim_port']), api_key(c), log=log)
        shisei(['config', 'set', 'comfy_url', f'http://127.0.0.1:{c["shim_port"]}'], c)
        shisei(['config', 'set', 'comfy_dir', ''], c)
    else:
        shisei(['config', 'set', 'comfy_url', c['comfy_url']], c)
        shisei(['config', 'set', 'comfy_dir', c.get('comfy_dir') or ''], c)
    if api_key(c):
        for k, v in (('vision_base_url', c['base_url']), ('vision_model', c['vision_model']),
                     ('vision_key_env', 'MODELSCOPE_API_KEY'), ('vision_source', 'key')):
            shisei(['config', 'set', k, v], c)


# ---------------------------------------------------------------- jobs

def job_dir(jid):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', jid or ''):
        raise ValueError('bad project id')
    d = HOME / 'jobs' / jid
    if not (d / 'job.json').exists():
        raise ValueError('no such project')
    return d


def list_jobs():
    out = []
    for p in sorted((HOME / 'jobs').glob('*/job.json'), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            j = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if j.get('kind') != 'drama' or j.get('archivedAt'):
            continue
        out.append({'id': p.parent.name, 'title': j.get('title') or p.parent.name, 'idea': j.get('idea') or j.get('prompt', ''),
                    'has_story': (p.parent / 'story.json').exists(), 'final': (p.parent / 'final.mp4').exists(),
                    'running': bool(TASKS.get(p.parent.name, {}).get('running'))})
    return out


def task_view(jid):
    t = TASKS.get(jid)
    return None if not t else {k: t[k] for k in ('kind', 'running', 'error', 'started', 'ended')} | {'log': t['log'][-40:]}


def start_task(jid, kind, fn):
    with LOCK:
        t = TASKS.get(jid)
        if t and t['running']:
            raise ValueError('这个项目已经有任务在跑了')
        t = TASKS[jid] = {'kind': kind, 'running': True, 'log': [], 'error': None, 'started': time.time(), 'ended': None}

    def say(m):
        t['log'].append(f'{time.strftime("%H:%M:%S")} {m}')
        log(f'[{jid}] {m}')

    def go():
        try:
            r = fn(say)
            if isinstance(r, dict) and not r.get('ok', True):
                t['error'] = r.get('error') or '失败'
                say(f'失败：{t["error"][:300]}')
            else:
                say('完成。')
        except Exception as e:  # noqa: BLE001
            t['error'] = str(e)
            say(f'出错：{e}')
        finally:
            t['running'], t['ended'] = False, time.time()
    threading.Thread(target=go, daemon=True).start()


def run_stage(jid, stage, say):
    c = load_conf()
    d = job_dir(jid)
    say({'assets': '画演员、场景、道具…', 'keyframes': '画首帧…', 'takes': '拍视频…'}[stage])
    p = subprocess.Popen([sys.executable, '-I', str(SCRIPT), 'drama-run', str(d), stage, '--confirm-billing'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env_for(c))
    def pump():
        for line in p.stderr:
            line = re.sub(r'^\[shisei [\d:]+\]\s*', '', line.strip())
            if line:
                say(line[:300])
    th = threading.Thread(target=pump, daemon=True)
    th.start()
    out = p.stdout.read()
    p.wait()
    th.join(timeout=2)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {'ok': False, 'error': out[-500:] or f'exit {p.returncode}'}


def director(c):
    if not api_key(c):
        raise ValueError('先在「设置」里填魔搭访问令牌')
    return D.Director(lambda a: shisei(a, c), c['director_model'], c['base_url'], api_key(c))


# ---------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    server_version = 'Piying'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _file(self, path):
        path = Path(path)
        if not path.is_file():
            return self._send(404, {'error': 'not found'})
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        rng = re.match(r'bytes=(\d*)-(\d*)', self.headers.get('Range') or '')
        with open(path, 'rb') as f:
            if rng and ctype.startswith('video'):
                a = int(rng[1] or 0)
                b = int(rng[2]) if rng[2] else size - 1
                f.seek(a)
                data = f.read(b - a + 1)
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {a}-{b}/{size}')
            else:
                data = f.read()
                self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if u.path in ('/', '/index.html'):
                return self._send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if u.path.startswith('/brand/'):
                return self._file(BRAND / Path(u.path).name)
            if u.path.startswith('/media/'):
                _, _, jid, name = u.path.split('/', 3)
                name = urllib.parse.unquote(name)
                if '..' in name or name.startswith('/'):
                    return self._send(400, {'error': 'bad path'})
                return self._file(job_dir(jid) / name)
            if u.path == '/api/state':
                c = load_conf()
                return self._send(200, {'config': {k: v for k, v in c.items() if k != 'key'}, 'key_set': bool(api_key(c)),
                                        'jobs': list_jobs(), 'home': str(HOME)})
            if u.path == '/api/templates':
                return self._send(200, shisei(['drama-templates']))
            if u.path == '/api/check':
                return self._send(200, shisei(['tunnel']))
            if u.path == '/api/job':
                d = job_dir(q['id'])
                story = json.loads((d / 'story.json').read_text(encoding='utf-8')) if (d / 'story.json').exists() else None
                job = json.loads((d / 'job.json').read_text(encoding='utf-8'))
                status = shisei(['drama-status', d]) if story else {'stage': 'script'}
                files = sorted(p.name for p in d.iterdir() if p.is_file())
                return self._send(200, {'id': q['id'], 'job': job, 'story': story, 'status': status, 'files': files,
                                        'task': task_view(q['id'])})
            return self._send(404, {'error': 'not found'})
        except (ValueError, KeyError) as e:
            return self._send(400, {'error': str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            b = self._body()
            c = load_conf()
            if u.path == '/api/config':
                for k in ('backend', 'comfy_url', 'comfy_dir', 'director_model', 'vision_model', 'base_url'):
                    if k in b:
                        c[k] = str(b[k]).strip()
                if b.get('key'):
                    c['key'] = b['key'].strip()
                save_conf(c)
                apply_backend(c)
                return self._send(200, {'ok': True})
            if u.path == '/api/new':
                args = ['drama-new']
                if b.get('template'):
                    args += ['--template', b['template']]
                else:
                    if not b.get('title') or not b.get('idea'):
                        raise ValueError('片名和一句话故事都要填')
                    args += ['--title', b['title'], '--idea', b['idea']]
                args += ['--seeds', str(int(b.get('seeds') or 1))]
                r = shisei(args, c)
                if r.get('ok') and not b.get('template'):
                    jid = r['job']
                    dr = director(c)
                    start_task(jid, 'write', lambda say: (setattr(dr, 'log', say), dr.write(job_dir(jid), b['title'], b['idea']))[1])
                return self._send(200, r)
            if u.path == '/api/revise':
                jid, text = b['job'], (b.get('text') or '').strip()
                if not text:
                    raise ValueError('说说哪里要改')
                dr = director(c)
                start_task(jid, 'revise', lambda say: (say(f'你：{text}'), setattr(dr, 'log', say), dr.revise(job_dir(jid), text))[2])
                return self._send(200, {'ok': True})
            if u.path == '/api/run':
                jid, stage = b['job'], b['stage']
                if stage not in ('assets', 'keyframes', 'takes'):
                    raise ValueError('bad stage')
                start_task(jid, stage, lambda say: run_stage(jid, stage, say))
                return self._send(200, {'ok': True})
            if u.path == '/api/review':
                args = ['drama-review', job_dir(b['job']), b['kind'], b['key'], str(b['value'])]
                if b.get('note'):
                    args += ['--note', b['note']]
                return self._send(200, shisei(args, c))
            if u.path == '/api/cut':
                jid = b['job']
                start_task(jid, 'cut', lambda say: (say('本机合成成片…'), shisei(['drama-cut', job_dir(jid)], c, timeout=1800))[1])
                return self._send(200, {'ok': True})
            if u.path == '/api/stop':
                (job_dir(b['job']) / 'stop').write_text('stop')
                return self._send(200, {'ok': True})
            return self._send(404, {'error': 'not found'})
        except (ValueError, KeyError) as e:
            return self._send(400, {'ok': False, 'error': str(e)})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--port', type=int, default=7860)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    HOME.mkdir(parents=True, exist_ok=True)
    c = load_conf()
    apply_backend(c)
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), H)
    url = f'http://127.0.0.1:{a.port}/'
    log(f'皮影 Piying Studio: {url}  (projects in {HOME / "jobs"})')
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
