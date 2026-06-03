#!/usr/bin/env python3
"""
Skill Sync Daemon — reads local Claude skills every 60 s, pushes skills.json
to GitHub on any change. Vercel auto-deploys from the push.

Config:  ~/.config/claude/skill-sync.json
Logs:    stdout → /tmp/skill-sync.log (when run via launchd)

Run manually:  python3 sync-daemon.py
Run setup:     bash setup.sh
"""
import base64, glob, hashlib, json, os, re, time, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

HOME          = Path.home()
CONFIG_PATH   = HOME / '.config' / 'claude' / 'skill-sync.json'
HISTORY_FILE  = Path(__file__).parent / 'skill-history.json'
REC_FILE      = Path(__file__).parent / 'recommendations.json'
POLL_INTERVAL = 60  # seconds

SCAN_PATTERNS = [
    (str(HOME / "Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/*/*/skills/*/SKILL.md"), "anthropic"),
    (str(HOME / ".claude/skills/*/SKILL.md"), "personal"),
    (str(HOME / ".claude/plugins/cache/*/skills/*/SKILL.md"), "plugin"),
    (str(HOME / ".claude/scheduled-tasks/*/SKILL.md"), "scheduled"),
]
SOURCE_META = {
    "anthropic": {"label": "Anthropic",   "color": "#8b5cf6"},
    "personal":  {"label": "Your Skills", "color": "#38bdf8"},
    "vercel":    {"label": "Vercel",      "color": "#cbd5e1"},
    "supabase":  {"label": "Supabase",    "color": "#34d399"},
    "scheduled": {"label": "Scheduled",   "color": "#fb923c"},
    "plugin":    {"label": "Plugin",      "color": "#64748b"},
}
SECTION_ORDER = ["personal", "anthropic", "vercel", "supabase", "plugin", "scheduled"]
VERCEL_KW = {"vercel","nextjs","next-","turbopack","shadcn","routing","deployment",
             "ai-sdk","ai-gateway","chat-sdk","middleware","runtime-cache","next-forge",
             "next-upgrade","next-cache","react-best","vercel-agent","vercel-cli",
             "vercel-firewall","vercel-functions","vercel-sandbox","vercel-storage"}

# Category detection — order matters, first match wins
CATEGORIES = [
    ("Video",        r'video|youtube|record|reel|\bclip\b|transcript'),
    ("Documents",    r'\bpdf\b|docx|pptx|xlsx|\bword\b|excel|spreadsheet'),
    ("Memory",       r'memory|obsidian|knowledge|vault|notebook|commonplace|journal'),
    ("Deploy",       r'\bdeploy\b|launch|hosting|publish|production'),
    ("AI & Agents",  r'\bagent\b|\bmcp\b|parallel|swarm|dispatch|ruflo|\bllm\b|brainstorm'),
    ("Research",     r'research|scrape|extract|crawl|competi|analys'),
    ("Design",       r'design|figma|brand|visual|style|token|art\b|image'),
    ("Writing",      r'writ|content|blog|copy|newsletter|essay'),
    ("Coding",       r'code|build|\bdev\b|debug|review|test|security|audit|refactor|import'),
    ("Productivity", r'schedule|cron|remind|loop|repeat|changelog|workflow|automat|task\b|insight'),
]
_CAT_RE = [(label, re.compile(pattern, re.I)) for label, pattern in CATEGORIES]

_TRIG  = re.compile(r'\.?\s+(?:Use (?:this (?:skill )?)?when(?:ever)?\s+|Trigger(?:s(?: automatically)?)? when\s+|Activate (?:this (?:skill )?)?when(?:ever)?\s+|Only use when\s+|Use only when\s+|Also trigger(?:s)? when\s+)', re.I)
_STRIG = re.compile(r'^(?:Use (?:this (?:skill )?)?(?:when(?:ever)?|any\s*time)\s+|Trigger(?:s)? when\s+|Activate when(?:ever)?\s+|Only use when\s+)', re.I)


# ── Category detection ────────────────────────────────────────────────────────

def detect_category(name, folder, what, raw_desc):
    text = f"{name} {folder} {what} {raw_desc}"
    for label, rx in _CAT_RE:
        if rx.search(text):
            return label
    return "Other"


# ── Skill history (first-seen timestamps) ────────────────────────────────────

def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding='utf-8')


# ── Skill scanning ────────────────────────────────────────────────────────────

def fm_field(fm, field):
    m = re.search(rf'^{re.escape(field)}:\s*(.*)$', fm, re.M)
    if not m: return ''
    v = m.group(1).strip()
    if v in ('>', '|', '>-', '|-', '>+', '|+', ''):
        lines = [l.strip() for l in fm[m.end():].split('\n') if l and l[0] in (' ', '\t')]
        v = ' '.join(lines)
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]: v = v[1:-1]
    return v.strip()

def split_desc(desc):
    if not desc: return '', ''
    m = _TRIG.search(desc)
    if m:
        what = desc[:m.start()].strip().rstrip('.')
        when = desc[m.end():].strip()
        if _STRIG.match(what): what = ''
        return what, when
    m2 = _STRIG.match(desc)
    if m2: return '', desc[m2.end():].strip()
    return desc, ''

