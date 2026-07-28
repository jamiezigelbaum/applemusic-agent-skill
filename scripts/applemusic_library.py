#!/usr/bin/env python3
"""applemusic-library - add Apple Music *catalog* tracks to your library.

AppleScript can only see the *local* library, so an agent can play and organise
what is already there but cannot add anything from the Apple Music catalog.
Doing that needs the Apple Music API, which normally needs a Music User Token -
and the MusicKit JS browser flow that mints one is broken for many accounts
(`authorize()` returns a bare `AUTHORIZATION_ERROR: Unauthorized`).

This tool sidesteps the token entirely. A Safari tab already signed in to
music.apple.com is *already* an authorised MusicKit session, and the page can
make user-scoped API calls on its own behalf. We drive that page over Apple
Events and read the answers back. No developer token, no Music User Token, no
Apple Developer Program membership.

TWO MODES
---------
The signed-in page will also *hand you* the credential pair it is using -
`MusicKit.getInstance().developerToken` and `.musicUserToken` - and that pair
works from an ordinary HTTPS client with no browser at all.

- `safari`  drives the page over Apple Events. macOS + Safari only.
- `http`    talks straight to api.music.apple.com with a harvested token pair.
            Runs anywhere, including Linux, headless, with no window on anyone's
            screen.

`harvest` moves you from the first to the second: it reads the pair out of the
live page once and writes it to a token file. After that, the browser is only
needed again when the developer token expires (~70 days; `doctor` warns first).

Python 3.8+, standard library only. `safari` mode shells out to `osascript`.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "2.0.0"

# Exit codes.
EXIT_OK = 0
EXIT_FAILED = 1  # the command ran but the operation did not fully succeed
EXIT_USAGE = 2  # bad arguments
EXIT_ENVIRONMENT = 3  # the session is not usable; `doctor` explains

API_BASE = "https://api.music.apple.com"

# The developer token the web player uses carries a `root_https_origin` claim,
# and Apple enforces it: send these calls without an Origin header and every one
# of them returns 401, including calls that need no user scope at all. This is
# the single easiest thing to get wrong when moving off the browser.
API_ORIGIN = "https://music.apple.com"

ENV_DEV_TOKEN = "APPLE_MUSIC_DEVELOPER_TOKEN"
ENV_USER_TOKEN = "APPLE_MUSIC_USER_TOKEN"
DEFAULT_TOKEN_PATH = "~/.config/applemusic-agent/tokens.json"

# Warn this far ahead of developer-token expiry. Expiry is a cliff, not a
# degradation: the token stops working outright, and re-harvesting needs a human
# at a signed-in Safari. Two weeks is enough notice to catch a warning even if
# nobody looks at the tool for a while.
TOKEN_EXPIRY_WARN_DAYS = 14

# Apple throttles bursts of catalog calls with HTTP 429. Retries are cheap and
# the alternative is a caller mistaking a rate limit for a dead credential.
RATE_LIMIT_RETRIES = 4

# A successful POST to /v1/me/library returns HTTP 202 with a *completely empty
# body*. MusicKit tries to JSON.parse that empty string and the promise rejects.
# These are the parse-failure messages that therefore mean SUCCESS, not failure.
# (WebKit says "JSON Parse error: Unexpected EOF"; the others are here so a
# future engine change does not turn a working add into a reported failure.)
_EMPTY_BODY_ERRORS = (
    "unexpected eof",
    "unexpected end of json input",
    "unexpected end of input",
    "unexpected end of data",
    "json parse error",
)

_MUSIC_HOST_RE = re.compile(r"^https?://(beta\.)?music\.apple\.com(/|$)", re.I)


class ToolError(Exception):
    """A clean, user-facing failure. Never let a traceback reach the user."""

    def __init__(self, message, remedy=None, code="error", exit_code=EXIT_FAILED):
        super().__init__(message)
        self.message = message
        self.remedy = remedy
        self.code = code
        self.exit_code = exit_code

    def to_dict(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message,
                                       "remedy": self.remedy}}


# --------------------------------------------------------------------------
# osascript plumbing
# --------------------------------------------------------------------------

def _run(argv, timeout=30):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise ToolError("`osascript` not found - this tool only runs on macOS.",
                        code="not_macos", exit_code=EXIT_ENVIRONMENT)
    except subprocess.TimeoutExpired:
        raise ToolError("osascript timed out. Safari may be blocked on a dialog.",
                        remedy="Bring Safari to the front and dismiss any open dialog, then retry.",
                        code="osascript_timeout", exit_code=EXIT_ENVIRONMENT)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _osascript(script, timeout=30):
    """Run an AppleScript source string and translate the usual failures."""
    rc, out, err = _run(["osascript", "-e", script], timeout=timeout)
    if rc != 0:
        low = err.lower()
        if "-1743" in err or "not authorized" in low or "not allowed assistive" in low:
            raise ToolError(
                "This process is not allowed to send Apple events to Safari.",
                remedy=("System Settings -> Privacy & Security -> Automation -> "
                        "<the app running this tool, e.g. Terminal> -> enable Safari. "
                        "If it is not listed, run the command once and approve the prompt."),
                code="automation_denied", exit_code=EXIT_ENVIRONMENT)
        if "-600" in err or "application isn't running" in low:
            raise ToolError("Safari is not running.",
                            remedy="Open Safari and load https://music.apple.com while signed in.",
                            code="safari_not_running", exit_code=EXIT_ENVIRONMENT)
        if "-2741" in err or "syntax error" in low:
            # Should be impossible: we transport JS as base64 precisely so that
            # no quote or backslash ever reaches the AppleScript parser.
            raise ToolError("AppleScript rejected the generated script: " + (err or "syntax error"),
                            remedy="This is a bug in this tool; please report it.",
                            code="applescript_syntax")
        raise ToolError("osascript failed: " + (err or "unknown error"), code="osascript_failed")
    return out


def _applescript_literal(text):
    """Quote a Python string as an AppleScript string literal.

    Only backslash and double-quote need escaping. Everything we pass through
    here is ASCII-safe by construction (base64 or a small fixed word), but the
    escaping stays because hand-written JS in an AppleScript literal is the
    single most common way to get a `-2741 syntax error`.
    """
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _js_payload(js):
    """Wrap JavaScript so AppleScript only ever sees base64.

    `do JavaScript "..."` takes an AppleScript *string literal*, so every quote
    and backslash in the JS has to be escaped, and any non-ASCII character is at
    the mercy of the text encoding. Base64 has none of those characters, so the
    escaping problem disappears structurally instead of being handled carefully.
    """
    blob = base64.b64encode(js.encode("utf-8")).decode("ascii")
    return ("eval(new TextDecoder().decode(Uint8Array.from(atob('%s'), "
            "function(c){return c.charCodeAt(0)})))" % blob)


def _safari_running():
    """True if Safari has a running process. Deliberately avoids Apple events:
    `tell application "Safari"` would *launch* Safari as a side effect."""
    rc, _, _ = _run(["pgrep", "-x", "Safari"], timeout=10)
    return rc == 0


# --------------------------------------------------------------------------
# The signed-in Safari session
# --------------------------------------------------------------------------

class MusicSession:
    """A music.apple.com tab in Safari, used as an authorised MusicKit client."""

    def __init__(self, timeout=30.0, poll_interval=0.25):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._tab = None  # (window_index, tab_index)
        self._storefront = None

    # -- tab discovery ----------------------------------------------------

    def list_music_tabs(self):
        """Every music.apple.com tab, in any window - not just the frontmost."""
        if not _safari_running():
            raise ToolError("Safari is not running.",
                            remedy="Open Safari and load https://music.apple.com while signed in.",
                            code="safari_not_running", exit_code=EXIT_ENVIRONMENT)
        script = (
            'tell application "Safari"\n'
            '  set acc to ""\n'
            '  repeat with w from 1 to (count of windows)\n'
            '    try\n'
            '      repeat with t from 1 to (count of tabs of window w)\n'
            '        set u to ""\n'
            '        try\n'
            '          set u to (URL of tab t of window w) as text\n'
            '        end try\n'
            '        set acc to acc & w & " " & t & " " & u & linefeed\n'
            '      end repeat\n'
            '    end try\n'
            '  end repeat\n'
            '  return acc\n'
            'end tell'
        )
        tabs = []
        for line in _osascript(script).splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) != 3:
                continue
            win, tab, url = parts
            if not win.isdigit() or not tab.isdigit():
                continue
            if _MUSIC_HOST_RE.match(url):
                tabs.append((int(win), int(tab), url))
        return tabs

    def find_tab(self, refresh=False):
        """Pick a music.apple.com tab that has a live, authorised MusicKit."""
        if self._tab and not refresh:
            return self._tab
        tabs = self.list_music_tabs()
        if not tabs:
            raise ToolError("No music.apple.com tab is open in Safari.",
                            remedy="Open https://music.apple.com in Safari and sign in.",
                            code="no_music_tab", exit_code=EXIT_ENVIRONMENT)
        first_error = None
        fallback = None
        for win, tab, _url in tabs:
            self._tab = (win, tab)
            try:
                state = self._eval_sync(
                    "(typeof MusicKit==='undefined')?'nomusickit':"
                    "(function(){try{return MusicKit.getInstance().isAuthorized?'auth':'noauth'}"
                    "catch(e){return 'noinstance'}})()")
            except ToolError as exc:
                first_error = first_error or exc
                continue
            if state == "auth":
                return self._tab
            if state in ("noauth", "noinstance") and fallback is None:
                fallback = (win, tab, state)
        if fallback:
            self._tab = (fallback[0], fallback[1])
            raise ToolError("Found a music.apple.com tab, but the MusicKit session is not signed in.",
                            remedy=("Click your account avatar in that Safari tab and sign in to "
                                    "Apple Music, then reload the page."),
                            code="not_authorized", exit_code=EXIT_ENVIRONMENT)
        self._tab = None
        if first_error:
            raise first_error
        raise ToolError("A music.apple.com tab is open but MusicKit never loaded in it.",
                        remedy="Reload the tab and wait for the player UI to appear, then retry.",
                        code="musickit_missing", exit_code=EXIT_ENVIRONMENT)

    # -- JavaScript execution --------------------------------------------

    def _do_javascript(self, js):
        win, tab = self._tab
        script = 'tell application "Safari" to do JavaScript %s in tab %d of window %d' % (
            _applescript_literal(_js_payload(js)), tab, win)
        return _osascript(script, timeout=max(20, int(self.timeout)))

    def _eval_sync(self, expr):
        """Evaluate a *synchronous* JS expression and return it as a string."""
        out = self._do_javascript("String(%s)" % expr)
        if out == "":
            # `do JavaScript` silently returns nothing when the Develop-menu
            # switch is off. That is the single most common setup mistake.
            raise ToolError("Safari returned nothing for a JavaScript call.",
                            remedy=("Safari -> Settings -> Advanced -> tick 'Show features for web "
                                    "developers', then Develop -> tick 'Allow JavaScript from Apple "
                                    "Events'."),
                            code="js_from_apple_events_disabled", exit_code=EXIT_ENVIRONMENT)
        return out

    def eval_async(self, expr, timeout=None):
        """Run a promise-returning JS expression and wait for its settlement.

        `do JavaScript` returns the moment the statement is evaluated, so an
        async call's value is never its return value. We stash the settled
        result on a unique `window` slot and poll for it.
        """
        timeout = self.timeout if timeout is None else timeout
        self.find_tab()
        slot = "__aml_%s" % uuid.uuid4().hex
        dispatch = (
            "(function(){"
            "var S=%(slot)s;"
            "window[S]=null;"
            "function info(e){return {message:String((e&&e.message)||e),"
            "name:String((e&&e.name)||''),status:(e&&e.status)||null};}"
            "try{Promise.resolve().then(function(){return (%(expr)s);})"
            ".then(function(v){window[S]=JSON.stringify({ok:true,value:(v===undefined?null:v)});})"
            ".catch(function(e){window[S]=JSON.stringify({ok:false,error:info(e)});});}"
            "catch(e){window[S]=JSON.stringify({ok:false,error:info(e)});}"
            "return 'dispatched';})()"
        ) % {"slot": json.dumps(slot), "expr": expr}

        if self._do_javascript(dispatch) == "":
            raise ToolError("Safari returned nothing for a JavaScript call.",
                            remedy=("Safari -> Settings -> Advanced -> tick 'Show features for web "
                                    "developers', then Develop -> tick 'Allow JavaScript from Apple "
                                    "Events'."),
                            code="js_from_apple_events_disabled", exit_code=EXIT_ENVIRONMENT)

        read = ("(function(){var S=%s;var v=window[S];"
                "if(v){try{delete window[S];}catch(e){window[S]=null;}}"
                "return v||'';})()") % json.dumps(slot)
        deadline = time.time() + timeout
        while True:
            raw = self._do_javascript(read)
            if raw:
                try:
                    return json.loads(raw)
                except ValueError:
                    raise ToolError("Could not parse the result returned by the Safari page.",
                                    code="bad_result")
            if time.time() >= deadline:
                raise ToolError("Timed out after %gs waiting for Apple Music to answer." % timeout,
                                remedy="Check the tab is still on music.apple.com and try again.",
                                code="timeout")
            time.sleep(self.poll_interval)

    # -- Apple Music API --------------------------------------------------

    def api_get(self, path, timeout=None):
        """GET through the page's own MusicKit instance.

        NOTE: the web player talks to `amp-api.music.apple.com`, Apple's
        internal endpoint - *not* the public `api.music.apple.com`. Parameter
        semantics differ (see `add_to_library`). Do not "correct" the shapes
        used here to match the public API docs; they are what amp-api accepts.
        """
        expr = ("MusicKit.getInstance().api.music(%s).then(function(r){"
                "return {status:r.status, data:r.data, errors:r.errors||null};})" % json.dumps(path))
        res = self.eval_async(expr, timeout=timeout)
        if not res.get("ok"):
            err = res.get("error") or {}
            raise ToolError("Apple Music request failed: %s" % (err.get("message") or "unknown"),
                            code="api_error")
        value = res.get("value") or {}
        status = value.get("status")
        if status is not None and int(status) >= 400:
            detail = _first_error_detail(value.get("errors") or (value.get("data") or {}).get("errors"))
            raise ToolError("Apple Music returned HTTP %s%s" % (status, (": " + detail) if detail else ""),
                            code="http_%s" % status)
        return value.get("data") or {}

    def storefront(self):
        if self._storefront:
            return self._storefront
        data = self.api_get("/v1/me/storefront")
        items = data.get("data") or []
        if not items or not items[0].get("id"):
            raise ToolError("Could not determine your Apple Music storefront.",
                            remedy="Confirm the Safari tab is signed in to Apple Music.",
                            code="no_storefront", exit_code=EXIT_ENVIRONMENT)
        self._storefront = items[0]["id"]
        return self._storefront

    # -- library mutation -------------------------------------------------

    def add_to_library(self, catalog_id, kind="songs"):
        """POST the catalog id into the library. Returns a raw outcome dict.

        Two quirks are load-bearing here:

        1. amp-api accepts the add only through `api.post()` with the ids in the
           query string. `api.music()` with the same `ids[songs]=` returns
           400 "Unsupported type specified: songs".
        2. A successful add returns an EMPTY body, which MusicKit then fails to
           JSON.parse - so success arrives as a *rejected* promise whose message
           is a JSON parse error. We treat that specific rejection as success and
           then verify independently, rather than trusting it.
        """
        path = "/v1/me/library?ids[%s]=%s" % (kind, urllib.parse.quote(str(catalog_id), safe=""))
        expr = ("MusicKit.getInstance().api.post(%s).then(function(r){"
                "return {settled:'resolved', status:(r&&r.status)||null};})"
                ".catch(function(e){return {settled:'rejected',"
                "message:String((e&&e.message)||e), status:(e&&e.status)||null};})" % json.dumps(path))
        res = self.eval_async(expr)
        if not res.get("ok"):
            err = res.get("error") or {}
            return {"accepted": False, "detail": err.get("message") or "unknown error"}
        value = res.get("value") or {}
        if value.get("settled") == "resolved":
            return {"accepted": True, "detail": "resolved with HTTP %s" % value.get("status")}
        message = value.get("message") or ""
        if _is_empty_body_error(message):
            return {"accepted": True, "detail": "empty response body (expected on success)"}
        return {"accepted": False, "detail": message or "request rejected"}

    # -- lookups ----------------------------------------------------------

    def catalog_songs(self, catalog_ids):
        """Look up catalog metadata for up to ~100 ids at once."""
        sf = self.storefront()
        ids = ",".join(urllib.parse.quote(str(i), safe="") for i in catalog_ids)
        path = "/v1/catalog/%s/songs?ids=%s" % (urllib.parse.quote(sf, safe=""), ids)
        expr = ("MusicKit.getInstance().api.music(%s).then(function(r){"
                "return {status:r.status, errors:r.errors||null, items:(((r.data||{}).data)||[])"
                ".map(function(x){var a=x.attributes||{};return {id:x.id, name:a.name||null,"
                "artistName:a.artistName||null, albumName:a.albumName||null,"
                "durationInMillis:a.durationInMillis||null};})};})" % json.dumps(path))
        res = self.eval_async(expr)
        if not res.get("ok"):
            return {}
        value = res.get("value") or {}
        if value.get("status") is not None and int(value["status"]) >= 400:
            return {}
        return {item["id"]: item for item in (value.get("items") or []) if item.get("id")}

    def find_library_song(self, catalog_id, title=None):
        """Return the library song (`i.xxxx`) for a catalog id, or None.

        `filter[catalog]` is rejected by amp-api ("No filters supplied on the
        request") whether the brackets are literal, percent-encoded, or passed
        as a params object - so we match on `playParams.catalogId`, which every
        library song carries. Two complementary passes, because neither alone is
        reliable: a title search (works for anything, needs the title) and a
        recently-added scan (works for a track added seconds ago, before the
        search index has caught up).
        """
        catalog_id = str(catalog_id)
        if title:
            hit = self._match_in(
                "/v1/me/library/search?term=%s&types=library-songs&limit=25"
                % urllib.parse.quote(title, safe=""),
                catalog_id, results_key="library-songs")
            if hit:
                return hit
        return self._match_in("/v1/me/library/songs?limit=50&sort=-dateAdded", catalog_id)

    def _match_in(self, path, catalog_id, results_key=None):
        if results_key:
            extract = ("(((r.data||{}).results||{})[%s]||{}).data||[]" % json.dumps(results_key))
        else:
            extract = "((r.data||{}).data)||[]"
        expr = ("MusicKit.getInstance().api.music(%s).then(function(r){"
                "return {status:r.status, items:(%s).map(function(x){var a=x.attributes||{};"
                "return {id:x.id, name:a.name||null, artistName:a.artistName||null,"
                "catalogId:((a.playParams||{}).catalogId)||null};})};})"
                % (json.dumps(path), extract))
        res = self.eval_async(expr)
        if not res.get("ok"):
            return None
        value = res.get("value") or {}
        if value.get("status") is not None and int(value["status"]) >= 400:
            return None
        for item in value.get("items") or []:
            if item.get("catalogId") and str(item["catalogId"]) == catalog_id:
                return item
        return None

    def search_catalog(self, term, types="songs", limit=10):
        """Catalog search using only the signed-in session - no developer token."""
        sf = self.storefront()
        path = ("/v1/catalog/%s/search?term=%s&types=%s&limit=%d"
                % (urllib.parse.quote(sf, safe=""), urllib.parse.quote(term, safe=""),
                   urllib.parse.quote(types, safe=""), limit))
        expr = ("MusicKit.getInstance().api.music(%s).then(function(r){"
                "var res=((r.data||{}).results)||{};var out={};"
                "Object.keys(res).forEach(function(k){out[k]=((res[k]||{}).data||[])"
                ".map(function(x){var a=x.attributes||{};return {id:x.id, type:x.type,"
                "name:a.name||null, artistName:a.artistName||null, albumName:a.albumName||null,"
                "durationInMillis:a.durationInMillis||null, releaseDate:a.releaseDate||null,"
                "genreNames:a.genreNames||[]};});});"
                "return {status:r.status, errors:r.errors||null, results:out};})" % json.dumps(path))
        res = self.eval_async(expr)
        if not res.get("ok"):
            err = res.get("error") or {}
            raise ToolError("Catalog search failed: %s" % (err.get("message") or "unknown"),
                            code="api_error")
        value = res.get("value") or {}
        if value.get("status") is not None and int(value["status"]) >= 400:
            detail = _first_error_detail(value.get("errors"))
            raise ToolError("Apple Music returned HTTP %s%s" % (value["status"], (": " + detail) if detail else ""),
                            code="http_%s" % value["status"])
        return value.get("results") or {}


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def token_path(explicit=None):
    return os.path.expanduser(explicit or os.environ.get("APPLE_MUSIC_TOKEN_FILE")
                              or DEFAULT_TOKEN_PATH)


def jwt_expiry(token):
    """Return the `exp` of a JWT as an aware datetime, or None if unreadable.

    The developer token is a signed JWT whose payload is plain base64 - reading
    the expiry needs no secret and no verification, because we are not deciding
    whether to trust it, only when to warn that it is about to die.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.datetime.fromtimestamp(int(claims["exp"]), datetime.timezone.utc)
    except Exception:
        return None


def load_tokens(explicit_path=None):
    """Find a token pair: environment first, then the token file.

    Environment wins so a caller can inject credentials for one invocation
    without them ever touching disk - which is how this runs under a secret
    broker.
    """
    dev = os.environ.get(ENV_DEV_TOKEN)
    user = os.environ.get(ENV_USER_TOKEN)
    if dev and user:
        return {"developer_token": dev, "music_user_token": user, "source": "environment"}

    path = token_path(explicit_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ToolError("Token file %s is unreadable: %s" % (path, exc),
                        remedy="Re-run `harvest` on a Mac with a signed-in Safari.",
                        code="bad_token_file", exit_code=EXIT_ENVIRONMENT)
    if not data.get("developer_token") or not data.get("music_user_token"):
        raise ToolError("Token file %s is missing a token." % path,
                        remedy="Re-run `harvest` on a Mac with a signed-in Safari.",
                        code="bad_token_file", exit_code=EXIT_ENVIRONMENT)
    data["source"] = path
    return data


def save_tokens(tokens, explicit_path=None):
    path = token_path(explicit_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
        os.chmod(directory, stat.S_IRWXU)
    # Create the file private and *then* write, so the tokens are never briefly
    # readable by anyone else.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle, indent=2)
        handle.write("\n")
    return path


def harvest_from_safari(session):
    """Read the credential pair out of the live signed-in page."""
    res = session.eval_async(
        "(function(){var m=MusicKit.getInstance();"
        "return {dev:m.developerToken||null, user:m.musicUserToken||null,"
        " storefront:m.storefrontId||null};})()")
    if not res.get("ok"):
        err = res.get("error") or {}
        raise ToolError("Could not read the tokens from the page: %s"
                        % (err.get("message") or "unknown"), code="harvest_failed")
    value = res.get("value") or {}
    if not value.get("dev") or not value.get("user"):
        raise ToolError("The page did not expose a token pair.",
                        remedy="Confirm the tab is signed in - run `doctor --safari`.",
                        code="harvest_empty", exit_code=EXIT_ENVIRONMENT)
    expiry = jwt_expiry(value["dev"])
    return {
        "developer_token": value["dev"],
        "music_user_token": value["user"],
        "storefront": value.get("storefront"),
        "harvested_at": datetime.datetime.now(datetime.timezone.utc)
                                .replace(microsecond=0).isoformat(),
        "developer_token_expires": expiry.replace(microsecond=0).isoformat() if expiry else None,
    }


# --------------------------------------------------------------------------
# http session - no browser
# --------------------------------------------------------------------------

class ApiSession:
    """The same surface as MusicSession, over plain HTTPS.

    Deliberately talks to the PUBLIC api.music.apple.com, not the `amp-api`
    host the web player uses. The amp-api quirks documented on MusicSession
    (`api.music` rejecting `ids[songs]`, `filter[catalog]` being refused) are
    properties of in-page MusicKit calls; a direct client is not bound by them.
    The one behaviour that does carry over is the empty success body - see
    `add_to_library`.
    """

    def __init__(self, tokens, timeout=30.0):
        self.tokens = tokens
        self.timeout = timeout
        self._storefront = tokens.get("storefront")

    def find_tab(self, refresh=False):
        """No-op: there is no tab. Present so commands can call it either way.

        Commands establish the session before working, which for Safari means
        locating an authorised tab. Over HTTPS there is nothing to locate - the
        credentials either work or `doctor` says why not.
        """
        return None

    # -- transport --------------------------------------------------------

    def _request(self, path, method="GET", timeout=None):
        """One request, retrying only what is worth retrying.

        Apple throttles bursts with 429 and reports it as "Request is
        forbidden", which reads like an auth failure and is not one - a caller
        that treats it as fatal concludes its credentials died. Back off and
        retry instead; anything still 429 after the last attempt is returned as
        itself so the caller sees the truth.
        """
        url = path if path.startswith("http") else API_BASE + path
        delay = 1.0
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            req = urllib.request.Request(url, method=method,
                                         data=b"" if method == "POST" else None)
            req.add_header("Authorization", "Bearer " + self.tokens["developer_token"])
            req.add_header("Music-User-Token", self.tokens["music_user_token"])
            req.add_header("Origin", API_ORIGIN)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == RATE_LIMIT_RETRIES:
                    return exc.code, exc.read()
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                time.sleep(min(wait, 30.0))
                delay *= 2
            except urllib.error.URLError as exc:
                raise ToolError("Could not reach Apple Music: %s" % exc.reason,
                                remedy="Check network connectivity.",
                                code="network", exit_code=EXIT_ENVIRONMENT)
        return 429, b""

    def _json(self, path, timeout=None):
        status, body = self._request(path, timeout=timeout)
        if status == 401 or status == 403:
            raise ToolError("Apple Music rejected the stored credentials (HTTP %d)." % status,
                            remedy="The tokens have expired or been revoked. Re-run "
                                   "`harvest` on a Mac with a signed-in Safari.",
                            code="unauthorized", exit_code=EXIT_ENVIRONMENT)
        payload = {}
        if body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {}
        if status >= 400:
            detail = _first_error_detail(payload.get("errors"))
            raise ToolError("Apple Music returned HTTP %d%s" % (status, (": " + detail) if detail else ""),
                            code="http_%d" % status)
        return payload

    # -- Apple Music API --------------------------------------------------

    def api_get(self, path, timeout=None):
        return self._json(path, timeout=timeout)

    def storefront(self):
        if self._storefront:
            return self._storefront
        data = self._json("/v1/me/storefront")
        items = data.get("data") or []
        if not items or not items[0].get("id"):
            raise ToolError("Could not determine your Apple Music storefront.",
                            remedy="Re-run `harvest`; the stored tokens may be stale.",
                            code="no_storefront", exit_code=EXIT_ENVIRONMENT)
        self._storefront = items[0]["id"]
        return self._storefront

    # -- library mutation -------------------------------------------------

    def add_to_library(self, catalog_id, kind="songs"):
        """POST the catalog id into the library.

        A successful add is HTTP 202 with a completely empty body. That is the
        same behaviour that, inside MusicKit, surfaces as a JSON parse failure -
        it is Apple's, not the browser's. Here it needs no special handling
        beyond not treating an empty body as a fault.
        """
        path = "/v1/me/library?ids[%s]=%s" % (kind, urllib.parse.quote(str(catalog_id), safe=""))
        status, body = self._request(path, method="POST")
        if status in (401, 403):
            raise ToolError("Apple Music rejected the stored credentials (HTTP %d)." % status,
                            remedy="Re-run `harvest` on a Mac with a signed-in Safari.",
                            code="unauthorized", exit_code=EXIT_ENVIRONMENT)
        if 200 <= status < 300:
            return {"accepted": True, "detail": "HTTP %d%s" % (
                status, " (empty body, expected on success)" if not body else "")}
        detail = ""
        if body:
            try:
                detail = _first_error_detail(json.loads(body).get("errors"))
            except ValueError:
                detail = body[:120].decode("utf-8", "replace")
        return {"accepted": False, "detail": "HTTP %d%s" % (status, (": " + detail) if detail else "")}

    # -- lookups ----------------------------------------------------------

    def catalog_songs(self, catalog_ids):
        sf = self.storefront()
        ids = ",".join(urllib.parse.quote(str(i), safe="") for i in catalog_ids)
        try:
            data = self._json("/v1/catalog/%s/songs?ids=%s" % (urllib.parse.quote(sf, safe=""), ids))
        except ToolError:
            return {}
        out = {}
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            if item.get("id"):
                out[item["id"]] = {
                    "id": item["id"],
                    "name": attrs.get("name"),
                    "artistName": attrs.get("artistName"),
                    "albumName": attrs.get("albumName"),
                    "durationInMillis": attrs.get("durationInMillis"),
                }
        return out

    def find_library_song(self, catalog_id, title=None):
        """Return the library song (`i.xxxx`) for a catalog id, or None.

        Same two-pass strategy as the Safari path, and for the same reason: a
        title search misses a track added seconds ago, and the recently-added
        scan misses anything older than the window.
        """
        catalog_id = str(catalog_id)
        if title:
            hit = self._match_in(
                "/v1/me/library/search?term=%s&types=library-songs&limit=25"
                % urllib.parse.quote(title, safe=""),
                catalog_id, results_key="library-songs")
            if hit:
                return hit
        return self._match_in("/v1/me/library/songs?limit=50&sort=-dateAdded", catalog_id)

    def _match_in(self, path, catalog_id, results_key=None):
        try:
            data = self._json(path)
        except ToolError:
            return None
        if results_key:
            items = ((data.get("results") or {}).get(results_key) or {}).get("data") or []
        else:
            items = data.get("data") or []
        for item in items:
            attrs = item.get("attributes") or {}
            found = (attrs.get("playParams") or {}).get("catalogId")
            if found and str(found) == catalog_id:
                return {"id": item.get("id"), "name": attrs.get("name"),
                        "artistName": attrs.get("artistName"), "catalogId": str(found)}
        return None

    def search_catalog(self, term, types="songs", limit=10):
        sf = self.storefront()
        data = self._json("/v1/catalog/%s/search?term=%s&types=%s&limit=%d"
                          % (urllib.parse.quote(sf, safe=""), urllib.parse.quote(term, safe=""),
                             urllib.parse.quote(types, safe=""), limit))
        out = {}
        for key, block in (data.get("results") or {}).items():
            out[key] = []
            for item in block.get("data") or []:
                attrs = item.get("attributes") or {}
                out[key].append({
                    "id": item.get("id"), "type": item.get("type"),
                    "name": attrs.get("name"), "artistName": attrs.get("artistName"),
                    "albumName": attrs.get("albumName"),
                    "durationInMillis": attrs.get("durationInMillis"),
                    "releaseDate": attrs.get("releaseDate"),
                    "genreNames": attrs.get("genreNames") or [],
                })
        return out


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _is_empty_body_error(message):
    low = (message or "").lower()
    return any(marker in low for marker in _EMPTY_BODY_ERRORS)


def _first_error_detail(errors):
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            return first.get("detail") or first.get("title") or ""
    return ""


_ID_RE = re.compile(r"^\d{4,}$")


def parse_catalog_ids(raw_values):
    """Accept ids, comma/space separated lists, and music.apple.com URLs.

    A song URL looks like .../album/<slug>/<album-id>?i=<song-id>, so the song
    id is the `i` parameter when present and the last path segment otherwise.
    """
    ids, bad = [], []
    for raw in raw_values:
        for token in re.split(r"[,\s]+", raw.strip()):
            if not token:
                continue
            candidate = token
            if "music.apple.com" in token or token.startswith("http"):
                parsed = urllib.parse.urlparse(token)
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("i"):
                    candidate = query["i"][0]
                else:
                    segments = [s for s in parsed.path.split("/") if s]
                    candidate = segments[-1] if segments else token
            if _ID_RE.match(candidate):
                if candidate not in ids:
                    ids.append(candidate)
            else:
                bad.append(token)
    return ids, bad


def _fmt_duration(ms):
    if not ms:
        return ""
    seconds = int(ms) // 1000
    return "%d:%02d" % (seconds // 60, seconds % 60)


def _emit(payload, as_json, text_lines):
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for line in text_lines:
            print(line)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_doctor(session, args):
    """Check whichever transport is actually in use.

    The two modes fail in completely different ways - a missing Develop-menu
    switch means nothing to an HTTPS client, and an expired token means nothing
    to a live page - so they get separate check lists rather than one list with
    half its entries skipped.
    """
    if isinstance(session, ApiSession):
        return _doctor_http(session, args)
    return _doctor_safari(session, args)


def _doctor_http(session, args):
    checks = []

    def record(name, ok, detail, remedy=None):
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "remedy": None if ok else remedy})
        return ok

    record("tokens_present", True, "loaded from %s" % session.tokens.get("source", "?"), None)

    expiry = jwt_expiry(session.tokens["developer_token"])
    if expiry is None:
        record("token_expiry", False, "developer token is not a readable JWT",
               "Re-run `harvest`; the stored value looks corrupt.")
    else:
        left = expiry - datetime.datetime.now(datetime.timezone.utc)
        days = left.days
        if days < 0:
            record("token_expiry", False,
                   "developer token EXPIRED %s (%d days ago)" % (expiry.date(), -days),
                   "Re-run `harvest` on a Mac with a signed-in Safari.")
        elif days < TOKEN_EXPIRY_WARN_DAYS:
            record("token_expiry", False,
                   "developer token expires %s - only %d days left" % (expiry.date(), days),
                   "Re-run `harvest` soon; when it expires every call returns 401.")
        else:
            record("token_expiry", True,
                   "developer token valid until %s (%d days)" % (expiry.date(), days), None)

    try:
        storefront = session.storefront()
        record("api_reachable", True,
               "GET /v1/me/storefront succeeded (storefront: %s)" % storefront, None)
    except ToolError as exc:
        record("api_reachable", False, exc.message, exc.remedy)
        return _doctor_report(checks, args)

    try:
        session.api_get("/v1/me/library/songs?limit=1")
        record("library_scope", True, "user-scoped read succeeded", None)
    except ToolError as exc:
        record("library_scope", False, exc.message, exc.remedy)

    return _doctor_report(checks, args)


def _doctor_safari(session, args):
    checks = []

    def record(name, ok, detail, remedy=None):
        # A remedy is only meaningful for a check that failed.
        checks.append({"check": name, "ok": bool(ok), "detail": detail,
                       "remedy": None if ok else remedy})
        return ok

    if not record("macos", sys.platform == "darwin",
                  "platform is %s" % sys.platform,
                  "This tool requires macOS; there is no headless or Linux equivalent."):
        return _doctor_report(checks, args)

    running = _safari_running()
    if not record("safari_running", running,
                  "Safari process found" if running else "no Safari process",
                  "Open Safari."):
        return _doctor_report(checks, args)

    try:
        tabs = session.list_music_tabs()
    except ToolError as exc:
        record("music_tab", False, exc.message, exc.remedy)
        return _doctor_report(checks, args)
    if not record("music_tab", tabs, "%d music.apple.com tab(s) open" % len(tabs),
                  "Open https://music.apple.com in Safari and sign in."):
        return _doctor_report(checks, args)

    # Probing any music tab exercises the Develop-menu switch.
    session._tab = (tabs[0][0], tabs[0][1])
    try:
        session._eval_sync("'ok'")
        record("js_from_apple_events", True, "Safari executes JavaScript from Apple Events", None)
    except ToolError as exc:
        record("js_from_apple_events", False, exc.message, exc.remedy)
        return _doctor_report(checks, args)

    try:
        session.find_tab(refresh=True)
        record("musickit_authorized", True,
               "MusicKit.getInstance().isAuthorized is true "
               "(window %d, tab %d)" % session._tab, None)
    except ToolError as exc:
        record("musickit_authorized", False, exc.message, exc.remedy)
        return _doctor_report(checks, args)

    try:
        storefront = session.storefront()
        record("storefront_probe", True,
               "GET /v1/me/storefront succeeded (storefront: %s)" % storefront, None)
    except ToolError as exc:
        record("storefront_probe", False, exc.message, exc.remedy)

    return _doctor_report(checks, args)


def _doctor_report(checks, args):
    ok = all(c["ok"] for c in checks)
    payload = {"ok": ok, "command": "doctor", "checks": checks}
    lines = []
    for check in checks:
        lines.append("%s %-22s %s" % ("PASS" if check["ok"] else "FAIL", check["check"], check["detail"]))
        if not check["ok"] and check["remedy"]:
            lines.append("     -> %s" % check["remedy"])
    lines.append("")
    lines.append("doctor: %s" % ("all checks passed" if ok else "one or more checks failed"))
    _emit(payload, args.json, lines)
    return EXIT_OK if ok else EXIT_ENVIRONMENT


def cmd_harvest(session, args):
    """Read the token pair out of the live page and store it.

    This is the only command that requires Safari. Everything else can run off
    what this writes, on any machine.
    """
    session.find_tab()
    tokens = harvest_from_safari(session)
    if args.stdout:
        # For a caller that wants to put these somewhere else entirely - a
        # secret manager, say - without them ever landing on disk here.
        print(json.dumps(tokens, indent=2))
        return EXIT_OK

    path = save_tokens(tokens, args.token_file)
    expires = tokens.get("developer_token_expires")
    lines = ["wrote token pair to %s (mode 0600)" % path,
             "storefront: %s" % (tokens.get("storefront") or "unknown")]
    if expires:
        left = jwt_expiry(tokens["developer_token"]) - datetime.datetime.now(datetime.timezone.utc)
        lines.append("developer token expires %s (%d days) - re-run `harvest` before then"
                     % (expires[:10], left.days))
    _emit({"ok": True, "command": "harvest", "path": path,
           "storefront": tokens.get("storefront"),
           "developer_token_expires": expires}, args.json, lines)
    return EXIT_OK


def cmd_storefront(session, args):
    storefront = session.storefront()
    _emit({"ok": True, "command": "storefront", "storefront": storefront},
          args.json, [storefront])
    return EXIT_OK


def cmd_search(session, args):
    results = session.search_catalog(args.query, types=args.types, limit=args.limit)
    payload = {"ok": True, "command": "search", "query": args.query,
               "storefront": session.storefront(), "results": results}
    lines = []
    for kind, items in results.items():
        if not items:
            continue
        lines.append("%s:" % kind)
        for item in items:
            bits = [item.get("artistName") or "", item.get("albumName") or ""]
            meta = " - ".join(b for b in bits if b)
            duration = _fmt_duration(item.get("durationInMillis"))
            lines.append("  %-12s %s%s%s" % (item.get("id") or "?", item.get("name") or "?",
                                             ("  [%s]" % meta) if meta else "",
                                             ("  (%s)" % duration) if duration else ""))
    if not lines:
        lines = ["no results"]
    _emit(payload, args.json, lines)
    return EXIT_OK


def cmd_resolve(session, args):
    catalog_ids, bad = parse_catalog_ids([args.catalog_id])
    if bad or not catalog_ids:
        raise ToolError("Not a usable catalog id: %s" % args.catalog_id,
                        remedy="Pass a numeric catalog id, or a music.apple.com song URL.",
                        code="bad_id", exit_code=EXIT_USAGE)
    catalog_id = catalog_ids[0]
    meta = session.catalog_songs([catalog_id]).get(catalog_id) or {}
    hit = session.find_library_song(catalog_id, title=meta.get("name"))
    payload = {"ok": bool(hit), "command": "resolve-library-id", "catalog_id": catalog_id,
               "library_id": (hit or {}).get("id"), "name": (hit or {}).get("name") or meta.get("name"),
               "artist_name": (hit or {}).get("artistName") or meta.get("artistName"),
               "in_library": bool(hit)}
    if hit:
        lines = [hit["id"]]
    else:
        lines = ["not in library: catalog id %s has no matching library song" % catalog_id,
                 "  -> run `add %s` first; playlists need the library id, not the catalog id"
                 % catalog_id]
    _emit(payload, args.json, lines)
    return EXIT_OK if hit else EXIT_FAILED


def cmd_add(session, args):
    catalog_ids, bad = parse_catalog_ids(args.ids)
    if not catalog_ids and not bad:
        raise ToolError("No catalog ids given.", code="bad_id", exit_code=EXIT_USAGE)

    session.find_tab()
    metadata = session.catalog_songs(catalog_ids) if catalog_ids else {}

    results = []
    for token in bad:
        results.append({"input": token, "catalog_id": None, "status": "invalid",
                        "library_id": None, "name": None,
                        "detail": "not a numeric catalog id or a recognisable Apple Music URL"})

    for catalog_id in catalog_ids:
        meta = metadata.get(catalog_id)
        entry = {"input": catalog_id, "catalog_id": catalog_id, "status": "failed",
                 "library_id": None, "name": (meta or {}).get("name"),
                 "artist_name": (meta or {}).get("artistName"), "detail": None}
        try:
            if meta is None:
                entry["status"] = "not_found"
                entry["detail"] = "no such song in the %s catalog" % session.storefront()
                results.append(entry)
                continue

            existing = session.find_library_song(catalog_id, title=meta.get("name"))
            if existing:
                entry.update(status="already_in_library", library_id=existing["id"],
                             detail="already present; not re-added")
                results.append(entry)
                continue

            outcome = session.add_to_library(catalog_id)
            if not outcome["accepted"]:
                entry["detail"] = outcome["detail"]
                results.append(entry)
                continue

            if args.no_verify:
                entry.update(status="added_unverified", detail=outcome["detail"])
                results.append(entry)
                continue

            # Verify rather than trust: the add "succeeds" by throwing, and the
            # library index needs a moment to catch up.
            found = None
            for delay in (0.0, 1.0, 2.0, 3.0, 4.0):
                if delay:
                    time.sleep(delay)
                found = session.find_library_song(catalog_id, title=meta.get("name"))
                if found:
                    break
            if found:
                entry.update(status="added", library_id=found["id"], detail=outcome["detail"])
            else:
                entry.update(status="unverified",
                             detail=("Apple accepted the request but the track has not appeared in "
                                     "the library yet; re-check with `resolve-library-id %s`"
                                     % catalog_id))
        except ToolError as exc:
            # One bad id must never abort the batch.
            entry["detail"] = exc.message
        results.append(entry)

    added = sum(1 for r in results if r["status"] == "added")
    ok = all(r["status"] in ("added", "already_in_library", "added_unverified") for r in results) and bool(results)
    payload = {"ok": ok, "command": "add", "added": added, "total": len(results), "results": results}

    lines = []
    for r in results:
        label = {"added": "ADDED", "already_in_library": "PRESENT", "added_unverified": "SENT",
                 "unverified": "UNVERIFIED", "not_found": "NOT-FOUND", "invalid": "INVALID"}.get(
                     r["status"], "FAILED")
        name = r.get("name") or r.get("input")
        artist = (" - %s" % r["artist_name"]) if r.get("artist_name") else ""
        lines.append("%-10s %-12s %s%s" % (label, r.get("catalog_id") or r["input"], name, artist))
        if r.get("library_id"):
            lines.append("           library id: %s" % r["library_id"])
        if r.get("detail") and r["status"] not in ("added", "already_in_library"):
            lines.append("           %s" % r["detail"])
    lines.append("")
    lines.append("%d added, %d of %d ok" % (
        added, sum(1 for r in results if r["status"] in ("added", "already_in_library", "added_unverified")),
        len(results)))
    _emit(payload, args.json, lines)
    return EXIT_OK if ok else EXIT_FAILED


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _add_common_flags(parser, top_level):
    """Add --json/--timeout so they work before *and* after the subcommand.

    Subcommand copies default to SUPPRESS, so that `--json add ...` is not
    silently undone by the subparser writing its own default over the value.
    Each parser gets its own action objects on purpose: `parents=[...]` shares
    action *instances*, and `set_defaults()` mutates `action.default` in place,
    which would push the top-level default back onto every subparser and
    reintroduce exactly the bug this avoids.
    """
    json_default = False if top_level else argparse.SUPPRESS
    timeout_default = 30.0 if top_level else argparse.SUPPRESS
    suppress = argparse.SUPPRESS
    parser.add_argument("--json", action="store_true", default=json_default,
                        help="emit machine-readable JSON")
    parser.add_argument("--timeout", type=float, default=timeout_default,
                        help="seconds to wait for each Apple Music call (default: 30)")
    parser.add_argument("--token-file", default=(None if top_level else suppress),
                        help="token pair to use (default: %s)" % DEFAULT_TOKEN_PATH)
    parser.add_argument("--safari", action="store_true", default=(False if top_level else suppress),
                        help="force the browser transport even if a token pair is stored")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="applemusic-library",
        description="Add Apple Music catalog tracks to your library via a signed-in Safari tab.")
    _add_common_flags(parser, top_level=True)
    parser.add_argument("--version", action="version", version="applemusic-library " + VERSION)
    sub = parser.add_subparsers(dest="command")

    def add_sub(name, help_text):
        child = sub.add_parser(name, help=help_text)
        _add_common_flags(child, top_level=False)
        return child

    add_sub("doctor", "check that the current transport is usable")
    add_sub("storefront", "print the signed-in storefront code")

    p_harvest = add_sub("harvest", "store the token pair so later runs need no browser")
    p_harvest.add_argument("--stdout", action="store_true",
                           help="print the tokens instead of writing them (for a secret manager)")

    p_search = add_sub("search", "search the Apple Music catalog (no developer token)")
    p_search.add_argument("query", help="search terms")
    p_search.add_argument("--limit", type=int, default=10, help="results per type (default: 10)")
    p_search.add_argument("--types", default="songs",
                          help="comma-separated: songs,albums,artists,playlists (default: songs)")

    p_add = add_sub("add", "add catalog songs to the library")
    p_add.add_argument("ids", nargs="+",
                       help="catalog ids (comma or space separated) or music.apple.com song URLs")
    p_add.add_argument("--no-verify", action="store_true",
                       help="skip the post-add library check (faster, less honest)")

    p_resolve = add_sub("resolve-library-id", "print the library id (i.xxxx) for a catalog id")
    p_resolve.add_argument("catalog_id", help="catalog id or music.apple.com song URL")

    return parser


def resolve_session(args):
    """Choose the transport.

    Prefer HTTPS whenever a token pair is available: it is faster, needs no
    window on anyone's screen, and works off this machine. Fall back to Safari
    only when there is nothing stored - and say so plainly on a host where
    Safari is not an option, rather than failing later with something obscure.
    """
    if args.command == "harvest":
        # Harvesting is precisely the act of reading the live page.
        return MusicSession(timeout=args.timeout)

    if not args.safari:
        tokens = load_tokens(args.token_file)
        if tokens:
            return ApiSession(tokens, timeout=args.timeout)

    if sys.platform != "darwin":
        raise ToolError("No stored token pair, and the browser transport needs macOS.",
                        remedy="Run `harvest` on a Mac with a signed-in Safari, then "
                               "copy %s here (or set %s and %s)."
                               % (DEFAULT_TOKEN_PATH, ENV_DEV_TOKEN, ENV_USER_TOKEN),
                        code="no_transport", exit_code=EXIT_ENVIRONMENT)
    return MusicSession(timeout=args.timeout)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    handlers = {"doctor": cmd_doctor, "storefront": cmd_storefront, "search": cmd_search,
                "add": cmd_add, "resolve-library-id": cmd_resolve, "harvest": cmd_harvest}
    try:
        session = resolve_session(args)
        return handlers[args.command](session, args)
    except ToolError as exc:
        if args.json:
            print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False))
        else:
            print("error: %s" % exc.message, file=sys.stderr)
            if exc.remedy:
                print("  -> %s" % exc.remedy, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
