"""A tiny ComfyUI stand-in that draws with ModelScope API-Inference instead of a GPU.

shisei.py's local mode talks to it exactly like a ComfyUI server (POST /prompt, /history, /view, /upload/image,
/object_info, /system_stats). It reads the drama workflows shisei.py sends and turns them into API calls:

  drama_image (TextEncodeQwenImage21)    no input images -> Qwen/Qwen-Image text-to-image
                                         1..4 images     -> Qwen/Qwen-Image-Edit with the images as data URLs
  drama_take  (MiniMaxH3ReferenceToVideo) -> MiniMax/MiniMax-H3 image-to-video from the first frame (keyframe)

ModelScope only takes first frames hosted on its own storage, so every picture this server makes remembers the URL
ModelScope returned; when shisei.py uploads the same bytes back as a keyframe, the URL is found by content hash.
Anything else (dance remake: motion capture, character swap) is refused with a clear message: that needs a 48 GB GPU.

Standard library only. Key: MODELSCOPE_API_KEY (or the `key` argument).
"""
import base64, hashlib, json, os, re, ssl, threading, time, urllib.parse, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = 'https://api-inference.modelscope.cn/v1'
MODELS = {'image': 'Qwen/Qwen-Image', 'edit': 'Qwen/Qwen-Image-Edit', 'video': 'MiniMax/MiniMax-H3'}
MAX_SIDE = 1664          # Qwen-Image accepts 64..1664 per side
VIDEO_SECONDS = (3, 5, 10)


def _ctx():
    ca = os.environ.get('SSL_CERT_FILE')
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


class ModelScope:
    def __init__(self, key, models=None, log=print):
        self.key, self.models, self.log = key, {**MODELS, **(models or {})}, log

    def _req(self, method, url, body=None, headers=None, timeout=120):
        h = {'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json', **(headers or {})}
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        for wait in (10, 30, 60, None):   # 5xx and dropped connections happen on busy afternoons: try again
            r = urllib.request.Request(url, data, h, method=method)
            try:
                with urllib.request.urlopen(r, timeout=timeout, context=_ctx()) as f:
                    return json.loads(f.read().decode())
            except urllib.error.HTTPError as e:
                msg = f'ModelScope {e.code}: {e.read().decode("utf-8", "replace")[:500]}'
                if 'insufficient balance' in msg:
                    raise RuntimeError('魔搭账号的余额或这个模型的免费额度不够了（insufficient balance）：到魔搭充值或等额度恢复，'
                                       '或在设置里换成自己的 ComfyUI') from None
                if e.code < 500 and e.code != 429 or wait is None:
                    raise RuntimeError(msg) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                msg = f'ModelScope unreachable: {getattr(e, "reason", e)}'
                if wait is None:
                    raise RuntimeError(msg) from None
            self.log(f'[modelscope] {msg[:120]}; retrying in {wait} s')
            time.sleep(wait)

    def _task(self, body, path, kind, limit):
        r = self._req('POST', f'{API}/{path}', body, {'X-ModelScope-Async-Mode': 'true'})
        if r.get('status') == 'FAILED' or 'task_id' not in r:
            raise RuntimeError(f'ModelScope refused: {r.get("errors") or r}')
        t0 = time.time()
        while time.time() - t0 < limit:
            d = self._req('GET', f'{API}/tasks/{r["task_id"]}', None, {'X-ModelScope-Task-Type': kind})
            st = d.get('task_status')
            if st == 'SUCCEED':
                return d
            if st in ('FAILED', 'CANCELED'):
                raise RuntimeError(f'ModelScope task {st}: {d.get("errors") or ""}')
            time.sleep(6)
        raise RuntimeError(f'ModelScope task timed out after {limit // 60} min')

    def image(self, prompt, size, images=(), seed=None):
        w, h = size
        k = min(1.0, MAX_SIDE / max(w, h))
        body = {'model': self.models['edit' if images else 'image'], 'prompt': prompt[:1990],
                'size': f'{int(w * k) // 16 * 16}x{int(h * k) // 16 * 16}'}
        if seed is not None:
            body['seed'] = int(seed) % (2 ** 31)
        if images:
            body['image_url'] = list(images)
        try:
            d = self._task(body, 'images/generations', 'image_generation', 480 if images else 900)
        except RuntimeError as e:
            if not images:
                raise
            # the edit model is the busier one: draw from the words alone rather than fail the whole run
            self.log(f'[modelscope] edit model failed ({str(e)[:100]}); drawing from the prompt alone')
            return self.image(prompt, size, (), seed)
        url = (d.get('output_images') or [None])[0]
        if not url:
            raise RuntimeError(f'no image in answer: {str(d)[:300]}')
        return url, self.get(url)

    def video(self, prompt, first_frame_url, size, seconds, seed=None):
        dur = next((s for s in VIDEO_SECONDS if s >= seconds - 0.5), VIDEO_SECONDS[-1])
        body = {'model': self.models['video'], 'prompt': prompt[:1990], 'size': f'{size[0]}x{size[1]}', 'duration': dur,
                'media': [{'type': 'image', 'url': first_frame_url}]}
        if seed is not None:
            body['seed'] = int(seed) % (2 ** 31)
        d = self._task(body, 'videos/generations', 'video_generation', 1800)
        url = (d.get('outputs') or {}).get('url')
        if not url:
            raise RuntimeError(f'no video in answer: {str(d)[:300]}')
        data = self.get(url)
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            raise RuntimeError('ModelScope content review refused this shot (it answered with a placeholder picture); '
                               'real-person faces are refused, drawn characters usually pass')
        return url, data

    def get(self, url):
        with urllib.request.urlopen(url, timeout=600, context=_ctx()) as f:
            return f.read()


