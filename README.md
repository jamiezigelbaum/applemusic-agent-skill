# applemusic-library

Add Apple Music **catalog** tracks to your library from the command line on
macOS, without a developer token, without a Music User Token, and without an
Apple Developer Program membership.

It works by driving a Safari tab you are already signed in to.

## The problem

Scripting Apple Music on macOS means AppleScript, and AppleScript can only see
your **local library**. Apple Music **catalog** tracks - anything you have not
already saved - are invisible to it. There is no AppleScript verb that adds a
catalog track, so:

```applescript
tell application "Music" to play track "Some Song"
-- fails for anything you have not already added
```

Adding a catalog track requires the Apple Music API, which wants two credentials:

1. a **developer token** - an ES256 JWT you can mint headlessly, but only with a
   paid Apple Developer Program membership and a MusicKit key; and
2. a **Music User Token** - which Apple issues *only* through a MusicKit JS
   browser sign-in.

## Why the normal browser flow fails

The MusicKit JS sign-in is widely broken.
`MusicKit.getInstance().authorize()` returns a bare
`AUTHORIZATION_ERROR: Unauthorized` **after** a successful sign-in and consent.

During development this was chased down exhaustively and every common
explanation was eliminated:

- the developer token was valid (catalog search worked with it);
- tried with the `origin` claim present **and** absent;
- `isSecureContext` was `true`;
- the subscription was active - the consent screen named the account;
- three browsers (Brave with shields down, Chrome, Safari);
- genuine HTTPS with a self-signed certificate;
- a MusicKit key correctly bound to a Media ID.

`configure()` succeeds, then `authorize()` fails anyway. There are unresolved
Apple Developer Forum threads describing exactly this, with no fix.

## What this does instead

**You do not need a Music User Token at all.**

A Safari tab already signed in to music.apple.com *is* an authorised MusicKit
session. The page can make user-scoped API calls on its own behalf - so instead
of extracting a credential, we ask the page to do the work:

```js
MusicKit.getInstance().isAuthorized                     // true
MusicKit.getInstance().api.music('/v1/me/storefront')   // works
MusicKit.getInstance().api.post('/v1/me/library?ids[songs]=<CATALOG_ID>')  // adds it
```

This tool sends that JavaScript into the tab over Apple Events and reads the
answers back. The token itself is never touched - it lives in an `HttpOnly`
cookie, absent from `document.cookie`, `localStorage`, and every enumerable
MusicKit property. We do not need it.

A useful consequence: **catalog search works too**, from the same session, with
no developer token. That removes the Apple Developer Program requirement
entirely.

## Install

Requires macOS and Python 3.8+. No third-party packages - it shells out to
`osascript`.

```bash
git clone <this-repo>
cd applemusic-skill
chmod +x scripts/applemusic_library.py
```

Optionally put it on your `PATH`:

```bash
ln -s "$PWD/scripts/applemusic_library.py" ~/.local/bin/applemusic-library
```

## Setup

1. **Safari → Settings → Advanced →** tick **"Show features for web developers"**.
2. **Safari → Develop →** tick **"Allow JavaScript from Apple Events"**.
   Without step 2, every call silently returns nothing - no error message.
3. Open **https://music.apple.com** in Safari and sign in. Leave the tab open.
4. Run any command once and approve the macOS Automation prompt.

Verify:

```console
$ applemusic-library doctor
PASS macos                  platform is darwin
PASS safari_running         Safari process found
PASS music_tab              1 music.apple.com tab(s) open
PASS js_from_apple_events   Safari executes JavaScript from Apple Events
PASS musickit_authorized    MusicKit.getInstance().isAuthorized is true (window 1, tab 1)
PASS storefront_probe       GET /v1/me/storefront succeeded (storefront: us)

doctor: all checks passed
```

Each failing check prints the exact fix.

## Usage

```console
$ applemusic-library search "some artist" --limit 3
songs:
  1234567890   Track One  [Some Artist - Some Album]  (3:29)
  1234567891   Track Two  [Some Artist - Some Album]  (3:27)

$ applemusic-library add 1234567890
ADDED      1234567890   Track One - Some Artist
           library id: i.AbCdEfGhIjK

1 added, 1 of 1 ok

$ applemusic-library resolve-library-id 1234567890
i.AbCdEfGhIjK
```

Everything supports `--json`:

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
      "name": "Track One",
      "artist_name": "Some Artist",
      "detail": "empty response body (expected on success)"
    }
  ]
}
```

Bad IDs never abort a batch:

```console
$ applemusic-library add 1234567890,99999999999,notanid
INVALID    notanid      notanid
           not a numeric catalog id or a recognisable Apple Music URL
ADDED      1234567890   Track One - Some Artist
           library id: i.AbCdEfGhIjK
NOT-FOUND  99999999999  99999999999
           no such song in the us catalog

1 added, 1 of 3 ok
```

### Playlists need library IDs

Catalog IDs (`1234567890`) **cannot** go into playlists. Only library IDs
(`i.AbCdEfGhIjK`) can. So the workflow is always library-first: `add` the catalog
track, take the `library_id` from the result, and use that.

## Two behaviours that look like bugs

**A successful add throws.** `POST /v1/me/library` returns HTTP 202 with an empty
body; MusicKit tries to `JSON.parse("")` and the promise rejects with
`JSON Parse error: Unexpected EOF`. That specific rejection **means success**.
The tool treats it as such and then independently verifies the track landed.

**The page uses `amp-api.music.apple.com`,** Apple's internal endpoint, not the
public `api.music.apple.com`. Parameter semantics differ - `api.music()` with
`ids[songs]=` returns `400 Unsupported type specified: songs` while `api.post()`
with the identical query string works. Do not "correct" these calls to match
Apple's public API documentation; it documents a different endpoint.

## Limitations

Stated plainly, because they are real:

- **macOS with Safari only.** It cannot run headless or on Linux. The entire
  method is borrowing a signed-in browser session.
- **Safari must be running** with a signed-in music.apple.com tab open. If the
  session logs out, a human has to sign in again - there is no headless
  re-authentication.
- **`amp-api` is undocumented and Apple-internal.** Apple may change or remove it
  at any time, without notice or versioning. `doctor` and the post-add
  verification are how you would find out.
- **Requires an active Apple Music subscription.**
- **`add` writes to a real library** and has no dry-run mode.
- **Automation permission is per-calling-app.** Running it from a different
  terminal or scheduler triggers a fresh macOS prompt, which needs a human once.
- **Library indexing can lag.** A track occasionally reports `unverified` for a
  few seconds after a successful add; re-run `resolve-library-id` to confirm.
