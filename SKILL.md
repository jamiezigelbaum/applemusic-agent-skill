---
name: apple-music-library
description: Search the Apple Music *catalog* and add catalog tracks to the user's library, and resolve catalog IDs to library IDs. Use when the user asks to add, save, or queue a song that is not already in their library, when AppleScript reports a track cannot be found, or when building a playlist from catalog search results. Borrows an already signed-in Safari session on macOS - no developer token, no Music User Token, no Apple Developer Program membership - and can then run headless anywhere, including Linux.
---

# Apple Music library additions

## Two modes - read this first

| Mode | Transport | Runs on | Needs |
| --- | --- | --- | --- |
| `safari` | Apple Events → a signed-in Safari tab | macOS only | Safari open, signed in |
| `http` | plain HTTPS → `api.music.apple.com` | anywhere, headless | a stored token pair |

The signed-in page will hand you the credential pair it is using
(`MusicKit.getInstance().developerToken` and `.musicUserToken`), and **that pair
works from an ordinary HTTPS client with no browser at all**.

```bash
python3 scripts/applemusic_library.py harvest   # once, on the Mac
```

After that every command picks `http` automatically and no window opens on
anyone's screen. Force the old path with `--safari`.

**The developer token expires after ~70 days.** That is a cliff, not a
degradation - when it lapses, every call returns 401. `doctor` warns 14 days
ahead; re-run `harvest` to refresh.

Token pair lookup order: `APPLE_MUSIC_DEVELOPER_TOKEN` + `APPLE_MUSIC_USER_TOKEN`
in the environment, then `--token-file`, then `~/.config/applemusic-agent/tokens.json`
(written 0600). `harvest --stdout` prints the pair instead of writing it, for
handing straight to a secret manager.

### The one thing that will waste your afternoon

Direct HTTPS calls **must** send `Origin: https://music.apple.com`. The developer
token carries a `root_https_origin` claim and Apple enforces it: without that
header every call returns 401, including ones needing no user scope.

## What this does

AppleScript can only see the **local library**. Apple Music **catalog** tracks -
anything the user has not already saved - are not scriptable at all. So the usual
`tell application "Music" to play track "..."` fails for any song the user has not
added yet, and there is no AppleScript verb that adds one.

This skill adds catalog tracks to the library by driving a Safari tab that is
already signed in to music.apple.com. That page holds an authorised MusicKit
session, so it can make user-scoped Apple Music API calls on its own behalf. We
send it JavaScript over Apple Events and read the answers back.

The point: **no credentials are needed.** No developer token, no Music User
Token, no Apple Developer Program membership. The user's existing Safari sign-in
is the whole authentication story.

## Setup (one time)

1. **Safari → Settings → Advanced →** tick **"Show features for web developers"**.
2. **Safari → Develop →** tick **"Allow JavaScript from Apple Events"**.
   Without this, every call silently returns an empty string - no error, no clue.
3. Open **https://music.apple.com** in Safari and **sign in** with an account that
   has an active Apple Music subscription. Leave the tab open.
4. The first run triggers a macOS Automation permission prompt - approve it.
   (System Settings → Privacy & Security → Automation → *your terminal app* → Safari.)

Then confirm:

```
python3 scripts/applemusic_library.py doctor
```

Every check must pass before anything else will work. Each failure names its own
fix.

## Commands

All commands accept `--json` (before or after the subcommand) and `--timeout N`.

```bash
# Check the session is usable. Run this first when anything misbehaves.
python3 scripts/applemusic_library.py doctor

# Search the catalog. Needs no token - the signed-in page does the call.
python3 scripts/applemusic_library.py search "artist or title" --limit 5
python3 scripts/applemusic_library.py search "some album" --types songs,albums

# Add catalog songs to the library. Accepts ids, comma/space lists, and URLs.
python3 scripts/applemusic_library.py add 1234567890
python3 scripts/applemusic_library.py add 1234567890,1234567891
python3 scripts/applemusic_library.py add "https://music.apple.com/us/album/<slug>/<album-id>?i=1234567890"

# Get the library id for a catalog id (playlists need this, not the catalog id).
python3 scripts/applemusic_library.py resolve-library-id 1234567890

# Print the signed-in storefront code (discovered at runtime, never hardcoded).
python3 scripts/applemusic_library.py storefront
```

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 1 | ran, but the operation did not fully succeed (some ids failed) |
| 2 | bad arguments |
| 3 | the Safari session is unusable - run `doctor` |

### `add` result statuses

| Status | Meaning |
| --- | --- |
| `added` | added **and** confirmed present in the library |
| `already_in_library` | was already there; not re-added |
| `added_unverified` | sent with `--no-verify`; not confirmed |
| `unverified` | Apple accepted it but it has not appeared yet - re-check later |
| `not_found` | no such song in this storefront |
| `invalid` | not a catalog id or a recognisable Apple Music URL |
| `failed` | the request was rejected; see `detail` |

