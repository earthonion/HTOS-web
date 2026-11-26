#!/usr/bin/env python3
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("HTOS_ROOT", "/opt/htos"))
REPO = os.environ.get("GTA6_EDITOR_REPO", "Joshhooger/gta6-money-editor")
WORK = ROOT / "workspace" / "gta6-editor-upstream"
STATE = WORK / "state.json"
BACKUP_ROOT = Path(os.environ.get("BACKUP_ROOT", "/opt/htos-backups"))
RESTART = os.environ.get("RESTART_SERVICE", "1") == "1"
FIXTURE = ROOT / "data" / "gta6-test-fixtures" / "SARC0001"
BLOCKED_EXTENSIONS = {".sh", ".bash", ".elf", ".bin", ".so", ".exe", ".dll", ".php", ".rb", ".pl", ".cgi", ".wsgi", ".conf", ".service", ".pem", ".key", ".crt", ".env"}


def log(message):
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    print(line, flush=True)
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "sync.log").open("a", encoding="utf-8") as output:
        output.write(line + "\n")


def download(url):
    headers = {"User-Agent": "GarlicSaves-GTA6-Editor-Sync/1.0"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def replace_required(text, old, new):
    if old not in text:
        raise RuntimeError(f"Editor integration point changed: {old[:100]}")
    return text.replace(old, new, 1)


def build_page(upstream_html, output):
    source = upstream_html.read_text(encoding="utf-8")
    style = source.split("<style>", 1)[1].split("</style>", 1)[0]
    main = "<main id=\"g6e\">" + source.split("<main id=\"g6e\">", 1)[1].split("</main>", 1)[0] + "</main>"
    main = replace_required(main, '<button id="go" disabled>Edit my save</button>', '<button id="go" disabled>Edit my save</button>\n  <button id="goEncrypt" class="secondary" disabled>Edit &amp; encrypt</button>')
    script = source.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    script = replace_required(script, "go = $('go'), msg", "go = $('go'), goEncrypt = $('goEncrypt'), msg")
    script = replace_required(script, "go.disabled = !(chosen && pack && anyOn())", "go.disabled = goEncrypt.disabled = !(chosen && pack && anyOn())")
    handler_start = "  go.addEventListener('click', async () => {"
    handler_end = "\n  });\n})();"
    if handler_start not in script or handler_end not in script:
        raise RuntimeError("Editor click handler integration point changed")
    before_handler, handler = script.split(handler_start, 1)
    handler, after_handler = handler.rsplit(handler_end, 1)
    handler = replace_required(handler, "go.disabled = true; say('busy', 'Editing… this takes a few seconds.');", "go.disabled = goEncrypt.disabled = true; say('busy', encrypt ? 'Editing and encrypting… this can take a little while.' : 'Editing… this takes a few seconds.');")
    handler = replace_required(handler, "const name = pack.kind", "let name = pack.kind")
    handler = replace_required(handler, "if (Object.keys(opts).length) {", "if (Object.keys(opts).length || encrypt) {")
    handler = replace_required(handler, "fd.append('options', JSON.stringify(opts));", "fd.append('options', JSON.stringify(opts));\n        if (encrypt) fd.append('encrypt', '1');")
    handler = replace_required(handler, "lines = JSON.parse(decodeURIComponent(r.headers.get('X-Edit-Summary') || '%5B%5D'));", "lines = JSON.parse(decodeURIComponent(r.headers.get('X-Edit-Summary') || '%5B%5D'));\n        if (encrypt) name = (r.headers.get('Content-Disposition') || '').match(/filename=\"?([^\"]+)\"?/)?.[1] || name;")
    handler = replace_required(handler, "      say('ok',", "      const next = encrypt ? 'Next: upload the encrypted slot and memory.dat if you edited both, and fully close the game before loading.' : 'Next: encrypt it for your PSN account, upload both the slot and memory.dat if you edited both, and fully close the game before loading.';\n      say('ok',")
    handler = replace_required(handler, "'\\n\\nNext: encrypt it for your PSN account, upload both the slot and memory.dat if you edited both, and fully close the game before loading.'", "'\\n\\n' + next")
    script = before_handler + "  async function run(encrypt) {" + handler + "\n  }\n  go.addEventListener('click', () => run(false));\n  goEncrypt.addEventListener('click', () => run(true));\n})();" + after_handler
    template = '''{% extends "base.html" %}
{% block title %}GTA VI Save Editor{% endblock %}
{% block meta_description %}Edit money, guns, skills, armour, criminal profile, health, cars and map fog in decrypted GTA VI saves, made by some asshole.{% endblock %}
{% block extra_head %}
<style>''' + style + '''</style>
<style>
#g6e button#goEncrypt { width:100%; margin-top:12px; padding:14px; border-radius:12px; font-weight:700; }
</style>
{% endblock %}
{% block content %}
<p class="flash flash-warning">Save modding is at your own risk. Keep an untouched backup. Editor by <strong>some asshole</strong>; native vehicle generator by <strong>Malygos from SaveRift</strong>. <a href="{{ url_for('main.disclaimer') }}">Full disclaimer</a>.</p>
{% raw %}''' + main + '''{% endraw %}
<script>window.GTA6_EDIT_URL = "/gta6/api/gta6/edit";
window.GTA6_CARS_BASE = "/gta6/cars/";</script>
{% raw %}<script>''' + script + '''</script>{% endraw %}
{% endblock %}
'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template, encoding="utf-8")


def reject_unsafe_paths(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            if full.is_symlink():
                raise RuntimeError(f"Upstream contains symlink: {full}")
        for name in filenames:
            if Path(name).suffix.lower() in BLOCKED_EXTENSIONS:
                raise RuntimeError(f"Blocked file type in upstream: {name}")


def validate(upstream):
    code = '''from pathlib import Path
import gta6_editor as E
p=Path(__import__("os").environ["FIXTURE"])
data=p.read_bytes();out,report=E.edit_any(data,{"max_all":True});again,_=E.edit_any(out,{"max_all":True})
assert out==again and E.summary(report)
'''
    env = dict(os.environ, PYTHONPATH=str(upstream), FIXTURE=str(FIXTURE))
    subprocess.run([sys.executable, "-c", code], check=True, timeout=300, env=env)


def deploy(upstream, sha):
    backup = BACKUP_ROOT / f"gta6-editor-sync-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    files = {
        "gta6_money.py": "services/gta6_money.py",
        "gta6_editor.py": "services/gta6_editor.py",
        "gta6_player.py": "services/gta6_player.py",
        "gta6_weapons.py": "services/gta6_weapons.py",
        "web/gta6-editor.html": "web/gta6-editor.html",
        "cars/gta6-cars.js": "static/gta6-cars/gta6-cars.js",
        "cars/gta6-fog.js": "static/gta6-cars/gta6-fog.js",
        "cars/car_library.json": "static/gta6-cars/car_library.json",
        "cars/native/native-street.bundle.js": "static/gta6-cars/native/native-street.bundle.js",
        "cars/THIRD_PARTY_NOTICES.md": "static/gta6-cars/THIRD_PARTY_NOTICES.md",
    }
    for source in (upstream / "data").glob("*.json"):
        files[f"data/{source.name}"] = f"services/data/{source.name}"
    for relative in set(files.values()) | {"templates/gta6_v2.html"}:
        source = ROOT / relative
        if source.exists():
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for source_name, relative in files.items():
        source = upstream / source_name
        if not source.is_file():
            raise RuntimeError(f"Upstream is missing {source_name}")
        destination = ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    editor_module = ROOT / "services/gta6_editor.py"
    text = editor_module.read_text(encoding="utf-8")
    text = text.replace("import gta6_money as money", "from . import gta6_money as money")
    text = text.replace("import gta6_player as player", "from . import gta6_player as player")
    text = text.replace("import gta6_weapons as weapons", "from . import gta6_weapons as weapons")
    editor_module.write_text(text, encoding="utf-8")
    build_page(upstream / "web/gta6-editor.html", ROOT / "templates/gta6_v2.html")
    subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / "services/gta6_money.py"), str(ROOT / "services/gta6_editor.py"), str(ROOT / "services/gta6_player.py"), str(ROOT / "services/gta6_weapons.py")], check=True, timeout=60, env=dict(os.environ, PYTHONPYCACHEPREFIX=str(WORK / "pycache")))
    if RESTART:
        subprocess.run(["systemctl", "restart", "htos"], check=True, timeout=60)


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "sync.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        commit = json.loads(download(f"https://api.github.com/repos/{REPO}/commits/main"))
        sha = commit["sha"]
        current = json.loads(STATE.read_text()) if STATE.exists() else {}
        if current.get("sha") == sha and os.environ.get("SYNC_FORCE") != "1":
            log(f"Already synchronized at {sha}")
            return
        staging = WORK / "staging" / sha
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        archive = WORK / f"{sha}.zip"
        archive.write_bytes(download(f"https://api.github.com/repos/{REPO}/zipball/{sha}"))
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(staging)
        reject_unsafe_paths(staging)
        roots = list(staging.iterdir())
        if len(roots) != 1:
            raise RuntimeError("Unexpected upstream archive layout")
        upstream = roots[0]
        validate(upstream)
        deploy(upstream, sha)
        STATE.write_text(json.dumps({"sha": sha, "deployed_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
        log(f"Deployed {sha}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"ERROR: {type(error).__name__}: {error}")
        raise SystemExit(1)
