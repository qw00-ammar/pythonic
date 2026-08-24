import fcntl
import json
import os
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# --- HARDCODED CONFIG --------------------------------------------------------
HANDLES = ["UooU672514", "i9yl9"]
INTERVAL_SECONDS = 10
BARK_KEY = "aAQmJDszVrdbc9braKD8am"
BARK_SERVER = "https://api.day.app"
NTFY_TOPIC = "JamilaActivatedHerXAccount"
NTFY_HEALTH_INTERVAL_SECONDS = 60 * 60

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
TIMEOUT = 10

X_API_HOSTS = ("api.x.com", "api.twitter.com")

# Cross-process singleton lock. Diploi runs this app under Gunicorn with
# multiple worker PROCESSES (default -w 4). Each worker imports this module
# independently, so an in-memory flag/threading.Lock only stops duplicate
# threads *within* one process, not across the 4 of them. A flock on a file
# in the shared container filesystem makes sure only one worker ever starts
# the background watcher thread.
WATCHER_LOCK_PATH = os.environ.get("XWATCHER_LOCK_PATH", "/tmp/xwatcher.singleton.lock")
# ----------------------------------------------------------------------------


app = Flask(__name__)
watcher_lock = threading.Lock()
watcher_started = False
_singleton_lock_file = None  # kept open for the process lifetime; closing releases the flock

session = requests.Session()
_retry = Retry(
    total=2,
    connect=2,
    read=2,
    backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
)
session.mount("https://", HTTPAdapter(max_retries=_retry))
session.mount("http://", HTTPAdapter(max_retries=_retry))


def default_account_state():
    return {
        "last_state": None,
        "last_status": None,
        "last_api_host": None,
        "last_error": None,
        "last_error_signature": None,
        "error_since": None,
        "healthy_since": None,
    }


state = {
    "running": False,
    "started_at": None,
    "check_count": 0,
    "last_check_at": None,
    "last_log": None,
    "next_health_at": None,
    "last_loop_error_signature": None,
    "accounts": {handle: default_account_state() for handle in HANDLES},
}


def now_text():
    return datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")


def log(message):
    line = f"[{now_text()}] {message}"
    state["last_log"] = line
    print(line, flush=True)


def profile_url(handle):
    return f"https://x.com/{handle}"


def format_x_errors(errors):
    parts = []
    for error in errors:
        message = str(error.get("message") or "error")
        code = error.get("code")
        if code is not None:
            parts.append(f"{message} (code {code})")
        else:
            parts.append(message)
    return "; ".join(parts) if parts else "error"


def get_guest_token():
    errors = []

    for api_host in X_API_HOSTS:
        response = session.post(
            f"https://{api_host}/1.1/guest/activate.json",
            headers={
                "Authorization": f"Bearer {BEARER}",
                "Content-Length": "0",
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://x.com",
                "Referer": "https://x.com/",
            },
            timeout=TIMEOUT,
        )

        if response.status_code != 200:
            errors.append(f"{api_host}: HTTP {response.status_code}")
            continue

        token = response.json().get("guest_token")
        if token:
            return token, api_host

        errors.append(f"{api_host}: guest token parse failed")

    raise RuntimeError("guest token failed (" + "; ".join(errors) + ")")


