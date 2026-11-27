"""The director without dsh: an OpenAI-compatible chat model (ModelScope by default) writes and revises story.json.

It is given the script rules and the story.json format from skills/shisei-drama/SKILL.md, answers with one JSON
object, and shisei.py's drama-set checks it. When drama-set lists problems, they go back to the model (at most
three rounds), the same loop the dsh director runs.
"""
import json, os, re, ssl, tempfile, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent / 'skills' / 'shisei-drama' / 'SKILL.md'
SECTIONS = ('## 写剧本的规矩', '## story.json 格式', '## 写好镜头的经验')


def rules():
    text = SKILL.read_text(encoding='utf-8')
    out = []
    for head in SECTIONS:
        i = text.find(head)
        if i < 0:
            continue
        j = text.find('\n## ', i + len(head))
        out.append(text[i:j if j > 0 else len(text)].strip())
    return '\n\n'.join(out)


SYSTEM = """你是「皮影」短剧的导演，负责写剧本和分镜。只输出一个 JSON 对象（story.json），不要任何解释、不要 markdown 代码块。
硬性要求：
- 角色都是成年人（age ≥ 18），look / keyframe / action 里不写裸露、性、暴露衣着。
- 每段 2–12 秒、同一场景、最多 2 个角色；每镜场景 + 角色 + 道具合计最多 3 张图。
- action 和 delivery 用英文，keyframe、look、台词用中文；action 里提到角色用 name_en。
- 必须写 segments，覆盖全部镜头。
- 总时长按用户要求，没说就控制在 20–40 秒。

{rules}
"""


def _ctx():
    ca = os.environ.get('SSL_CERT_FILE')
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


def chat(messages, model, base_url, key, timeout=600):
    body = {'model': model, 'messages': messages, 'temperature': 0.7, 'enable_thinking': False}
    def send(b):
        r = urllib.request.Request(base_url.rstrip('/') + '/chat/completions', json.dumps(b).encode(),
                                   {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(r, timeout=timeout, context=_ctx()) as f:
            return json.loads(f.read().decode())
    try:
        d = send(body)
    except urllib.error.HTTPError as e:
        if e.code == 400:   # a server that does not take enable_thinking
            body.pop('enable_thinking')
            d = send(body)
        else:
            raise RuntimeError(f'director model {e.code}: {e.read().decode("utf-8", "replace")[:400]}') from None
    return d['choices'][0]['message']['content'] or ''


def extract_json(text):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S).strip()
    m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
    if m:
        text = m.group(1)
    i, j = text.find('{'), text.rfind('}')
    if i < 0 or j < i:
        raise ValueError('the director did not answer with JSON')
    return json.loads(text[i:j + 1])


class Director:
    def __init__(self, run_shisei, model, base_url, key, log=print):
        self.run, self.model, self.base, self.key, self.log = run_shisei, model, base_url, key, log

    def _set(self, jobdir, story, why=None):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump(story, f, ensure_ascii=False, indent=1)
            path = f.name
        try:
            return self.run(['drama-set', str(jobdir), path] + (['--why', why] if why else []))
        finally:
            os.unlink(path)

    def _loop(self, jobdir, messages, why=None, rounds=4):
        last = None
        for n in range(rounds):
            self.log(f'导演在写（第 {n + 1} 轮）…')
            answer = chat(messages, self.model, self.base, self.key)
            messages.append({'role': 'assistant', 'content': answer})
            try:
                story = extract_json(answer)
            except (ValueError, json.JSONDecodeError) as e:
                last = str(e)
                messages.append({'role': 'user', 'content': f'没能解析成 JSON（{e}）。只输出一个完整的 JSON 对象。'})
                continue
            r = self._set(jobdir, story, why)
            if r.get('ok'):
                self.log('剧本已保存。')
                return {'ok': True, 'story': story, 'result': r}
            last = r.get('error', 'drama-set refused')
            self.log(f'剧本没通过检查：{last[:200]}')
            messages.append({'role': 'user', 'content': f'drama-set 拒绝了，问题如下，改好后重新输出完整 JSON：\n{last}'})
        return {'ok': False, 'error': last or 'the director gave up'}

    def write(self, jobdir, title, idea, extra=''):
        messages = [{'role': 'system', 'content': SYSTEM.format(rules=rules())},
                    {'role': 'user', 'content': f'片名：{title}\n一句话故事：{idea}\n{extra}'.strip()}]
        return self._loop(jobdir, messages)

    def revise(self, jobdir, request):
        story = json.loads((Path(jobdir) / 'story.json').read_text(encoding='utf-8'))
        messages = [{'role': 'system', 'content': SYSTEM.format(rules=rules())},
                    {'role': 'user', 'content': '这是现在的 story.json：\n' + json.dumps(story, ensure_ascii=False)},
                    {'role': 'user', 'content': f'用户的意见：{request}\n只改需要改的地方，其余原样保留（包括 origin、look_young、outfit 等字段），输出完整的新 JSON。'}]
        return self._loop(jobdir, messages, why=request)
