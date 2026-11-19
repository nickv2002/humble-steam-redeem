"""Extract Humble Bundle keys by driving a headless browser (``agent-browser`` CLI).

Humble's 2026 site is Next.js with server actions and no REST API.  The library
page (/home/library?page=N) loads its tiles by POSTing the ``filterUserLibrary``
server action, which returns structured JSON (title, bundle name, key type,
revealed key, gift status, expiry, ...).  We replay that action from inside the
logged-in page (same-origin ``fetch``) with a large page size, so the whole
library takes a few dozen requests instead of one browser page per 5 tiles.

Reveal is ``redeemEntitlementAction(entitlementId, keyIntegrationId, extra)``,
triggered by the "Reveal" button on /home/library/content/<uuid>.  It is
irreversible, so ``reveal_key`` clicks that button for exactly one key and only
when explicitly called; nothing else in this module ever clicks it.

There is no Steam app id on the new site.  Steam key dicts still carry the
``steam_app_id`` key (value ``None``) so ``find_dict_keys(..., "steam_app_id",
True)`` selects them; ownership then falls back to fuzzy name matching.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Optional

SITE = "https://www.humblebundle.com"
LIBRARY_URL = f"{SITE}/home/library"
DEFAULT_PROFILE = "~/.humble-browser-profile"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
BROWSER_ARGS = "--no-sandbox,--disable-blink-features=AutomationControlled"
PAGE_LIMIT = 100  # tiles per server-action call (server accepts at least 100)
LOGIN_ENV_FILE = "humble-login.env"
LIST_ACTION_NAME = "filterUserLibrary"

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.]{3,}$")


class HumbleLoginRequired(RuntimeError):
    """The saved browser session is not logged in to Humble."""


class HumbleBrowserError(RuntimeError):
    """The browser or site did not behave as expected."""


# --------------------------------------------------------------------------
# Pure parsing helpers (no browser access; unit-tested)
# --------------------------------------------------------------------------


def parse_flight_payload(text: str) -> dict:
    """Return the JSON object from a server-action (text/x-component) response.

    The response looks like ``0:{...}\\n1:{"results":[...],...}\\n``; the line
    starting with ``1:`` holds the action result.  A bare JSON object (as
    returned by our in-page helper) is accepted too.
    """
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("1:"):
            return json.loads(line[2:])
    raise ValueError("no result line in server action response")


def _undef(value: Any) -> Any:
    """React flight encodes ``undefined`` as the string ``"$undefined"``."""
    return None if value == "$undefined" else value


def parse_key_entitlement(tile: dict, key: dict, key_index: int = 0) -> dict:
    """Convert one library key (plus its tile) to the legacy tpk dict shape."""
    key_type = (key.get("type") or tile.get("type") or "").lower()
    revealed = ((key.get("redemptionData") or {}).get("keyFormatted")) or None
    tile_id = tile.get("id") or key.get("id") or ""
    out: dict = {
        "human_name": key.get("title") or tile.get("title") or "",
        "key_type_human_name": key_type,
        "is_gift": key.get("giftStatus") == "gifted" and not revealed,
        "is_expired": bool(key.get("expired")),
        "gamekey": tile.get("productEntitlementId") or "",
        "key_id": key.get("id") or tile_id,
        "machine_name": tile.get("contentId") or "",
        "expires": _undef(key.get("unavailableAfter")),
        "detail_url": f"{LIBRARY_URL}/content/{tile_id}",
        "key_index": key_index,
    }
    if key_type == "steam":
        # The new site exposes no Steam app id; None = unknown (never faked).
        out["steam_app_id"] = None
    if revealed:
        out["redeemed_key_val"] = revealed
    return out


def parse_library_results(results: list[dict]) -> list[dict]:
    """Flatten library tiles into key dicts (download-only tiles are skipped)."""
    keys: list[dict] = []
    for tile in results:
        for idx, key in enumerate((tile.get("entitlements") or {}).get("keys") or []):
            keys.append(parse_key_entitlement(tile, key, idx))
    return keys


def group_into_orders(keys: list[dict], bundle_names: Optional[dict] = None) -> list[dict]:
    """Wrap key dicts in the legacy order shape, grouped by purchase.

    ``[{"gamekey", "product": {"human_name"}, "tpkd_dict": {"all_tpks": [..]}}]``
    """
    bundle_names = bundle_names or {}
    orders: dict[str, dict] = {}
    for key in keys:
        gk = key.get("gamekey") or key.get("key_id", "")
        order = orders.get(gk)
        if order is None:
            order = orders[gk] = {
                "gamekey": key.get("gamekey", ""),
                "product": {"human_name": bundle_names.get(gk, "")},
                "tpkd_dict": {"all_tpks": []},
            }
        order["tpkd_dict"]["all_tpks"].append(key)
    return list(orders.values())


def extract_key_from_detail_html(html: str, key_id: str) -> str:
    """Pull ``keyFormatted`` for ``key_id`` out of a content page's flight data.

    Returns "" if the key is not (yet) revealed or the data is not present.
    """
    pattern = re.compile(
        re.escape(key_id)
        + r'.{0,600}?keyFormatted\\*"\s*:\s*\\*"([^"\\]+)\\*"',
        re.S,
    )
    m = pattern.search(html)
    return m.group(1) if m else ""


def find_action_id(js_text: str, action_name: str) -> Optional[str]:
    """Find a server action id by its exported name inside a JS chunk."""
    m = re.search(
        r'createServerReference\)\("([0-9a-f]+)",[\w$]+\.callServer,void 0,'
        r'[\w$]+\.findSourceMapURL,"' + re.escape(action_name) + '"\\)',
        js_text,
    )
    return m.group(1) if m else None


def parse_login_env(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines with plain string splitting (never shell-evaluated)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip("\n")
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def is_auth_url(url: str) -> bool:
    return "auth.humblebundle.com" in url or "/login" in url.split("?")[0]


# --------------------------------------------------------------------------
# Browser driver
# --------------------------------------------------------------------------


def _retry(fn: Callable[[], Any], attempts: int = 4, base_delay: float = 1.5,
           retry_on: tuple = (HumbleBrowserError,)) -> Any:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except HumbleLoginRequired:
            raise
        except retry_on as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise HumbleBrowserError(f"gave up after {attempts} attempts: {last}")


class HumbleBrowser:
    def __init__(self, profile_dir: str = DEFAULT_PROFILE, session_name: str = "humble",
                 user_agent: str = DEFAULT_USER_AGENT, binary: str = "agent-browser"):
        self.profile_dir = os.path.expanduser(profile_dir)
        self.session_name = session_name
        self.binary = binary
        self._env = {
            **os.environ,
            "AGENT_BROWSER_PROFILE": self.profile_dir,
            "AGENT_BROWSER_USER_AGENT": user_agent,
        }
        self._started = False
        self._action_id: Optional[str] = None

    # ---- low level -------------------------------------------------------

    def _run(self, *args: str, timeout: float = 90) -> str:
        cmd = [self.binary, "--session", self.session_name, *args]
        try:
            proc = subprocess.run(cmd, env=self._env, capture_output=True, text=True,
                                  timeout=timeout)
        except FileNotFoundError as exc:
            raise HumbleBrowserError(
                "agent-browser CLI not found; install it and make sure it is on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise HumbleBrowserError(f"agent-browser {args[0]} timed out") from exc
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise HumbleBrowserError(
                f"agent-browser {args[0]} failed: {msg[-1][:200] if msg else proc.returncode}"
            )
        return proc.stdout

    def _eval(self, js: str, timeout: float = 90) -> Any:
        out = self._run("eval", js, timeout=timeout).strip()
        # agent-browser prints the JSON-encoded result; strings arrive quoted.
        try:
            val = json.loads(out)
        except json.JSONDecodeError:
            return out
        return val

    def _eval_json(self, js: str, timeout: float = 90) -> Any:
        val = self._eval(js, timeout=timeout)
        return json.loads(val) if isinstance(val, str) else val

    def _start(self) -> None:
        if self._started:
            return
        # --args are ignored if a browser is already running, so close first.
        for sess in (["--session", self.session_name], []):
            subprocess.run([self.binary, *sess, "close"], env=self._env,
                           capture_output=True, text=True, timeout=60)
        self._run("--args", BROWSER_ARGS, "open", "about:blank", timeout=120)
        self._started = True

    def _url(self) -> str:
        v = self._eval("location.href")
        return v if isinstance(v, str) else ""

    def _goto(self, url: str, ready_js: str = "true", timeout: float = 30) -> None:
        """Open ``url`` and wait until ``ready_js`` is truthy (or auth redirect)."""
        self._start()
        self._run("open", url, timeout=120)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_auth_url(self._url()):
                raise HumbleLoginRequired("Humble session expired; run interactively to log in")
            try:
                if self._eval(f"document.readyState==='complete' && ({ready_js})") is True:
                    return
            except HumbleBrowserError:
                pass
            time.sleep(0.5)
        raise HumbleBrowserError(f"timed out waiting for {url}")

    def _open_library(self) -> None:
        _retry(lambda: self._goto(
            LIBRARY_URL + "?page=1",
            ready_js="document.body.innerText.includes('Your Library')"
                     " && !document.body.innerText.includes('Oops')"))

    # ---- login -----------------------------------------------------------

    def ensure_logged_in(self, *, interactive: bool) -> None:
        try:
            self._open_library()
            return
        except HumbleLoginRequired:
            if not interactive:
                raise HumbleLoginRequired("run interactively to log in") from None
        self._login()
        self._open_library()

    def _fill(self, selector: str, value: str) -> None:
        self._run("wait", selector, timeout=30)
        self._run("fill", selector, value)

    def _click_text(self, pattern: str) -> bool:
        js = (
            "(()=>{const re=new RegExp(%s,'i');"
            "const els=[...document.querySelectorAll('button,a,[role=button],[role=radio],label,li')]"
            ".filter(e=>re.test((e.innerText||'').trim())&&(e.innerText||'').length<80);"
            "if(!els.length)return false;els[els.length-1].click();return true})()"
            % json.dumps(pattern)
        )
        return self._eval(js) is True

    def _wait_js(self, js: str, timeout: float = 20) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self._eval(js) is True:
                    return True
            except HumbleBrowserError:
                pass
            time.sleep(0.4)
        return False

    def _login(self) -> None:
        creds = self._read_credentials()
        try:
            from rich.prompt import Prompt  # type: ignore
            ask = lambda msg: Prompt.ask(msg)  # noqa: E731
        except ImportError:  # pragma: no cover
            ask = input

        # NOTE: the login DOM could not be exercised in testing (session was
        # already logged in); selectors below are best-effort and generic.
        email_sel = "input[type=email], input[name=email], input[name=username], input[autocomplete=username]"
        self._fill(email_sel, creds["USERNAME"])
        if not self._click_text(r"^(continue|next|log ?in|sign ?in)"):
            self._run("press", "Enter")
        if not self._wait_js("!!document.querySelector('input[type=password]')", 25):
            raise HumbleBrowserError("password field did not appear")
        self._fill("input[type=password]", creds["PASSWORD"])
        if not self._click_text(r"^(continue|log ?in|sign ?in|submit)"):
            self._run("press", "Enter")

        if not self._wait_js("/authenticator/i.test(document.body.innerText)", 25):
            if not is_auth_url(self._url()):
                return  # no second factor required
            raise HumbleBrowserError("two-factor method chooser did not appear")
        self._click_text(r"authenticator")
        self._click_text(r"^(continue|next|select|submit)")
        code_sel = ("input[autocomplete=one-time-code], input[inputmode=numeric], "
                    "input[name*=code i], input[type=text], input[type=tel]")
        if not self._wait_js(f"!!document.querySelector({json.dumps(code_sel)})", 20):
            raise HumbleBrowserError("one-time-code field did not appear")
        # The flow expires within seconds: ask, then submit immediately.
        otp = ""
        while not re.fullmatch(r"\d{6}", otp):
            otp = ask("Enter the 6-digit authenticator code").strip()
        self._fill(code_sel, otp)
        if not self._click_text(r"^(verify|continue|submit|log ?in|sign ?in)"):
            self._run("press", "Enter")
        end = time.time() + 30
        while time.time() < end:
            if not is_auth_url(self._url()):
                return
            time.sleep(0.5)
        raise HumbleLoginRequired("login did not complete (wrong or expired code?)")

    @staticmethod
    def _read_credentials(path: str = LOGIN_ENV_FILE) -> dict[str, str]:
        try:
            with open(path, encoding="utf-8") as fh:
                creds = parse_login_env(fh.read())
        except OSError as exc:
            raise HumbleLoginRequired(f"cannot read {path} for interactive login") from exc
        if not creds.get("USERNAME") or not creds.get("PASSWORD"):
            raise HumbleLoginRequired(f"{path} must define USERNAME= and PASSWORD=")
        return creds

    # ---- reading ---------------------------------------------------------

    def _discover_action_id(self) -> str:
        if self._action_id:
            return self._action_id
        js = (
            "(async()=>{const urls=[...new Set(performance.getEntriesByType('resource')"
            ".map(e=>e.name).filter(n=>n.includes('/_next/static/')&&n.endsWith('.js')))];"
            "for(const u of urls){try{const t=await(await fetch(u)).text();"
            "if(t.includes(%s))return t}catch(e){}}return ''})()" % json.dumps(LIST_ACTION_NAME)
        )
        chunk = self._eval(js, timeout=120)
        aid = find_action_id(chunk, LIST_ACTION_NAME) if isinstance(chunk, str) else None
        if not aid:
            raise HumbleBrowserError(f"could not locate the {LIST_ACTION_NAME} server action")
        self._action_id = aid
        return aid

    def _fetch_page(self, page: int, limit: int) -> dict:
        aid = self._discover_action_id()
        js = (
            "(async()=>{const fd=new FormData();"
            "const set=(k,v)=>fd.append(k,String(v));"
            "set('_1_requestId',%d);set('_1_query','');set('_1_sort','mostRecent');"
            "set('_1_cursor',%d);set('_1_page',%d);set('_1_limit',%d);"
            "set('_1_hideRevealed','false');set('_1_hideExpired','false');set('_1_dateExpires','');"
            "set('0','[{\"results\":[]},\"$K1\"]');"
            "const r=await fetch('/home/library?page=%d',{method:'POST',"
            "headers:{Accept:'text/x-component','next-action':%s},body:fd});"
            "if(r.redirected&&r.url.includes('auth.'))return JSON.stringify({auth:true});"
            "if(!r.ok)return JSON.stringify({error:r.status});"
            "const t=await r.text();const l=t.split('\\n').find(x=>x.startsWith('1:'));"
            "if(!l)return JSON.stringify({error:'noresult'});"
            "const j=JSON.parse(l.slice(2));"
            "for(const x of j.results||[]){for(const k of (x.entitlements&&x.entitlements.keys)||[]){"
            "delete k.instructionsHtml;delete k.customDisclaimer}}"
            "return JSON.stringify(j)})()"
            % (page, (page - 1) * limit, page, limit, page, json.dumps(aid))
        )
        data = self._eval_json(js, timeout=120)
        if isinstance(data, dict) and data.get("auth"):
            raise HumbleLoginRequired("Humble session expired; run interactively to log in")
        if not isinstance(data, dict) or "results" not in data:
            raise HumbleBrowserError(f"library page {page} failed: {str(data)[:120]}")
        return data

    def fetch_order_details(self) -> tuple[list[dict], list[str]]:
        """Return ``(order_details, failed_ids)`` for every key in the library."""
        self._open_library()
        keys: list[dict] = []
        seen: set = set()
        failed: list[str] = []
        total_docs: Optional[int] = None
        bundle_names: dict[str, str] = {}
        page, total_pages = 1, None
        while total_pages is None or page <= total_pages:
            try:
                data = _retry(lambda: self._fetch_page(page, PAGE_LIMIT))
            except HumbleLoginRequired:
                raise
            except HumbleBrowserError:
                failed.append(f"page:{page}")
                page += 1
                if page > 2000:
                    break
                continue
            pag = data.get("pagination") or {}
            total_pages = pag.get("totalPages", total_pages)
            total_docs = pag.get("totalDocs", total_docs)
            results = data.get("results") or []
            if not results:
                break
            for tile in results:
                for k in parse_library_results([tile]):
                    ident = (k["key_id"], k["key_index"])
                    if ident in seen:
                        continue
                    seen.add(ident)
                    keys.append(k)
                    bundle_names.setdefault(k["gamekey"], tile.get("subTitle") or "")
            page += 1
            if total_pages is None:
                total_pages = page  # unknown: keep going until an empty page
        return group_into_orders(keys, bundle_names), failed

    # ---- reveal ----------------------------------------------------------

    def reveal_key(self, tpk: dict) -> str:
        """Click Reveal for exactly this key; return the key, or "" on failure.

        IRREVERSIBLE on Humble's side: only call for keys you intend to redeem.
        """
        if tpk.get("redeemed_key_val"):
            return tpk["redeemed_key_val"]
        url = tpk.get("detail_url")
        key_id = tpk.get("key_id")
        if not url or not key_id or tpk.get("is_expired") or tpk.get("is_gift"):
            return ""
        try:
            self._goto(url, ready_js=(
                "[...document.querySelectorAll('button')].some(b=>b.innerText.trim()==='Reveal')"
                "||/\\b[A-Z0-9]{4,}-[A-Z0-9]{4,}/.test(document.body.innerText)"))
            idx = int(tpk.get("key_index", 0))
            clicked = self._eval(
                "(()=>{const b=[...document.querySelectorAll('main button')]"
                ".filter(b=>b.innerText.trim()==='Reveal');"
                "if(b.length<=%d)return 'none';b[%d].click();return 'ok'})()" % (idx, idx))
            if clicked != "ok":
                # Already revealed (e.g. in another session): just read it.
                return self._read_key(url, key_id, wait=0)
            return self._read_key(url, key_id, wait=30)
        except HumbleLoginRequired:
            raise
        except HumbleBrowserError:
            return ""

    def _read_key(self, url: str, key_id: str, wait: float) -> str:
        end = time.time() + wait
        while True:
            html = self._eval(
                "(async()=>await(await fetch(%s)).text())()" % json.dumps(url), timeout=60)
            key = extract_key_from_detail_html(html, key_id) if isinstance(html, str) else ""
            if key or time.time() >= end:
                return key
            time.sleep(1.5)

    # ---- teardown --------------------------------------------------------

    def close(self) -> None:
        try:
            subprocess.run([self.binary, "--session", self.session_name, "close"],
                           env=self._env, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        self._started = False
        self._action_id = None

    def __enter__(self) -> "HumbleBrowser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
