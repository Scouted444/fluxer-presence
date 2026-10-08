# fluxer-presence

Discord-style "rich presence" for [Fluxer](https://fluxer.app) on Windows.

Fluxer has no native Rich Presence or plugin API yet, so this is a small standalone script. It watches what is running on your PC and sets your Fluxer **custom status** to something like:

```
🎮 Playing Bully · 1h 23m
💻 Coding in VS Code — main.rs · 42m
```

It uses the documented `PATCH https://api.fluxer.app/v1/users/@me/settings` endpoint. No injection, no client mods. Once Fluxer ships native rich presence this will be obsolete.

## Features

- Zero dependencies: Python 3.9+ standard library and `ctypes` only (Windows)
- Focused app wins; otherwise the highest-priority running app from your list
- Elapsed timer from the real process start time
- Optional window title per app (`"title": true`)
- Restores your original custom status when nothing matches, you go idle, or you quit
- Status carries an expiry, so it clears itself even if the script is killed
- Rate-limit aware (honours `Retry-After`), never updates more than every 30 s

## Setup

1. Install [Python 3.9+](https://www.python.org/downloads/) (tick "Add to PATH").
2. Download this repo (or `git clone`).
3. Run `python fluxer_presence.py` once. It creates `config.json` and exits asking for a token.
4. Put your Fluxer token in `config.json` under `"token"`, or set the `FLUXER_TOKEN` environment variable.
5. Edit `apps` in `config.json` to match the programs you use.
6. Run `python fluxer_presence.py` again. Leave it running.

Test safely first with `python fluxer_presence.py --dry-run`, which prints what it would set and never contacts Fluxer.

### Run at login, hidden

Press `Win+R`, type `shell:startup`, and drop a shortcut to `start_hidden.bat` in that folder. Stop it by ending `pythonw.exe` in Task Manager (your original status will expire on its own within 5 minutes).

### Getting your token

The script edits your own account settings, so it needs your user token: open Fluxer in your browser, open DevTools (F12) → Network, click any request to `api.fluxer.app`, and copy the `Authorization` request header value.

## Config reference

| Key | Default | Meaning |
|---|---|---|
| `token` | `""` | Your Fluxer token (or use `FLUXER_TOKEN`) |
| `poll_seconds` | `2` | How often to scan for apps (local only) |
| `min_update_seconds` | `30` | Minimum gap between API calls |
| `status_ttl_minutes` | `5` | Status auto-expires this long after the last push |
| `idle_minutes` | `10` | No input for this long clears the status (`0` = off) |
| `restore_original_status` | `true` | Put your previous custom status back |
| `apps` | see `config.example.json` | `exe name → {name, verb, emoji, priority, title?}` |

Exe names are matched case-insensitively. Lower `priority` wins when nothing listed is focused. Emoji must be plain Unicode (custom emoji need an ID and an account entitlement).

## Limits and warnings

- **Keep `config.json` private.** Anyone with your token controls your account. It is in `.gitignore`; don't remove that.
- Automating a user account may not be allowed by Fluxer's rules. You use this at your own risk.
- It only knows the app name, timer, and window title. It can't show in-game details like Discord's Game SDK.
- Windows only.

## Related

- [FluxPresence](https://github.com/g00bPy/FluxPresence): GUI status automator
- [fluxer-rpc](https://github.com/letruxux/fluxer-rpc): mirrors real Discord Rich Presence to Fluxer

Not affiliated with Fluxer. MIT licensed.
