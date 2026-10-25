#!/usr/bin/env python3
"""Read the inbox and publish availability state for the Social Match server (CONTRACT.md S5, S9).

Python standard library only: urllib.request, json, argparse, os, sys.

    python3 scripts/bus.py --poll [--since N]   # print new inbox messages, then "NEXT SINCE <seq>"
    python3 scripts/bus.py --send-state FILE    # POST /api/state (windows, tags, public events, busy_load), print the reply
    python3 scripts/bus.py --activity ID        # show the current activity offer on a match
    python3 scripts/bus.py --propose-activity ID --version N --tag TAG --url HTTPS_LINK
    python3 scripts/bus.py --respond-activity ID --offer N --accept
    python3 scripts/bus.py --respond-activity ID --offer N --pass --reason CODE
    python3 scripts/bus.py --respond-activity ID --offer N --counter --tag TAG --url HTTPS_LINK [--reason CODE]
    python3 scripts/bus.py --revoke [ID]        # web-session-only: prints what to do, exits 2
    python3 scripts/bus.py --whoami             # print this agent's stored identity (exit 0 when paired)
    python3 scripts/bus.py --selftest           # offline argument/payload checks

Server: env SOCIAL_MATCH_SERVER, default http://localhost:8000.
Auth file: <--data-dir>/auth.json (default ./skill_data), written by pair.py.
Exit codes: 0 ok, 1 server/state error (the server's {"error","detail"} body is printed when present),
2 usage error or a local payload/file problem that must be fixed before sending.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

DEFAULT_SERVER = "http://localhost:8000"
# WAFs reject the stock Python-urllib signature (Cloudflare: error 1010).
USER_AGENT = "study-match-skill/0.1"
DEFAULT_DATA_DIR = "./skill_data"
TIMEOUT_SECONDS = 20

# CONTRACT S5: the preferred vocabulary. Interests are free text now (the server
# canonicalises them into this list), so an unknown tag is NOT an error here either.
TAG_VOCAB = [
    "bouldering", "climbing", "hiking", "running", "gym", "swimming", "cycling", "basketball", "soccer",
    "tennis", "ultimate_frisbee", "board_games", "video_games", "chess", "painting", "drawing", "photography",
    "pottery", "guitar", "piano", "singing", "live_music", "edm", "techno", "rock", "jazz", "coffee", "baking",
    "cooking", "language_exchange", "debate", "anime", "cinema", "volunteering",
]
WANTS_VOCAB = ["hangout", "event"]
REQUIRED_KEYS = {"windows", "tags", "wants", "free_time", "consent"}
# Pairing signals are optional: public events from the inbox/calendar, the student's approval of that
# list, and one coarse 0-3 number for how heavy the next two weeks are. Nothing else is allowed.
OPTIONAL_KEYS = {"events", "events_confirmed_at", "busy_load"}
PAYLOAD_KEYS = REQUIRED_KEYS | OPTIONAL_KEYS
EVENT_KEYS = {"url", "start", "end", "tz", "category"}
EVENT_CATEGORIES = ["career", "club", "talk", "music", "sports", "arts", "food", "party", "volunteer", "other"]
MAX_EVENTS = 10
WINDOW_KEYS = {"start", "end", "tz"}
FREE_TIME_KEYS = {"source", "student_confirmed_at"}
CONSENT_KEYS = {"matching", "publication"}
PUBLICATION_KEYS = {"tier"}
MAX_WINDOWS = 20
MAX_TAGS = 8
MAX_TAG_CHARS = 32
# Activity negotiation: an offer is a tag + an https link, nothing else. Reasons are a closed list.
REASONS = ["too_expensive", "too_far", "not_my_vibe", "wrong_time"]
MAX_URL_LEN = 300


# ---------------------------------------------------------------- config


# Set from --server in main(); outranks the env var. None means "no override".
_SERVER_OVERRIDE = None


def server_url():
    """Base URL: --server, else SOCIAL_MATCH_SERVER, else http://localhost:8000.

    The flag exists because a hosted deployment cannot rely on an exported env var
    surviving between separate sandbox commands; Cubbie can pass --server every time.
    """
    if _SERVER_OVERRIDE:
        return _SERVER_OVERRIDE.strip().rstrip("/")
    return (os.environ.get("SOCIAL_MATCH_SERVER") or DEFAULT_SERVER).strip().rstrip("/")


def auth_path(data_dir):
    return os.path.join(data_dir, "auth.json")


# ---------------------------------------------------------------- auth.json


def read_auth(data_dir):
    """The pairing pair.py stored, or None when the student has not paired yet."""
    path = auth_path(data_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or not data.get("token") or not data.get("agent_id"):
        print(f"{path} is incomplete; run `python3 scripts/pair.py --start` again", file=sys.stderr)
        sys.exit(1)
    return data


def require_auth(data_dir):
    auth = read_auth(data_dir)
    if auth is None:
        print("not paired yet: run `python3 scripts/pair.py --start`", file=sys.stderr)
        sys.exit(1)
    return auth


# ---------------------------------------------------------------- HTTP


def error_lines(body_text, status):
    """Human lines for a failed response. Handles raw {"error","detail"} and FastAPI's {"detail": {...}}."""
    node = None
    try:
        parsed = json.loads(body_text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        node = parsed["detail"] if isinstance(parsed.get("detail"), dict) else parsed
    if isinstance(node, dict) and ("error" in node or "detail" in node):
        lines = []
        if node.get("error"):
            lines.append(f"error: {node['error']}")
        detail = node.get("detail")
        if detail:
            lines.append(f"detail: {detail if isinstance(detail, str) else json.dumps(detail)}")
        if lines:
            return lines
    text = (body_text or "").strip()
    return [f"HTTP {status}" + (f": {text[:300]}" if text else "")]


def api(method, path, payload=None, token=None):
    """Call the server, return the parsed JSON body. On an HTTP error print the body and exit 1."""
    url = server_url() + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - SOCIAL_MATCH_SERVER
    # Cloudflare (and most WAFs) block the default "Python-urllib/x.y" signature
    # outright — error 1010. Identify the Skill honestly instead.
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        # http(s) only: the base URL comes from SOCIAL_MATCH_SERVER.
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        for line in error_lines(raw, exc.code):
            print(line, file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        print(f"cannot reach {server_url()}: {reason}", file=sys.stderr)
        sys.exit(1)
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        print(f"server sent something that is not JSON: {raw[:300]}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------- payload checks


def payload_problems(payload):
    """Local pre-flight for CONTRACT S5. The server is still the authority (422 payload_forbidden).

    Only checks what the server is known to reject, so it can never block a payload the server accepts.
    Every extra key is a problem: extra="forbid" means a stray key fails the whole request, and a stray
    key is exactly how an event title would leak into a request body.
    """
    problems = []
    if not isinstance(payload, dict):
        return ["the payload must be a JSON object"]

    extra = sorted(set(payload) - PAYLOAD_KEYS)
    if extra:
        problems.append(f"unknown top-level key(s): {', '.join(extra)}; only {sorted(PAYLOAD_KEYS)} are allowed")
    missing = sorted(REQUIRED_KEYS - set(payload))
    if missing:
        problems.append(f"missing key(s): {', '.join(missing)}")

    windows = payload.get("windows", [])
    if not isinstance(windows, list):
        problems.append("windows must be a list")
        windows = []
    elif len(windows) > MAX_WINDOWS:
        problems.append(f"windows has {len(windows)} entries; at most {MAX_WINDOWS} are allowed")
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            problems.append(f"windows[{index}] must be an object")
            continue
        stray = sorted(set(window) - WINDOW_KEYS)
        if stray:
            problems.append(f"windows[{index}] has unknown key(s): {', '.join(stray)}")
        for key in sorted(WINDOW_KEYS - set(window)):
            problems.append(f"windows[{index}] is missing {key}")
        start, end = window.get("start"), window.get("end")
        if isinstance(start, str) and isinstance(end, str) and not start < end:
            problems.append(f"windows[{index}] start must be before end (local 'YYYY-MM-DDTHH:MM' strings)")

    tags = payload.get("tags", [])
    if not isinstance(tags, list):
        problems.append("tags must be a list")
    else:
        for tag in tags:
            if not isinstance(tag, str) or not tag.strip():
                problems.append("every tag must be a non-empty string")
                continue
            if len(tag.strip()) > MAX_TAG_CHARS:
                problems.append(f"tag {tag[:20]!r} is longer than {MAX_TAG_CHARS} characters")
            if any(ord(ch) < 0x20 for ch in tag):
                problems.append(f"tag {tag[:20]!r} contains control characters")
        if len(set(tags)) > MAX_TAGS:
            problems.append(f"at most {MAX_TAGS} tags are allowed")

    wants = payload.get("wants", [])
    if not isinstance(wants, list):
        problems.append("wants must be a list")
    else:
        unknown = sorted({want for want in wants if want not in WANTS_VOCAB})
        if unknown:
            problems.append(f"wants must be from {WANTS_VOCAB}, got: {', '.join(map(str, unknown))}")

    free_time = payload.get("free_time")
    if not isinstance(free_time, dict):
        problems.append("free_time must be an object")
    else:
        stray = sorted(set(free_time) - FREE_TIME_KEYS)
        if stray:
            problems.append(f"free_time has unknown key(s): {', '.join(stray)}")
        source = free_time.get("source")
        if source not in ("student", "cubbie_computed"):
            problems.append("free_time.source must be 'student' or 'cubbie_computed'")
        if source == "cubbie_computed" and not free_time.get("student_confirmed_at"):
            problems.append("free_time.source 'cubbie_computed' needs student_confirmed_at (when the student said yes)")

    problems.extend(event_problems(payload))

    consent = payload.get("consent")
    if not isinstance(consent, dict):
        problems.append("consent must be an object")
    else:
        stray = sorted(set(consent) - CONSENT_KEYS)
        if stray:
            problems.append(f"consent has unknown key(s): {', '.join(stray)}")
        if not isinstance(consent.get("matching"), bool):
            problems.append("consent.matching must be true or false")
        publication = consent.get("publication")
        if not isinstance(publication, dict):
            problems.append("consent.publication must be an object")
        else:
            stray = sorted(set(publication) - PUBLICATION_KEYS)
            if stray:
                problems.append(f"consent.publication has unknown key(s): {', '.join(stray)}")
            if publication.get("tier") not in ("card", "card+name"):
                problems.append("consent.publication.tier must be 'card' or 'card+name'")
    return problems


def event_problems(payload):
    """Pre-flight for the pairing signals. Stricter than the server on one point, on purpose: an event link
    must not carry a ?query. Invitation emails put personal RSVP tokens there; the server would drop it, but it
    should never leave the phone at all."""
    problems = []
    events = payload.get("events", [])
    if not isinstance(events, list):
        return ["events must be a list"]
    if len(events) > MAX_EVENTS:
        problems.append(f"events has {len(events)} entries; at most {MAX_EVENTS} are allowed")
    if events and not payload.get("events_confirmed_at"):
        problems.append("events needs events_confirmed_at (the moment the student approved the event list)")
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            problems.append(f"events[{index}] must be an object")
            continue
        stray = sorted(set(event) - EVENT_KEYS)
        if stray:
            problems.append(f"events[{index}] has unknown key(s): {', '.join(stray)}; only {sorted(EVENT_KEYS)} are allowed")
        for key in sorted(EVENT_KEYS - set(event)):
            problems.append(f"events[{index}] is missing {key}")
        if event.get("category") not in EVENT_CATEGORIES:
            problems.append(f"events[{index}].category must be one of {', '.join(EVENT_CATEGORIES)}")
        url = event.get("url")
        for problem in url_problems(url):
            problems.append(f"events[{index}].url: {problem}")
        if isinstance(url, str) and "?" in url:
            problems.append(f"events[{index}].url has a ?query part; send the public event page without it")
        start, end = event.get("start"), event.get("end")
        if isinstance(start, str) and isinstance(end, str) and not start < end:
            problems.append(f"events[{index}] start must be before end (local 'YYYY-MM-DDTHH:MM' strings)")
    load = payload.get("busy_load")
    if "busy_load" in payload and (isinstance(load, bool) or not isinstance(load, int) or not 0 <= load <= 3):
        problems.append("busy_load must be a whole number from 0 (light) to 3 (heavy)")
    return problems


def load_payload(path):
    """Read and pre-flight a payload file. Local problems are usage errors: print and exit 2."""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        print(f"no such file: {path}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:
        print(f"{path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    problems = payload_problems(payload)
    if problems:
        print(f"{path} is not a valid CONTRACT S5 payload:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        sys.exit(2)
    return payload


# ---------------------------------------------------------------- commands


def poll_lines(items):
    """Lines for the model to read. Student-facing items print `message`, then the card link, then any
    activity link, each on its own line. Agent-facing items (payload.audience == "agent") are NOT for
    the student: they print one machine line starting with AGENT, and the link on the next line.
    Items without an audience are student-facing, as before."""
    lines = []
    for item in items:
        payload = item.get("payload") or {}
        if payload.get("audience") == "agent":
            lines.extend(agent_lines(payload))
            continue
        message = payload.get("message")
        if message:
            lines.append(str(message))
        card_url = payload.get("card_url")
        if card_url:
            lines.append(str(card_url))
        activity_url = payload.get("activity_url")
        if activity_url and (not message or str(activity_url) not in str(message)):
            lines.append(str(activity_url))
    return lines


def agent_lines(payload):
    kind = payload.get("kind")
    if kind == "activity_offer":
        head = (
            f"AGENT activity_offer candidate={payload.get('candidate_id')} offer={payload.get('offer_id')}"
            f" round={payload.get('round')}/{payload.get('max_rounds')} version={payload.get('proposal_version')}"
            f" tag={payload.get('activity_tag')} window={payload.get('window')!r}"
            f" counter_of={payload.get('counter_of') or '-'} reason={payload.get('reason') or '-'}"
        )
        out = [head]
        if payload.get("activity_url"):
            out.append(f"AGENT link {payload['activity_url']}")
        return out
    if kind == "activity_response":
        return [
            f"AGENT activity_response candidate={payload.get('candidate_id')} offer={payload.get('offer_id')}"
            f" response={payload.get('response')} reason={payload.get('reason') or '-'}"
        ]
    return [f"AGENT {kind or 'unknown'}"]


def cmd_poll(args):
    auth = require_auth(args.data_dir)
    data = api("GET", f"/api/inbox?since={args.since}", token=auth["token"])
    items = data.get("items") or []
    for line in poll_lines(items):
        print(line)
    seqs = [item.get("seq") for item in items if isinstance(item.get("seq"), int)]
    print(f"NEXT SINCE {max(seqs) if seqs else args.since}")
    return 0


def cmd_send_state(args):
    auth = require_auth(args.data_dir)
    payload = load_payload(args.send_state)
    data = api("POST", "/api/state", payload, token=auth["token"])
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def url_problems(url):
    """Local pre-flight mirroring the server's link rules. The server stays the authority."""
    if not isinstance(url, str) or not url:
        return ["the link is empty"]
    problems = []
    if len(url) > MAX_URL_LEN:
        problems.append(f"the link is longer than {MAX_URL_LEN} characters")
    if not url.isascii() or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        problems.append("the link must be plain ASCII with no spaces")
    if not url.startswith("https://"):
        problems.append("the link must start with https://")
    rest = url[len("https://"):].split("/", 1)[0].split("?", 1)[0]
    if "@" in rest:
        problems.append("the link must not contain a username or password")
    if "#" in url:
        problems.append("the link must not contain a #fragment")
    return problems


def activity_problems(tag, url, reason=None, need_link=True):
    problems = []
    if need_link:
        if tag not in TAG_VOCAB:
            problems.append(f"tag {tag!r} is not in TAG_VOCAB (the closed interest list)")
        problems.extend(url_problems(url))
    if reason is not None and reason not in REASONS:
        problems.append(f"reason must be one of {', '.join(REASONS)}")
    return problems


def fail_usage(problems):
    for problem in problems:
        print(f"- {problem}", file=sys.stderr)
    return 2


def cmd_activity(args):
    auth = require_auth(args.data_dir)
    data = api("GET", f"/api/agent/activity/{int(args.activity)}", token=auth["token"])
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_propose_activity(args):
    if args.version is None or not args.tag or not args.url:
        print("--propose-activity needs --version, --tag and --url", file=sys.stderr)
        return 2
    problems = activity_problems(args.tag, args.url)
    if problems:
        return fail_usage(problems)
    auth = require_auth(args.data_dir)
    body = {"candidate_id": int(args.propose_activity), "proposal_version": args.version,
            "activity_tag": args.tag, "activity_url": args.url}
    print(json.dumps(api("POST", "/api/agent/activity/propose", body, token=auth["token"]), indent=2, sort_keys=True))
    return 0


def cmd_respond_activity(args):
    if args.offer is None:
        print("--respond-activity needs --offer", file=sys.stderr)
        return 2
    chosen = [name for name, on in (("accept", args.accept), ("pass", args.pass_), ("counter", args.counter)) if on]
    if len(chosen) != 1:
        print("choose exactly one of --accept, --pass, --counter", file=sys.stderr)
        return 2
    response = chosen[0]
    body = {"candidate_id": int(args.respond_activity), "offer_id": args.offer, "response": response}
    problems = []
    if response == "accept":
        if args.reason or args.tag or args.url:
            problems.append("--accept carries no reason, tag or link")
    elif response == "pass":
        if not args.reason:
            problems.append("--pass needs --reason")
        if args.tag or args.url:
            problems.append("--pass carries no tag or link; use --counter to suggest something else")
        problems.extend(activity_problems(None, None, args.reason, need_link=False))
        body["reason"] = args.reason
    else:
        if not args.tag or not args.url:
            problems.append("--counter needs --tag and --url")
        else:
            problems.extend(activity_problems(args.tag, args.url, args.reason))
        body["activity_tag"], body["activity_url"] = args.tag, args.url
        if args.reason:
            body["reason"] = args.reason
    if problems:
        return fail_usage(problems)
    auth = require_auth(args.data_dir)
    print(json.dumps(api("POST", "/api/agent/activity/respond", body, token=auth["token"]), indent=2, sort_keys=True))
    return 0


def cmd_revoke(args):
    """Revoking needs the student's web session (CONTRACT S4/S11). There is no bearer path, on purpose."""
    link = f"{server_url()}/m/{args.revoke}" if args.revoke else f"{server_url()}/"
    print("Revoking a plan has to happen on the website, not from here.")
    print("It needs the student's own web session, so this Skill can't do it for them.")
    print("Send the student this link and ask them to tap Revoke there:")
    print(link)
    return 2


def cmd_whoami(args):
    auth = read_auth(args.data_dir)
    if auth is None:
        print("not paired: run `python3 scripts/pair.py --start`", file=sys.stderr)
        return 1
    print(f"agent_id: {auth.get('agent_id')}")
    print(f"server: {auth.get('server') or '(unknown)'}")
    print(f"paired_at: {auth.get('paired_at') or '(unknown)'}")
    print(f"auth: {auth_path(args.data_dir)}")
    if auth.get("server") and auth["server"] != server_url():
        print(f"note: SOCIAL_MATCH_SERVER is {server_url()} but auth.json was paired with {auth['server']}")
    return 0


# ---------------------------------------------------------------- selftest


def quiet(call):
    """Run call with its output discarded; return what it returns, or the SystemExit code it raises."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return call()
        except SystemExit as exc:
            return exc.code


def check(condition, label, failures):
    if condition:
        print(f"ok: {label}")
    else:
        print(f"FAIL: {label}")
        failures.append(label)


def good_payload():
    return {
        "windows": [
            {"start": "2026-10-29T19:00", "end": "2026-10-29T21:00", "tz": "America/Los_Angeles"},
            {"start": "2026-11-01T10:00", "end": "2026-11-01T12:00", "tz": "America/Los_Angeles"},
        ],
        "tags": ["climbing", "coffee"],
        "wants": ["hangout"],
        "free_time": {"source": "cubbie_computed", "student_confirmed_at": "2026-10-24T09:10:00Z"},
        "consent": {"matching": True, "publication": {"tier": "card"}},
    }


def selftest(args):
    """Argument, payload and rendering checks only. No network, no writes outside a temp dir."""
    failures = []

    previous_server = os.environ.get("SOCIAL_MATCH_SERVER")
    try:
        os.environ.pop("SOCIAL_MATCH_SERVER", None)
        check(server_url() == DEFAULT_SERVER, f"default server is {DEFAULT_SERVER}", failures)
        os.environ["SOCIAL_MATCH_SERVER"] = "http://example.test:9000/"
        check(server_url() == "http://example.test:9000", "SOCIAL_MATCH_SERVER is honored, trailing slash dropped", failures)
        globals()["_SERVER_OVERRIDE"] = "https://devhacks.navraj.me/"
        check(server_url() == "https://devhacks.navraj.me", "--server overrides env and drops trailing slash", failures)
        globals()["_SERVER_OVERRIDE"] = None
        os.environ["SOCIAL_MATCH_SERVER"] = ""
        check(server_url() == DEFAULT_SERVER, "empty SOCIAL_MATCH_SERVER falls back to the default", failures)
    finally:
        if previous_server is None:
            os.environ.pop("SOCIAL_MATCH_SERVER", None)
        else:
            os.environ["SOCIAL_MATCH_SERVER"] = previous_server

    check(len(TAG_VOCAB) == 34 and len(set(TAG_VOCAB)) == 34, "TAG_VOCAB has the 34 unique CONTRACT S5 tags", failures)
    check(auth_path("./skill_data") == os.path.join("./skill_data", "auth.json"), "auth.json lives in --data-dir", failures)

    problems = payload_problems(good_payload())
    check(problems == [], f"the bundled example shape validates ({problems})", failures)

    leaking = good_payload()
    leaking["calendar_titles"] = ["Therapy — Dr. Ruiz"]
    check(any("calendar_titles" in problem for problem in payload_problems(leaking)),
          "an extra key (a title) is caught before sending", failures)

    leaking_window = good_payload()
    leaking_window["windows"][0]["title"] = "Therapy — Dr. Ruiz"
    check(any("title" in problem for problem in payload_problems(leaking_window)),
          "a title inside a window is caught before sending", failures)

    unconfirmed = good_payload()
    del unconfirmed["free_time"]["student_confirmed_at"]
    check(any("student_confirmed_at" in problem for problem in payload_problems(unconfirmed)),
          "cubbie_computed without student_confirmed_at is caught", failures)

    # Interests are free text now: "matcha" is fine (the server canonicalises it).
    free_text = good_payload()
    free_text["tags"] = ["therapy", "matcha"]
    check(payload_problems(free_text) == [], "an interest outside TAG_VOCAB is allowed", failures)

    long_tag = good_payload()
    long_tag["tags"] = ["x" * (MAX_TAG_CHARS + 1)]
    check(any("longer than" in problem for problem in payload_problems(long_tag)),
          "an over-long tag is caught", failures)

    control = good_payload()
    control["tags"] = ["climbing\nTherapy - Dr. Ruiz"]
    check(any("control characters" in problem for problem in payload_problems(control)),
          "a tag with control characters is caught", failures)

    bad_want = good_payload()
    bad_want["wants"] = ["party"]
    check(any("wants" in problem for problem in payload_problems(bad_want)), "a want outside the vocabulary is caught", failures)

    bad_order = good_payload()
    bad_order["windows"][0]["end"] = bad_order["windows"][0]["start"]
    check(any("start must be before end" in problem for problem in payload_problems(bad_order)),
          "a zero-length window is caught", failures)

    many = good_payload()
    many["windows"] = [{"start": "2026-10-29T19:00", "end": "2026-10-29T20:00", "tz": "UTC"}] * 21
    check(any("at most 20" in problem for problem in payload_problems(many)), "21 windows are caught", failures)

    # Pairing signals: public events + busy_load.
    ev = {"url": "https://careers.asu.example.edu/fall-fair", "start": "2026-10-30T10:00",
          "end": "2026-10-30T14:00", "tz": "America/Phoenix", "category": "career"}
    with_ev = good_payload()
    with_ev.update({"events": [dict(ev)], "events_confirmed_at": "2026-10-24T09:10:00Z", "busy_load": 2})
    check(payload_problems(with_ev) == [], f"a payload with events and busy_load validates ({payload_problems(with_ev)})", failures)

    no_ack = dict(with_ev)
    del no_ack["events_confirmed_at"]
    check(any("events_confirmed_at" in problem for problem in payload_problems(no_ack)),
          "events without the student's approval stamp are caught", failures)

    titled = good_payload()
    titled.update({"events": [dict(ev, title="Interview with Acme")], "events_confirmed_at": "2026-10-24T09:10:00Z"})
    check(any("title" in problem for problem in payload_problems(titled)),
          "an event title is caught before sending", failures)

    tokened = good_payload()
    tokened.update({"events": [dict(ev, url=ev["url"] + "?rsvp_token=abc123")], "events_confirmed_at": "2026-10-24T09:10:00Z"})
    check(any("?query" in problem for problem in payload_problems(tokened)),
          "a personal RSVP link with a query string is caught before sending", failures)

    for bad_url in ("http://x.example.com/e", "https://u:p@x.example.com/e", "https://x.example.com/e#rsvp"):
        bad = good_payload()
        bad.update({"events": [dict(ev, url=bad_url)], "events_confirmed_at": "2026-10-24T09:10:00Z"})
        check(payload_problems(bad) != [], f"event link {bad_url!r} is caught", failures)

    bad_cat = good_payload()
    bad_cat.update({"events": [dict(ev, category="interview")], "events_confirmed_at": "2026-10-24T09:10:00Z"})
    check(any("category" in problem for problem in payload_problems(bad_cat)),
          "an event category outside the list is caught", failures)

    too_many = good_payload()
    too_many.update({"events": [dict(ev)] * (MAX_EVENTS + 1), "events_confirmed_at": "2026-10-24T09:10:00Z"})
    check(any("at most 10" in problem for problem in payload_problems(too_many)), "11 events are caught", failures)

    for bad_load in (4, -1, 1.5, "2", True):
        bad = good_payload()
        bad["busy_load"] = bad_load
        check(any("busy_load" in problem for problem in payload_problems(bad)), f"busy_load {bad_load!r} is caught", failures)
    zero = good_payload()
    zero["busy_load"] = 0
    check(payload_problems(zero) == [], "busy_load 0 (a light fortnight) is valid", failures)

    items = [
        {"seq": 4, "kind": "candidate", "payload": {"message": "Ava is up for Thu 19:00-21:00.", "card_url": "http://x/m/1"}},
        {"seq": 7, "kind": "match", "payload": {"message": "It's on: Thu 19:00-21:00 with Ava."}},
    ]
    check(poll_lines(items) == [
        "Ava is up for Thu 19:00-21:00.",
        "http://x/m/1",
        "It's on: Thu 19:00-21:00 with Ava.",
    ], "poll prints the message then card_url, one per line", failures)
    seqs = [item["seq"] for item in items]
    check(max(seqs) == 7, "NEXT SINCE takes the highest seq", failures)
    check(poll_lines([]) == [], "an empty inbox prints no item lines", failures)

    fastapi_body = json.dumps({"detail": {"error": "payload_forbidden", "detail": "unknown key 'calendar_titles'"}})
    check(error_lines(fastapi_body, 422) == ["error: payload_forbidden", "detail: unknown key 'calendar_titles'"],
          "FastAPI {'detail': {...}} errors are unwrapped", failures)
    plain_body = json.dumps({"error": "token_revoked", "detail": "this agent was unlinked"})
    check(error_lines(plain_body, 401) == ["error: token_revoked", "detail: this agent was unlinked"],
          "bare {'error','detail'} bodies are printed", failures)
    check(error_lines("oops", 500) == ["HTTP 500: oops"], "non-JSON errors still print", failures)

    with tempfile.TemporaryDirectory() as tmp:
        check(read_auth(tmp) is None, "no auth.json yet reads as not paired", failures)
        bad_file = os.path.join(tmp, "bad.json")
        with open(bad_file, "w", encoding="utf-8") as handle:
            handle.write('{"windows": [}')
        check(quiet(lambda: load_payload(bad_file)) == 2, "an unreadable payload file exits 2", failures)
        good_file = os.path.join(tmp, "payload.json")
        with open(good_file, "w", encoding="utf-8") as handle:
            json.dump(good_payload(), handle)
        check(load_payload(good_file)["tags"] == ["climbing", "coffee"], "a valid payload file loads", failures)

    parser = build_parser()
    for argv, label in (([], "no command"), (["--poll", "--whoami"], "two commands at once"),
                        (["--poll", "--since", "abc"], "a non-numeric --since")):
        check(quiet(lambda argv=argv: parser.parse_args(argv)) == 2, f"{label} is a usage error (exit 2)", failures)

    parsed = parser.parse_args(["--poll", "--since", "12"])
    check(parsed.poll is True and parsed.since == 12, "--poll --since N parses", failures)
    parsed = parser.parse_args(["--send-state", "payload.example.json"])
    check(parsed.send_state == "payload.example.json", "--send-state FILE parses", failures)
    parsed = parser.parse_args(["--revoke", "5"])
    check(parsed.revoke == "5", "--revoke [ID] parses", failures)

    check(quiet(lambda: cmd_revoke(parser.parse_args(["--revoke", "5"]))) == 2,
          "--revoke tells the student to use the web and exits 2", failures)

    check(url_problems("https://rockgym.example.com/open-climb") == [], "a plain https link passes the local check", failures)
    for bad, label in (("http://x.example.com", "http"), ("https://u:p@x.example.com", "credentials"),
                       ("https://x.example.com/a#b", "fragment"), ("https://x.example.com/a b", "space"),
                       ("https://x.example.com/" + "a" * 300, "overlong")):
        check(url_problems(bad) != [], f"a {label} link is caught before sending", failures)
    check(activity_problems("Phoenix Rock Gym", "https://x.example.com/a") != [], "a free-text activity tag is caught", failures)
    check(activity_problems("climbing", "https://x.example.com/a", reason="broke") != [], "a free-text reason is caught", failures)
    check(activity_problems("climbing", "https://x.example.com/a", reason="too_far") == [], "a closed-list reason passes", failures)

    offer_item = {"seq": 9, "kind": "activity_offer", "payload": {
        "kind": "activity_offer", "audience": "agent", "candidate_id": 4, "offer_id": 7, "round": 1, "max_rounds": 3,
        "proposal_version": 1, "activity_tag": "climbing", "activity_url": "https://rockgym.example.com/a",
        "window": "Thu 19:00-21:00", "counter_of": None, "reason": None}}
    offer_lines = poll_lines([offer_item])
    check(len(offer_lines) == 2 and offer_lines[0].startswith("AGENT activity_offer candidate=4 offer=7 round=1/3 version=1")
          and offer_lines[1] == "AGENT link https://rockgym.example.com/a",
          "agent-facing offers print as AGENT lines, never as a student message", failures)
    agreed_item = {"seq": 10, "kind": "activity_agreed", "payload": {
        "kind": "activity_agreed", "audience": "student", "message": "You and Ben landed on climbing for Thu 19:00-21:00. Confirm your side: http://x/m/4",
        "card_url": "http://x/m/4", "activity_url": "https://rockgym.example.com/a"}}
    check(poll_lines([agreed_item])[-2:] == ["http://x/m/4", "https://rockgym.example.com/a"],
          "a student-facing agreement prints the card link then the activity link", failures)

    parsed = parser.parse_args(["--respond-activity", "4", "--offer", "7", "--pass", "--reason", "too_far"])
    check(parsed.respond_activity == "4" and parsed.pass_ and parsed.offer == 7 and parsed.reason == "too_far",
          "--respond-activity --pass --reason parses", failures)
    check(quiet(lambda: cmd_respond_activity(parser.parse_args(["--respond-activity", "4", "--offer", "7", "--pass"]))) == 2,
          "--pass without a reason is a usage error", failures)
    check(quiet(lambda: cmd_propose_activity(parser.parse_args(
        ["--propose-activity", "4", "--version", "1", "--tag", "climbing", "--url", "http://x.example.com"]))) == 2,
          "--propose-activity with an http link is a usage error", failures)

    if failures:
        print(f"\n{len(failures)} check(s) failed", file=sys.stderr)
        return 1
    print("\nselftest passed (no network calls were made)")
    return 0


# ---------------------------------------------------------------- CLI


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bus.py",
        description="Read the match inbox and publish the student's free windows and tags.",
    )
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="where auth.json lives (default: ./skill_data)")
    parser.add_argument("--server", default=None,
                        help="base URL of the match server; overrides SOCIAL_MATCH_SERVER")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--poll", action="store_true", help="print new inbox messages, then NEXT SINCE <seq>")
    actions.add_argument("--send-state", metavar="FILE", help="POST a CONTRACT S5 payload file to /api/state")
    actions.add_argument("--revoke", metavar="ID", nargs="?", const="", default=None,
                         help="web-session-only: prints what the student should do and exits 2")
    actions.add_argument("--whoami", action="store_true", help="print the stored identity (exit 0 when paired)")
    actions.add_argument("--activity", metavar="ID", help="show the current activity offer on candidate ID")
    actions.add_argument("--propose-activity", metavar="ID", help="propose an activity for candidate ID")
    actions.add_argument("--respond-activity", metavar="ID", help="answer the current activity offer on candidate ID")
    actions.add_argument("--selftest", action="store_true", help="run offline checks and exit 0/1")
    parser.add_argument("--since", type=int, default=0, help="with --poll: last seq already seen (default 0)")
    parser.add_argument("--version", type=int, default=None, help="with --propose-activity: the proposal_version from the card")
    parser.add_argument("--offer", type=int, default=None, help="with --respond-activity: the offer id from the AGENT line")
    parser.add_argument("--accept", action="store_true", help="with --respond-activity: accept the offer")
    parser.add_argument("--pass", dest="pass_", action="store_true", help="with --respond-activity: pass, needs --reason")
    parser.add_argument("--counter", action="store_true", help="with --respond-activity: suggest another activity")
    parser.add_argument("--tag", default=None, help="activity tag from the closed interest list")
    parser.add_argument("--url", default=None, help="https link to the activity")
    parser.add_argument("--reason", default=None, help=f"one of: {', '.join(REASONS)}")
    return parser


def main(argv=None):
    global _SERVER_OVERRIDE
    args = build_parser().parse_args(argv)
    if getattr(args, "server", None):
        _SERVER_OVERRIDE = args.server
    if args.selftest:
        return selftest(args)
    if args.poll:
        return cmd_poll(args)
    if args.send_state:
        return cmd_send_state(args)
    if args.revoke is not None:
        return cmd_revoke(args)
    if args.whoami:
        return cmd_whoami(args)
    if args.activity is not None:
        return cmd_activity(args)
    if args.propose_activity is not None:
        return cmd_propose_activity(args)
    if args.respond_activity is not None:
        return cmd_respond_activity(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