def body_intro(content, end):
    acc = []
    for line in content[end:].strip().split('\n'):
        s = line.strip()
        if s.startswith('#'):
            if acc: break
            continue
        if s.startswith('```') or s in ('---', '***', '___'):
            if acc: break
            continue
        if s:
            s = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', s)
            s = re.sub(r'`([^`]+)`', r'\1', s)
            s = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', s)
            acc.append(s)
        elif acc:
            break
    intro = ' '.join(acc).strip()
    return (intro[:320].rsplit(' ', 1)[0] + '…') if len(intro) > 320 else intro

def parse_skill(path):
    try:
        content = Path(path).read_text(encoding='utf-8')
        fm_m = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
        if not fm_m: return None
        fm = fm_m.group(1); end = fm_m.end()
        name = fm_field(fm, 'name') or Path(path).parent.name
        raw  = fm_field(fm, 'description')
        what, when = split_desc(raw)
        intro = body_intro(content, end)
        if not what and intro: what = intro
        return {'name': name, 'what': what, 'when': when, 'raw_desc': raw,
                'folder': Path(path).parent.name, 'mtime': os.path.getmtime(path), '_path': path}
    except Exception:
        return None

def detect_src(base, name, folder):
    if base != 'plugin': return base
    t = (name + ' ' + folder).lower()
    if any(k in t for k in VERCEL_KW): return 'vercel'
    if 'supabase' in t: return 'supabase'
    return 'plugin'

def scan():
    seen = {}
    for pat, base in SCAN_PATTERNS:
        for p in glob.glob(pat):
            s = parse_skill(p)
            if not s: continue
            src  = detect_src(base, s['name'], s['folder'])
            meta = SOURCE_META.get(src, SOURCE_META['plugin'])
            s['source'] = src; s['source_label'] = meta['label']; s['source_color'] = meta['color']
            k = s['folder']
            if k not in seen or s['mtime'] > seen[k]['mtime']:
                seen[k] = s
    result = sorted(seen.values(), key=lambda s: (
        SECTION_ORDER.index(s['source']) if s['source'] in SECTION_ORDER else 99,
        s['name'].lower()))
    for s in result:
        del s['_path']; del s['mtime']
    return result

def enrich(skills, history, now_iso):
    """Add category + first_seen to each skill. Mutates history in place."""
    for s in skills:
        k = s['folder']
        if k not in history:
            history[k] = now_iso
        s['first_seen'] = history[k]
        s['category']   = detect_category(s['name'], s['folder'], s.get('what', ''), s.get('raw_desc', ''))
    return skills

def shash(skills):
    return hashlib.md5(json.dumps([(s['folder'], s['name']) for s in skills], sort_keys=True).encode()).hexdigest()


# ── GitHub API ────────────────────────────────────────────────────────────────

def gh_request(method, url, token, data=None):
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'skill-sync-daemon/1.0',
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def push_file_to_github(config, filename, content_str, commit_msg):
    token = config['github_token']
    owner = config['github_owner']
    repo  = config['github_repo']
    url   = f'https://api.github.com/repos/{owner}/{repo}/contents/{filename}'

    sha = None
    try:
        current = gh_request('GET', url, token)
        sha = current.get('sha')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    body = {
        'message': commit_msg,
        'content': base64.b64encode(content_str.encode()).decode(),
        'committer': {'name': 'Skill Sync', 'email': 'skill-sync@local'},
    }
    if sha:
        body['sha'] = sha

    gh_request('PUT', url, token, json.dumps(body).encode())


# ── Main loop ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'[{ts}] {msg}', flush=True)

def main():
    if not CONFIG_PATH.exists():
        print(f'Config not found: {CONFIG_PATH}')
        print('Run:  bash ~/Claude/skill-dashboard/setup.sh')
        raise SystemExit(1)

    config        = json.loads(CONFIG_PATH.read_text())
    last_hash     = ''
    last_rec_hash = ''
    history       = load_history()

    log(f'Skill Sync Daemon started — polling every {POLL_INTERVAL}s')
    log(f'Target: github.com/{config["github_owner"]}/{config["github_repo"]}')

    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            skills  = scan()
            enrich(skills, history, now_iso)
            save_history(history)

            h = shash(skills)
            if h != last_hash:
                payload = json.dumps({'synced_at': now_iso, 'hash': h, 'skills': skills}, indent=2)
                push_file_to_github(config, 'skills.json', payload, 'chore: sync skills')
                last_hash = h
                log(f'Pushed {len(skills)} skills (hash: {h[:8]}…)')
            else:
                log(f'No change ({len(skills)} skills)')

            # Sync recommendations.json if it exists and changed
            if REC_FILE.exists():
                rec_content = REC_FILE.read_text(encoding='utf-8')
                rec_h = hashlib.md5(rec_content.encode()).hexdigest()
                if rec_h != last_rec_hash:
                    push_file_to_github(config, 'recommendations.json', rec_content, 'chore: sync recommendations')
                    last_rec_hash = rec_h
                    log(f'Pushed recommendations.json (hash: {rec_h[:8]}…)')

        except Exception as e:
            log(f'Error: {e}')

        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
