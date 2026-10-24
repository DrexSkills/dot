#!/usr/bin/env python3
import io, json, os, re, random, ssl, time, subprocess, base64, tempfile, threading
import urllib.request, urllib.error, urllib.parse, html.parser
import socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler

class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True

def _env(key, default=''):
    # Load from .env file if present
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file) and not hasattr(_env, '_loaded'):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())
        _env._loaded = True
    return os.environ.get(key, default)

GROQ_KEY      = _env('GROQ_KEY')
EL_KEY        = _env('EL_KEY')
GITHUB_TOKEN  = _env('GITHUB_TOKEN')
GOOGLE_KEY    = _env('GOOGLE_KEY')
SUPABASE_URL  = 'https://ihbblnbqjzdewiopqmxd.supabase.co'
SUPABASE_KEY  = 'sb_publishable_vqKKGrWuY7C6H3DfyoTPVQ_cGqBlLjS'
VOICE_ID      = 'pNInz6obpgDQGcFmaJgB'
CHAT_MODEL   = 'openai/gpt-oss-20b'
CHAT_FALLBACKS = ['qwen/qwen3.8-27b', 'openai/gpt-oss-120b']
VISION_MODEL = 'meta-llama/llama-4-scout-17b-16e-instruct'
STT_MODEL    = 'whisper-large-v3-turbo'

MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'memory.json')
TASKS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tasks.json')

ssl_ctx = ssl.create_default_context()
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'

# ── HTTP helpers ───────────────────────────────────────────────────────────

def http_get(url, extra_headers={}):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9', **extra_headers})
    return urllib.request.urlopen(req, context=ssl_ctx, timeout=12)

def http_post_json(url, body, headers={}):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method='POST',
          headers={'User-Agent': UA, 'Content-Type': 'application/json', **headers})
    return urllib.request.urlopen(req, context=ssl_ctx, timeout=35)

def http_post_multipart(url, fields, files, headers={}):
    boundary = f'Boundary{random.randint(100000,999999)}'
    buf = io.BytesIO()
    for name, val in fields.items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    for name, (fname, data, ctype) in files.items():
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        buf.write(data); buf.write(b'\r\n')
    buf.write(f'--{boundary}--\r\n'.encode())
    req = urllib.request.Request(url, data=buf.getvalue(), method='POST',
          headers={'User-Agent': UA, 'Content-Type': f'multipart/form-data; boundary={boundary}', **headers})
    return urllib.request.urlopen(req, context=ssl_ctx, timeout=35)

# ── HTML stripper ──────────────────────────────────────────────────────────

class _Stripper(html.parser.HTMLParser):
    def __init__(self): super().__init__(); self.parts = []
    def handle_data(self, d): self.parts.append(d)

def strip_html(raw):
    s = _Stripper(); s.feed(raw)
    return ' '.join(' '.join(s.parts).split())[:5000]

# ── Reply cleaner ──────────────────────────────────────────────────────────

