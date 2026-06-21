#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Usage Widget
===============
Claude Pro / ChatGPT Plus(など)の「サブスク利用枠」を、PC常駐の小さな
フローティング・ウィジェットとしてリアルタイム表示するツールです。
各枠の「残り%」と「リセットまでの時間」をバー表示します。

────────────────────────────────────────────────────────────────────────
【前提 / How it works】
- サブスク利用枠の "残り" を返す公式APIは存在しません。各公式クライアントが
  内部で使うエンドポイントを、ローカルの認証情報で取得します(非公式)。
- ChatGPT/Codex: ~/.codex/auth.json のトークンで
    GET https://chatgpt.com/backend-api/wham/usage
- Claude: claude.ai の sessionKey クッキーで
    GET https://claude.ai/api/organizations/{org}/usage
  ※ claude.ai は Cloudflare 配下のため、まれに standalone アプリからの取得が
    ブロックされることがあります(ブラウザ拡張ならブロックされません)。
  ※ Claude Code の OAuth トークンを api.anthropic.com に使う方式は規約違反 &
    サーバ側ブロック(アカBANリスク)のため使いません。

【依存】  pip install requests   (tkinter は Python 標準)
【起動】  python ai_usage_widget.py   /   常駐: pythonw ai_usage_widget.py
【設定】  ウィジェット右上の ⚙、または右クリックメニューから。
────────────────────────────────────────────────────────────────────────
"""

import base64
import json
import os
import queue
import socket
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.stderr.write("requests がありません。先に `pip install requests` を実行してください。\n")
    sys.exit(1)

import tkinter as tk
import tkinter.font as tkfont


# ─────────────────────────── 設定 ───────────────────────────

CONFIG_DIR = Path.home() / ".ai-usage-widget"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "refresh_minutes": 5,
    "low_threshold_pct": 20,
    "alert_sound": True,
    "ui_scale": 1.0,
    "window": {"x": None, "y": None},
    "providers": {
        "chatgpt": {"enabled": True, "auth_path": ""},
        "claude": {"enabled": True, "session_key": ""},
        "custom": [],
    },
    "cache": {},
}

REQUEST_TIMEOUT = 12
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    return _merge_defaults(cfg, DEFAULT_CONFIG)


def _merge_defaults(cfg: dict, defaults: dict) -> dict:
    out = json.loads(json.dumps(defaults))
    for k, v in (cfg or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_defaults(v, out[k])
        else:
            out[k] = v
    return out


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


# ─────────────────────────── EXE / OS まわり ───────────────────────────

APP_RUN_NAME = "AIUsageWidget"
_SINGLE_INSTANCE_SOCK = None


def log_error(exc: BaseException) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "error.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}]\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def acquire_single_instance(port: int = 49219) -> bool:
    global _SINGLE_INSTANCE_SOCK
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _SINGLE_INSTANCE_SOCK = s
        return True
    except OSError:
        return False
    except Exception:
        return True


def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = sys.executable
    if os.path.basename(pyw).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(pyw), "pythonw.exe")
        if os.path.exists(cand):
            pyw = cand
    return f'"{pyw}" "{os.path.abspath(__file__)}"'


def startup_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
        ) as k:
            winreg.QueryValueEx(k, APP_RUN_NAME)
        return True
    except Exception:
        return False


def set_startup(enable: bool) -> None:
    if os.name != "nt":
        return
    import winreg
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    if enable:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_RUN_NAME, 0, winreg.REG_SZ, _startup_command())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, APP_RUN_NAME)
        except FileNotFoundError:
            pass


def enable_dpi_awareness() -> None:
    """高DPIで文字がにじまないよう Per-Monitor v2 を有効化(Windows)。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            # Per-Monitor v2 (DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4)
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def beep_alert() -> None:
    if os.name != "nt":
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


# ─────────────────────────── ユーティリティ ───────────────────────────

def human_duration(seconds) -> str:
    if seconds is None:
        return ""
    s = int(max(0, seconds))
    if s >= 86400:
        return f"{s // 86400}日{(s % 86400) // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


def iso_to_reset_seconds(iso_str):
    if not iso_str or not isinstance(iso_str, str):
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


