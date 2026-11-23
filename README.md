# applemusic-library

Search the Apple Music **catalog** and add tracks to your library from the
command line — with **no Apple Developer Program membership, no developer token,
and no Music User Token**.

Borrow a signed-in Safari session once, and after that it runs anywhere: your
Mac, a Linux box, a container, a cron job. No browser, no window on your screen.

```bash
applemusic-library harvest                  # once, on a Mac with Safari signed in
applemusic-library search "burial untrue"   # …then anywhere
applemusic-library add 1234567890
```

- Python 3.8+, **standard library only** — no dependencies, ever
- Structured `--json` on every command, and honest exit codes
- Written to be driven by scripts and AI agents (see [Using it from an agent](#using-it-from-an-agent))

---

## Why this exists

Scripting Apple Music on macOS means AppleScript, and AppleScript can only see
your **local library**. Anything in the Apple Music *catalog* that you have not
already saved is invisible to it — there is no AppleScript verb that adds a
catalog track:

```applescript
tell application "Music" to play track "Some Song"
-- fails for anything you have not already added
```

The documented alternative is the Apple Music API, which wants a **developer
token** (a paid Apple Developer Program membership and a MusicKit key) and a
**Music User Token** (issued only through a MusicKit JS browser sign-in).

That browser sign-in is broken for many accounts:
`MusicKit.getInstance().authorize()` returns a bare
`AUTHORIZATION_ERROR: Unauthorized` *after* a successful sign-in and consent,
and there are unresolved Apple Developer Forum threads describing exactly this.

**You do not need either credential.** A Safari tab already signed in to
music.apple.com holds an authorised MusicKit session, and it will tell you the
token pair it is using. That pair works from any ordinary HTTPS client.

## How it works

Two transports, same commands:

| Mode | Talks to | Runs on | Needs |
| --- | --- | --- | --- |
| `http` *(default)* | `api.music.apple.com` over HTTPS | anywhere, headless | a harvested token pair |
| `safari` | a signed-in Safari tab, over Apple Events | macOS only | Safari open and signed in |

`harvest` reads `MusicKit.getInstance().developerToken` and `.musicUserToken`
out of the live page and stores them. From then on every command uses plain
HTTPS. The browser is needed again only when the developer token expires —
about every 70 days.

### The one thing that will waste your afternoon

Direct calls **must** send an `Origin: https://music.apple.com` header. The
developer token carries a `root_https_origin` claim and Apple enforces it:
without that header, *every* request returns `401`, including ones that need no
user scope at all. This tool always sends it; if you are reimplementing this
idea yourself, that is the detail to get right first.

---

## Install

Requires Python 3.8+. `harvest` additionally requires macOS with Safari.

```bash
git clone https://github.com/jamiezigelbaum/applemusic-agent-skill
cd applemusic-agent-skill
chmod +x scripts/applemusic_library.py
ln -s "$PWD/scripts/applemusic_library.py" ~/.local/bin/applemusic-library
```

### One-time setup on the Mac

1. **Safari → Settings → Advanced →** tick **“Show features for web developers”**.
2. **Safari → Develop →** tick **“Allow JavaScript from Apple Events”**.
   Without this, every call silently returns nothing — no error, no clue.
3. Open **https://music.apple.com** in Safari and sign in. Leave the tab open.
4. Run `applemusic-library harvest` and approve the macOS Automation prompt.

```console
$ applemusic-library harvest
wrote token pair to /Users/you/.config/applemusic-agent/tokens.json (mode 0600)
storefront: us
developer token expires 2026-09-29 (62 days) - re-run `harvest` before then
```

Then confirm — anywhere:

```console
$ applemusic-library doctor
PASS tokens_present         loaded from /Users/you/.config/applemusic-agent/tokens.json
PASS token_expiry           developer token valid until 2026-09-29 (62 days)
PASS api_reachable          GET /v1/me/storefront succeeded (storefront: us)
PASS library_scope          user-scoped read succeeded

doctor: all checks passed
```

Every failing check prints the exact fix. Run `doctor` first whenever anything
misbehaves — it distinguishes “not signed in” from “expired token” from
“Develop-menu switch off”, which need completely different responses.

## Commands

```bash
applemusic-library doctor                    # check the current transport
applemusic-library harvest                   # store the token pair (macOS only)
applemusic-library push                      # resend the stored pair to push_command
applemusic-library search "artist or title" --limit 5 --types songs,albums
applemusic-library add 1234567890            # ids, comma/space lists, or URLs
applemusic-library resolve-library-id 1234567890
applemusic-library storefront
```

Common flags, accepted before or after the subcommand: `--json`, `--timeout N`,
`--token-file PATH`, `--safari` (force the browser transport).

```console
$ applemusic-library add 1234567890
ADDED      1234567890    Archangel - Burial
           library id: i.AbCdEfGhIjK

1 added, 1 of 1 ok
```

Bad IDs never abort a batch:

```console
$ applemusic-library add 1234567890,99999999999,notanid
INVALID    notanid      notanid
           not a numeric catalog id or a recognisable Apple Music URL
PRESENT    1234567890    Archangel - Burial
           library id: i.AbCdEfGhIjK
NOT-FOUND  99999999999  99999999999
           no such song in the us catalog

0 added, 1 of 3 ok
```

### Playlists need library IDs, not catalog IDs

They are different namespaces, and this trips up everyone once:

- catalog ID — all digits, `1234567890`
- library ID — starts with `i.`, `i.AbCdEfGhIjK`

A playlist only accepts library IDs, and a library ID does not exist until the
track is in the library. So adding a catalog track to a playlist is always
library-first: `add` it, take `library_id` from the result, use that.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| `1` | ran, but the operation did not fully succeed (some ids failed) |
| `2` | bad arguments |
| `3` | the session is unusable — run `doctor` |

## Using it from an agent

`SKILL.md` in this repo is an agent-readable operating guide, in the format used
by Claude Code and compatible tools — drop the repo into a skills directory and
an agent can use it unaided.

Everything speaks `--json`, and `status` is the field to trust:

```console
$ applemusic-library add 1234567890 --json
{
  "ok": true,
  "command": "add",
  "added": 1,
  "total": 1,
  "results": [
    {
      "input": "1234567890",
      "catalog_id": "1234567890",
      "status": "added",
      "library_id": "i.AbCdEfGhIjK",
      "name": "Archangel",
      "artist_name": "Burial",
      "detail": "HTTP 202 (empty body, expected on success)"
    }
  ]
}
```

`status` is one of `added`, `already_in_library`, `added_unverified`,
`unverified`, `not_found`, `invalid`, `failed`. An add is verified independently
before it is reported as `added` — the tool would rather tell you `unverified`
than claim a success it has not confirmed.

For unattended use, run `doctor --json` on a schedule and alert on
`token_expiry`: expiry is a cliff, not a slow degradation.

## Credentials and what they can do

Be deliberate about this — `harvest` writes two real credentials to disk.

- They are stored in `~/.config/applemusic-agent/tokens.json`, **mode 0600**,
  created private before anything is written to it.
- The **Music User Token grants full access to your Apple Music account** —
  library reads and writes, playlists, listening history. Treat the file exactly
  as you would an API key or a password.
- Prefer the environment for anything shared or automated:
  `APPLE_MUSIC_DEVELOPER_TOKEN` and `APPLE_MUSIC_USER_TOKEN` are read first, so a
  secret manager can inject them per-invocation and nothing ever lands on disk.
  `harvest --stdout` prints the pair instead of writing it, for exactly that.
- If the pair is used on another machine through a secret manager, set
  `push_command` in `~/.config/applemusic-agent/config.json` (an argv list,
  e.g. `{"push_command": ["~/bin/store-apple-music-tokens"]}`). After every
  successful `harvest`, the command gets the pair as JSON on stdin and must
  exit 0 once your store holds it; `push` resends the stored pair. Without
  this, a re-harvest refreshes only the local file, and a copy elsewhere keeps
  expiring.
- The developer token expires in ~70 days. The user token is longer-lived; if it
  is revoked or the account signs out, everything returns `401` until you
  `harvest` again.
- **Do not commit the token file**, and do not pass tokens as command-line
  arguments — argv is visible to every process on the box.

## Two behaviours that look like bugs

**A successful add returns nothing.** `POST /v1/me/library` answers HTTP 202
with a completely empty body. In `safari` mode MusicKit tries to `JSON.parse("")`
and the promise *rejects* with `JSON Parse error: Unexpected EOF` — so success
arrives as an error. That specific rejection means success; the tool treats it
as such and then verifies the track actually landed.

**The web player uses `amp-api.music.apple.com`,** Apple's internal endpoint,
not the public one, and its parameter handling differs: `api.music()` with
`ids[songs]=` returns `400 Unsupported type specified: songs` while `api.post()`
with the identical query string works, and `filter[catalog]=` is rejected
outright. Those quirks apply to `safari` mode only — the public API this tool
uses in `http` mode accepts the documented shapes.

## Limitations

- **`harvest` needs macOS with Safari.** Everything else runs anywhere. If you
  have no Mac at all, there is no path in — the whole method is borrowing a
  signed-in browser session.
- **Requires an active Apple Music subscription.**
- **Re-authentication needs a human** at a signed-in Safari, roughly every 70
  days. There is no headless refresh.
- **`add` writes to a real library** and has no dry-run.
- **Apple rate-limits bursts aggressively.** Sustained catalog lookups earn
  `429`s whose body reads `Request is forbidden` — which looks like an auth
  failure and is not. The tool retries with backoff, but pace your own loops:
  a heavy burst can leave an account throttled for fifteen minutes or more.
- **This is undocumented territory.** Apple can change any of it without notice.
  `doctor` and post-add verification are how you find out.
- **Automation permission is per-calling-app.** Running `harvest` from a
  different terminal or scheduler triggers a fresh macOS prompt, which needs a
  human once.

## Contributing

Issues and pull requests welcome. The tool has no dependencies and no build
step — clone it and run `scripts/applemusic_library.py`.

If you are fixing something, please keep `doctor` honest: its job is to tell a
user precisely which of several similar-looking failures they actually have.

## License

MIT — see [LICENSE](LICENSE).
