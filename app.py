#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 批量注册机（单文件 Web 版）
启动: python app.py  ->  浏览器打开 http://127.0.0.1:8000
手机号/代理/API/验证码 全部在网页填写。验证码为网页人工填码。
"""
import os
import json
import time
import random
import string
import threading
import asyncio
from collections import deque

from flask import Flask, request, jsonify, send_from_directory
from telethon import TelegramClient
from telethon import errors as tg_errors

try:
    from python_socks import ProxyType
    SOCKS5 = ProxyType.SOCKS5
except Exception:
    SOCKS5 = 2  # 与 socks.SOCKS5 / python_socks.ProxyType.SOCKS5 值一致

# ===================== 常量 =====================
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8000))
SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions")

app = Flask(__name__)

# ===================== 全局状态 =====================
STATE = {
    "status": "idle",       # idle / running / stopped
    "current": None,
    "provider": None,
}
RESULTS = []
LOG_BUF = deque(maxlen=1000)
LOG_ID = [0]
_lock = threading.Lock()
_log_lock = threading.Lock()
_stop = threading.Event()
_worker_thread = None


def append_log(msg, level="info"):
    with _log_lock:
        LOG_ID[0] += 1
        LOG_BUF.append({
            "id": LOG_ID[0],
            "ts": time.strftime("%H:%M:%S"),
            "level": level,
            "msg": str(msg),
        })


def add_result(phone, ok, reason):
    with _lock:
        for r in RESULTS:
            if r["phone"] == phone:
                r["ok"] = ok
                r["reason"] = reason
                return
        RESULTS.append({"phone": phone, "ok": ok, "reason": reason})


# ===================== 验证码对接（网页人工填码） =====================
class WebCodeProvider:
    """脚本触发发码后把手机号挂起，网页人工填码后继续。"""
    def __init__(self):
        self._lk = threading.Lock()
        self._pending = {}   # phone -> {"event": Event, "code": [None]}

    def request_code(self, phone):
        with self._lk:
            self._pending[phone] = {"event": threading.Event(), "code": [None]}

    def get_code(self, phone, timeout):
        with self._lk:
            rec = self._pending.get(phone)
        if not rec:
            return ""
        if not rec["event"].wait(timeout):
            with self._lk:
                self._pending.pop(phone, None)
            return ""
        code = rec["code"][0]
        with self._lk:
            self._pending.pop(phone, None)
        return (code or "").strip()

    def set_code(self, phone, code):
        with self._lk:
            rec = self._pending.get(phone)
        if rec:
            rec["code"][0] = str(code).strip()
            rec["event"].set()
            return True
        return False

    def waiting_phones(self):
        with self._lk:
            return list(self._pending.keys())

    def cancel_all(self):
        with self._lk:
            for rec in self._pending.values():
                rec["event"].set()
            self._pending.clear()


# ===================== 解析输入 =====================
def normalize_phone(p):
    p = p.strip().replace(" ", "").replace("-", "").replace("\t", "")
    if not p:
        return None
    if p.startswith("00"):
        p = "+" + p[2:]
    elif not p.startswith("+"):
        p = "+" + p
    return p


def parse_phones(text):
    out = []
    for raw in text.replace(",", "\n").replace(";", "\n").replace("，", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if " " in line:
            line = line.split()[0]
        p = normalize_phone(line)
        if p:
            out.append(p)
    seen = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def parse_proxy(line):
    line = line.strip()
    if not line:
        return None
    if "://" in line:
        line = line.split("://", 1)[1]
    parts = line.split(":")
    host = parts[0]
    port = int(parts[1]) if len(parts) > 1 and parts[1] else 1080
    user = parts[2] if len(parts) > 2 and parts[2] else None
    pwd = parts[3] if len(parts) > 3 and parts[3] else None
    if user and pwd:
        return (SOCKS5, host, port, True, user, pwd)
    return (SOCKS5, host, port, True)


def parse_proxies(text):
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = parse_proxy(line)
        if p:
            out.append(p)
    return out


def rand_name(base):
    return f"{base}{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"


# ===================== 核心注册逻辑 =====================
async def register_account(phone, proxy, provider, cfg, idx, total):
    os.makedirs(SESSION_DIR, exist_ok=True)
    session_file = os.path.join(SESSION_DIR, phone.replace("+", ""))
    client = TelegramClient(session_file, cfg["api_id"], cfg["api_hash"], proxy=proxy)

    try:
        await client.connect()
        if await client.is_user_authorized():
            return True, "已有有效 session，该号已注册，跳过"

        await client.send_code_request(phone)
        append_log(f"[{idx}/{total}] {phone} 验证码已请求，请在网页输入", "info")
        provider.request_code(phone)

        code = await asyncio.to_thread(provider.get_code, phone, cfg["code_timeout"])
        if not code:
            return False, "验证码超时/未取得"

        try:
            await client.sign_in(phone, code)
            return True, "登录成功(该号此前已注册)"
        except tg_errors.PhoneNumberUnoccupiedError:
            first = rand_name(cfg["first_name"] or "User")
            last = cfg["last_name"] or ""
            try:
                await client.sign_up(code, first, last)
                return True, "注册成功"
            except tg_errors.TermsOfServiceUpdateError:
                await client.accept_tos()
                await client.sign_up(code, rand_name(cfg["first_name"] or "User"), last)
                return True, "注册成功(已接受ToS)"
        except tg_errors.SessionPasswordNeededError:
            return False, "该号已注册且开启2FA，跳过"
        except tg_errors.PhoneCodeInvalidError:
            return False, "验证码错误"
        except tg_errors.PhoneCodeExpiredError:
            return False, "验证码已过期"
        except tg_errors.PhoneCodeEmptyError:
            return False, "验证码为空"

    except tg_errors.PhoneNumberInvalidError:
        return False, "手机号格式无效"
    except tg_errors.PhoneNumberBannedError:
        return False, "手机号已被 Telegram 封禁"
    except tg_errors.FloodWaitError as e:
        return False, f"FLOOD_WAIT:{e.seconds}"
    except tg_errors.FloodError:
        return False, "请求过于频繁"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def run_all(cfg):
    phones = cfg["phones"]
    proxies = cfg["proxies"]
    provider = cfg["provider"]
    total = len(phones)
    ok = 0

    for i, phone in enumerate(phones, 1):
        if _stop.is_set():
            append_log("收到停止指令，中止任务", "warn")
            break
        STATE["current"] = phone
        proxy = proxies[(i - 1) % len(proxies)] if proxies else None
        success = False

        for attempt in range(1, cfg["max_retry"] + 1):
            if _stop.is_set():
                break
            if attempt > 1:
                append_log(f"[{i}/{total}] {phone} 第 {attempt} 次重试...", "info")
            ok_flag, reason = await register_account(phone, proxy, provider, cfg, i, total)
            if ok_flag:
                success = True
                ok += 1
                add_result(phone, True, reason)
                append_log(f"[{i}/{total}] {phone} 成功 -> {reason}", "ok")
                break
            if reason.startswith("FLOOD_WAIT:"):
                secs = int(reason.split(":", 1)[1])
                append_log(f"[{i}/{total}] {phone} 触发限流，等待 {secs}s", "warn")
                await asyncio.sleep(secs + 5)
                continue
            add_result(phone, False, reason)
            append_log(f"[{i}/{total}] {phone} 失败 -> {reason}", "warn")
            if attempt < cfg["max_retry"] and not _stop.is_set():
                await asyncio.sleep(random.uniform(5, 15))

        if not success and not _stop.is_set():
            add_result(phone, False, "多次尝试失败")

        if i < total and not _stop.is_set():
            d = random.uniform(cfg["min_delay"], cfg["max_delay"])
            append_log(f"下个号前暂停 {d:.0f}s", "info")
            await asyncio.sleep(d)

    STATE["current"] = None
    append_log(f"任务结束：成功 {ok}/{total}", "info")


def _worker(cfg):
    try:
        asyncio.run(run_all(cfg))
    except Exception as e:
        append_log(f"任务异常: {type(e).__name__}: {e}", "error")
    finally:
        with _lock:
            STATE["status"] = "idle"
            STATE["provider"] = None
        _stop.clear()


# ===================== 工具 =====================
def list_session_files():
    if not os.path.isdir(SESSION_DIR):
        return []
    files = []
    for f in os.listdir(SESSION_DIR):
        if f.endswith(".session") and "-journal" not in f:
            p = os.path.join(SESSION_DIR, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            files.append({"name": f, "size": size})
    files.sort(key=lambda x: x["name"])
    return files


# ===================== 路由 =====================
@app.get("/")
def index():
    return HTML


@app.post("/start")
def start():
    global _worker_thread
    with _lock:
        if STATE["status"] == "running":
            return jsonify(ok=False, err="任务已在运行中")

        data = request.json or {}
        try:
            api_id = int(data.get("api_id", ""))
        except Exception:
            return jsonify(ok=False, err="api_id 必须是整数")
        api_hash = (data.get("api_hash") or "").strip()
        if not api_id or not api_hash:
            return jsonify(ok=False, err="请填写 api_id 和 api_hash")

        phones = parse_phones(data.get("phones", ""))
        if not phones:
            return jsonify(ok=False, err="没有有效手机号")
        proxies = parse_proxies(data.get("proxies", ""))
        try:
            min_delay = max(0, float(data.get("min_delay", 30)))
            max_delay = max(min_delay, float(data.get("max_delay", 90)))
            max_retry = max(1, int(data.get("max_retry", 3)))
            code_timeout = max(30, int(data.get("code_timeout", 180)))
        except Exception:
            return jsonify(ok=False, err="延迟/重试/超时参数格式错误")

        cfg = {
            "api_id": api_id,
            "api_hash": api_hash,
            "phones": phones,
            "proxies": proxies,
            "first_name": (data.get("first_name") or "User").strip(),
            "last_name": (data.get("last_name") or "").strip(),
            "min_delay": min_delay,
            "max_delay": max_delay,
            "max_retry": max_retry,
            "code_timeout": code_timeout,
        }

        with _log_lock:
            LOG_BUF.clear()
            LOG_ID[0] = 0
        with _lock:
            RESULTS.clear()
        _stop.clear()
        provider = WebCodeProvider()
        cfg["provider"] = provider
        STATE["provider"] = provider
        STATE["status"] = "running"

        append_log(f"任务启动：{len(phones)} 个号，代理 {len(proxies)} 个", "info")
        if not proxies:
            append_log("未配置代理，将直连（建议使用 socks5）", "warn")

        _worker_thread = threading.Thread(target=_worker, args=(cfg,), daemon=True)
        _worker_thread.start()
        return jsonify(ok=True, total=len(phones))


@app.post("/stop")
def stop():
    _stop.set()
    p = STATE.get("provider")
    if p:
        p.cancel_all()
    STATE["status"] = "stopped"
    append_log("已请求停止", "warn")
    return jsonify(ok=True)


@app.post("/code")
def put_code():
    data = request.json or {}
    phone = normalize_phone(data.get("phone", ""))
    code = (data.get("code") or "").strip()
    if not phone or not code:
        return jsonify(ok=False, err="缺少手机号或验证码")
    p = STATE.get("provider")
    if not p:
        return jsonify(ok=False, err="当前没有运行中的任务")
    if p.set_code(phone, code):
        append_log(f"{phone} 验证码已录入: {code}", "ok")
        return jsonify(ok=True)
    return jsonify(ok=False, err="该手机号不在等待队列")


@app.get("/state")
def state():
    since = 0
    try:
        since = int(request.args.get("since", 0))
    except Exception:
        pass
    p = STATE.get("provider")
    waiting = p.waiting_phones() if p else []
    with _log_lock:
        new_logs = [l for l in LOG_BUF if l["id"] > since]
    return jsonify({
        "status": STATE["status"],
        "current": STATE["current"],
        "waiting": waiting,
        "logs": new_logs,
        "results": list(RESULTS),
        "sessions": list_session_files(),
        "log_id": LOG_ID[0],
    })


@app.get("/download/<path:name>")
def download(name):
    if not name.endswith(".session") or "/" in name or "\\" in name or ".." in name:
        return jsonify(ok=False, err="非法文件名"), 400
    return send_from_directory(SESSION_DIR, name, as_attachment=True)


# ===================== 前端 =====================
HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram 批量注册机</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:20px; }
  .wrap { max-width: 1080px; margin: 0 auto; display:grid; grid-template-columns: 360px 1fr; gap:16px; }
  .card { background:#181b24; border:1px solid #2a2e3a; border-radius:10px; padding:16px; }
  h1 { font-size:18px; margin:0 0 12px; }
  h2 { font-size:14px; color:#9aa3b5; margin:16px 0 6px; }
  label { display:block; font-size:12px; color:#9aa3b5; margin:10px 0 4px; }
  input, textarea { width:100%; background:#0d0f14; border:1px solid #2a2e3a; color:#e6e6e6; border-radius:6px; padding:8px 10px; font-size:13px; font-family:inherit; }
  textarea { resize: vertical; min-height:70px; }
  .row { display:flex; gap:8px; }
  .row > div { flex:1; }
  button { background:#2f6fed; color:#fff; border:none; border-radius:6px; padding:10px 16px; font-size:14px; cursor:pointer; }
  button.stop { background:#c0392b; }
  button.gray { background:#3a4150; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .status { padding:8px 12px; border-radius:6px; font-size:13px; margin:10px 0; }
  .idle { background:#232733; color:#9aa3b5; }
  .running { background:#1b2b20; color:#5cd67a; }
  .stopped { background:#3a2a2a; color:#e88; }
  .codebox { border:1px solid #5a4a1a; background:#2a2413; border-radius:8px; padding:10px; margin:6px 0; }
  .codebox input { margin:6px 0; }
  .log { background:#0a0c10; border:1px solid #222; border-radius:6px; height:360px; overflow-y:auto; padding:10px; font-family:monospace; font-size:12px; white-space:pre-wrap; word-break:break-all; }
  .log .ok { color:#5cd67a; } .log .warn { color:#e8b45c; } .log .error { color:#ff6b6b; } .log .info { color:#9aa3b5; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { border-bottom:1px solid #242936; padding:6px 8px; text-align:left; }
  .ok-b { color:#5cd67a; } .fail { color:#ff6b6b; }
  a { color:#6ea8ff; text-decoration:none; }
  .muted { color:#6b7384; font-size:12px; }
  @media (max-width:820px){ .wrap { grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>📱 TG 批量注册</h1>

    <label>API ID（my.telegram.org 申请）</label>
    <input id="api_id" placeholder="12345678">

    <label>API Hash</label>
    <input id="api_hash" placeholder="你的 api_hash">

    <label>手机号（每行一个，国际格式 +8613800138000）</label>
    <textarea id="phones" placeholder="+8613800138000&#10;+85261234567"></textarea>

    <label>Socks5 代理（每行 host:port:user:pass，留空则直连）</label>
    <textarea id="proxies" placeholder="127.0.0.1:1080:user:pass&#10;1.2.3.4:1080"></textarea>

    <h2>注册参数</h2>
    <div class="row">
      <div><label>昵称前缀</label><input id="first_name" value="User"></div>
      <div><label>姓氏（可空）</label><input id="last_name" value=""></div>
    </div>
    <div class="row">
      <div><label>最小延迟(s)</label><input id="min_delay" value="30"></div>
      <div><label>最大延迟(s)</label><input id="max_delay" value="90"></div>
    </div>
    <div class="row">
      <div><label>最大重试</label><input id="max_retry" value="3"></div>
      <div><label>等码超时(s)</label><input id="code_timeout" value="180"></div>
    </div>

    <div style="margin-top:14px; display:flex; gap:8px;">
      <button id="btnStart" onclick="start()" style="flex:1">▶ 开始注册</button>
      <button id="btnStop" class="stop" onclick="stop()" disabled>■ 停止</button>
    </div>

    <div id="status" class="status idle">空闲</div>

    <div id="codeArea"></div>

    <h2>注册成功的 Session（可下载复用）</h2>
    <div id="sessions" class="muted">暂无</div>
  </div>

  <div class="card">
    <h2>结果</h2>
    <div style="max-height:180px; overflow:auto;">
      <table><thead><tr><th>手机号</th><th>状态</th><th>说明</th></tr></thead>
      <tbody id="results"><tr><td colspan="3" class="muted">无</td></tr></tbody></table>
    </div>
    <h2>运行日志</h2>
    <div class="log" id="log"></div>
  </div>
</div>

<script>
let lastLogId = 0;
let renderedWaiting = new Set();

async function api(url, body) {
  const r = await fetch(url, body ? {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
  } : { method: 'GET' });
  return r.json();
}

function start() {
  const data = {
    api_id: document.getElementById('api_id').value.trim(),
    api_hash: document.getElementById('api_hash').value.trim(),
    phones: document.getElementById('phones').value,
    proxies: document.getElementById('proxies').value,
    first_name: document.getElementById('first_name').value.trim() || 'User',
    last_name: document.getElementById('last_name').value.trim(),
    min_delay: document.getElementById('min_delay').value,
    max_delay: document.getElementById('max_delay').value,
    max_retry: document.getElementById('max_retry').value,
    code_timeout: document.getElementById('code_timeout').value,
  };
  api('/start', data).then(d => {
    if (!d.ok) { alert(d.err); return; }
    renderedWaiting = new Set();
    document.getElementById('log').innerHTML = '';
    lastLogId = 0;
    setRunning(true);
  });
}

function stop() { api('/stop', {}).then(() => setRunning(false)); }

function setRunning(on) {
  document.getElementById('btnStart').disabled = on;
  document.getElementById('btnStop').disabled = !on;
}

async function submitCode(phone) {
  const code = document.getElementById('code_' + phone.replace('+','')).value.trim();
  if (!code) return;
  const d = await api('/code', { phone, code });
  if (!d.ok) alert(d.err);
  renderedWaiting.delete(phone);
  document.getElementById('codeArea').innerHTML = '';
}

function renderWaiting(list) {
  const area = document.getElementById('codeArea');
  let html = '';
  for (const p of list) {
    if (renderedWaiting.has(p)) continue;
    renderedWaiting.add(p);
    const id = 'code_' + p.replace('+','');
    html += `<div class="codebox">📲 <b>${p}</b> 等待验证码<br>
      <input id="${id}" placeholder="输入短信验证码">
      <button class="gray" onclick="submitCode('${p}')">提交</button></div>`;
  }
  if (html) area.innerHTML = html;
}

function appendLogs(logs) {
  const el = document.getElementById('log');
  for (const l of logs) {
    lastLogId = l.id;
    const d = document.createElement('div');
    d.className = l.level === 'ok' ? 'ok' : l.level;
    d.textContent = `[${l.ts}] ${l.msg}`;
    el.appendChild(d);
  }
  while (el.children.length > 600) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

function renderResults(rs) {
  const el = document.getElementById('results');
  if (!rs.length) { el.innerHTML = '<tr><td colspan="3" class="muted">无</td></tr>'; return; }
  el.innerHTML = rs.map(r =>
    `<tr><td>${r.phone}</td><td class="${r.ok ? 'ok-b' : 'fail'}">${r.ok ? '成功' : '失败'}</td><td>${r.reason||''}</td></tr>`
  ).join('');
}

function renderSessions(list) {
  const el = document.getElementById('sessions');
  if (!list.length) { el.textContent = '暂无'; return; }
  el.innerHTML = list.map(s =>
    `<div><a href="/download/${s.name}">⬇ ${s.name}</a> <span class="muted">${(s.size/1024).toFixed(1)} KB</span></div>`
  ).join('');
}

async function poll() {
  try {
    const d = await api('/state?since=' + lastLogId);
    const st = document.getElementById('status');
    if (d.status === 'running') { st.className = 'status running'; st.textContent = '运行中' + (d.current ? ' · 当前: ' + d.current : ''); setRunning(true); }
    else if (d.status === 'stopped') { st.className = 'status stopped'; st.textContent = '已停止'; }
    else { st.className = 'status idle'; st.textContent = '空闲'; setRunning(false); }
    if (d.logs) appendLogs(d.logs);
    if (d.waiting && d.waiting.length) renderWaiting(d.waiting);
    renderResults(d.results || []);
    renderSessions(d.sessions || []);
  } catch(e) {}
}

poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    os.makedirs(SESSION_DIR, exist_ok=True)
    print(f"Telegram 注册机已启动: http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