def decode_jwt_unverified(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def clamp_pct(x) -> float:
    try:
        return max(0.0, min(100.0, float(x)))
    except Exception:
        return 0.0


def label_for_window_seconds(win_s, default):
    if not win_s:
        return default
    if win_s >= 518400:
        return "週"
    hours = round(win_s / 3600)
    return f"{hours}時間" if hours < 48 else f"{round(win_s / 86400)}日"


def _walk_used_percent(obj, name_hint=None, found=None):
    if found is None:
        found = []
    if isinstance(obj, dict):
        pct = None
        for key in ("used_percent", "utilization_pct", "utilization", "used_pct"):
            if key in obj and isinstance(obj[key], (int, float)):
                pct = obj[key]
                break
        if pct is not None:
            reset_s = obj.get("reset_after_seconds")
            if reset_s is None:
                reset_s = iso_to_reset_seconds(obj.get("resets_at") or obj.get("reset_at"))
            found.append({"hint": name_hint, "used": pct, "reset_s": reset_s,
                          "win_s": obj.get("limit_window_seconds")})
        for k, v in obj.items():
            _walk_used_percent(v, name_hint=k, found=found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_used_percent(v, name_hint=name_hint, found=found)
    return found


# ─────────────────────────── ChatGPT / Codex ───────────────────────────

def find_codex_auth_path(override: str):
    if override:
        p = Path(os.path.expanduser(override))
        return p if p.exists() else None
    candidates = []
    if os.environ.get("CODEX_HOME"):
        candidates.append(Path(os.environ["CODEX_HOME"]) / "auth.json")
    candidates.append(Path.home() / ".codex" / "auth.json")
    if os.name == "nt" and os.environ.get("USERPROFILE"):
        candidates.append(Path(os.environ["USERPROFILE"]) / ".codex" / "auth.json")
    for c in candidates:
        if c.exists():
            return c
    return None


def get_codex_credentials(auth_path: Path):
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    tokens = data.get("tokens") or {}
    access_token = tokens.get("access_token") or data.get("access_token")
    if not access_token:
        raise RuntimeError("auth.json に access_token がありません")
    account_id = tokens.get("account_id") or data.get("account_id")
    if not account_id:
        claims = decode_jwt_unverified(access_token)
        auth_claim = claims.get("https://api.openai.com/auth", {}) or {}
        account_id = (auth_claim.get("chatgpt_account_id")
                      or auth_claim.get("organization_id")
                      or claims.get("chatgpt_account_id"))
    return access_token, account_id


def fetch_chatgpt(pcfg: dict) -> dict:
    result = {"label": "ChatGPT", "ok": False, "plan": "", "windows": [], "error": ""}
    auth_path = find_codex_auth_path(pcfg.get("auth_path", ""))
    if not auth_path:
        result["error"] = "Codex 未ログイン (~/.codex/auth.json なし)"
        return result
    try:
        token, account_id = get_codex_credentials(auth_path)
    except Exception as e:
        result["error"] = f"認証情報エラー: {e}"
        return result

    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli",
               "originator": "codex_cli_rs", "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        r = requests.get("https://chatgpt.com/backend-api/wham/usage",
                         headers=headers, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        result["error"] = f"通信失敗: {e}"
        return result
    if r.status_code in (401, 403):
        result["error"] = "トークン期限切れ (codex を一度起動)"
        return result
    if not r.ok:
        result["error"] = f"HTTP {r.status_code}"
        return result
    try:
        data = r.json()
    except Exception:
        result["error"] = "応答が JSON でない"
        return result

    result["plan"] = str(data.get("plan_type") or "")
    rl = data.get("rate_limit") or {}

    def win(node, default):
        if not isinstance(node, dict):
            return None
        pct = node.get("used_percent")
        if pct is None:
            pct = node.get("utilization_pct")
        if pct is None:
            return None
        reset_s = node.get("reset_after_seconds")
        if reset_s is None:
            reset_s = iso_to_reset_seconds(node.get("resets_at") or node.get("reset_at"))
        return {"name": label_for_window_seconds(node.get("limit_window_seconds"), default),
                "used": clamp_pct(pct), "reset_s": reset_s}

    windows = [w for w in (win(rl.get("primary_window"), "セッション"),
                           win(rl.get("secondary_window"), "週")) if w]
    if not windows:
        for f in _walk_used_percent(rl or data)[:2]:
            windows.append({"name": label_for_window_seconds(f.get("win_s"), "枠"),
                            "used": clamp_pct(f["used"]), "reset_s": f.get("reset_s")})
    if not windows:
        result["error"] = "利用枠フィールドが見つからない"
        return result
    result["windows"] = windows
    result["ok"] = True
    return result


# ─────────────────────────── Claude ───────────────────────────

CLAUDE_USAGE_PATHS = [
    "/api/organizations/{uuid}/usage",
    "/api/organizations/{uuid}/usage_summary",
]
CLAUDE_MAX_ORGS = 6


def _clean_session_key(raw: str) -> str:
    def unquote(t):
        t = t.strip()
        if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
            t = t[1:-1].strip()
        return t

    sk = unquote(raw or "")
    if sk.lower().startswith("sessionkey="):
        sk = unquote(sk.split("=", 1)[1])
    return sk


_CLAUDE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
    "Referer": "https://claude.ai/settings/usage",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def _browser_jar_ex(domain, require_cookie=None):
    """ブラウザから指定ドメインの Cookie を取得。(jar_or_None, 状態文字列)。"""
    try:
        import browser_cookie3 as bc3
    except Exception:
        return None, "browser_cookie3 未導入(EXEに同梱されていない)"
    read_any = False
    tried = False
    for name in ("chrome", "edge", "brave", "chromium", "vivaldi", "opera", "firefox"):
        fn = getattr(bc3, name, None)
        if not fn:
            continue
        tried = True
        try:
            cj = fn(domain_name=domain)
        except Exception:
            continue
        try:
            names = [getattr(ck, "name", "") for ck in cj]
        except Exception:
            continue
        read_any = True
        if require_cookie is None and names:
            return cj, f"OK ({name})"
        if require_cookie is not None and require_cookie in names:
            return cj, f"OK ({name})"
    if not tried:
        return None, "対応ブラウザの読取関数が無い"
    if read_any:
        return None, "ブラウザは読めたが対象Cookieが無い(未ログイン)"
    return None, "ブラウザCookieを復号できない(最近のChrome/Edge暗号化の可能性)"


def _browser_claude_jar_ex():
    return _browser_jar_ex("claude.ai", "sessionKey")


def _browser_claude_jar():
    return _browser_jar_ex("claude.ai", "sessionKey")[0]


def _is_cf_block(resp) -> bool:
    try:
        ct = resp.headers.get("content-type", "").lower()
        srv = resp.headers.get("server", "").lower()
        if resp.headers.get("cf-mitigated") or (srv == "cloudflare" and resp.status_code in (403, 503)):
            head = resp.text[:300].lower()
            if "just a moment" in head or "cloudflare" in head or "cf-chl" in head:
                return True
        if "text/html" in ct and "just a moment" in resp.text[:300].lower():
            return True
    except Exception:
        pass
    return False


def _parse_claude_windows(usage_json):
    windows = []
    for f in _walk_used_percent(usage_json):
        hint = (f.get("hint") or "").lower()
        if "opus" in hint:
            name = "週(Opus)"
        elif "five" in hint or "5h" in hint or ("hour" in hint and "seven" not in hint):
            name = "5時間"
        elif "seven" in hint or "week" in hint or "7d" in hint:
            name = "週"
        else:
            name = hint or "枠"
        windows.append({"name": name, "used": clamp_pct(f["used"]), "reset_s": f.get("reset_s")})
    order = {"5時間": 0, "週": 1, "週(Opus)": 2}
    seen, uniq = set(), []
    for w in sorted(windows, key=lambda x: order.get(x["name"], 9)):
        if w["name"] in seen:
            continue
        seen.add(w["name"])
        uniq.append(w)
    return uniq[:3]


def _cookies_from_jar(jar, domain_substr="claude") -> dict:
    out = {}
    try:
        for ck in jar:
            dom = getattr(ck, "domain", "") or ""
            if (not domain_substr) or (domain_substr in dom):
                out[ck.name] = ck.value
    except Exception:
        pass
    return out


class _ClaudeHttp:
    """claude.ai 用 HTTP クライアント。curl_cffi があれば Chrome の TLS を偽装し
    Cloudflare を通りやすくする。無ければ requests にフォールバック。"""

    def __init__(self, cookies: dict):
        self.cookies = cookies or {}
        self.engine = "requests"
        try:
            from curl_cffi import requests as creq
            try:
                self._s = creq.Session(impersonate="chrome124")
            except Exception:
                self._s = creq.Session(impersonate="chrome")
            self.engine = "curl_cffi"
        except Exception:
            self._s = requests.Session()
        try:
            self._s.headers.update(_CLAUDE_HEADERS)
        except Exception:
            pass

    def get(self, url):
        return self._s.get(url, cookies=self.cookies, timeout=REQUEST_TIMEOUT)


def _pretty_plan(raw) -> str:
    """API が返すプラン/ティア文字列を見やすく整える。
    'default_claude_ai' のような内部識別子は非表示(空文字)にする。"""
    s = str(raw or "").strip()
    low = s.lower()
    if not low:
        return ""
    if "max" in low:
        return "Max"
    if "enterprise" in low:
        return "Enterprise"
    if "team" in low:
        return "Team"
    if "pro" in low:
        return "Pro"
    if "free" in low:
        return "Free"
    # default_claude_ai 等、内部識別子っぽいものは表示しない
    if "_" in low or low.startswith("default") or low.endswith("claude_ai") or low.endswith("ai"):
        return ""
    return s[:12]


def _attempt_claude(http) -> dict:
    """1つのクライアントで 組織一覧→usage を取得して正規化する。"""
    out = {"ok": False, "plan": "", "windows": [], "error": ""}
    try:
        ro = http.get("https://claude.ai/api/organizations")
    except Exception as e:
        out["error"] = f"通信失敗: {e}"
        return out
    if _is_cf_block(ro):
        out["error"] = "Cloudflareでブロック (拡張機能なら可)"
        return out
    if ro.status_code in (401, 403):
        out["error"] = "sessionKey 無効/期限切れ (再取得)"
        return out
    if not ro.ok:
        out["error"] = f"組織取得 HTTP {ro.status_code}"
        return out
    try:
        orgs = ro.json()
    except Exception:
        out["error"] = "組織応答が不正 (Cloudflare/ログイン?)"
        return out
    if isinstance(orgs, dict):
        orgs = orgs.get("organizations") or orgs.get("data") or [orgs]
    if not isinstance(orgs, list) or not orgs:
        out["error"] = "組織が見つからない"
        return out

    usage_json = None
    last_status = None
    plan = ""
    for org in orgs[:CLAUDE_MAX_ORGS]:
        uuid = org.get("uuid") or org.get("id")
        if not uuid:
            continue
        for tmpl in CLAUDE_USAGE_PATHS:
            try:
                r = http.get("https://claude.ai" + tmpl.format(uuid=uuid))
            except Exception:
                continue
            last_status = r.status_code
            if r.ok:
                try:
                    j = r.json()
                except Exception:
                    continue
                if _walk_used_percent(j):
                    usage_json = j
                    plan = _pretty_plan(org.get("rate_limit_tier") or org.get("billing_type")
                                        or org.get("name") or "")
                    break
        if usage_json is not None:
            break

    if usage_json is None:
        out["error"] = (f"利用状況の取得に失敗 (HTTP {last_status})"
                        if last_status else "利用状況の取得に失敗")
        return out
    windows = _parse_claude_windows(usage_json)
    if not windows:
        out["error"] = "利用率フィールドが見つからない"
        return out
    out["ok"] = True
    out["plan"] = plan
    out["windows"] = windows
    return out


def _claude_clients(pcfg: dict):
    """試行するクライアントを順に返す: 設定キー → ログイン済みブラウザのCookie。"""
    clients = []
    sk = _clean_session_key(pcfg.get("session_key", ""))
    if sk:
        clients.append(_ClaudeHttp({"sessionKey": sk}))
    jar = _browser_claude_jar()
    if jar is not None:
        ck = _cookies_from_jar(jar)
        if ck:
            clients.append(_ClaudeHttp(ck))
    return clients


def fetch_claude(pcfg: dict) -> dict:
    result = {"label": "Claude", "ok": False, "plan": "", "windows": [], "error": ""}
    clients = _claude_clients(pcfg)
    if not clients:
        result["error"] = "sessionKey 未設定 (⚙ から設定 / ブラウザでログイン)"
        return result
    last_err = ""
    for http in clients:
        out = _attempt_claude(http)
        if out["ok"]:
            result.update(ok=True, plan=out["plan"], windows=out["windows"])
            return result
        last_err = out["error"]
    result["error"] = last_err or "取得失敗"
    return result


def claude_diagnose(pcfg: dict) -> str:
    """接続テスト用に、原因の手掛かりを多めに返す。"""
    lines = []
    sk = _clean_session_key(pcfg.get("session_key", ""))
    lines.append(f"設定sessionKey: {('あり(' + str(len(sk)) + '文字)') if sk else 'なし'}")
    jar, jar_status = _browser_claude_jar_ex()
    if jar is None:
        lines.append(f"ブラウザCookie: {jar_status}")
    else:
        ck = _cookies_from_jar(jar)
        lines.append(f"ブラウザCookie: {jar_status} / {len(ck)}個 "
                     f"(cf_clearance {'有' if 'cf_clearance' in ck else '無'})")

    have_auth = bool(sk) or (jar is not None)
    try:
        cookies = {"sessionKey": sk} if sk else (_cookies_from_jar(jar) if jar else {})
        probe = _ClaudeHttp(cookies)
        lines.append(f"HTTPエンジン: {probe.engine}"
                     + ("" if probe.engine == "curl_cffi" else "  (curl_cffi 未導入)"))
        r = probe.get("https://claude.ai/api/organizations")
        lines.append(f"組織API応答: HTTP {r.status_code} / {r.headers.get('content-type','?')[:24]}")
        if _is_cf_block(r):
            lines.append("→ Cloudflare のチャレンジに当たっています")
        elif r.status_code in (401, 403) and not have_auth:
            lines.append("→ 認証情報が無いための403です(正常)。⚙でsessionKeyを設定してください")
        elif r.status_code in (401, 403) and have_auth:
            lines.append("→ 認証はあるが拒否。sessionKeyが古い可能性(取り直し)")
    except Exception as e:
        lines.append(f"プローブ失敗: {e}")

    res = fetch_claude(pcfg)
    lines.append("")
    if res["ok"]:
        lines.append("結果: OK  " + " / ".join(
            f"{w['name']}=残り{100 - w['used']:.0f}%" for w in res["windows"]))
    else:
        lines.append("結果: NG  " + res["error"])
        if not have_auth:
            lines.append("")
            lines.append("【対処】⚙ を開き、claude.ai の Cookie『sessionKey』を貼り付けて保存。")
            lines.append("  取り方: claude.ai を開く → F12 → Application → Cookies →")
            lines.append("          https://claude.ai → sessionKey の値をコピー")
    return "\n".join(lines)


# ─────────────── sessionKey 自動取得 ───────────────

def read_sessionkey_auto():
    """無操作でブラウザから sessionKey を取得。(key_or_None, 状態文字列)。
    Firefox/旧Chrome系は取得可。Chrome127+ は暗号化のため不可(その旨を返す)。"""
    notes = []
    try:
        import rookiepy
        for name in ("firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"):
            fn = getattr(rookiepy, name, None)
            if not fn:
                continue
            try:
                cks = fn(["claude.ai"])
            except Exception:
                continue
            for c in cks:
                if c.get("name") == "sessionKey" and c.get("value"):
                    return c["value"], f"rookiepy/{name}"
        notes.append("rookiepy:該当なし")
    except Exception:
        notes.append("rookiepy:未導入")

    jar, st = _browser_claude_jar_ex()
    if jar is not None:
        d = _cookies_from_jar(jar)
        if d.get("sessionKey"):
            return d["sessionKey"], "browser_cookie3"
    notes.append("bc3:" + st)
    return None, " / ".join(notes)


def _find_chromium():
    import shutil
    if os.name == "nt":
        cands = []
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            cands.append((os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"), "Chrome"))
            cands.append((os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"), "Edge"))
        for p, n in cands:
            if os.path.exists(p):
                return p, n
        for exe, n in (("chrome", "Chrome"), ("msedge", "Edge")):
            w = shutil.which(exe)
            if w:
                return w, n
        return None, ""
    for exe, n in (("google-chrome", "Chrome"), ("chromium", "Chromium"),
                   ("chromium-browser", "Chromium"), ("microsoft-edge", "Edge")):
        w = shutil.which(exe)
        if w:
            return w, n
    return None, ""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --- 最小 WebSocket クライアント(CDP 用・標準ライブラリのみ) ---

def _ws_connect(ws_url, timeout=8):
    import base64 as _b64
    assert ws_url.startswith("ws://")
    hostport, _, path = ws_url[5:].partition("/")
    host, _, port = hostport.partition(":")
    sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
    sock.settimeout(timeout)
    key = _b64.b64encode(os.urandom(16)).decode()
    req = (f"GET /{path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("handshake closed")
        buf += chunk
    return sock


def _ws_send(sock, text):
    import struct
    data = text.encode()
    n = len(data)
    hdr = bytearray([0x81])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    mask = os.urandom(4)
    hdr += mask
    sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv(sock):
    import struct

    def readn(n):
        b = b""
        while len(b) < n:
            c = sock.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b

    b0, b1 = readn(2)
    opcode = b0 & 0x0F
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", readn(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", readn(8))[0]
    mask = readn(4) if (b1 & 0x80) else b""
    payload = readn(ln)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _cdp_all_cookies(ws_url, timeout=6):
    sock = _ws_connect(ws_url, timeout)
    try:
        _ws_send(sock, json.dumps({"id": 1, "method": "Network.getAllCookies"}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            op, payload = _ws_recv(sock)
            if op == 0x8:
                break
            if op not in (0x1, 0x2):
                continue
            try:
                msg = json.loads(payload.decode("utf-8", "replace"))
            except Exception:
                continue
            if msg.get("id") == 1:
                return (msg.get("result") or {}).get("cookies", []) or []
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return []


def capture_sessionkey_login(on_status, should_cancel, timeout=240):
    """専用プロファイルのブラウザでユーザーが claude.ai にログインし、
    ブラウザ自身が保持するセッションCookieをCDP経由で読み取る。"""
    import subprocess
    import shutil
    import tempfile
    import urllib.request

    exe, name = _find_chromium()
    if not exe:
        return None, "Chrome / Edge が見つかりません"
    port = _free_port()
    tmp = tempfile.mkdtemp(prefix="aiusage_login_")
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={tmp}",
            "--no-first-run", "--no-default-browser-check", "--new-window",
            "https://claude.ai/login"]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, f"ブラウザ起動失敗: {e}"

    on_status(f"{name} が開きます。claude.ai にログインしてください…")
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            if should_cancel():
                return None, "キャンセルしました"
            ws_url = None
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=4) as r:
                    targets = json.loads(r.read().decode())
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
            except Exception:
                pass
            if ws_url:
                try:
                    for c in _cdp_all_cookies(ws_url, timeout=6):
                        if c.get("name") == "sessionKey" and "claude" in (c.get("domain", "") or "") and c.get("value"):
                            on_status("取得しました。ウィンドウを閉じます…")
                            return c["value"], "OK"
                except Exception:
                    pass
            time.sleep(1.5)
        return None, "時間切れ(ログイン未完了)"
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


_GENERIC_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}


def _http_get(url, cookies=None, headers=None, timeout=REQUEST_TIMEOUT):
    """curl_cffi(あれば Chrome 互換の通信設定)→無ければ requests。"""
    h = dict(_GENERIC_HEADERS)
    h.update(headers or {})
    try:
        from curl_cffi import requests as creq
        try:
            s = creq.Session(impersonate="chrome124")
        except Exception:
            s = creq.Session(impersonate="chrome")
    except Exception:
        s = requests.Session()
    try:
        s.headers.update(h)
    except Exception:
        pass
    return s.get(url, cookies=cookies or {}, timeout=timeout)


def _abbrev(n):
    try:
        n = float(n)
    except Exception:
        return str(n)
    if abs(n) >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{int(n)}"


def _json_path(obj, path):
    """'a.b.0.c' / 'a[0].b' 形式で JSON を辿る。見つからなければ None。"""
    if not path:
        return None
    cur = obj
    for part in str(path).replace("[", ".").replace("]", ".").split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return None
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        else:
            return None
    return cur


def _to_number(v):
    try:
        return float(v)
    except Exception:
        return None


def _reset_seconds_from(v):
    if v is None:
        return None
    if isinstance(v, str):
        return iso_to_reset_seconds(v)
    n = _to_number(v)
    if n is None:
        return None
    if n > 1e7:                     # epoch 秒っぽい
        return n - time.time()
    if n > 1e10:                    # epoch ミリ秒っぽい
        return n / 1000.0 - time.time()
    return n                        # 残り秒として扱う


def _domain_of(url):
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""


def fetch_custom(cp: dict) -> dict:
    """ユーザー定義のAIプロバイダ(クレジット/使用量)を取得。
    cp: name,color,url,auth{type,...},value_path,value_kind,total_path,reset_path,window_name,unit"""
    name = cp.get("name") or "AI"
    result = {"label": name, "ok": False, "plan": "", "windows": [], "error": "",
              "color": cp.get("color") or "#8a8f98"}
    url = (cp.get("url") or "").strip()
    if not url:
        result["error"] = "URL未設定"
        return result

    auth = cp.get("auth") or {}
    atype = auth.get("type", "browser")
    cookies, headers = {}, {}
    if atype == "cookie":
        if auth.get("name"):
            cookies[auth["name"]] = auth.get("value", "")
    elif atype == "bearer":
        headers["Authorization"] = "Bearer " + (auth.get("token", "") or "")
    elif atype == "header":
        if auth.get("name"):
            headers[auth["name"]] = auth.get("value", "")
    elif atype == "browser":
        dom = auth.get("domain") or _domain_of(url)
        jar = _browser_jar_ex(dom)[0]
        if jar is None:
            result["error"] = "ブラウザCookie取得不可(ログイン/暗号化)"
            return result
        cookies = _cookies_from_jar(jar, dom)

    try:
        r = _http_get(url, cookies=cookies, headers=headers)
    except Exception as e:
        result["error"] = f"通信失敗: {e}"
        return result
    if r.status_code in (401, 403):
        result["error"] = f"認証エラー HTTP {r.status_code}"
        return result
    if not getattr(r, "ok", r.status_code < 400):
        result["error"] = f"HTTP {r.status_code}"
        return result
    try:
        data = r.json()
    except Exception:
        result["error"] = "応答がJSONでない"
        return result

    raw = _json_path(data, cp.get("value_path"))
    val = _to_number(raw)
    if val is None:
        result["error"] = "値が見つからない(value_path確認)"
        return result

    kind = cp.get("value_kind", "remaining_credits")
    total = _to_number(_json_path(data, cp.get("total_path"))) if cp.get("total_path") else None
    reset_s = _reset_seconds_from(_json_path(data, cp.get("reset_path"))) if cp.get("reset_path") else None
    unit = cp.get("unit", "cr")
    wname = cp.get("window_name") or ("使用率" if "percent" in kind else "残量")

    if kind == "used_percent":
        used = clamp_pct(val)
        display = f"残り {100 - used:.0f}%"
        no_bar = False
        cell_label = wname
        center = None
    elif kind == "remaining_percent":
        rem = clamp_pct(val)
        used = 100.0 - rem
        display = f"残り {rem:.0f}%"
        no_bar = False
        cell_label = wname
        center = None
    else:  # remaining_credits
        if total and total > 0:
            rem = max(0.0, min(val, total))
            used = clamp_pct((1.0 - rem / total) * 100.0)
            display = f"{int(rem):,} / {int(total):,}{unit}"
            no_bar = False
            cell_label = f"{_abbrev(rem)}/{_abbrev(total)}"
            center = None
        else:
            used = 0.0
            display = f"残り {int(val):,}{unit}"
            no_bar = True
            cell_label = unit
            center = _abbrev(val)

    result["windows"] = [{"name": wname, "used": used, "reset_s": reset_s,
                          "display": display, "no_bar": no_bar,
                          "cell_label": cell_label, "center": center}]
    result["ok"] = True
    return result


def fetch_all(cfg: dict) -> list:
    out = []
    pcs = cfg.get("providers", {})
    if pcs.get("claude", {}).get("enabled", True):
        out.append(fetch_claude(pcs.get("claude", {})))
    if pcs.get("chatgpt", {}).get("enabled", True):
        out.append(fetch_chatgpt(pcs.get("chatgpt", {})))
    for cp in (pcs.get("custom") or []):
        if cp.get("enabled", True):
            out.append(fetch_custom(cp))
    return out


# ─────────────────────────── 配色(モダンなダーク) ───────────────────────────

TRANSPARENT = "#010203"     # Windows で角丸の外側を透過させる魔法色
CARD = "#15171c"
CARD_HEAD = "#1c1f27"
BORDER = "#2a2f3a"
DIVIDER = "#23272f"
PILL_BG = "#262b34"
TEXT = "#f2f4f7"
MUTED = "#a7adba"
FAINT = "#6c7380"
TRACK = "#262b34"
FIELD = "#0e1015"
GREEN = "#43c463"
AMBER = "#e2b340"
RED = "#f3603f"
DOT_CLAUDE = "#d97757"       # Claude 系のクレイ色
DOT_CHATGPT = "#19c37d"      # ChatGPT 系のグリーン


def _lighten(hexcol, amt=0.25):
    try:
        h = hexcol.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = int(r + (255 - r) * amt)
        g = int(g + (255 - g) * amt)
        b = int(b + (255 - b) * amt)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hexcol


def remaining_color(remaining_pct, low):
    if remaining_pct <= low:
        return RED
    if remaining_pct <= low * 2.5:
        return AMBER
    return GREEN


# ─────────────────────────── ウィジェット ───────────────────────────

class UsageWidget:
    BASE_W = 268  # 論理幅(px・スケール前)

    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.q = queue.Queue()
        self.fetching = False
        self._drag = {"x": 0, "y": 0}
        self.cache, self.cache_ts = {}, {}
        self._below = set()
        self._refresh_after = None
        self._settings_win = None
        self._status_text = "読み込み中…"
        self._H = 0
        self._mode = "move"

        # スケール = DPI倍率 × ユーザーズーム。フォントは負値=ピクセル指定で常に鮮明。
        try:
            self.dpi = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:
            self.dpi = 1.0
        try:
            self.user_scale = float(cfg.get("ui_scale", 1.0))
        except Exception:
            self.user_scale = 1.0
        self.user_scale = max(0.7, min(3.0, self.user_scale))
        self.scale = self.dpi * self.user_scale

        for label, obj in (cfg.get("cache") or {}).items():
            try:
                self.cache[label] = {"label": label, "ok": True, "plan": obj.get("plan", ""),
                                     "windows": obj.get("windows", []), "color": obj.get("color")}
                self.cache_ts[label] = obj.get("ts", 0)
            except Exception:
                pass

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        win_bg = CARD
        if os.name == "nt":
            try:
                root.attributes("-transparentcolor", TRANSPARENT)
                win_bg = TRANSPARENT
            except Exception:
                win_bg = CARD
        self._win_bg = win_bg
        root.configure(bg=win_bg)

        self.W = self.S(self.BASE_W)
        self.canvas = tk.Canvas(root, width=self.W, height=self.S(160),
                                bg=win_bg, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._make_fonts()

        # 右上のボタン(キャンバス上にオーバーレイ)
        self._make_buttons()

        # ドラッグ & 右クリック
        self.canvas.bind("<Button-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Motion>", self._on_motion)
        self.root.bind("<Button-3>", self._show_menu)
        # ホイールでズーム(右下グリップのドラッグでも可)
        self.canvas.bind("<MouseWheel>", self._on_wheel)            # Win/Mac
        self.canvas.bind("<Button-4>", lambda e: self.zoom(+0.1))   # Linux up
        self.canvas.bind("<Button-5>", lambda e: self.zoom(-0.1))   # Linux down

        if self.cache:
            self._render(self._cached_display())
        else:
            self._render(None)

        x = cfg["window"].get("x")
        y = cfg["window"].get("y")
        if x is None or y is None:
            x, y = root.winfo_screenwidth() - self.W - self.S(24), self.S(48)
        root.geometry(f"+{int(x)}+{int(y)}")
        self._clamp_position()

        self.refresh()
        self.root.after(300, self._poll_queue)
        self._schedule_refresh()

    def S(self, v):
        return int(round(v * self.scale))

    def _make_fonts(self):
        # 見やすさ重視のサイズ(負値=ピクセル)。ズームで全体が拡大しても鮮明。
        self.f_title = tkfont.Font(family="Segoe UI", size=-self.S(14), weight="bold")
        self.f_prov = tkfont.Font(family="Segoe UI", size=-self.S(13), weight="bold")
        self.f_win = tkfont.Font(family="Segoe UI", size=-self.S(10))
        self.f_pct = tkfont.Font(family="Segoe UI Semibold", size=-self.S(13), weight="bold")
        self.f_small = tkfont.Font(family="Segoe UI", size=-self.S(10))
        self.f_btn = tkfont.Font(family="Segoe UI", size=-self.S(13))
        self.f_ring = tkfont.Font(family="Segoe UI Semibold", size=-self.S(13), weight="bold")
        self.f_ringsub = tkfont.Font(family="Segoe UI", size=-self.S(8))

    # ---------- ボタン ----------
    def _make_buttons(self):
        self.buttons = []
        specs = [("✕", RED, self.quit), ("⟳", TEXT, self.refresh), ("⚙", TEXT, self.open_settings)]
        for text, hover, cmd in specs:
            b = tk.Label(self.canvas, text=text, bg=CARD_HEAD, fg=MUTED,
                         font=self.f_btn, cursor="hand2")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b, h=hover: w.config(fg=h))
            b.bind("<Leave>", lambda e, w=b: w.config(fg=MUTED))
            self.buttons.append(b)
        self._place_buttons()

    def _place_buttons(self):
        x = self.W - self.S(10)
        for b in self.buttons:
            x -= self.S(22)
            b.config(font=self.f_btn, bg=CARD_HEAD)
            b.place(x=x, y=self.S(7), width=self.S(20), height=self.S(20))

    def _apply_scale(self):
        self.scale = self.dpi * self.user_scale
        self.W = self.S(self.BASE_W)
        self._make_fonts()
        self._place_buttons()
        self._render(self._last_display)

    def zoom(self, delta):
        self.user_scale = max(0.7, min(3.0, round(self.user_scale + delta, 3)))
        self._apply_scale()
        self.cfg["ui_scale"] = self.user_scale
        save_config(self.cfg)

    def zoom_reset(self):
        self.user_scale = 1.0
        self._apply_scale()
        self.cfg["ui_scale"] = 1.0
        save_config(self.cfg)

    def _on_wheel(self, e):
        self.zoom(+0.1 if getattr(e, "delta", 0) >= 0 else -0.1)

    def _on_motion(self, e):
        try:
            self.canvas.config(cursor="bottom_right_corner" if self._in_resize_grip(e) else "")
        except Exception:
            pass

    # ---------- ドラッグ(移動 / 右下でリサイズ) ----------
    def _in_resize_grip(self, e):
        g = self.S(20)
        return e.x >= self.W - g and e.y >= self._H - g

    def _drag_start(self, e):
        self._mode = "resize" if self._in_resize_grip(e) else "move"
        self._sx, self._sy, self._sw = e.x_root, e.y_root, self.W

    def _drag_move(self, e):
        if self._mode == "resize":
            new_w = self._sw + (e.x_root - self._sx)
            us = new_w / (self.dpi * self.BASE_W)
            us = max(0.7, min(3.0, us))
            if abs(us - self.user_scale) > 0.005:
                self.user_scale = us
                self._apply_scale()
        else:
            self.root.geometry(f"+{self.root.winfo_x() + (e.x_root - self._sx)}"
                               f"+{self.root.winfo_y() + (e.y_root - self._sy)}")
            self._sx, self._sy = e.x_root, e.y_root

    def _drag_end(self, e):
        self.cfg["window"]["x"] = self.root.winfo_x()
        self.cfg["window"]["y"] = self.root.winfo_y()
        self.cfg["ui_scale"] = round(self.user_scale, 3)
        save_config(self.cfg)

    # ---------- 位置補正 ----------
    def _virtual_bounds(self):
        if os.name == "nt":
            try:
                import ctypes
                g = ctypes.windll.user32.GetSystemMetrics
                x, y, w, h = g(76), g(77), g(78), g(79)
                if w and h:
                    return x, y, w, h
            except Exception:
                pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _clamp_position(self):
        self.root.update_idletasks()
        vx, vy, vw, vh = self._virtual_bounds()
        ww = self.root.winfo_width() or self.W
        wh = self.root.winfo_height() or self.S(160)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        m = self.S(24)
        if x > vx + vw - m or x + ww < vx + m or y > vy + vh - m or y + wh < vy + m:
            x, y = vx + vw - ww - self.S(20), vy + self.S(40)
        else:
            x = min(max(x, vx), vx + vw - ww)
            y = min(max(y, vy), vy + vh - wh)
        self.root.geometry(f"+{int(x)}+{int(y)}")

    # ---------- メニュー ----------
    def _show_menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="今すぐ更新", command=self.refresh)
        m.add_command(label="Claude接続をテスト…", command=self.test_claude)
        m.add_separator()
        zoom = tk.Menu(m, tearoff=0)
        zoom.add_command(label="拡大 (+)", command=lambda: self.zoom(+0.1))
        zoom.add_command(label="縮小 (−)", command=lambda: self.zoom(-0.1))
        zoom.add_command(label="等倍に戻す", command=self.zoom_reset)
        m.add_cascade(label=f"表示倍率（現在 {self.user_scale:.1f}x）", menu=zoom)
        m.add_command(label="設定…", command=self.open_settings)
        m.add_command(label="設定ファイルを開く", command=self.open_config_file)
        if os.name == "nt":
            mark = "✓ " if startup_enabled() else "　"
            m.add_command(label=f"{mark}Windows起動時に自動実行", command=self.toggle_startup)
        m.add_separator()
        m.add_command(label="終了", command=self.quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def test_claude(self, pcfg=None):
        win = tk.Toplevel(self.root)
        win.title("Claude 接続テスト")
        win.configure(bg=CARD)
        win.attributes("-topmost", True)
        f = tkfont.Font(family="Consolas", size=-self.S(12))
        tk.Label(win, text="診断中…", bg=CARD, fg=TEXT, font=f, justify="left",
                 anchor="w").pack(fill="both", expand=True, padx=self.S(14), pady=self.S(12))
        lbl = win.winfo_children()[0]
        px0, py0 = self.root.winfo_x(), self.root.winfo_y()
        win.geometry(f"+{max(px0 - self.S(40), 20)}+{max(py0 + self.S(20), 20)}")

        if pcfg is None:
            pcfg = self.cfg["providers"]["claude"]
        pcfg = json.loads(json.dumps(pcfg))
        q = queue.Queue()
        threading.Thread(target=lambda: q.put(claude_diagnose(pcfg)), daemon=True).start()

        def poll():
            try:
                text = q.get_nowait()
            except queue.Empty:
                win.after(200, poll)
                return
            lbl.config(text=text)
            btns = tk.Frame(win, bg=CARD)
            btns.pack(fill="x", padx=self.S(14), pady=(0, self.S(12)))
            tk.Button(btns, text="コピー", relief="flat", bg=CARD_HEAD, fg=TEXT,
                      cursor="hand2", command=lambda: (self.root.clipboard_clear(),
                                                       self.root.clipboard_append(text))
                      ).pack(side="left")
            tk.Button(btns, text="閉じる", relief="flat", bg=CARD_HEAD, fg=TEXT,
                      cursor="hand2", command=win.destroy).pack(side="right")
        win.after(200, poll)

    def toggle_startup(self):
        try:
            set_startup(not startup_enabled())
        except Exception:
            pass

    def open_config_file(self):
        save_config(self.cfg)
        p = str(CONFIG_PATH)
        try:
            if os.name == "nt":
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{p}"')
            else:
                os.system(f'xdg-open "{p}"')
        except Exception:
            pass

    def open_settings(self):
        if self._settings_win is not None and self._settings_win.winfo_exists():
            self._settings_win.lift()
            return
        SettingsDialog(self)

    def apply_settings(self):
        save_config(self.cfg)
        if self._refresh_after is not None:
            try:
                self.root.after_cancel(self._refresh_after)
            except Exception:
                pass
        self._schedule_refresh()
        self.refresh()

    # ---------- 取得 ----------
    def refresh(self):
        if self.fetching:
            return
        self.fetching = True
        self._status_text = "更新中…"
        self._redraw_footer_only()
        cfg_copy = json.loads(json.dumps(self.cfg))

        def worker():
            try:
                res = fetch_all(cfg_copy)
            except Exception as e:
                res = [{"label": "?", "ok": False, "windows": [], "error": str(e)}]
            self.q.put(res)

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_refresh(self):
        minutes = max(1, int(self.cfg.get("refresh_minutes", 5)))
        self._refresh_after = self.root.after(minutes * 60 * 1000, self._auto_refresh)

    def _auto_refresh(self):
        self.refresh()
        self._schedule_refresh()

    def _poll_queue(self):
        try:
            while True:
                results = self.q.get_nowait()
                self.fetching = False
                self._render(self._merge(results))
        except queue.Empty:
            pass
        self.root.after(300, self._poll_queue)

    # ---------- キャッシュ ----------
    def _merge(self, results):
        now = time.time()
        display = []
        for prov in results:
            label = prov.get("label", "?")
            if prov.get("ok"):
                self.cache[label] = prov
                self.cache_ts[label] = now
                self.cfg.setdefault("cache", {})[label] = {
                    "plan": prov.get("plan", ""), "windows": prov.get("windows", []),
                    "color": prov.get("color"), "ts": now}
                display.append({**prov, "stale": False, "as_of": now})
            elif label in self.cache:
                display.append({**self.cache[label], "stale": True,
                                "as_of": self.cache_ts.get(label, 0), "error": prov.get("error", "")})
            else:
                display.append({**prov, "stale": False, "as_of": now})
        return display

    def _cached_display(self):
        return [{**v, "stale": True, "as_of": self.cache_ts.get(k, 0)}
                for k, v in self.cache.items()]

    # ---------- 描画ヘルパ ----------
    def _round_rect(self, x1, y1, x2, y2, r, tag="content", **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, tags=tag, **kw)

    def _redraw_footer_only(self):
        # 取得中表示などのための簡易全再描画。
        if hasattr(self, "_last_display"):
            self._render(self._last_display)

    @staticmethod
    def _abbrev(n):
        try:
            n = float(n)
        except Exception:
            return str(n)
        if abs(n) >= 1_000_000:
            return f"{n / 1e6:.1f}M"
        if abs(n) >= 1000:
            return f"{n / 1000:.1f}k"
        return f"{int(n)}"

    @staticmethod
    def _short_name(name):
        return {"週(Opus)": "Opus", "24時間": "24h", "セッション": "Sess"}.get(name, name)

    def _ring(self, cx, cy, d, thick, remaining, col, center_text, draw_arc=True):
        c = self.canvas
        x1, y1, x2, y2 = cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2
        c.create_arc(x1, y1, x2, y2, start=90, extent=180, style="arc",
                     outline=TRACK, width=thick, tags="content")
        c.create_arc(x1, y1, x2, y2, start=270, extent=180, style="arc",
                     outline=TRACK, width=thick, tags="content")
        if draw_arc and remaining > 0:
            c.create_arc(x1, y1, x2, y2, start=90, extent=-(remaining / 100.0) * 359.999,
                         style="arc", outline=col, width=thick, tags="content")
        if center_text:
            c.create_text(cx, cy, text=center_text, anchor="center", fill=col,
                          font=self.f_ring, tags="content")

    # ---------- 描画 ----------
    # display: プロバイダ dict のリスト / [](両方無効) / None(初期・読み込み中)
    def _render(self, display):
        self._last_display = display
        c = self.canvas
        c.delete("content")
        c.delete("bg")

        pad = self.S(14)
        W = self.W
        low = float(self.cfg.get("low_threshold_pct", 20))
        worst = None
        any_data = False
        now_below = set()

        y = self.S(12)
        c.create_text(pad, y, text="AI 利用枠", anchor="w", fill=TEXT,
                      font=self.f_title, tags="content")
        y += self.S(21)

        if display is None:
            c.create_text(W / 2, y + self.S(12), text="読み込み中…", anchor="center",
                          fill=MUTED, font=self.f_small, tags="content")
            y += self.S(40)
        elif not display:
            c.create_text(pad, y + self.S(10), text="プロバイダが無効です（⚙ から有効化）",
                          anchor="w", fill=MUTED, font=self.f_small, tags="content")
            y += self.S(36)
        else:
            for idx, prov in enumerate(display):
                if idx > 0:  # プロバイダ間の区切り線
                    c.create_line(pad, y, W - pad, y, fill=DIVIDER, tags="content")
                    y += self.S(8)
                stale = prov.get("stale")
                dot = prov.get("color") or (DOT_CLAUDE if prov["label"] == "Claude"
                                            else DOT_CHATGPT if prov["label"] == "ChatGPT" else "#8a8f98")
                cy = y + self.S(7)
                rdot = self.S(4)
                c.create_oval(pad, cy - rdot, pad + 2 * rdot, cy + rdot, fill=dot,
                              outline="", tags="content")
                tx = pad + 2 * rdot + self.S(8)
                c.create_text(tx, cy, text=prov["label"], anchor="w",
                              fill=(MUTED if stale else TEXT), font=self.f_prov, tags="content")
                if prov.get("plan"):  # プラン名を小さなピル(角丸チップ)で右寄せ
                    ptxt = str(prov["plan"])
                    pw = self.f_small.measure(ptxt)
                    cx2 = W - pad
                    cx1 = cx2 - pw - self.S(14)
                    self._round_rect(cx1, cy - self.S(8), cx2, cy + self.S(8), self.S(8),
                                     fill=PILL_BG, outline="")
                    c.create_text((cx1 + cx2) / 2, cy, text=ptxt, anchor="center",
                                  fill=MUTED, font=self.f_small, tags="content")
                y += self.S(22)

                if not prov.get("ok"):
                    c.create_text(pad + self.S(2), y, text="⚠ " + prov.get("error", "取得失敗"),
                                  anchor="w", fill=RED, font=self.f_small, tags="content")
                    y += self.S(22)
                    continue

                any_data = True
                # リングのセルを横並び(幅が足りなければ折り返し)
                d = self.S(36)
                thick = self.S(5)
                cell_w = self.S(58)
                cell_h = d + self.S(2) + self.S(10) + self.S(9)  # リング+名前+リセット
                x = pad
                row_top = y
                for win in prov["windows"]:
                    if x + cell_w > W - pad + self.S(2):  # 折り返し
                        x = pad
                        row_top += cell_h
                    remaining = clamp_pct(100.0 - win["used"])
                    no_bar = win.get("no_bar")
                    if not no_bar:
                        worst = remaining if worst is None else min(worst, remaining)
                        if remaining <= low:
                            now_below.add((prov["label"], win["name"]))
                    col = remaining_color(remaining, low) if not no_bar else TEXT

                    cx = x + cell_w / 2
                    cyr = row_top + d / 2
                    if no_bar:  # 総量不明のクレジット → リングなしで数値を中央に
                        self._ring(cx, cyr, d, thick, 0, "#3a3f49", "", draw_arc=False)
                        c.create_text(cx, cyr, text=(win.get("center") or "—"),
                                      anchor="center", fill=TEXT, font=self.f_ring, tags="content")
                    else:
                        self._ring(cx, cyr, d, thick, remaining, col, f"{remaining:.0f}")
                    # 名前(または残/総クレジット)
                    label = win.get("cell_label") or self._short_name(win["name"])
                    c.create_text(cx, row_top + d + self.S(7), text=label, anchor="center",
                                  fill=MUTED, font=self.f_small, tags="content")
                    # リセット(小)
                    reset_txt = "" if stale else human_duration(win.get("reset_s"))
                    if reset_txt:
                        c.create_text(cx, row_top + d + self.S(17), text="⟳" + reset_txt,
                                      anchor="center", fill=FAINT, font=self.f_ringsub, tags="content")
                    x += cell_w
                y = row_top + cell_h

                if stale:
                    t = datetime.fromtimestamp(prov["as_of"]).strftime("%H:%M") if prov.get("as_of") else "?"
                    c.create_text(pad, y - self.S(2), text=f"⚠ 更新失敗・{t}時点", anchor="w",
                                  fill=AMBER, font=self.f_ringsub, tags="content")
                    y += self.S(11)
                y += self.S(3)

        # フッター
        if self.fetching:
            footer = "更新中…"
        elif display is None:
            footer = "読み込み中…"
        elif not display:
            footer = "プロバイダ無効"
        else:
            footer = f"最終更新 {datetime.now().strftime('%H:%M:%S')}" + ("" if any_data else " ・取得不可")
            self._status_text = footer
        y += self.S(2)
        c.create_text(pad, y + self.S(4), text=footer, anchor="w",
                      fill=FAINT, font=self.f_small, tags="content")
        H = y + self.S(20)

        # ヘッダのタイトル横に「全体の健康状態」ドット
        acc = remaining_color(worst, low) if worst is not None else "#5b6472"
        tw = self.f_title.measure("AI 利用枠")
        hd = self.S(4)
        c.create_oval(pad + tw + self.S(10), self.S(12) - hd, pad + tw + self.S(10) + 2 * hd,
                      self.S(12) + hd, fill=acc, outline="", tags="content")

        # 背景(カード→ヘッダ帯→ガラスのハイライト→アクセント線)を最背面へ
        r = self.S(14)
        head_h = self.S(34)
        self._round_rect(0, 0, W - 1, H - 1, r, tag="bg", fill=CARD, outline=BORDER, width=1)
        self._round_rect(1, 1, W - 2, head_h, r, tag="bg", fill=CARD_HEAD, outline="")
        c.create_rectangle(1, head_h - r, W - 2, head_h, fill=CARD_HEAD, outline="", tags="bg")
        # 上端の薄いハイライト(ガラス感)
        c.create_line(r, 1, W - 1 - r, 1, fill=_lighten(CARD_HEAD, 0.4),
                      width=max(1, self.S(1)), tags="bg")
        c.create_rectangle(0, head_h, W, head_h + max(2, self.S(2)), fill=acc, outline="", tags="bg")
        c.tag_lower("bg")

        # リサイズ用グリップ(右下) — ドラッグで拡大縮小(ベクター再描画=画質維持)
        gx, gy = W - self.S(5), H - self.S(5)
        for d in (self.S(4), self.S(8), self.S(12)):
            c.create_line(gx - d, gy, gx, gy - d, fill=BORDER, width=max(1, self.S(1)),
                          tags="content")

        self._H = H
        c.config(width=W, height=H)
        self.root.geometry(f"{W}x{H}")
        self._place_buttons()

        # しきい値を新たに下回ったら警告音(実データ更新時のみ)
        if not self.fetching and isinstance(display, list) and display:
            if self.cfg.get("alert_sound", True):
                for _ in now_below - self._below:
                    beep_alert()
                    break
            self._below = now_below

    def quit(self):
        self.cfg["window"]["x"] = self.root.winfo_x()
        self.cfg["window"]["y"] = self.root.winfo_y()
        save_config(self.cfg)
        self.root.destroy()


# ─────────────────────────── 設定ダイアログ ───────────────────────────

class SettingsDialog:
    def __init__(self, app: "UsageWidget"):
        self.app = app
        self.cfg = app.cfg
        s = app.scale

        def px(v):
            return int(round(v * s))

        top = tk.Toplevel(app.root)
        self.top = top
        app._settings_win = top
        top.title("設定 — AI Usage Widget")
        top.configure(bg=CARD)
        top.attributes("-topmost", True)
        top.resizable(False, False)

        f_lbl = tkfont.Font(family="Segoe UI", size=-px(13), weight="bold")
        f_txt = tkfont.Font(family="Segoe UI", size=-px(12))
        f_hint = tkfont.Font(family="Segoe UI", size=-px(10))
        pad = {"padx": px(16)}

        def section(text):
            tk.Label(top, text=text, bg=CARD, fg=TEXT, font=f_lbl, anchor="w").pack(
                fill="x", pady=(px(14), px(2)), **pad)

        def hint(text):
            tk.Label(top, text=text, bg=CARD, fg=FAINT, font=f_hint, anchor="w",
                     justify="left").pack(fill="x", **pad)

        def entry(initial, show=None):
            var = tk.StringVar(value=str(initial))
            e = tk.Entry(top, textvariable=var, show=show, bg=FIELD, fg=TEXT,
                         insertbackground=TEXT, relief="flat", font=f_txt,
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=GREEN)
            e.pack(fill="x", ipady=px(5), **pad)
            return var, e

        def check(text, var, fg=TEXT, cmd=None):
            tk.Checkbutton(top, text=text, variable=var, command=cmd, bg=CARD, fg=fg,
                           selectcolor=FIELD, activebackground=CARD, activeforeground=TEXT,
                           font=f_txt, anchor="w").pack(fill="x", **pad)

        pc = self.cfg["providers"]["claude"]
        pg = self.cfg["providers"]["chatgpt"]

        section("Claude")
        self.v_claude_on = tk.BooleanVar(value=pc.get("enabled", True))
        check("Claude を表示する", self.v_claude_on)
        hint("sessionKey（claude.ai の Cookie）")
        self.v_session, self.e_session = entry(pc.get("session_key", ""), show="•")
        self.v_show = tk.BooleanVar(value=False)
        check("入力を表示", self.v_show, fg=MUTED,
              cmd=lambda: self.e_session.config(show="" if self.v_show.get() else "•"))

        getrow = tk.Frame(top, bg=CARD)
        getrow.pack(fill="x", pady=(px(2), 0), **pad)
        tk.Button(getrow, text="自動取得", command=self.acquire_auto, bg=CARD_HEAD, fg=TEXT,
                  relief="flat", font=f_txt, cursor="hand2", activebackground=BORDER).pack(side="left")
        tk.Button(getrow, text="ログインして取得", command=self.acquire_login, bg=CARD_HEAD,
                  fg=TEXT, relief="flat", font=f_txt, cursor="hand2",
                  activebackground=BORDER).pack(side="left", padx=px(6))
        self.v_getstatus = tk.StringVar(value="")
        tk.Label(top, textvariable=self.v_getstatus, bg=CARD, fg=MUTED, font=f_hint,
                 anchor="w", justify="left").pack(fill="x", **pad)
        hint("自動取得=Firefox等で有効。Chrome/Edgeは暗号化のため『ログインして取得』を推奨。\n"
             "手動: claude.ai → F12 → Application → Cookies → sessionKey をコピー")

        section("ChatGPT")
        self.v_gpt_on = tk.BooleanVar(value=pg.get("enabled", True))
        check("ChatGPT を表示する", self.v_gpt_on)
        hint("auth.json のパス（空欄=自動: ~/.codex/auth.json）")
        self.v_auth, _ = entry(pg.get("auth_path", ""))

        section("カスタムAI（Manus / Cursor / v0 などクレジット制）")
        self._f_txt = f_txt
        self._f_hint = f_hint
        self._pad = pad
        self.custom_frame = tk.Frame(top, bg=CARD)
        self.custom_frame.pack(fill="x", **pad)
        tk.Button(top, text="＋ AIを追加", command=self.add_custom, bg=CARD_HEAD, fg=TEXT,
                  relief="flat", font=f_txt, cursor="hand2", activebackground=BORDER).pack(
            anchor="w", pady=(px(2), 0), **pad)
        hint("追加方法: 対象サイトにログイン →F12→Network で残高/使用量のAPIを探し、\n"
             "そのURLと、JSON内の値の場所(例 data.credits.remaining)を入れます。")
        self._refresh_custom_list()

        section("動作")
        row = tk.Frame(top, bg=CARD)
        row.pack(fill="x", **pad)
        tk.Label(row, text="更新間隔(分)", bg=CARD, fg=MUTED, font=f_txt).pack(side="left")
        self.v_interval = tk.StringVar(value=str(self.cfg.get("refresh_minutes", 5)))
        tk.Spinbox(row, from_=1, to=180, textvariable=self.v_interval, width=5, bg=FIELD,
                   fg=TEXT, relief="flat", font=f_txt, buttonbackground=CARD_HEAD,
                   insertbackground=TEXT).pack(side="left", padx=px(8))
        tk.Label(row, text="警告 残り%", bg=CARD, fg=MUTED, font=f_txt).pack(side="left", padx=(px(12), 0))
        self.v_thr = tk.StringVar(value=str(self.cfg.get("low_threshold_pct", 20)))
        tk.Spinbox(row, from_=1, to=99, textvariable=self.v_thr, width=5, bg=FIELD, fg=TEXT,
                   relief="flat", font=f_txt, buttonbackground=CARD_HEAD,
                   insertbackground=TEXT).pack(side="left", padx=px(8))

        self.v_sound = tk.BooleanVar(value=self.cfg.get("alert_sound", True))
        check("しきい値を下回ったら音で知らせる（Windows）", self.v_sound)

        btns = tk.Frame(top, bg=CARD)
        btns.pack(fill="x", pady=px(16), **pad)
        tk.Button(btns, text="保存", command=self.save, bg=GREEN, fg="#0b0d10",
                  relief="flat", font=f_lbl, width=10, cursor="hand2",
                  activebackground="#5cc76a").pack(side="right")
        tk.Button(btns, text="キャンセル", command=self.close, bg=CARD_HEAD, fg=TEXT,
                  relief="flat", font=f_txt, width=10, cursor="hand2",
                  activebackground=BORDER).pack(side="right", padx=px(8))
        tk.Button(btns, text="接続テスト", command=self.test_connection, bg=CARD_HEAD,
                  fg=TEXT, relief="flat", font=f_txt, width=10, cursor="hand2",
                  activebackground=BORDER).pack(side="left")

        top.update_idletasks()
        px0, py0 = app.root.winfo_x(), app.root.winfo_y()
        top.geometry(f"+{max(px0 - px(60), 20)}+{max(py0 + px(20), 20)}")
        top.protocol("WM_DELETE_WINDOW", self.close)

    def close(self):
        self.app._settings_win = None
        try:
            self.top.destroy()
        except Exception:
            pass

    def test_connection(self):
        # 入力中（未保存）の sessionKey でそのまま接続テストする
        self.app.test_claude({"enabled": True, "session_key": self.v_session.get().strip()})

    def acquire_auto(self):
        self.v_getstatus.set("自動取得中…")
        q = queue.Queue()
        threading.Thread(target=lambda: q.put(read_sessionkey_auto()), daemon=True).start()

        def poll():
            try:
                key, status = q.get_nowait()
            except queue.Empty:
                self.top.after(200, poll)
                return
            if key:
                self.v_session.set(key)
                self.v_getstatus.set(f"取得成功（{status}）。『保存』で確定。")
            else:
                self.v_getstatus.set(f"自動取得できず（{status}）→『ログインして取得』を試してください")
        self.top.after(200, poll)

    def acquire_login(self):
        dlg = tk.Toplevel(self.top)
        dlg.title("ログインして取得")
        dlg.configure(bg=CARD)
        dlg.attributes("-topmost", True)
        s = self.app.scale
        f = tkfont.Font(family="Segoe UI", size=-int(round(12 * s)))
        msg = tk.StringVar(value="ブラウザを起動しています…")
        tk.Label(dlg, textvariable=msg, bg=CARD, fg=TEXT, font=f, wraplength=int(300 * s),
                 justify="left", anchor="w").pack(fill="both", expand=True,
                                                  padx=int(16 * s), pady=int(14 * s))
        cancelled = {"v": False}
        bar = tk.Frame(dlg, bg=CARD)
        bar.pack(fill="x", padx=int(16 * s), pady=(0, int(12 * s)))

        def do_cancel():
            cancelled["v"] = True
            msg.set("キャンセル中…")
        tk.Button(bar, text="キャンセル", command=do_cancel, bg=CARD_HEAD, fg=TEXT,
                  relief="flat", font=f, cursor="hand2", activebackground=BORDER).pack(side="right")
        px0, py0 = self.top.winfo_x(), self.top.winfo_y()
        dlg.geometry(f"+{px0 + int(30 * s)}+{py0 + int(60 * s)}")

        q = queue.Queue()

        def work():
            res = capture_sessionkey_login(lambda t: q.put(("status", t)),
                                           lambda: cancelled["v"])
            q.put(("done", res))
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                while True:
                    kind, val = q.get_nowait()
                    if kind == "status":
                        msg.set(val)
                    else:
                        key, status = val
                        if key:
                            self.v_session.set(key)
                            self.v_getstatus.set("ログインして取得：成功。『保存』で確定。")
                            dlg.destroy()
                        else:
                            msg.set(f"取得できませんでした：{status}")
                            return
            except queue.Empty:
                pass
            if dlg.winfo_exists():
                dlg.after(250, poll)
        dlg.after(250, poll)

    # ---------- カスタムAI 管理 ----------
    def _custom_list(self):
        return self.cfg["providers"].setdefault("custom", [])

    def _refresh_custom_list(self):
        for w in self.custom_frame.winfo_children():
            w.destroy()
        items = self._custom_list()
        if not items:
            tk.Label(self.custom_frame, text="（未登録）", bg=CARD, fg=FAINT,
                     font=self._f_hint, anchor="w").pack(fill="x")
            return
        for i, cp in enumerate(items):
            row = tk.Frame(self.custom_frame, bg=CARD)
            row.pack(fill="x", pady=1)
            dot = cp.get("color") or "#8a8f98"
            tk.Label(row, text="●", bg=CARD, fg=dot, font=self._f_txt).pack(side="left")
            on = cp.get("enabled", True)
            tk.Label(row, text=cp.get("name", "AI") + ("" if on else " (無効)"),
                     bg=CARD, fg=(TEXT if on else FAINT), font=self._f_txt,
                     anchor="w").pack(side="left", padx=6)
            tk.Button(row, text="削除", command=lambda idx=i: self.del_custom(idx),
                      bg=CARD_HEAD, fg=TEXT, relief="flat", font=self._f_hint,
                      cursor="hand2", activebackground=BORDER).pack(side="right")
            tk.Button(row, text="編集", command=lambda idx=i: self.edit_custom(idx),
                      bg=CARD_HEAD, fg=TEXT, relief="flat", font=self._f_hint,
                      cursor="hand2", activebackground=BORDER).pack(side="right", padx=4)

    def add_custom(self):
        CustomProviderEditor(self, None, self._on_custom_saved)

    def edit_custom(self, idx):
        items = self._custom_list()
        if 0 <= idx < len(items):
            CustomProviderEditor(self, (idx, items[idx]), self._on_custom_saved)

    def del_custom(self, idx):
        items = self._custom_list()
        if 0 <= idx < len(items):
            items.pop(idx)
            save_config(self.cfg)
            self._refresh_custom_list()
            self.app.refresh()

    def _on_custom_saved(self, idx, data):
        items = self._custom_list()
        if idx is None:
            items.append(data)
        elif 0 <= idx < len(items):
            items[idx] = data
        save_config(self.cfg)
        self._refresh_custom_list()
        self.app.refresh()

    def save(self):
        def to_int(v, default, lo, hi):
            try:
                return max(lo, min(hi, int(float(v))))
            except Exception:
                return default

        self.cfg["providers"]["claude"]["enabled"] = bool(self.v_claude_on.get())
        self.cfg["providers"]["claude"]["session_key"] = self.v_session.get().strip()
        self.cfg["providers"]["chatgpt"]["enabled"] = bool(self.v_gpt_on.get())
        self.cfg["providers"]["chatgpt"]["auth_path"] = self.v_auth.get().strip()
        self.cfg["refresh_minutes"] = to_int(self.v_interval.get(), 5, 1, 180)
        self.cfg["low_threshold_pct"] = to_int(self.v_thr.get(), 20, 1, 99)
        self.cfg["alert_sound"] = bool(self.v_sound.get())
        self.close()
        self.app.apply_settings()


class CustomProviderEditor:
    """カスタムAIの追加/編集ダイアログ。"""

    AUTH_LABELS = [("ブラウザ自動", "browser"), ("Cookie貼付", "cookie"),
                   ("Bearerトークン", "bearer"), ("カスタムヘッダ", "header")]
    KIND_LABELS = [("残クレジット", "remaining_credits"), ("使用率%", "used_percent"),
                   ("残り%", "remaining_percent")]

    def __init__(self, owner, existing, on_save):
        self.owner = owner
        self.app = owner.app
        self.on_save = on_save
        self.idx = existing[0] if existing else None
        cp = existing[1] if existing else {}
        s = self.app.scale

        def px(v):
            return int(round(v * s))

        top = tk.Toplevel(owner.top)
        self.top = top
        top.title("カスタムAI " + ("編集" if existing else "追加"))
        top.configure(bg=CARD)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        f_lbl = tkfont.Font(family="Segoe UI", size=-px(12), weight="bold")
        f_txt = tkfont.Font(family="Segoe UI", size=-px(12))
        f_hint = tkfont.Font(family="Segoe UI", size=-px(10))
        pad = {"padx": px(14)}

        def field(label, initial, width=42, show=None):
            tk.Label(top, text=label, bg=CARD, fg=MUTED, font=f_hint, anchor="w").pack(
                fill="x", pady=(px(6), 0), **pad)
            var = tk.StringVar(value=str(initial or ""))
            tk.Entry(top, textvariable=var, show=show, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                     relief="flat", font=f_txt, highlightthickness=1, highlightbackground=BORDER,
                     highlightcolor=GREEN, width=width).pack(fill="x", ipady=px(4), **pad)
            return var

        self.v_name = field("名前（例: Manus）", cp.get("name", ""))
        self.v_color = field("色 #RRGGBB（任意）", cp.get("color", "#7c5cff"))
        self.v_url = field("使用量API の URL（F12→Network で取得）", cp.get("url", ""), show=None)

        # 認証方式
        auth = cp.get("auth") or {"type": "browser"}
        tk.Label(top, text="認証方式", bg=CARD, fg=MUTED, font=f_hint, anchor="w").pack(
            fill="x", pady=(px(8), 0), **pad)
        self.v_auth_type = tk.StringVar(value=auth.get("type", "browser"))
        arow = tk.Frame(top, bg=CARD)
        arow.pack(fill="x", **pad)
        for text, val in self.AUTH_LABELS:
            tk.Radiobutton(arow, text=text, value=val, variable=self.v_auth_type,
                           command=self._show_auth_fields, bg=CARD, fg=TEXT, selectcolor=FIELD,
                           activebackground=CARD, activeforeground=TEXT, font=f_hint).pack(side="left")

        self.auth_box = tk.Frame(top, bg=CARD)
        self.auth_box.pack(fill="x", **pad)
        self._pad = pad
        self._f_txt, self._f_hint = f_txt, f_hint
        self.v_domain = tk.StringVar(value=auth.get("domain", ""))
        self.v_ckname = tk.StringVar(value=auth.get("name", "") if auth.get("type") == "cookie" else "")
        self.v_ckval = tk.StringVar(value=auth.get("value", "") if auth.get("type") == "cookie" else "")
        self.v_token = tk.StringVar(value=auth.get("token", ""))
        self.v_hdname = tk.StringVar(value=auth.get("name", "") if auth.get("type") == "header" else "")
        self.v_hdval = tk.StringVar(value=auth.get("value", "") if auth.get("type") == "header" else "")
        self._show_auth_fields()

        self.v_vpath = field("値の場所 value_path（例: data.credits.remaining）", cp.get("value_path", ""))
        # 種別
        tk.Label(top, text="種別", bg=CARD, fg=MUTED, font=f_hint, anchor="w").pack(
            fill="x", pady=(px(8), 0), **pad)
        self.v_kind = tk.StringVar(value=cp.get("value_kind", "remaining_credits"))
        krow = tk.Frame(top, bg=CARD)
        krow.pack(fill="x", **pad)
        for text, val in self.KIND_LABELS:
            tk.Radiobutton(krow, text=text, value=val, variable=self.v_kind, bg=CARD, fg=TEXT,
                           selectcolor=FIELD, activebackground=CARD, activeforeground=TEXT,
                           font=f_hint).pack(side="left")
        self.v_total = field("総量の場所 total_path（残クレジット時・任意）", cp.get("total_path", ""))
        self.v_reset = field("リセット日時の場所 reset_path（任意）", cp.get("reset_path", ""))
        rowu = tk.Frame(top, bg=CARD)
        rowu.pack(fill="x", pady=(px(6), 0), **pad)
        tk.Label(rowu, text="単位", bg=CARD, fg=MUTED, font=f_hint).pack(side="left")
        self.v_unit = tk.StringVar(value=cp.get("unit", "cr"))
        tk.Entry(rowu, textvariable=self.v_unit, width=6, bg=FIELD, fg=TEXT, relief="flat",
                 font=f_txt, insertbackground=TEXT).pack(side="left", padx=px(6))
        self.v_enabled = tk.BooleanVar(value=cp.get("enabled", True))
        tk.Checkbutton(rowu, text="有効", variable=self.v_enabled, bg=CARD, fg=TEXT,
                       selectcolor=FIELD, activebackground=CARD, activeforeground=TEXT,
                       font=f_hint).pack(side="left", padx=px(10))

        self.v_status = tk.StringVar(value="")
        tk.Label(top, textvariable=self.v_status, bg=CARD, fg=MUTED, font=f_hint,
                 anchor="w", justify="left", wraplength=px(320)).pack(fill="x", pady=(px(6), 0), **pad)

        btns = tk.Frame(top, bg=CARD)
        btns.pack(fill="x", pady=px(12), **pad)
        tk.Button(btns, text="保存", command=self._save, bg=GREEN, fg="#0b0d10", relief="flat",
                  font=f_lbl, width=8, cursor="hand2", activebackground="#5cc76a").pack(side="right")
        tk.Button(btns, text="キャンセル", command=top.destroy, bg=CARD_HEAD, fg=TEXT,
                  relief="flat", font=f_txt, width=8, cursor="hand2",
                  activebackground=BORDER).pack(side="right", padx=px(6))
        tk.Button(btns, text="テスト", command=self._test, bg=CARD_HEAD, fg=TEXT, relief="flat",
                  font=f_txt, width=8, cursor="hand2", activebackground=BORDER).pack(side="left")

        px0, py0 = owner.top.winfo_x(), owner.top.winfo_y()
        top.update_idletasks()
        top.geometry(f"+{px0 + px(20)}+{max(py0 - px(20), 20)}")

    def _show_auth_fields(self):
        for w in self.auth_box.winfo_children():
            w.destroy()
        t = self.v_auth_type.get()
        pad, f_txt, f_hint = self._pad, self._f_txt, self._f_hint

        def mini(label, var, show=None):
            tk.Label(self.auth_box, text=label, bg=CARD, fg=FAINT, font=f_hint, anchor="w").pack(fill="x")
            tk.Entry(self.auth_box, textvariable=var, show=show, bg=FIELD, fg=TEXT,
                     insertbackground=TEXT, relief="flat", font=f_txt, highlightthickness=1,
                     highlightbackground=BORDER).pack(fill="x", ipady=2)

        if t == "browser":
            mini("ドメイン（空欄=URLから自動。例: manus.im）", self.v_domain)
        elif t == "cookie":
            mini("Cookie名", self.v_ckname)
            mini("Cookie値", self.v_ckval, show="•")
        elif t == "bearer":
            mini("トークン", self.v_token, show="•")
        elif t == "header":
            mini("ヘッダ名（例: Authorization）", self.v_hdname)
            mini("ヘッダ値", self.v_hdval, show="•")

    def _build(self):
        t = self.v_auth_type.get()
        if t == "cookie":
            auth = {"type": "cookie", "name": self.v_ckname.get().strip(), "value": self.v_ckval.get().strip()}
        elif t == "bearer":
            auth = {"type": "bearer", "token": self.v_token.get().strip()}
        elif t == "header":
            auth = {"type": "header", "name": self.v_hdname.get().strip(), "value": self.v_hdval.get().strip()}
        else:
            auth = {"type": "browser", "domain": self.v_domain.get().strip()}
        data = {
            "name": self.v_name.get().strip() or "AI",
            "color": self.v_color.get().strip() or "#7c5cff",
            "url": self.v_url.get().strip(),
            "auth": auth,
            "value_path": self.v_vpath.get().strip(),
            "value_kind": self.v_kind.get(),
            "unit": self.v_unit.get().strip() or "cr",
            "enabled": bool(self.v_enabled.get()),
        }
        if self.v_total.get().strip():
            data["total_path"] = self.v_total.get().strip()
        if self.v_reset.get().strip():
            data["reset_path"] = self.v_reset.get().strip()
        return data

    def _test(self):
        self.v_status.set("テスト中…")
        data = self._build()
        q = queue.Queue()
        threading.Thread(target=lambda: q.put(fetch_custom(data)), daemon=True).start()

        def poll():
            try:
                r = q.get_nowait()
            except queue.Empty:
                self.top.after(200, poll)
                return
            if r["ok"]:
                w = r["windows"][0]
                self.v_status.set(f"OK: {w.get('display','')}")
            else:
                self.v_status.set("NG: " + r["error"])
        self.top.after(200, poll)

    def _save(self):
        data = self._build()
        if not data["url"] or not data["value_path"]:
            self.v_status.set("URL と value_path は必須です")
            return
        self.on_save(self.idx, data)
        self.top.destroy()


# ─────────────────────────── main ───────────────────────────

def main():
    if not acquire_single_instance():
        return
    enable_dpi_awareness()
    cfg = load_config()
    root = tk.Tk()
    root.title("AI Usage Widget")
    UsageWidget(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(e)