## The library-first workflow (important)

**Catalog IDs cannot go into playlists.** A playlist only accepts **library IDs**.
The two are different namespaces:

- catalog ID - all digits, e.g. `1234567890`
- library ID - starts with `i.`, e.g. `i.AbCdEfGhIjK`

So adding a catalog track to a playlist is always **two steps**:

```bash
# 1. add to the library (this is what mints the library id)
python3 scripts/applemusic_library.py add 1234567890 --json
#    -> {"status": "added", "library_id": "i.AbCdEfGhIjK", ...}

# 2. use the library id from step 1 - now AppleScript and playlists can see it
python3 scripts/applemusic_library.py resolve-library-id 1234567890
```

Once a track is in the library, ordinary AppleScript control of the Music app
works on it normally.

## Rules for agents

1. **Run `doctor` first** if any call fails. It distinguishes "not signed in" from
   "Develop menu switch off" from "no tab open" - these need completely different
   fixes and are easy to confuse.
2. **Never hardcode a storefront.** It is discovered at runtime from
   `/v1/me/storefront`. Storefronts differ per account and catalog IDs are
   storefront-specific.
3. **Trust `status`, not the absence of an error.** An add "succeeds" by throwing
   (see below), so the tool verifies independently. Report `unverified` honestly
   rather than claiming success.
4. **Do not add tracks the user did not ask for.** This writes to a real library.
5. **Adding is idempotent by check** - the tool looks first and reports
   `already_in_library` rather than adding twice.
6. **One bad ID must not stop a batch.** The tool already guarantees this; keep it
   that way.
7. **The user must stay signed in.** There is no way to re-authenticate headlessly.

## Two quirks a maintainer must not "fix"

These look like bugs. They are not. Changing them breaks the tool.

**Both are properties of the in-page MusicKit path.** In `http` mode the empty
success body arrives as a plain HTTP 202 needing no special handling, and the
public API accepts the shapes amp-api rejects. Do not "simplify" the Safari path
to match what works over HTTPS - they are different endpoints.

### 1. Success arrives as a thrown JSON parse error

A successful `POST /v1/me/library` returns **HTTP 202 with a completely empty
body**. MusicKit calls `JSON.parse("")` on that and the promise **rejects** with
`JSON Parse error: Unexpected EOF`.

So the empty-body parse failure **means the add worked**. The tool matches that
specific message and treats it as success - then verifies the track really landed
rather than trusting it. Do not "repair" this by treating all rejections as
failures; every add would report failure while actually working.

### 2. The page talks to `amp-api`, not the public API

The web player routes through **`amp-api.music.apple.com`**, Apple's internal
endpoint - **not** the public `api.music.apple.com`. Parameter handling differs,
and the documented public-API shapes are sometimes wrong here:

- `api.post('/v1/me/library?ids[songs]=<ID>')` — **works**.
- `api.music('/v1/me/library?ids[songs]=<ID>')` — returns
  `400 Unsupported type specified: songs`.
- `filter[catalog]=<ID>` is rejected as *"No filters supplied on the request"* -
  literal brackets, percent-encoded brackets, and a params object all fail. That
  is why catalog→library resolution matches on `playParams.catalogId` instead.

Do not rewrite these calls to match Apple's public documentation. The
documentation describes a different endpoint.

## How it works under the hood

- **Tab discovery** scans every window and tab for a music.apple.com URL and picks
  one whose `MusicKit.getInstance().isAuthorized` is `true`. It does not assume
  the frontmost window.
- **JS transport is base64.** `do JavaScript "..."` takes an AppleScript *string
  literal*, so an unescaped quote or backslash raises `-2741 syntax error`. Rather
  than escape carefully, the tool base64-encodes the JavaScript, so the
  AppleScript literal only ever contains `A-Za-z0-9+/=`. If you hand-write JS
  here, build it with single quotes and escape `\` then `"` - in that order.
- **Async needs dispatch-then-poll.** `do JavaScript` returns as soon as the
  statement evaluates, so a promise's value is never its return value. The tool
  stashes the settled result on a unique `window.__aml_<uuid>` slot, polls for it,
  then deletes the slot.

## Limitations

- **macOS + Safari only.** There is no headless or Linux path. The whole method is
  "borrow a signed-in browser session".
- **Safari must be running with a signed-in music.apple.com tab.** If the session
  logs out, everything fails until a human signs in again.
- **`amp-api` is undocumented and Apple-internal.** Apple can change it without
  notice. `doctor` plus verified adds are how you find out.
- **Requires an active Apple Music subscription.**
- **Writes to a real library.** There is no dry-run for `add`.
