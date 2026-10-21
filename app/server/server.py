#!/usr/bin/env python3
"""NAS Search - FTS5 full-text file search server for fnOS."""
import http.server
import json
import os
import re
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --- Unix socket HTTP server for Python 3.11 ---
class UnixHTTPServer(socketserver.UnixStreamServer, http.server.HTTPServer):
    """HTTP server on a Unix domain socket."""
    def __init__(self, server_address, RequestHandlerClass):
        socketserver.UnixStreamServer.__init__(self, server_address, RequestHandlerClass)
        # HTTPServer expects a server_name; set it after init
        self.server_name = 'nas-search'
        self.server_port = 0

# --- Config ---
DEFAULT_DB_DIR = "/vol1/@appdata/nas-search"
DB_PATTERN = "find_index_vol{idx}.db"
VOLUMES = [1, 2, 3, 4]
SEARCH_LIMIT = 200
PAGE_SIZE = 20
BUILD_SCRIPT = os.environ.get("SEARCH_BUILD_SCRIPT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "build_find_index.py"))

DB_DIR = os.environ.get("SEARCH_DB_DIR", DEFAULT_DB_DIR)
CONF_FILE = os.environ.get("SEARCH_CONF", os.path.join(DB_DIR, "nas_search_config.json"))
PREFIX = os.environ.get("GATEWAY_PREFIX", "/app/nas-search")


def get_db_paths():
    return {idx: os.path.join(DB_DIR, DB_PATTERN.format(idx=idx)) for idx in VOLUMES}


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_db(db_path, keyword, limit=SEARCH_LIMIT, folder=False):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        kw = f"%{_like_escape(keyword)}%"
        if folder:
            rows = conn.execute(
                "SELECT path FROM dirs WHERE name LIKE ? ESCAPE '\\' ORDER BY path LIMIT ?",
                (kw, limit),
            ).fetchall()
            return [{"path": r[0], "size": 0, "is_dir": True} for r in rows]
        rows = conn.execute(
            "SELECT path, size FROM files WHERE name LIKE ? ESCAPE '\\' LIMIT ?",
            (kw, limit),
        ).fetchall()
        return [{"path": r[0], "size": r[1], "is_dir": False} for r in rows]
    finally:
        conn.close()


def db_info(db_path):
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        count = conn.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0]
        size = os.path.getsize(db_path)
        return {"file_count": count, "db_size": size}
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def load_config():
    if os.path.exists(CONF_FILE):
        try:
            with open(CONF_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"db_dir": DB_DIR, "schedule": "daily", "schedule_time": "02:00", "last_rebuild": None}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONF_FILE), exist_ok=True)
    with open(CONF_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def rebuild_index(db_dir=None, callback=None):
    target_dir = db_dir or DB_DIR
    script = BUILD_SCRIPT
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(__file__), "..", "app", "scripts", "build_find_index.py")
        if not os.path.exists(script):
            script = os.path.join(os.path.dirname(__file__), "scripts", "build_find_index.py")
    try:
        env = os.environ.copy()
        env["SEARCH_DB_DIR"] = target_dir
        p = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env
        )
        output = []
        for line in p.stdout:
            output.append(line.strip())
            if callback:
                callback(line.strip())
        p.wait()
        cfg = load_config()
        cfg["last_rebuild"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_config(cfg)
        return {"ok": True, "log": output[-20:], "exit_code": p.returncode}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Rebuild log (shared state) ---
REBUILD_LOG_FILE = os.path.join(DB_DIR, "nas_search_rebuild.log")
REBUILD_RUNNING = False


def rebuild_index_bg(target_dir=None):
    global REBUILD_RUNNING
    REBUILD_RUNNING = True
    target_dir = target_dir or DB_DIR
    script = BUILD_SCRIPT
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(__file__), "..", "app", "scripts", "build_find_index.py")
        if not os.path.exists(script):
            script = os.path.join(os.path.dirname(__file__), "scripts", "build_find_index.py")
    total_files = 0
    try:
        with open(REBUILD_LOG_FILE, "w") as lf:
            lf.write(f"[{time.strftime('%H:%M:%S')}] 开始重建索引...\n")
            lf.flush()
            for vol, root in [("vol1", "/vol1"), ("vol2", "/vol2"), ("vol3", "/vol3"), ("vol4", "/vol4")]:
                db_path = os.path.join(target_dir, f"find_index_{vol}.db")
                cmd = [sys.executable, script, root, db_path]
                lf.write(f"[{time.strftime('%H:%M:%S')}] {vol}: {' '.join(cmd)}\n")
                lf.flush()
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    lf.write(f"[{time.strftime('%H:%M:%S')}] {line.strip()}\n")
                    lf.flush()
                p.wait()
                if p.returncode != 0:
                    lf.write(f"[{time.strftime('%H:%M:%S')}] {vol} FAILED exit={p.returncode}\n")
                else:
                    # count files
                    try:
                        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                        n = conn.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0]
                        conn.close()
                        total_files += n
                        lf.write(f"[{time.strftime('%H:%M:%S')}] {vol}: {n:,} files\n")
                    except Exception:
                        pass
                lf.flush()
            lf.write(f"[{time.strftime('%H:%M:%S')}] ALL DONE {total_files:,} files\n")
        cfg = load_config()
        cfg["last_rebuild"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_config(cfg)
    except Exception as e:
        try:
            with open(REBUILD_LOG_FILE, "a") as lf:
                lf.write(f"[{time.strftime('%H:%M:%S')}] 错误: {e}\n")
        except Exception:
            pass
    finally:
        REBUILD_RUNNING = False


# --- HTTP Server ---
class SearchHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _strip_prefix(self, path):
        if path.startswith(PREFIX):
            return path[len(PREFIX):] or "/"
        return path

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self._strip_prefix(urlparse(self.path).path)
        qs = parse_qs(urlparse(self.path).query)

        if path == "/" or path == "/index.html":
            self._send_html(HTML_PAGE)
        elif path == "/api/search":
            keyword = qs.get("q", [""])[0].strip()
            vol = qs.get("vol", ["all"])[0]
            limit = int(qs.get("limit", [str(SEARCH_LIMIT)])[0])
            if not keyword:
                self._send_json({"error": "missing q"}, 400)
                return
            folder_only = qs.get("folder", [""])[0] == "1"
            results = []
            db_paths = get_db_paths()
            vols_to_search = [int(vol)] if vol.isdigit() and int(vol) in VOLUMES else VOLUMES
            for idx in vols_to_search:
                dbp = db_paths.get(idx)
                if dbp and os.path.exists(dbp):
                    try:
                        if folder_only:
                            rows = search_db(dbp, keyword, limit, folder=True)
                        else:
                            rows = search_db(dbp, keyword, limit, folder=False)
                            rows += search_db(dbp, keyword, limit, folder=True)
                        for r in rows:
                            r["vol"] = idx
                        results.extend(rows)
                    except Exception as e:
                        results.append({"error": str(e), "vol": idx})
            for r in results:
                if "path" in r and "error" not in r:
                    try: r["mtime"] = os.path.getmtime(r["path"])
                    except Exception: r["mtime"] = 0
            results.sort(key=lambda x: x.get("mtime", 0), reverse=True)
            self._send_json({"results": results, "total": len(results)})
        elif path == "/api/config":
            self._send_json(load_config())
        elif path == "/api/status":
            info = {"db_dir": DB_DIR, "volumes": {}, "config": load_config(), "rebuild_running": REBUILD_RUNNING}
            for idx, dbp in get_db_paths().items():
                info["volumes"][f"vol{idx}"] = db_info(dbp) or {"file_count": 0}
            self._send_json(info)
        elif path == "/api/rebuild-log":
            log_text = ""
            if os.path.exists(REBUILD_LOG_FILE):
                try:
                    with open(REBUILD_LOG_FILE) as f:
                        log_text = f.read()
                except Exception:
                    pass
            self._send_json({"log": log_text, "running": REBUILD_RUNNING})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self._strip_prefix(urlparse(self.path).path)
        if path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                cfg = json.loads(self.rfile.read(length)) if length else {}
                old = load_config()
                old.update(cfg)
                save_config(old)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
        elif path == "/api/rebuild":
            if REBUILD_RUNNING:
                self._send_json({"ok": False, "error": "正在重建中，请等待完成"})
                return
            t = threading.Thread(target=rebuild_index_bg, daemon=True)
            t.start()
            self._send_json({"ok": True, "message": "已在后台开始重建"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# --- HTML Page v2 (no emoji, clean text icons, mobile-first, debug logging) ---
HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>NAS 全盘搜索</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#f0f2f5;color:#333;min-height:100vh}
.header{background:#1677ff;color:#fff;padding:14px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
.header h1{font-size:18px;font-weight:600}
.header .gear{font-size:20px;cursor:pointer;opacity:.9;user-select:none;padding:4px 8px;border-radius:6px}
.header .gear:active{opacity:1;background:rgba(255,255,255,.2)}
.search-box{padding:12px 16px;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06);position:sticky;top:46px;z-index:49}
.search-row{display:flex;gap:8px}
.search-input{flex:1;padding:10px 14px;border:2px solid #e8e8e8;border-radius:8px;font-size:15px;outline:none;transition:border .2s;-webkit-appearance:none}
.search-input:focus{border-color:#1677ff}
.search-btn{padding:10px 20px;background:#1677ff;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:500;cursor:pointer;white-space:nowrap;-webkit-tap-highlight-color:transparent;min-width:64px}
.search-btn:active{background:#0958d9}
.filters{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.filter-btn{padding:5px 12px;border:1.5px solid #d9d9d9;border-radius:14px;background:#fff;cursor:pointer;font-size:12px;transition:all .15s;-webkit-tap-highlight-color:transparent}
.filter-btn.active{background:#1677ff;color:#fff;border-color:#1677ff}
.results{padding:12px 16px}
.result-item{background:#fff;border-radius:8px;padding:12px 14px;margin-bottom:8px;box-shadow:0 1px 2px rgba(0,0,0,.04);display:flex;align-items:flex-start;gap:10px}
.result-icon{font-size:13px;font-weight:700;flex-shrink:0;width:36px;height:36px;border-radius:6px;display:flex;align-items:center;justify-content:center;background:#f5f5f5;color:#888}
.result-info{flex:1;min-width:0}
.result-name{font-size:14px;font-weight:500;color:#1677ff;word-break:break-all;margin-bottom:2px;line-height:1.4}
.result-path{font-size:11px;color:#999;word-break:break-all;line-height:1.3}
.result-size{font-size:12px;color:#666;white-space:nowrap;flex-shrink:0;margin-top:2px}
.result-actions{display:flex;gap:4px;flex-shrink:0;margin-top:2px}
.btn-copy{padding:4px 10px;border:1px solid #d9d9d9;border-radius:4px;background:#fff;cursor:pointer;font-size:11px;color:#666;-webkit-tap-highlight-color:transparent}
.btn-copy:active{background:#f0f0f0}
.empty{text-align:center;padding:48px 20px;color:#bbb}
.empty-icon{font-size:36px;margin-bottom:8px;color:#d9d9d9}
.loading{text-align:center;padding:24px;color:#999}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.45);z-index:100;display:flex;align-items:flex-end;justify-content:center}
@media(min-width:600px){.modal-overlay{align-items:center}}
.modal{background:#fff;border-radius:12px 12px 0 0;padding:20px 16px 24px;width:100%;max-width:500px;max-height:90vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
@media(min-width:600px){.modal{border-radius:12px;margin:20px}}
.modal h2{font-size:17px;margin-bottom:16px;font-weight:600}
.modal label{display:block;font-size:12px;color:#888;margin-bottom:3px;margin-top:8px}
.modal input,.modal select{width:100%;padding:9px 12px;border:2px solid #e8e8e8;border-radius:8px;font-size:14px;margin-bottom:12px;outline:none;-webkit-appearance:none;background:#fff}
.modal input:focus,.modal select:focus{border-color:#1677ff}
.modal select{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23888' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px;flex-wrap:wrap}
.btn{padding:9px 18px;border-radius:8px;font-size:14px;cursor:pointer;border:none;font-weight:500;-webkit-tap-highlight-color:transparent}
.btn-primary{background:#1677ff;color:#fff}
.btn-primary:active{background:#0958d9}
.btn-secondary{background:#f5f5f5;color:#333}
.btn-secondary:active{background:#e8e8e8}
.btn-danger{background:#ff4d4f;color:#fff}
.btn-danger:active{background:#d9363e}
.btn:disabled{opacity:.4;pointer-events:none}
.pg-btn{min-width:32px;height:32px;line-height:32px;text-align:center;border:1px solid #d9d9d9;border-radius:6px;background:#fff;cursor:pointer;font-size:13px;color:#333;padding:0 8px;transition:all .15s}
.pg-btn:hover:not(:disabled){border-color:#1677ff;color:#1677ff}
.pg-btn:disabled{opacity:.4;cursor:default}
.pg-btn.pg-now{background:#1677ff;color:#fff;border-color:#1677ff;font-weight:600}
.pg-dots{color:#999;padding:0 2px;font-size:13px}
.pn-dots{display:inline-block;width:32px;height:32px;line-height:32px;text-align:center;font-size:13px;color:#999;user-select:none}
.status-line{font-size:11px;color:#999;margin-top:6px;line-height:1.5}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.8);color:#fff;padding:10px 20px;border-radius:8px;font-size:13px;z-index:200;opacity:0;transition:opacity .3s;pointer-events:none;max-width:90vw;text-align:center}
.toast.show{opacity:1}
.rebuild-log{background:#1a1a2e;color:#a0ffa0;font-family:'SF Mono',Monaco,'Cascadia Code',monospace;font-size:11px;padding:10px;border-radius:6px;max-height:180px;overflow-y:auto;margin-top:8px;white-space:pre-wrap;line-height:1.5}
.confirm-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);z-index:300;display:flex;align-items:center;justify-content:center}
.confirm-box{background:#fff;border-radius:12px;padding:24px 20px;text-align:center;max-width:300px;width:90%}
.confirm-box p{font-size:15px;margin-bottom:20px;line-height:1.5}
.confirm-box button{margin:0 6px;min-width:72px}
.debug-bar{background:#fffbe6;border-bottom:1px solid #ffe58f;padding:6px 16px;font-size:10px;color:#ad8b00;font-family:monospace;display:none;word-break:break-all}
</style>
</head>
<body>
<div class="header">
  <h1>NAS 全盘搜索</h1>
  <span class="gear" id="gearBtn" title="设置">&#x2699;</span>
</div>
<div class="debug-bar" id="debugBar"></div>
<div class="search-box">
  <div class="search-row">
    <input class="search-input" id="searchInput" placeholder="输入关键词搜索文件..." autofocus>
    <button class="search-btn" id="searchBtn">搜索</button>
  </div>
  <div class="filters" id="filters">
    <span class="filter-btn active" data-vol="all">全部</span>
    <span class="filter-btn" data-vol="1">vol1</span>
    <span class="filter-btn" data-vol="2">vol2</span>
    <span class="filter-btn" data-vol="3">vol3</span>
    <span class="filter-btn" data-vol="4">vol4</span>
  </div>
  <div class="filters" id="typeFilters" style="margin-top:6px">
    <span class="filter-btn active" data-type="all">全部类型</span>
    <span class="filter-btn" data-type="video">视频</span>
    <span class="filter-btn" data-type="audio">音频</span>
    <span class="filter-btn" data-type="image">图片</span>
    <span class="filter-btn" data-type="doc">文档</span>
    <span class="filter-btn" data-type="archive">压缩包</span>
    <span class="filter-btn" data-type="folder">📁 文件夹</span>
    <span class="filter-btn" data-type="other">其他</span>
  </div>
</div>
<div class="results" id="results">
  <div class="empty"><div class="empty-icon">&#x1F50D;</div><div>输入关键词开始搜索</div></div>
</div>
<div id="pager" style="display:none;text-align:center;padding:8px 16px 16px"></div>
<div id="toast" class="toast"></div>

<div id="settingsOverlay" class="modal-overlay" style="display:none">
  <div class="modal">
    <h2>索引设置</h2>
    <label>索引库目录</label>
    <input type="text" id="cfgDbDir" placeholder="/vol1/@apphome/hermes-agent/data">
    <label>自动重建计划</label>
    <select id="cfgSchedule">
      <option value="off">关闭</option>
      <option value="daily">每天</option>
      <option value="weekly">每周</option>
    </select>
    <label>执行时间 (HH:MM)</label>
    <input type="text" id="cfgTime" placeholder="02:00" maxlength="5">
    <hr style="margin:14px 0;border:none;border-top:1px solid #f0f0f0">
    <div id="rebuildSection">
      <button class="btn btn-primary" id="rebuildBtn" style="width:100%;margin-bottom:10px">立即重建索引</button>
      <div class="status-line" id="statusLine">加载中...</div>
      <div class="rebuild-log" id="rebuildLog" style="display:none"></div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-danger" id="resetBtn" style="margin-right:auto">恢复默认</button>
      <button class="btn btn-secondary" id="closeSettings">关闭</button>
      <button class="btn btn-primary" id="saveSettings">保存</button>
    </div>
  </div>
</div>

<div id="confirmOverlay" class="confirm-overlay" style="display:none">
  <div class="confirm-box">
    <p id="confirmMsg"></p>
    <button class="btn btn-primary" id="confirmYes">确定</button>
    <button class="btn btn-secondary" id="confirmNo">取消</button>
  </div>
</div>

<script>
(function() {
  'use strict';
  var DBG = document.getElementById('debugBar');

  function log(msg) {
    DBG.style.display = 'block';
    DBG.textContent = '[DEBUG] ' + msg;
    console.log('[nas-search]', msg);
  }

  try {
    var API_BASE = (function() {
      var p = location.pathname.replace(/\\/index\\.html$/, '').replace(/\\/$/, '');
      var base = p + '/api';
      log('API_BASE=' + base + ' | pathname=' + location.pathname);
      return base;
    })();
    var currentVol = 'all';
    var currentType = 'all';
    var currentPage = 1;
    var PAGE_SIZE = 20;
    var allResults = [];
    var rebuildPollTimer = null;

    function showToast(msg) {
      var t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      clearTimeout(t._timer);
      t._timer = setTimeout(function() { t.classList.remove('show'); }, 2500);
    }

    function fmtSize(bytes) {
      if (!bytes || bytes === 0) return '0 B';
      var units = ['B', 'KB', 'MB', 'GB', 'TB'];
      var i = Math.floor(Math.log(bytes) / Math.log(1024));
      if (i >= units.length) i = units.length - 1;
      return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
    }

    function typeIcon(path, isDir) {
      if (isDir) return '📂';
      var ext = (path || '').split('.').pop().toLowerCase();
      var v=['mp4','mkv','avi','mov','flv','wmv','webm','rmvb','ts','m2ts'];
      var a=['mp3','flac','wav','aac','ogg','wma','m4a','ape'];
      var i=['jpg','jpeg','png','gif','webp','bmp','svg','ico','heic'];
      var d=['pdf','doc','docx','xls','xlsx','ppt','pptx','txt','md','csv'];
      var z=['zip','rar','7z','tar','gz','bz2','xz','iso'];
      if (v.indexOf(ext)>=0) return '🎬';
      if (a.indexOf(ext)>=0) return '🎵';
      if (i.indexOf(ext)>=0) return '🖼️';
      if (d.indexOf(ext)>=0) return '📄';
      if (z.indexOf(ext)>=0) return '📦';
      return '📄';
    }

    function fileType(path) {
      var ext = (path || '').split('.').pop().toLowerCase();
      var v=['mp4','mkv','avi','mov','flv','wmv','webm','rmvb','ts','m2ts'];
      var a=['mp3','flac','wav','aac','ogg','wma','m4a','ape','dts','ac3'];
      var i=['jpg','jpeg','png','gif','webp','bmp','svg','ico','heic','raw','tif','tiff'];
      var d=['pdf','doc','docx','xls','xlsx','ppt','pptx','odt','ods','odp','pages','numbers','key'];
      var z=['zip','rar','7z','tar','gz','bz2','xz','iso','dmg','pkg'];
      if (v.indexOf(ext)>=0) return 'video';
      if (a.indexOf(ext)>=0) return 'audio';
      if (i.indexOf(ext)>=0) return 'image';
      if (d.indexOf(ext)>=0) return 'doc';
      if (z.indexOf(ext)>=0) return 'archive';
      return 'other';
    }

    function filterByType(results, type) {
      if (type === 'all') return results;
      if (type === 'folder') return results.filter(function(r) { return r.is_dir === true; });
      return results.filter(function(r) { return fileType(r.path) === type; });
    }

    function renderPage() {
      var filtered = filterByType(allResults, currentType);
      var total = filtered.length;
      var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      if (currentPage > totalPages) currentPage = totalPages;
      var start = (currentPage - 1) * PAGE_SIZE;
      var page = filtered.slice(start, start + PAGE_SIZE);
      renderResults(page);
      renderPager(total, totalPages);
    }

    function goPage(n) {
      var filtered = filterByType(allResults, currentType);
      var totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
      var np = n;
      if (typeof n === 'number') np = n;
      if (np < 1) np = 1;
      if (np > totalPages) np = totalPages;
      currentPage = np;
      renderPage();
      window.scrollTo(0, 0);
    }

    function renderPager(total, totalPages) {
      var pager = document.getElementById('pager');
      if (total <= PAGE_SIZE) { pager.innerHTML = ''; pager.style.display = 'none'; return; }
      pager.style.display = 'block';
      var info = (currentPage-1)*PAGE_SIZE+1 + '-' + Math.min(currentPage*PAGE_SIZE, total) + ' / ' + total + ' \u6761\uFF08\u5171 ' + totalPages + ' \u9875\uFF09';
      var html = '<div style="display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap;padding:12px 0;font-size:13px">';
      html += '<span style="color:#888;margin-right:6px">' + info + '</span>';
      html += '<button class="pg-btn" data-pg="prev" ' + (currentPage<=1?'disabled':'') + '>\u2190</button>';
      var s = Math.max(1, currentPage-3), e = Math.min(totalPages, currentPage+3);
      if (s > 1) { html += '<button class="pg-btn" data-pg="1">1</button>'; if (s>2) html += '<span class="pg-dots">...</span>'; }
      for (var i = s; i <= e; i++) {
        html += '<button class="pg-btn' + (i===currentPage?' pg-now':'') + '" data-pg="' + i + '">' + i + '</button>';
      }
      if (e < totalPages) { if (e<totalPages-1) html += '<span class="pg-dots">...</span>'; html += '<button class="pg-btn" data-pg="' + totalPages + '">' + totalPages + '</button>'; }
      html += '<button class="pg-btn" data-pg="next" ' + (currentPage>=totalPages?'disabled':'') + '>\u2192</button>';
      html += '</div>';
      pager.innerHTML = html;
    }

    function renderResults(results) {
      var container = document.getElementById('results');
      if (!results || results.length === 0) {
        container.innerHTML = '<div class="empty"><div class="empty-icon">&#x1F4ED;</div><div>未找到匹配文件</div></div>';
        return;
      }
      log('渲染 ' + results.length + ' 条结果');
      var html = '';
      for (var i = 0; i < results.length; i++) {
        var r = results[i];
        var parts = r.path.split('/');
        var name = parts[parts.length - 1];
        var dir = parts.slice(0, -1).join('/');
        html += '<div class="result-item">' +
          '<div class="result-icon">' + typeIcon(r.path, r.is_dir) + '</div>' +
          '<div class="result-info">' +
            '<div class="result-name">' + esc(name) + '</div>' +
            '<div class="result-path">' + esc(dir) + '</div>' +
          '</div>' +
          '<div class="result-size">' + (r.is_dir ? '' : fmtSize(r.size)) + '</div>' +
          '<div class="result-actions">' +
            '<button class="btn-copy" data-path="' + escAttr(r.path) + '">复制</button>' +
          '</div>' +
        '</div>';
      }
      container.innerHTML = html;
      var btns = container.querySelectorAll('.btn-copy');
      for (var j = 0; j < btns.length; j++) {
        btns[j].addEventListener('click', function() { copyPath(this.dataset.path); });
      }
    }

    function esc(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function escAttr(s) {
      return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    }

    function copyPath(path) {
      log('复制: ' + path);
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(path).then(function() {
          showToast('已复制路径');
        })['catch'](function() { fallbackCopy(path); });
      } else {
        fallbackCopy(path);
      }
    }
    function fallbackCopy(text) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;top:-9999px';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); showToast('已复制路径'); } catch(e) { showToast('复制失败'); }
      document.body.removeChild(ta);
    }

    function doSearch() {
      var q = document.getElementById('searchInput').value.trim();
      if (!q) { showToast('请输入关键词'); return; }
      var url = API_BASE + '/search?q=' + encodeURIComponent(q) + '&vol=' + currentVol + '&limit=200' + (currentType === 'folder' ? '&folder=1' : '');
      log('搜索: ' + url);
      document.getElementById('results').innerHTML = '<div class="loading">搜索中...</div>';
      document.getElementById('pager').style.display = 'none';
      fetch(url).then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function(data) {
        allResults = data.results || data || [];
        currentPage = 1;
        log('收到 ' + allResults.length + ' 条（共 ' + (data.total || allResults.length) + '）');
        renderPage();
      })['catch'](function(e) {
        log('搜索失败: ' + e.message);
        document.getElementById('results').innerHTML =
          '<div class="empty"><div class="empty-icon">&#x26A0;</div><div>搜索失败: ' + esc(e.message) + '</div></div>';
      });
    }

    function loadStatus() {
      fetch(API_BASE + '/status').then(function(r) { return r.json(); }).then(function(data) {
        var vols = data.volumes || {};
        var parts = [];
        for (var k in vols) {
          var fc = vols[k].file_count || 0;
          parts.push(k + ':' + fc.toLocaleString());
        }
        var last = (data.config && data.config.last_rebuild) || '从未重建';
        document.getElementById('statusLine').textContent =
          '最近重建: ' + last + ' | ' + parts.join(', ');
        if (data.rebuild_running) startRebuildPoll();
      })['catch'](function() {
        document.getElementById('statusLine').textContent = '加载状态失败';
      });
    }

    function loadConfig() {
      fetch(API_BASE + '/config').then(function(r) { return r.json(); }).then(function(cfg) {
        document.getElementById('cfgDbDir').value = cfg.db_dir || '';
        document.getElementById('cfgSchedule').value = cfg.schedule || 'off';
        document.getElementById('cfgTime').value = cfg.schedule_time || '02:00';
      })['catch'](function() {});
    }

    function saveConfig() {
      var cfg = {
        db_dir: document.getElementById('cfgDbDir').value.trim(),
        schedule: document.getElementById('cfgSchedule').value,
        schedule_time: document.getElementById('cfgTime').value.trim()
      };
      fetch(API_BASE + '/config', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(cfg)
      }).then(function(r) { return r.json(); }).then(function(resp) {
        if (resp.ok) showToast('设置已保存');
        else showToast('保存失败: ' + (resp.error || ''));
      })['catch'](function(e) { showToast('请求失败: ' + e.message); });
    }

    function resetConfig() {
      showConfirm('确定恢复默认设置吗？', function() {
        fetch(API_BASE + '/config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({db_dir:'',schedule:'daily',schedule_time:'02:00'})
        }).then(function(r) { return r.json(); }).then(function() {
          loadConfig(); loadStatus(); showToast('已恢复默认设置');
        });
      });
    }

    function showConfirm(msg, onYes) {
      document.getElementById('confirmMsg').textContent = msg;
      document.getElementById('confirmOverlay').style.display = 'flex';
      var yes = document.getElementById('confirmYes');
      var no = document.getElementById('confirmNo');
      var done = false;
      function cleanup() {
        if (done) return; done = true;
        document.getElementById('confirmOverlay').style.display = 'none';
        yes.removeEventListener('click', handler);
        no.removeEventListener('click', cleanup);
      }
      function handler() { cleanup(); onYes(); }
      yes.addEventListener('click', handler);
      no.addEventListener('click', cleanup);
    }

    function triggerRebuild() {
      var btn = document.getElementById('rebuildBtn');
      btn.disabled = true;
      btn.textContent = '启动中...';
      fetch(API_BASE + '/rebuild', { method: 'POST' }).then(function(r) { return r.json(); }).then(function(resp) {
        if (resp.ok) {
          showToast('重建已开始');
          btn.textContent = '重建中...';
          startRebuildPoll();
        } else {
          showToast('启动失败: ' + (resp.error || ''));
          btn.disabled = false;
          btn.textContent = '立即重建索引';
        }
      })['catch'](function(e) {
        showToast('请求失败: ' + e.message);
        btn.disabled = false;
        btn.textContent = '立即重建索引';
      });
    }

    function startRebuildPoll() {
      var logEl = document.getElementById('rebuildLog');
      logEl.style.display = 'block';
      stopRebuildPoll();
      rebuildPollTimer = setInterval(function() {
        fetch(API_BASE + '/rebuild-log').then(function(r) { return r.json(); }).then(function(data) {
          if (data.log) logEl.textContent = data.log;
          if (!data.running) {
            stopRebuildPoll();
            var btn = document.getElementById('rebuildBtn');
            if (btn) { btn.disabled = false; btn.textContent = '立即重建索引'; }
            showToast('索引重建完成');
            loadStatus();
            if (data.log) logEl.textContent = data.log + '\\n--- 重建完成 ---';
          }
        })['catch'](function() {});
      }, 1000);
    }

    function stopRebuildPoll() {
      if (rebuildPollTimer) { clearInterval(rebuildPollTimer); rebuildPollTimer = null; }
    }

    // === Init ===
    log('初始化开始');

    var searchBtn = document.getElementById('searchBtn');
    var searchInput = document.getElementById('searchInput');
    if (searchBtn) {
      searchBtn.addEventListener('click', doSearch);
      log('搜索按钮已绑定');
    } else {
      log('ERROR: 找不到 searchBtn');
    }
    if (searchInput) {
      searchInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); doSearch(); }
      });
      log('输入框回车已绑定');
    }

    var filterBtns = document.querySelectorAll('#filters .filter-btn');
    for (var i = 0; i < filterBtns.length; i++) {
      filterBtns[i].addEventListener('click', function() {
        var all = document.querySelectorAll('#filters .filter-btn');
        for (var j = 0; j < all.length; j++) all[j].classList.remove('active');
        this.classList.add('active');
        currentVol = this.dataset.vol;
        doSearch();
      });
    }

    var typeBtns = document.querySelectorAll('#typeFilters .filter-btn');
    for (var k = 0; k < typeBtns.length; k++) {
      typeBtns[k].addEventListener('click', function() {
        var all = document.querySelectorAll('#typeFilters .filter-btn');
        for (var j = 0; j < all.length; j++) all[j].classList.remove('active');
        this.classList.add('active');
        currentType = this.dataset.type;
        currentPage = 1;
        renderPage();
      });
    }

    var prevBtn = document.getElementById('prevPage');
    var nextBtn = document.getElementById('nextPage');

    // pager delegation — buttons created dynamically via innerHTML
    var pagerEl = document.getElementById('pager');
    if (pagerEl) {
      pagerEl.addEventListener('click', function(e) {
        var btn = e.target.closest('.pg-btn');
        if (!btn || btn.disabled) return;
        var pg = btn.dataset.pg;
        if (pg === 'prev') goPage(currentPage - 1);
        else if (pg === 'next') goPage(currentPage + 1);
        else goPage(parseInt(pg, 10));
      });
    }

    var gearBtn = document.getElementById('gearBtn');
    if (gearBtn) {
      gearBtn.addEventListener('click', function() {
        document.getElementById('settingsOverlay').style.display = 'flex';
        loadStatus();
        loadConfig();
      });
    }

    var closeSettings = document.getElementById('closeSettings');
    if (closeSettings) closeSettings.addEventListener('click', function() {
      document.getElementById('settingsOverlay').style.display = 'none';
      stopRebuildPoll();
    });
    var saveSettings = document.getElementById('saveSettings');
    if (saveSettings) saveSettings.addEventListener('click', saveConfig);
    var resetBtn = document.getElementById('resetBtn');
    if (resetBtn) resetBtn.addEventListener('click', resetConfig);
    var rebuildBtn = document.getElementById('rebuildBtn');
    if (rebuildBtn) rebuildBtn.addEventListener('click', triggerRebuild);

    // 初始检查
    fetch(API_BASE + '/status').then(function(r) { return r.json(); }).then(function(data) {
      log('状态加载成功');
      if (data.rebuild_running) {
        var btn = document.getElementById('rebuildBtn');
        if (btn) { btn.disabled = true; btn.textContent = '重建中...'; }
        document.getElementById('rebuildLog').style.display = 'block';
        startRebuildPoll();
      }
    })['catch'](function(e) { log('状态加载失败: ' + e.message); });

    log('初始化完成 - 可以搜索了');

  } catch(e) {
    log('FATAL: ' + e.message);
    document.getElementById('results').innerHTML =
      '<div class="empty"><div class="empty-icon">&#x26A0;</div><div>页面初始化失败: ' + e.message + '</div></div>';
  }
})();
</script>
</body>
</html>"""


def main():
    if len(sys.argv) < 2:
        print("Usage: server.py <unix-socket-path>", file=sys.stderr)
        sys.exit(1)
    socket_path = sys.argv[1]
    sock_dir = os.path.dirname(socket_path)
    os.makedirs(sock_dir, exist_ok=True)
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = UnixHTTPServer(socket_path, SearchHandler)
    os.chmod(socket_path, 0o777)
    print(f"Listening on {socket_path}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
