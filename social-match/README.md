# Social Match (Cubbie Skill)

A Cubbie Skill for the social matcher in this repo. It turns a student's own calendar into a short list of free
windows plus a few interest tags, sends only that to the match server, and texts the student when someone else's
evening lines up. Calendar titles, attendees, locations and course codes never leave the phone.

- `SKILL.md` — what the Cubbie agent reads: triggers, the five steps, the redaction rules, and the exact words of the
  confirmation question.
- `scripts/pair.py` — pair this phone with the server (`--start`, `--finish`, `--status`, `--forget`, `--selftest`).
- `scripts/bus.py` — inbox and state (`--poll`, `--send-state`, `--revoke`, `--whoami`, `--selftest`).
- `payload.example.json` — a valid CONTRACT S5 payload (windows, tags, public events, `busy_load`) to copy the shape from. The dates are illustrative: windows
  must fall inside the next 30 days at the moment they are sent.

## Requirements

Python 3.11+ and nothing else. Both scripts use only `urllib.request`, `json`, `argparse`, `os`, `sys` (plus `time`,
`re`, `stat`, `tempfile` in `pair.py`, and `tempfile` in `bus.py`, for their selftests). No venv, no third-party
packages, no `uv`.

## Server and saved state

- Base URL: the env var `SOCIAL_MATCH_SERVER`, default `http://localhost:8000`.
- Pairing: `auth.json` under `--data-dir` (default `./skill_data`), mode 600, holding
  `{token, agent_id, server, paired_at}`. Created by `pair.py --finish`, deleted by `--forget`. The token is never
  printed.

## Run it by hand

From this folder (the repo's `social-match/`):

```bash
python3 scripts/bus.py --selftest && python3 scripts/pair.py --selftest   # offline, no network

python3 scripts/pair.py --status                  # exit 0 = already paired
python3 scripts/pair.py --start                   # prints code + the link the student taps
python3 scripts/pair.py --finish AB12-CD34        # stores auth.json (600), prints agent_id
python3 scripts/bus.py --send-state payload.example.json
python3 scripts/bus.py --poll --since 0           # messages, then "NEXT SINCE <n>"
python3 scripts/pair.py --forget                  # unlink, delete auth.json
```

Exit codes, both scripts: `0` ok, `1` server or state error (the server's `{"error","detail"}` body is printed when the
server sends one), `2` usage error or a local payload/file problem.

`bus.py --revoke <candidate_id>` does not revoke anything. Revoking needs the student's own web session (the endpoint
is session-only, by design), so the script prints a link for the student and exits 2. There is no bearer revoke path
and none should be added.

## Platform notes

- The kit's `cubbie-testing.md` states that Cubbie has **no install rules of its own**: a Skill is any folder with a
  `SKILL.md`, and a leftover `cubbie.yaml` is "not needed, ignored" (it appears as a warning in the structure check).
  The kit documents no schema for `cubbie.yaml` anywhere — `schemas.md` covers `evals.json`, `history.json`,
  `grading.json`, `metrics.json`, `timing.json`, `benchmark.json`, `comparison.json` and `analysis.json`, not
  manifests — and `cubbie-capabilities.md` and `cubbie-auth.md` say nothing about one either.
- **`cubbie.yaml` is a harmless declaration, not a gate.** It lists the one host the scripts call. The kit says it is
  "not needed, ignored", and a real Cubbie told us on 24 Oct that listing a host there does not grant access: scripts can
  make HTTPS requests on port 443 either way. `SKILL.md` is the source of truth — a Skill is the folder.
- Sign-in is approach 4 in `cubbie-auth.md` (CLI login with a link), expressed as two steps because the server's
  pairing flow is start-then-approve: `pair.py --start` prints the link, `pair.py --finish CODE` saves the pairing.
  There is no key to paste and nothing that belongs in the chat.
- Scripts cannot call Cubbie's abilities, so `SKILL.md` has Cubbie read the calendar and Canvas itself and do the
  redaction before anything is written to a file for `--send-state`.
- The default `http://localhost:8000` is for local development. Cubbie's sandbox reaches HTTPS on 443 to the
  internet, so a hosted deployment must set `SOCIAL_MATCH_SERVER` to an `https://` URL.

## Installing

The folder is `social-match/` in this repository, named after the skill, so you can point
Cubbie at it; Cubbie lists a Skill by the `name` in `SKILL.md` either way.

## Testing done for this package

```bash
python3 scripts/bus.py --selftest && python3 scripts/pair.py --selftest    # both exit 0
```

The selftests check argument parsing, the auth-file round trip and its 600 mode, the server error-body handling, the
poll line rendering, and the local CONTRACT S5 payload pre-flight (including a payload carrying
`"Therapy — Dr. Ruiz"`, which is rejected before any request is made). Neither makes a network call.

A live round trip against `uvicorn server.app:app` on a throwaway database covered: `--start`, a directly seeded
`pairing_codes` row, `--finish` (auth.json at mode 600), `--status`, `--send-state` (`{"changed": true}`), `--whoami`,
code reuse (server `error: code_used`, exit 1), a bad token (`error: token_invalid`, exit 1), and `--forget` followed
by `--status` exiting 1.