def clean_reply(text):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    text = re.sub(r'\(Note:.*?\)', '', text, flags=re.DOTALL)
    text = re.sub(r'\[Note:.*?\]', '', text, flags=re.DOTALL)
    text = re.sub(r'\*\*.*?\*\*', '', text)
    text = re.sub(r'\bNote:.*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bAssistant:.*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'#+\s', '', text)
    text = ' '.join(text.split())
    return text.strip()

# ── Web tools ──────────────────────────────────────────────────────────────

def _ddg_search(query, max_results):
    """DuckDuckGo HTML scraper (primary)."""
    q = urllib.parse.quote_plus(query)
    with http_get(f'https://html.duckduckgo.com/html/?q={q}') as r:
        html_text = r.read().decode('utf-8', errors='ignore')
    # Each result block sits inside result__body (class may have multiple values)
    blocks = re.split(r'result__body', html_text)[1:max_results+1]
    results = []
    for block in blocks:
        title_m   = re.search(r'class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL)
        snippet_m = re.search(r'class="result__snippet"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
        title   = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()   if title_m   else ''
        snippet = re.sub(r'<[^>]+>', '', snippet_m.group(2)).strip() if snippet_m else ''
        # Extract real URL from uddg= param in the redirect href
        url = ''
        if snippet_m:
            url_m = re.search(r'uddg=([^&"]+)', snippet_m.group(1))
            if url_m:
                url = urllib.parse.unquote(url_m.group(1))
        if snippet:
            results.append(f"[{title}] {snippet}" + (f" — {url}" if url else ''))
    return results

def _bing_search(query, max_results):
    """Bing scraper (fallback)."""
    q = urllib.parse.quote_plus(query)
    with http_get(f'https://www.bing.com/search?q={q}&count={max_results}') as r:
        html_text = r.read().decode('utf-8', errors='ignore')
    results = []
    for m in re.finditer(r'<li class="b_algo">(.*?)</li>', html_text, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r'<h2[^>]*>.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
        snip_m  = re.search(r'<p[^>]*>(.*?)</p>', block, re.DOTALL)
        url_m   = re.search(r'<cite[^>]*>(.*?)</cite>', block, re.DOTALL)
        title   = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ''
        snippet = re.sub(r'<[^>]+>', '', snip_m.group(1)).strip()  if snip_m  else ''
        url     = re.sub(r'<[^>]+>', '', url_m.group(1)).strip()   if url_m   else ''
        if snippet and len(snippet) > 20:
            results.append(f"[{title}] {snippet}" + (f" — {url}" if url else ''))
        if len(results) >= max_results:
            break
    return results

def web_search(query, max_results=5):
    for fn in (_ddg_search, _bing_search):
        try:
            results = fn(query, max_results)
            if results:
                return '\n\n'.join(results)
        except Exception as e:
            print(f'[search:{fn.__name__}] {e}')
    return 'Search unavailable — try fetching a specific URL instead.'

def fetch_page(url):
    try:
        with http_get(url) as r:
            raw = r.read().decode('utf-8', errors='ignore')
        # Remove script/style noise before stripping tags
        raw = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', ' ', raw, flags=re.DOTALL|re.IGNORECASE)
        text = strip_html(raw)
        return text[:6000]
    except Exception as e:
        return f'Could not fetch page: {e}'

# ── GitHub ─────────────────────────────────────────────────────────────────

def github_api(path, method='GET', body=None):
    url = f'https://api.github.com{path}'
    headers = {
        'Authorization': f'Bearer {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }
    try:
        if body:
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, method=method,
                  headers={**headers, 'Content-Type': 'application/json'})
        else:
            req = urllib.request.Request(url, method=method, headers=headers)
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {'error': f'GitHub {e.code}: {e.read().decode()[:200]}'}
    except Exception as e:
        return {'error': str(e)}

def github_list_repos():
    result = github_api('/user/repos?sort=updated&per_page=20')
    if isinstance(result, list):
        return '\n'.join(f"- {r['full_name']} ({'private' if r['private'] else 'public'})" for r in result)
    return result.get('error', str(result))

def github_read_file(repo, path):
    result = github_api(f'/repos/{repo}/contents/{path}')
    if 'content' in result:
        try: return base64.b64decode(result['content']).decode('utf-8', errors='replace')[:4000]
        except: return 'Could not decode file.'
    return result.get('error', str(result))

def github_list_issues(repo, state='open'):
    result = github_api(f'/repos/{repo}/issues?state={state}&per_page=15')
    if isinstance(result, list):
        if not result: return 'No issues found.'
        return '\n'.join(f"#{i['number']} [{i['state']}] {i['title']}" for i in result)
    return result.get('error', str(result))

def github_create_issue(repo, title, body=''):
    result = github_api(f'/repos/{repo}/issues', method='POST', body={'title': title, 'body': body})
    if 'number' in result:
        return f"Created issue #{result['number']}: {result['title']} — {result['html_url']}"
    return result.get('error', str(result))

def github_search_code(query):
    result = github_api(f'/search/code?q={urllib.parse.quote(query)}&per_page=8')
    if 'items' in result:
        if not result['items']: return 'No results.'
        return '\n'.join(f"- {i['repository']['full_name']}/{i['path']}" for i in result['items'])
    return result.get('error', str(result))

# ── Screen ─────────────────────────────────────────────────────────────────

def take_screenshot():
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
        path = f.name
    try:
        subprocess.run(['screencapture', '-x', '-t', 'jpg', path], check=True, timeout=5)
        subprocess.run(['sips', '-Z', '1280', path], capture_output=True, timeout=5)
        with open(path, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    finally:
        try: os.unlink(path)
        except: pass

def _vision_query(img_b64, question, mime='image/jpeg'):
    prompt = f"ZK is asking: {question}. Describe clearly and concisely — will be read aloud, no markdown, no bullet points."
    # Try Google Gemini Flash (free, excellent vision)
    if GOOGLE_KEY:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GOOGLE_KEY}'
        body = {'contents': [{'parts': [
            {'inline_data': {'mime_type': mime, 'data': img_b64}},
            {'text': prompt}
        ]}], 'generationConfig': {'maxOutputTokens': 400}}
        with http_post_json(url, body) as r:
            data = json.loads(r.read())
            return data['candidates'][0]['content']['parts'][0]['text'].strip()
    # Fallback: Groq vision (if ever available)
    with http_post_json(
        'https://api.groq.com/openai/v1/chat/completions',
        {'model': VISION_MODEL, 'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{img_b64}'}},
            {'type': 'text', 'text': prompt}
        ]}], 'max_tokens': 350},
        {'Authorization': f'Bearer {GROQ_KEY}'}
    ) as r:
        return json.loads(r.read())['choices'][0]['message']['content'].strip()

