#!/usr/bin/env python3
"""Pair this phone with the Social Match server (CONTRACT.md S4).

Python standard library only: urllib.request, json, argparse, os, sys.

    python3 scripts/pair.py --start                 # print the code + tap link for the student
    python3 scripts/pair.py --finish AB12-CD34      # store auth.json (mode 600), print agent_id
    python3 scripts/pair.py --status                # exit 0 when paired, 1 when not
    python3 scripts/pair.py --forget                # unlink this agent, delete auth.json
    python3 scripts/pair.py --selftest              # no network, no files outside a temp dir

Server: env SOCIAL_MATCH_SERVER, default http://localhost:8000.
Auth file: <--data-dir>/auth.json (default ./skill_data), mode 600.
Exit codes: 0 ok, 1 server/state error (the server's {"error","detail"} body is printed when present), 2 usage error.
"""

import argparse
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_SERVER = "http://localhost:8000"
# WAFs reject the stock Python-urllib signature (Cloudflare: error 1010).
USER_AGENT = "study-match-skill/0.1"
DEFAULT_DATA_DIR = "./skill_data"
TIMEOUT_SECONDS = 20
CROCKFORD = re.compile(r"^[0-9A-HJKMNP-TV-Z]{8}$")


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


def utc_now():
    """'YYYY-MM-DDTHH:MM:SSZ', the timestamp format the server stores."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_code(code):
    """'ab12cd34' / 'ab12-cd34' -> 'AB12-CD34'."""
    bare = re.sub(r"[\s-]", "", code or "").upper()
    return bare[:4] + "-" + bare[4:] if CROCKFORD.match(bare) else (code or "").strip().upper()


# ---------------------------------------------------------------- auth.json


def read_auth(data_dir):
    """Stored pairing, or None when the student has not paired yet."""
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
        print(f"{path} is incomplete; run --start and --finish again", file=sys.stderr)
        sys.exit(1)
    return data


def require_auth(data_dir):
    auth = read_auth(data_dir)
    if auth is None:
        print("not paired yet: run `python3 scripts/pair.py --start`", file=sys.stderr)
        sys.exit(1)
    return auth


def write_auth(data_dir, data):
    """Write auth.json with mode 600 (owner read/write only). Returns the path."""
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    path = auth_path(data_dir)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)  # O_TRUNC keeps an existing file's mode; force it
    return path


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
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - base URL comes from SOCIAL_MATCH_SERVER
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


# ---------------------------------------------------------------- commands


def cmd_start(args):
    body = {"label": args.label} if args.label else {}
    data = api("POST", "/api/pair/start", body)
    code = data.get("code")
    link = data.get("url")
    if not code:
        print(json.dumps(data, indent=2, sort_keys=True))
        print("the server did not return a pairing code", file=sys.stderr)
        return 1
    print(f"code: {code}")
    print(f"url: {link or f'{server_url()}/m/pair/{code}'}")
    if data.get("expires_in"):
        print(f"expires_in: {data['expires_in']}")
    return 0


def cmd_finish(args):
    code = normalize_code(args.finish)
    if not code:
        print("usage error: --finish needs a pairing code", file=sys.stderr)
        return 2
    data = api("POST", "/api/pair/finish", {"code": code})
    token = data.get("token")
    agent_id = data.get("agent_id")
    if not token or not agent_id:
        print(f"the server returned no token/agent_id: {json.dumps(data, sort_keys=True)}", file=sys.stderr)
        return 1
    path = write_auth(
        args.data_dir,
        {"token": token, "agent_id": agent_id, "server": server_url(), "paired_at": utc_now()},
    )
    print(f"agent_id: {agent_id}")
    print(f"server: {server_url()}")
    print(f"saved: {path} (mode 600)")
    return 0


def cmd_status(args):
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


def cmd_forget(args):
    auth = require_auth(args.data_dir)
    api("POST", "/api/pair/forget", {}, token=auth["token"])
    try:
        os.remove(auth_path(args.data_dir))
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"unlinked, but could not delete {auth_path(args.data_dir)}: {exc}", file=sys.stderr)
        return 1
    print(f"unlinked agent {auth.get('agent_id')}; auth.json removed.")
    return 0


# ---------------------------------------------------------------- selftest


def check(condition, label, failures):
    if condition:
        print(f"ok: {label}")
    else:
        print(f"FAIL: {label}")
        failures.append(label)


def selftest(args):
    """Argument parsing and auth-file checks only. No network, no writes outside a temp dir."""
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

    check(auth_path("./skill_data") == os.path.join("./skill_data", "auth.json"), "auth.json lives in --data-dir", failures)
    check(normalize_code("ab12cd34") == "AB12-CD34", "code normalization adds the dash and uppercases", failures)
    check(normalize_code("AB12-CD34") == "AB12-CD34", "an already formatted code is left alone", failures)
    check(normalize_code("nope!") == "NOPE!", "a garbled code is passed through for the server to reject", failures)

    fastapi_body = json.dumps({"detail": {"error": "token_invalid", "detail": "unknown token"}})
    check(error_lines(fastapi_body, 401) == ["error: token_invalid", "detail: unknown token"],
          "FastAPI {'detail': {...}} errors are unwrapped", failures)
    plain_body = json.dumps({"error": "payload_forbidden", "detail": "unknown key"})
    check(error_lines(plain_body, 422) == ["error: payload_forbidden", "detail: unknown key"],
          "bare {'error','detail'} bodies are printed", failures)
    check(error_lines("<html>oops</html>", 500) == ["HTTP 500: <html>oops</html>"], "non-JSON errors still print", failures)

    with tempfile.TemporaryDirectory() as tmp:
        check(read_auth(tmp) is None, "no auth.json yet reads as not paired", failures)
        path = write_auth(tmp, {"token": "t", "agent_id": "ag_1", "server": DEFAULT_SERVER, "paired_at": utc_now()})
        check(os.path.isfile(path), "write_auth creates auth.json", failures)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check(mode == 0o600, f"auth.json is mode 600 (got {mode:o})", failures)
        os.chmod(path, 0o644)
        write_auth(tmp, {"token": "t2", "agent_id": "ag_1", "server": DEFAULT_SERVER, "paired_at": utc_now()})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        check(mode == 0o600, f"rewriting a looser auth.json tightens it back to 600 (got {mode:o})", failures)
        stored = read_auth(tmp)
        check(stored is not None and stored.get("token") == "t2", "auth.json round-trips", failures)

    parser = build_parser()
    for argv, label in (([], "no command"), (["--start", "--status"], "two commands at once")):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                parser.parse_args(argv)
                code = None
            except SystemExit as exc:
                code = exc.code
        check(code == 2, f"{label} is a usage error (exit 2)", failures)

    parsed = parser.parse_args(["--start", "--label", "phone"])
    check(parsed.start is True and parsed.label == "phone", "--start --label parses", failures)
    parsed = parser.parse_args(["--finish", "AB12-CD34", "--data-dir", "/tmp/x"])
    check(parsed.finish == "AB12-CD34" and parsed.data_dir == "/tmp/x", "--finish CODE --data-dir parses", failures)

    if failures:
        print(f"\n{len(failures)} check(s) failed", file=sys.stderr)
        return 1
    print("\nselftest passed (no network calls were made)")
    return 0


# ---------------------------------------------------------------- CLI


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pair.py",
        description="Pair this phone with the Social Match server, or check/forget the pairing.",
    )
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="where auth.json lives (default: ./skill_data)")
    parser.add_argument("--server", default=None,
                        help="base URL of the match server; overrides SOCIAL_MATCH_SERVER")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--start", action="store_true", help="create a pairing code and print the tap link")
    actions.add_argument("--finish", metavar="CODE", help="finish pairing with the code the student approved")
    actions.add_argument("--status", action="store_true", help="print the stored pairing (exit 0 when paired)")
    actions.add_argument("--forget", action="store_true", help="unlink this agent and delete auth.json")
    actions.add_argument("--selftest", action="store_true", help="run offline checks and exit 0/1")
    parser.add_argument("--label", default=None, help="optional name for this phone, used with --start")
    return parser


def main(argv=None):
    global _SERVER_OVERRIDE
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "server", None):
        _SERVER_OVERRIDE = args.server
    if args.label and not args.start:
        parser.error("--label only makes sense with --start")
    if args.selftest:
        return selftest(args)
    if args.start:
        return cmd_start(args)
    if args.finish:
        return cmd_finish(args)
    if args.status:
        return cmd_status(args)
    if args.forget:
        return cmd_forget(args)
    parser.error("pick one of --start, --finish, --status, --forget, --selftest")


if __name__ == "__main__":
    sys.exit(main())