def query_user(handle, token, api_host):
    variables = {
        "screen_name": handle,
        "withGrokTranslatedBio": True,
    }
    features = {
        "hidden_profile_subscriptions_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    }
    url = f"https://{api_host}/graphql/IGgvgiOx4QZndDHuD3x9TQ/UserByScreenName"

    response = session.get(
        url,
        params={
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(features, separators=(",", ":")),
        },
        headers={
            "Authorization": f"Bearer {BEARER}",
            "x-guest-token": token,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "content-type": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://x.com",
            "Referer": "https://x.com/",
        },
        timeout=TIMEOUT,
    )

    try:
        data = response.json()
    except ValueError:
        return f"non_json_http_{response.status_code}"

    if data.get("errors"):
        return format_x_errors(data["errors"])

    user = data.get("data", {}).get("user", {}).get("result")
    if user and user.get("legacy", {}).get("withheld_in_countries"):
        return "withheld"
    if user:
        return "active"
    return "no_user_result"


def check_x_account(handle, acc):
    try:
        token, api_host = get_guest_token()
        status = query_user(handle, token, api_host)
        return status, api_host
    except requests.exceptions.RequestException as err:
        raise RuntimeError(f"X/Twitter API unreachable: {err}") from err


def status_state(status):
    if status == "active":
        return "active"

    lowered = status.lower()
    transient_markers = (
        "rate limit",
        "could not authenticate",
        "temporarily",
        "timeout",
        "non_json_http_",
    )
    if any(marker in lowered for marker in transient_markers):
        return "unknown"

    return "deactivated"


def send_bark(title, message, url):
    response = session.post(
        f"{BARK_SERVER}/{BARK_KEY}",
        json={
            "title": title,
            "body": message,
            "sound": "chime",
            "level": "critical",
            "Icon": "https://pbs.twimg.com/profile_images/2071530886810771456/gwvAIXM2_400x400.jpg",
        },
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 200:
        raise RuntimeError(f"Bark error: {data.get('message')}")


NTFY_PRIORITY_MAP = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def send_ntfy(title, message, click_url="", tags="information_source", priority="default"):
    # Published via ntfy's JSON API (UTF-8 body) rather than HTTP headers.
    # Headers are Latin-1 only, so an emoji title would break the old
    # header-based approach with a UnicodeEncodeError.
    payload = {
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "priority": NTFY_PRIORITY_MAP.get(priority, 3),
    }
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if click_url:
        payload["click"] = click_url

    response = session.post(
        "https://ntfy.sh/",
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def send_ntfy_safe(title, message, click_url="", tags="information_source", priority="default"):
    try:
        send_ntfy(title, message, click_url, tags, priority)
    except Exception as err:
        log(f"ntfy failed: {err}")


def send_bark_safe(title, message, url):
    try:
        send_bark(title, message, url)
    except Exception as err:
        log(f"Bark failed: {err}")


def notify_startup(started_at):
    send_ntfy_safe(
        "🚀 X WatcherRemote started",
        (
            "Status: Online\n"
            f"Started at: {started_at}\n"
            f"Monitored accounts: [{' - '.join('@' + h for h in HANDLES)}]\n"
            f"Check interval: {INTERVAL_SECONDS}s"
        ),
        profile_url(HANDLES[0]),
        tags="rocket",
        priority="default",
    )


def notify_health():
    any_error = any(acc["last_error_signature"] for acc in state["accounts"].values())

    lines = [
        f"Overall status: {'DEGRADED' if any_error else 'OK'}",
        "",
        f"Time: {now_text()}",
        f"Checks completed: {state['check_count']}",
    ]

    for handle in HANDLES:
        acc = state["accounts"][handle]
        watching_for = "deactivation" if acc["last_state"] == "active" else "activation"
        status_line = (
            f"  Status: ERROR since {acc['error_since']} ({acc['last_error_signature']})"
            if acc["last_error_signature"] else "  Status: OK"
        )
        lines.append(
            "\n"
            f"@{handle}\n"
            f"{status_line}\n"
            f"  Current: {acc['last_state']}\n"
            f"  Watching for: {watching_for}\n"
            f"  Last response: {acc['last_status']}\n"
            f"  Healthy since: {acc['healthy_since']}\n"
        )

    send_ntfy_safe(
        f"{'⚠️' if any_error else '✅'} X WatcherRemote Status: {'DEGRADED' if any_error else 'OK'}",
        "\n".join(lines),
        profile_url(HANDLES[0]),
        tags=("warning" if any_error else "white_check_mark"),
        priority=("high" if any_error else "default"),
    )


def notify_error(handle, error_text):
    send_ntfy_safe(
        f"⚠️ X WatcherRemote ERROR",
        (
            "Severity: Error\n"
            f"Account: @{handle}\n"
            f"Time: {now_text()}\n"
            f"Check #: {state['check_count']}\n"
            f"Details: {error_text}"
        ),
        profile_url(handle),
        tags="rotating_light",
        priority="urgent",
    )


def notify_recovered(handle, error_since, last_error_signature):
    send_ntfy_safe(
        f"🟢 X WatcherRemote RECOVERED",
        (
            "Severity: Recovered\n"
            f"Account: @{handle}\n"
            f"Down since: {error_since}\n"
            f"Recovered at: {now_text()}\n"
            f"Last error: {last_error_signature}"
        ),
        profile_url(handle),
        tags="white_check_mark",
        priority="default",
    )


def notify_activated(handle, detected_at):
    url = profile_url(handle)
    title = f"😎😎 @{handle} ONLINE"
    message = (
        f"Account: : @{handle}\n"
        "Event: Account activated\n"
        f"URL: {url}\n"
        f"Detected at: {detected_at}"
    )
    send_ntfy_safe(title, message, url, tags="white_check_mark", priority="urgent")
    send_bark_safe(title, message, url)


def notify_deactivated(handle, status, detected_at):
    url = profile_url(handle)
    title = f"❌❌ @{handle} OFFLINE"
    message = (
        f"Account: @{handle}\n"
        "Event: Account deactivated\n"
        f"Response: {status}\n"
        f"Detected at: {detected_at}"
    )
    send_ntfy_safe(title, message, url, tags="rotating_light", priority="urgent")
    send_bark_safe(title, message, url)


def enter_error_state(handle, acc, signature):
    if signature != acc["last_error_signature"]:
        notify_error(handle, signature)
        acc["last_error_signature"] = signature
        if acc["error_since"] is None:
            acc["error_since"] = now_text()
        acc["healthy_since"] = None
        state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS


def leave_error_state(handle, acc):
    if acc["last_error_signature"] is not None:
        notify_recovered(handle, acc["error_since"], acc["last_error_signature"])
    acc["last_error_signature"] = None
    acc["error_since"] = None


def handle_error(handle, acc, error_text):
    enter_error_state(handle, acc, error_text)
    acc["last_error"] = error_text
    log(f"[{handle}] Error: {error_text}")


def check_one_handle(handle):
    acc = state["accounts"][handle]

    try:
        status, api_host = check_x_account(handle, acc)
        current_state = status_state(status)
        acc["last_status"] = status
        acc["last_api_host"] = api_host
        acc["last_error"] = None

        if current_state == "unknown":
            signature = f"X/Twitter API returned: {status}"
            enter_error_state(handle, acc, signature)
            log(f"[{handle}] Unknown API response: {status}")
            return

        was_error = acc["last_error_signature"] is not None
        leave_error_state(handle, acc)
        if was_error or acc["healthy_since"] is None:
            acc["healthy_since"] = now_text()
            state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS

        if acc["last_state"] is None:
            acc["last_state"] = current_state
            target = "deactivation" if current_state == "active" else "activation"
            log(f"[{handle}] Initial state: {current_state} ({status}). Waiting for {target}.")
        elif acc["last_state"] != current_state:
            detected_at = now_text()
            if current_state == "active":
                notify_activated(handle, detected_at)
            else:
                notify_deactivated(handle, status, detected_at)
            acc["last_state"] = current_state

    except Exception as err:
        handle_error(handle, acc, str(err))


def watcher_loop():
    try:
        state["running"] = True
        state["started_at"] = now_text()
        state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS
        for handle in HANDLES:
            acc = state["accounts"][handle]
            acc["healthy_since"] = state["started_at"]
        notify_startup(state["started_at"])
        log("Watcher loop started.")

        while True:
            try:
                state["check_count"] += 1
                state["last_check_at"] = now_text()

                for handle in HANDLES:
                    check_one_handle(handle)

                if state["next_health_at"] is not None and time.monotonic() >= state["next_health_at"]:
                    notify_health()
                    state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS
            except Exception as err:
                # check_one_handle already guards per-account errors; this is a
                # last-resort net so a bug here can never silently kill the thread.
                signature = f"loop crash: {err}"
                if signature != state["last_loop_error_signature"]:
                    send_ntfy_safe(
                        "🛑 X WatcherRemote CRITICAL: Internal error",
                        (
                            "Severity: Critical\n"
                            "Component: Watcher loop\n"
                            f"Time: {now_text()}\n"
                            f"Details: {err}\n"
                            f"Recovery: Retrying every {INTERVAL_SECONDS}s"
                        ),
                        profile_url(HANDLES[0]),
                        tags="rotating_light",
                        priority="urgent",
                    )
                    state["last_loop_error_signature"] = signature
                log(f"Watcher loop crash: {err}")
            else:
                state["last_loop_error_signature"] = None

            time.sleep(INTERVAL_SECONDS)
    finally:
        # Only reached if something escapes the inner loop entirely.
        state["running"] = False
        log("Watcher loop exited unexpectedly.")


def acquire_singleton_lock():
    """Try to become the one process that runs the watcher.

    Returns True if this process won the lock (should start the watcher),
    False if another worker process already holds it.
    """
    global _singleton_lock_file
    try:
        _singleton_lock_file = open(WATCHER_LOCK_PATH, "w")
        fcntl.flock(_singleton_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _singleton_lock_file.write(str(os.getpid()))
        _singleton_lock_file.flush()
        return True
    except OSError:
        # Another worker process already holds the flock.
        if _singleton_lock_file:
            _singleton_lock_file.close()
            _singleton_lock_file = None
        return False


def ensure_watcher_started():
    global watcher_started
    with watcher_lock:
        if watcher_started:
            return
        watcher_started = True

        if not acquire_singleton_lock():
            log("Watcher already running in another worker process; skipping start here.")
            return

        thread = threading.Thread(target=watcher_loop, name="xwatcher", daemon=True)
        thread.start()


@app.get("/")
def index():
    ensure_watcher_started()
    return jsonify(
        {
            "ok": True,
            "service": "xwatcher",
            "handles": HANDLES,
            "running": state["running"],
            "check_count": state["check_count"],
            "last_check_at": state["last_check_at"],
            "last_log": state["last_log"],
            "accounts": state["accounts"],
        }
    )


@app.get("/health")
def health():
    ensure_watcher_started()
    any_error = any(acc["last_error"] for acc in state["accounts"].values())

    stale = False
    if state["last_check_at"]:
        last_dt = datetime.strptime(state["last_check_at"], "%Y-%m-%d %I:%M:%S %p")
        stale = (datetime.now() - last_dt).total_seconds() > INTERVAL_SECONDS * 6

    ok = state["running"] and not stale
    return jsonify(
        {
            "ok": ok,
            "running": state["running"],
            "has_error": any_error,
            "stale": stale,
            "last_check_at": state["last_check_at"],
        }
    )


ensure_watcher_started()


if __name__ == "__main__":
    ensure_watcher_started()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