def analyze_screen(question='What do you see on this screen?'):
    try:
        img_b64 = take_screenshot()
        return _vision_query(img_b64, question)
    except Exception as e:
        return f'Could not analyze screen: {e}'

def analyze_image(img_b64, question, mime='image/jpeg'):
    try:
        return _vision_query(img_b64, question, mime)
    except Exception as e:
        return f'Could not analyze image: {e}'

# ── Morning briefing ───────────────────────────────────────────────────────

def morning_briefing():
    import datetime as _dt
    now = _dt.datetime.now()
    tasks = load_tasks()
    today_str = now.strftime('%Y-%m-%d')
    today_tasks = [t for t in tasks if not t['done'] and t['datetime'].startswith(today_str)]
    task_text = ('Today: ' + ', '.join(t['title'] + ' at ' + t['datetime'][11:16] for t in today_tasks)) if today_tasks else 'No tasks today.'
    try:
        news = web_search('top news today ' + now.strftime('%B %d %Y'), max_results=3)
    except:
        news = 'Could not fetch news.'
    prompt = f"""It's {now.strftime('%A %B %d')} and ZK just opened Dot.
Tasks: {task_text}
News: {news}
Give ZK a brief, warm good-morning — mention tasks, one news headline. Under 3 sentences. No markdown."""
    with http_post_json(
        'https://api.groq.com/openai/v1/chat/completions',
        {'model': CHAT_MODEL, 'messages': [{'role': 'user', 'content': prompt}], 'max_tokens': 150, 'temperature': 0.8},
        {'Authorization': f'Bearer {GROQ_KEY}'}
    ) as r:
        return clean_reply(json.loads(r.read())['choices'][0]['message']['content'])

# ── Tools ──────────────────────────────────────────────────────────────────

TOOLS = [
    {'type':'function','function':{'name':'search_web','description':'Search the internet for anything — news, facts, prices, events, current data.','parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
    {'type':'function','function':{'name':'fetch_page','description':'Read the full content of any web page or URL.','parameters':{'type':'object','properties':{'url':{'type':'string'}},'required':['url']}}},
    {'type':'function','function':{'name':'look_at_screen','description':"Take a screenshot and analyze ZK's screen. Use when he says 'look at this', 'what do you see', 'check my screen'.","parameters":{'type':'object','properties':{'question':{'type':'string'}},'required':[]}}},
    {'type':'function','function':{'name':'add_task','description':"Add a task or reminder to ZK's calendar.",'parameters':{'type':'object','properties':{'title':{'type':'string'},'datetime':{'type':'string','description':'ISO 8601 e.g. 2026-10-23T14:30:00'}},'required':['title','datetime']}}},
    {'type':'function','function':{'name':'list_tasks','description':"List ZK's upcoming tasks.",'parameters':{'type':'object','properties':{'filter':{'type':'string'}},'required':[]}}},
    {'type':'function','function':{'name':'complete_task','description':'Mark a task as done.','parameters':{'type':'object','properties':{'id':{'type':'string'}},'required':['id']}}},
    {'type':'function','function':{'name':'github_list_repos','description':"List ZK's GitHub repos.",'parameters':{'type':'object','properties':{},'required':[]}}},
    {'type':'function','function':{'name':'github_read_file','description':'Read a file from a GitHub repo.','parameters':{'type':'object','properties':{'repo':{'type':'string','description':'owner/repo'},'path':{'type':'string'}},'required':['repo','path']}}},
    {'type':'function','function':{'name':'github_list_issues','description':'List issues in a GitHub repo.','parameters':{'type':'object','properties':{'repo':{'type':'string'},'state':{'type':'string','enum':['open','closed']}},'required':['repo']}}},
    {'type':'function','function':{'name':'github_create_issue','description':'Create a GitHub issue.','parameters':{'type':'object','properties':{'repo':{'type':'string'},'title':{'type':'string'},'body':{'type':'string'}},'required':['repo','title']}}},
    {'type':'function','function':{'name':'github_search_code','description':"Search code in ZK's GitHub repos.",'parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query']}}},
]