class Shim:
    """State of the stand-in server: its folder (input/, output/), the queue and the hash -> ModelScope URL map."""
    def __init__(self, root, ms):
        self.root, self.ms = Path(root), ms
        for sub in ('input', 'output'):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.urls_file = self.root / 'urls.json'
        self.urls = json.loads(self.urls_file.read_text()) if self.urls_file.exists() else {}
        self.hist, self.lock = {}, threading.Lock()

    def remember(self, data, url):
        with self.lock:
            self.urls[hashlib.sha256(data).hexdigest()] = url
            self.urls_file.write_text(json.dumps(self.urls))

    def url_of(self, path):
        return self.urls.get(hashlib.sha256(Path(path).read_bytes()).hexdigest())

    def data_url(self, name):
        p = self.root / 'input' / name
        mime = 'image/png' if p.suffix.lower() == '.png' else 'image/jpeg'
        return f'data:{mime};base64,' + base64.b64encode(p.read_bytes()).decode()

    def submit(self, api):
        pid = uuid.uuid4().hex
        self.hist[pid] = {'status': {'status_str': 'running', 'completed': False, 'messages': []}, 'outputs': {}}
        threading.Thread(target=self._run, args=(pid, api), daemon=True).start()
        return pid

    def _run(self, pid, api):
        try:
            node, item = self._make(api)
            self.hist[pid] = {'status': {'status_str': 'success', 'completed': True, 'messages': []},
                              'outputs': {node: item}}
        except Exception as e:   # noqa: BLE001 - every failure goes back to shisei.py as a ComfyUI execution error
            self.ms.log(f'[modelscope] failed: {e}')
            self.hist[pid] = {'status': {'status_str': 'error', 'completed': True, 'messages': [
                ['execution_error', {'exception_message': str(e)[:600], 'node_type': 'ModelScope'}]]}, 'outputs': {}}

    def _save(self, prefix, ext, data):
        sub, _, stem = prefix.rpartition('/')
        folder = self.root / 'output' / sub
        folder.mkdir(parents=True, exist_ok=True)
        n = 1
        while (folder / f'{stem}_{n:05d}_.{ext}').exists():
            n += 1
        name = f'{stem}_{n:05d}_.{ext}'
        (folder / name).write_bytes(data)
        return sub, name

    def _make(self, api):
        by = {k: v for k, v in api.items() if isinstance(v, dict)}
        kinds = {v.get('class_type') for v in by.values()}
        if 'TextEncodeQwenImage21' in kinds:
            enc = next(v for v in by.values() if v['class_type'] == 'TextEncodeQwenImage21')['inputs']
            lat = next(v for v in by.values() if v['class_type'] == 'EmptyLatentImage')['inputs']
            seed = next((v['inputs'].get('seed') for v in by.values() if v['class_type'] == 'KSampler'), None)
            save_id, save = next((k, v) for k, v in by.items() if v['class_type'] == 'SaveImage')
            imgs = [by[enc[k][0]]['inputs']['image'] for k in sorted(enc) if k.startswith('images.image_')]
            self.ms.log(f'[modelscope] picture {save["inputs"]["filename_prefix"]} ({len(imgs)} reference pictures)')
            url, data = self.ms.image(enc['prompt'], (int(lat['width']), int(lat['height'])),
                                      [self.data_url(i) for i in imgs], seed)
            self.remember(data, url)
            sub, name = self._save(save['inputs']['filename_prefix'], 'png', data)
            return save_id, {'images': [{'filename': name, 'subfolder': sub, 'type': 'output'}]}
        if 'MiniMaxH3ReferenceToVideo' in kinds:
            r = next(v for v in by.values() if v['class_type'] == 'MiniMaxH3ReferenceToVideo')['inputs']
            guide = next(v for v in by.values() if v['class_type'] == 'MiniMaxH3AddGuide')['inputs']
            kf = by[guide['image'][0]]['inputs']['image']
            seed = next((v['inputs'].get('noise_seed') for v in by.values() if v['class_type'] == 'RandomNoise'), None)
            save_id, save = next((k, v) for k, v in by.items() if v['class_type'] == 'SaveVideo')
            url = self.url_of(self.root / 'input' / kf)
            if not url:
                # a first frame not drawn here (a close-up cut on this computer): give it to the edit model once so it
                # lives on ModelScope's storage, unchanged
                self.ms.log(f'[modelscope] hosting first frame {kf} through the edit model')
                url, data = self.ms.image('Keep this picture exactly as it is. Do not change anything.',
                                          (576, 1024), [self.data_url(kf)])
                self.remember(data, url)
            prompt = re.sub(r'<(Picture|Audio) \d+>', '', r['prompt'])
            seconds = int(r['length']) / 24
            self.ms.log(f'[modelscope] video {save["inputs"]["filename_prefix"]} ({seconds:.1f} s)')
            _, data = self.ms.video(prompt, url, (int(r['width']), int(r['height'])), seconds, seed)
            sub, name = self._save(save['inputs']['filename_prefix'], 'mp4', data)
            return save_id, {'images': [{'filename': name, 'subfolder': sub, 'type': 'output'}], 'animated': [True]}
        raise RuntimeError('this step needs a real GPU (dance remake: motion capture and character swap); '
                           'the ModelScope backend only draws short-drama pictures and takes')