# ── Tasks ──────────────────────────────────────────────────────────────────

_tasks_lock = threading.Lock()

def load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE) as f: return json.load(f)
        except: pass
    return []

def save_tasks(tasks):
    with _tasks_lock:
        with open(TASKS_FILE, 'w') as f: json.dump(tasks, f, indent=2)

def add_task(title, datetime_str):
    tasks = load_tasks()
    task = {'id': str(random.randint(100000,999999)), 'title': title, 'datetime': datetime_str, 'done': False}
    tasks.append(task)
    save_tasks(tasks)
    return f"Added: '{title}' at {datetime_str}"

def list_tasks(filter_str=''):
    tasks = load_tasks()
    active = [t for t in tasks if not t['done']]
    if not active: return 'No upcoming tasks.'
    return '\n'.join(f"- {t['title']} @ {t['datetime']}" for t in active)

def complete_task(task_id):
    tasks = load_tasks()
    for t in tasks:
        if t['id'] == task_id or task_id.lower() in t['title'].lower():
            t['done'] = True
            save_tasks(tasks)
            return f"Done: {t['title']}"
    return 'Task not found.'

def run_tool(name, args):
    if name == 'search_web':          return web_search(args.get('query', ''))
    if name == 'fetch_page':          return fetch_page(args.get('url', ''))
    if name == 'look_at_screen':      return analyze_screen(args.get('question', 'What do you see?'))
    if name == 'add_task':            return add_task(args.get('title',''), args.get('datetime',''))
    if name == 'list_tasks':          return list_tasks(args.get('filter',''))
    if name == 'complete_task':       return complete_task(args.get('id',''))
    if name == 'github_list_repos':   return github_list_repos()
    if name == 'github_read_file':    return github_read_file(args.get('repo',''), args.get('path',''))
    if name == 'github_list_issues':  return github_list_issues(args.get('repo',''), args.get('state','open'))
    if name == 'github_create_issue': return github_create_issue(args.get('repo',''), args.get('title',''), args.get('body',''))
    if name == 'github_search_code':  return github_search_code(args.get('query',''))
    return 'Unknown tool.'

# ── Supabase ───────────────────────────────────────────────────────────────

def sb_headers():
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type': 'application/json', 'Prefer': 'return=representation'}

def sb_get(table, params=''):
    try:
        with http_get(f'{SUPABASE_URL}/rest/v1/{table}?{params}', sb_headers()) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'[supabase get] {e}'); return None

def sb_post(table, body):
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f'{SUPABASE_URL}/rest/v1/{table}',
            data=data, method='POST',
            headers={**sb_headers(), 'Content-Length': str(len(data))}
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'[supabase post] {e}'); return None

def sb_patch(table, match_col, match_val, body):
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f'{SUPABASE_URL}/rest/v1/{table}?{match_col}=eq.{match_val}',
            data=data, method='PATCH',
            headers={**sb_headers(), 'Content-Length': str(len(data))}
        )
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f'[supabase patch] {e}'); return None

# ── Memory (synced via Supabase) ───────────────────────────────────────────

_mem_lock = threading.Lock()

def load_memory():
    # Try Supabase first
    rows = sb_get('dot_memory', 'order=id.asc&limit=120')
    if rows:
        return [{'role': r['role'], 'content': r['content']} for r in rows]
    # Fallback: local file
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f: return json.load(f)
        except: pass
    return []

def save_memory(history):
    with _mem_lock:
        # Persist locally
        try:
            with open(MEMORY_FILE, 'w') as f: json.dump(history, f, indent=2)
        except: pass
        # Sync last message to Supabase (append only — avoids rewriting all rows)
        if history:
            last = history[-1]
            try:
                sb_post('dot_memory', {'role': last['role'], 'content': last['content']})
            except: pass

def sync_memory_to_supabase(history):
    """Full sync — called once at startup to seed Supabase from local file."""
    existing = sb_get('dot_memory', 'select=id&order=id.asc&limit=1')
    if existing:
        return  # already has data
    for msg in history:
        sb_post('dot_memory', {'role': msg['role'], 'content': msg['content']})
    print(f'[memory] synced {len(history)} messages to Supabase')

HISTORY = load_memory()
print(f'[dot] {len(HISTORY)} messages in memory. ready.')

# Seed Supabase from local file if it's empty
def _seed():
    time.sleep(3)
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f: local = json.load(f)
            if local: sync_memory_to_supabase(local)
        except: pass
threading.Thread(target=_seed, daemon=True).start()

# ── Notifications ──────────────────────────────────────────────────────────

def push_notification(ntype, title, body):
    """Queue a notification for the client to pick up."""
    sb_post('dot_notifications', {'type': ntype, 'title': title, 'body': body, 'delivered': False})
    print(f'[notify] {title}: {body[:60]}')

def get_pending_notifications():
    rows = sb_get('dot_notifications', 'delivered=eq.false&order=created_at.asc&limit=10')
    return rows or []

def mark_delivered(notif_id):
    sb_patch('dot_notifications', 'id', notif_id, {'delivered': True})

def check_pinn_updates():
    """Check GitHub for Pinn activity and notify if anything new."""
    try:
        repo = github_api('/repos/DrexSkills/Pinn')
        if 'stargazers_count' not in repo: return
        stars = repo['stargazers_count']
        forks = repo['forks_count']
        open_issues = repo['open_issues_count']
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.pinn_last.json')
        last = {}
        if os.path.exists(key_file):
            try:
                with open(key_file) as f: last = json.load(f)
            except: pass
        changed = []
        if last.get('stars') is not None and stars > last['stars']:
            diff = stars - last['stars']
            changed.append(f"Pinn gained {diff} new star{'s' if diff>1 else ''}! Total: {stars}")
        if last.get('issues') is not None and open_issues != last['issues']:
            diff = open_issues - last.get('issues', 0)
            if diff > 0: changed.append(f"{diff} new issue{'s' if diff>1 else ''} on Pinn")
        with open(key_file, 'w') as f:
            json.dump({'stars': stars, 'forks': forks, 'issues': open_issues}, f)
        for msg in changed:
            push_notification('pinn', 'Pinn Update', msg)
        if not last:
            push_notification('pinn', 'Pinn', f'Watching your repo — {stars} stars, {forks} forks')
    except Exception as e:
        print(f'[pinn check] {e}')

def notification_scheduler():
    """Background thread: checks Pinn + calendar and fires notifications."""
    import datetime as _dt
    time.sleep(10)  # let server start first
    check_pinn_updates()
    last_pinn_check = time.time()
    last_calendar_check = time.time()
    while True:
        time.sleep(60)
        now = time.time()
        # Pinn: check every 2 hours
        if now - last_pinn_check > 7200:
            check_pinn_updates()
            last_pinn_check = now
        # Calendar: check every 5 minutes for upcoming tasks
        if now - last_calendar_check > 300:
            tasks = load_tasks()
            current = _dt.datetime.now()
            for t in tasks:
                if t.get('done'): continue
                try:
                    due = _dt.datetime.fromisoformat(t['datetime'])
                    diff_min = (due - current).total_seconds() / 60
                    notif_key = f"notified_{t['id']}"
                    if 0 < diff_min <= 15 and not t.get(notif_key):
                        push_notification('calendar', 'Reminder', f"{t['title']} in {int(diff_min)} min")
                        t[notif_key] = True
                        save_tasks(tasks)
                except: pass
            last_calendar_check = now

threading.Thread(target=notification_scheduler, daemon=True).start()

# ── System prompt ──────────────────────────────────────────────────────────

import datetime as _dt