def handler(shim):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == '/system_stats':
                return self._json({'system': {'comfyui_version': 'piying-modelscope'}, 'devices': [{'name': 'ModelScope API'}]})
            if u.path.startswith('/object_info/'):
                c = u.path.rsplit('/', 1)[1]
                return self._json({c: {}})
            m = re.fullmatch(r'/history/(\w+)', u.path)
            if m:
                h = shim.hist.get(m[1])
                return self._json({m[1]: h} if h else {})
            if u.path == '/view':
                q = dict(urllib.parse.parse_qsl(u.query))
                base = (shim.root / q.get('type', 'output')).resolve()
                p = (base / q.get('subfolder', '') / q['filename']).resolve()
                if not str(p).startswith(str(base)) or not p.is_file():
                    return self._json({'error': 'not found'}, 404)
                b = p.read_bytes()
                self.send_response(200)
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            self._json({'error': 'not found'}, 404)

        def do_POST(self):
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n)
            if self.path == '/prompt':
                return self._json({'prompt_id': shim.submit(json.loads(body)['prompt']), 'number': 0, 'node_errors': {}})
            if self.path == '/upload/image':
                ct = self.headers.get('Content-Type', '')
                b = ct.split('boundary=')[-1].encode()
                sub, name, data = '', None, None
                for part in body.split(b'--' + b):
                    head, _, val = part.partition(b'\r\n\r\n')
                    val = val[:-2] if val.endswith(b'\r\n') else val
                    if b'name="subfolder"' in head:
                        sub = val.decode()
                    m = re.search(rb'filename="([^"]+)"', head)
                    if m:
                        name, data = m[1].decode(), val
                if not name or '..' in name or '..' in sub:
                    return self._json({'error': 'bad upload'}, 400)
                d = shim.root / 'input' / sub
                d.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(data)
                return self._json({'name': name, 'subfolder': sub, 'type': 'input'})
            self._json({'error': 'not found'}, 404)
    return H


def serve(root, port=8190, key=None, models=None, log=print):
    key = key or os.environ.get('MODELSCOPE_API_KEY')
    if not key:
        raise SystemExit('MODELSCOPE_API_KEY is not set')
    shim = Shim(root, ModelScope(key, models, log))
    srv = ThreadingHTTPServer(('127.0.0.1', port), handler(shim))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f'[modelscope] ComfyUI stand-in on http://127.0.0.1:{port}')
    return srv


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--port', type=int, default=8190)
    ap.add_argument('--root', default=str(Path.home() / '.dsh' / 'shisei' / 'modelscope'))
    a = ap.parse_args()
    serve(a.root, a.port)
    while True:
        time.sleep(3600)