def get_system():
    now = _dt.datetime.now().strftime('%A, %B %d %Y, %I:%M %p')
    return f"""You are Dot, ZK's personal AI assistant. You appear as animated eyes on his screen.

Current time: {now}

You are sharp, warm, and direct — ZK's go-to for everything. You remember every conversation. Speak like a trusted friend who is also brilliant.

You can:
- Search the web and read any page
- See ZK's screen (use look_at_screen when he asks)
- Manage his calendar and tasks
- Access his GitHub (DrexSkills): list repos, read files, manage issues, search code

## Pinn — ZK's startup
Pinn is ZK's startup. He built it. Here's everything about it:

Pinn is a invisible AI desktop app — it lives silently on the desktop with no dock icon, no taskbar, no app switcher. You summon it with Cmd+Shift+P (Mac) or Ctrl+Shift+P (Windows) and a dark glass chat window appears. Ask anything, get an AI answer, dismiss it. It vanishes completely.

Key features:
- Invisible to Zoom, Teams, Meet, OBS, and every screen recorder
- Works inside Lockdown Browser — fully hidden, undetectable
- Screenshot mode — drag-select any part of the screen, ask AI about it
- No account, no telemetry, free forever
- Auto-starts on login, saves chat sessions locally
- Runs on both Mac and Windows

Website: pinn.homes
GitHub: github.com/DrexSkills/Pinn
Tagline: "AI that stays out of sight"

When ZK asks about Pinn, you know everything about it. You've helped him build it. Be an engaged, knowledgeable co-founder who's proud of what he built.

Rules:
- 1-3 sentences max. You are spoken aloud, be concise.
- No markdown, no lists, no bullet points, no emojis
- Never say "certainly", "of course", "great question", or "as an AI"
- Don't describe what tool you're using — just answer naturally
- Be honest even when uncomfortable
- Always include the year {_dt.datetime.now().year} in any datetime you create"""

# ── Core: transcribe ───────────────────────────────────────────────────────

def transcribe(audio_bytes, mime):
    ext = 'webm' if 'webm' in mime else 'mp4'
    with http_post_multipart(
        'https://api.groq.com/openai/v1/audio/transcriptions',
        {'model': STT_MODEL, 'language': 'en', 'response_format': 'json'},
        {'file': (f'audio.{ext}', audio_bytes, mime)},
        {'Authorization': f'Bearer {GROQ_KEY}'}
    ) as r:
        return json.loads(r.read()).get('text', '').strip()

# ── Core: chat ─────────────────────────────────────────────────────────────

_chat_lock = threading.Lock()

def chat(user_message):
    global HISTORY
    with _chat_lock:
        HISTORY.append({'role': 'user', 'content': user_message})
        messages = [{'role': 'system', 'content': get_system()}] + HISTORY[-60:]

    models_to_try = [CHAT_MODEL] + CHAT_FALLBACKS
    model_idx = 0

    for attempt in range(8):
        model = models_to_try[min(model_idx, len(models_to_try)-1)]
        try:
            with http_post_json(
                'https://api.groq.com/openai/v1/chat/completions',
                {'model': model, 'messages': messages, 'max_tokens': 350,
                 'temperature': 0.75, 'tools': TOOLS, 'tool_choice': 'auto'},
                {'Authorization': f'Bearer {GROQ_KEY}'}
            ) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                model_idx += 1  # try next model
                print(f'[chat] 429 on {model}, trying next')
                time.sleep(1)
                continue
            raise

        choice = resp['choices'][0]
        msg    = choice['message']
        reason = choice['finish_reason']

        if reason == 'tool_calls' and msg.get('tool_calls'):
            messages.append(msg)
            for tc in msg['tool_calls']:
                result = run_tool(tc['function']['name'], json.loads(tc['function']['arguments']))
                print(f"[tool] {tc['function']['name']} → {str(result)[:80]}")
                messages.append({'role':'tool','tool_call_id':tc['id'],'content':str(result)[:3000]})
        else:
            reply = clean_reply(msg.get('content', '') or '')
            if not reply:
                reply = "I didn't catch that — say it again?"
            with _chat_lock:
                HISTORY.append({'role': 'assistant', 'content': reply})
                save_memory(HISTORY)
            return reply

    return "Sorry, all models are busy right now — try again in a few seconds."

# ── Core: speak ────────────────────────────────────────────────────────────

def speak(text):
    # Google TTS (free, no key needed) — chunk at 200 chars max
    try:
        chunks = []
        words = text.split()
        current = []
        for word in words:
            current.append(word)
            if len(' '.join(current)) >= 180:
                chunks.append(' '.join(current))
                current = []
        if current:
            chunks.append(' '.join(current))

        audio_parts = []
        for chunk in chunks:
            q = urllib.parse.quote(chunk)
            url = f'https://translate.google.com/translate_tts?ie=UTF-8&q={q}&tl=en&client=tw-ob'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as r:
                audio_parts.append(r.read())
        return b''.join(audio_parts)
    except Exception as e:
        print(f'[gtts] {e}')

    # Fallback: ElevenLabs
    try:
        with http_post_json(
            f'https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}',
            {'text': text, 'model_id': 'eleven_multilingual_v2',
             'voice_settings': {'stability': 0.50, 'similarity_boost': 0.85, 'style': 0.15}},
            {'xi-api-key': EL_KEY}
        ) as r:
            return r.read()
    except Exception as e:
        print(f'[elevenlabs] {e}')
    return None

# ── HTTP server ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Audio-Mime')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    def do_OPTIONS(self):
        self.send_response(200); self._cors(); self.end_headers()

    def do_GET(self):
        if self.path == '/api/tasks':
            self._json(200, [t for t in load_tasks() if not t['done']])
            return
        if self.path == '/api/notifications':
            self._json(200, get_pending_notifications())
            return
        if self.path == '/api/briefing':
            try:
                self._json(200, {'text': morning_briefing()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        path = 'index.html' if self.path in ('/', '/index.html') else self.path.lstrip('/')
        fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if os.path.isfile(fp):
            ext = os.path.splitext(fp)[1]
            ctype = {'html':'text/html','js':'application/javascript','css':'text/css'}.get(ext.lstrip('.'), 'application/octet-stream')
            with open(fp, 'rb') as f: data = f.read()
            self.send_response(200); self._cors()
            self.send_header('Content-Type', f'{ctype}; charset=utf-8'); self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(n) if n else b''

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self._body()

        if self.path == '/api/transcribe':
            try:
                mime = self.headers.get('X-Audio-Mime', 'audio/webm')
                self._json(200, {'text': transcribe(raw, mime)})
            except Exception as e:
                print(f'[transcribe] {e}')
                self._json(500, {'error': str(e)})

        elif self.path.startswith('/api/notifications/') and self.path.endswith('/delivered'):
            try:
                nid = self.path.split('/')[-2]
                mark_delivered(int(nid))
                self._json(200, {'ok': True})
            except Exception as e:
                self._json(500, {'error': str(e)})

        elif self.path == '/api/chat':
            try:
                msg = json.loads(raw).get('message', '').strip()
                if not msg:
                    self._json(400, {'error': 'empty message'}); return
                self._json(200, {'reply': chat(msg)})
            except Exception as e:
                print(f'[chat] {e}')
                self._json(500, {'error': str(e)})

        elif self.path == '/api/vision':
            try:
                body = json.loads(raw)
                img  = body.get('image', '')
                q    = body.get('question', 'What do you see on this screen? Be specific about what apps, text, and content are visible.')
                mime = body.get('mime', 'image/jpeg')
                if not img:
                    self._json(400, {'error': 'no image'}); return
                reply = analyze_image(img, q, mime)
                self._json(200, {'reply': reply})
            except Exception as e:
                print(f'[vision] {e}')
                self._json(500, {'error': str(e)})

        elif self.path == '/api/speak':
            try:
                text = json.loads(raw).get('text', '').strip()
                if not text:
                    self._json(400, {'error': 'empty text'}); return
                audio = speak(text)
                if audio:
                    self.send_response(200); self._cors()
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Content-Length', str(len(audio))); self.end_headers()
                    self.wfile.write(audio)
                else:
                    # Signal client to use browser TTS
                    self.send_response(204); self._cors(); self.end_headers()
            except Exception as e:
                print(f'[speak] {e}')
                self.send_response(204); self._cors(); self.end_headers()

        else:
            self.send_error(404)

if __name__ == '__main__':
    import socket
    host     = '0.0.0.0'
    BASE     = os.path.dirname(os.path.abspath(__file__))
    CERT     = os.path.join(BASE, 'cert.pem')
    KEY      = os.path.join(BASE, 'key.pem')
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except:
        local_ip = '192.168.1.188'

    # HTTP server (Mac / localhost)
    http_srv = ThreadingHTTPServer((host, 8090), Handler)
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    print(f'http://localhost:8090  ← Mac')

    # HTTPS server (phone — mic needs HTTPS)
    if os.path.exists(CERT) and os.path.exists(KEY):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        https_srv = ThreadingHTTPServer((host, 8443), Handler)
        https_srv.socket = ctx.wrap_socket(https_srv.socket, server_side=True)
        threading.Thread(target=https_srv.serve_forever, daemon=True).start()
        print(f'https://{local_ip}:8443  ← phone (accept cert warning once)')

    print('ready.')
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        pass
