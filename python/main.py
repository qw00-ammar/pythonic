import itertools
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import quote

import requests
from flask import Flask, jsonify


# --- HARDCODED CONFIG --------------------------------------------------------
HANDLES = ["UooU672514", "i9yl9"]
INTERVAL_SECONDS = 8
BARK_KEY = "aAQmJDszVrdbc9braKD8am"
BARK_SERVER = "https://api.day.app"
NTFY_TOPIC = "JamilaActivatedHerXAccount"
NTFY_HEALTH_INTERVAL_SECONDS = 60*60

BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
PROXY_USERNAME = "c6gwek0u-y8pgnsw"
PROXY_PASSWORD = "m8ytsw4bqg"
TIMEOUT = 6

X_API_HOSTS = ("api.x.com", "api.twitter.com")
PROXIES = [
    ("Netherlands/Amsterdam", "nl-025.totallyacdn.com", 443),
    ("Germany/Frankfurt", "de-037.totallyacdn.com", 443),
    ("Netherlands/Amsterdam", "nl-024.totallyacdn.com", 443),
    ("Netherlands/Amsterdam", "nl-046.totallyacdn.com", 443),
    ("United Kingdom/London", "uk-050.totallyacdn.com", 443),
    ("Germany/Frankfurt", "de-038.totallyacdn.com", 443),
    ("France/Paris", "fr-033.totallyacdn.com", 443),
    ("Germany/Frankfurt", "de-036.totallyacdn.com", 443),
    ("Netherlands/Amsterdam", "nl-047.totallyacdn.com", 443),
    ("United Kingdom/London", "uk-040.totallyacdn.com", 443),
    ("Romania", "ro-019.totallyacdn.com", 443), 
    ("Romania", "ro-017.totallyacdn.com", 443),
    
]
# ----------------------------------------------------------------------------


app = Flask(__name__)
proxy_cycle = itertools.cycle(PROXIES)
watcher_lock = threading.Lock()
watcher_started = False


def default_account_state():
    return {
        "last_state": None,
        "last_status": None,
        "last_api_host": None,
        "last_proxy": None,
        "last_error": None,
        "last_error_signature": None,
        "healthy_since": None,
    }


state = {
    "running": False,
    "started_at": None,
    "check_count": 0,
    "last_check_at": None,
    "last_log": None,
    "next_health_at": None,
    "accounts": {handle: default_account_state() for handle in HANDLES},
}


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{now_text()}] {message}"
    state["last_log"] = line
    print(line, flush=True)


def profile_url(handle):
    return f"https://x.com/{handle}"


def proxy_dict(hostname, port):
    username = quote(PROXY_USERNAME, safe="")
    password = quote(PROXY_PASSWORD, safe="")
    proxy_url = f"https://{username}:{password}@{hostname}:{port}"
    return {"http": proxy_url, "https": proxy_url}


def proxy_label(proxy):
    location, hostname, port = proxy
    return f"{location} ({hostname}:{port})"


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


def get_guest_token(proxy):
    _, hostname, port = proxy
    errors = []

    for api_host in X_API_HOSTS:
        response = requests.post(
            f"https://{api_host}/1.1/guest/activate.json",
            headers={
                "Authorization": f"Bearer {BEARER}",
                "Content-Length": "0",
                "User-Agent": "Mozilla/5.0",
                "Origin": "https://x.com",
                "Referer": "https://x.com/",
            },
            proxies=proxy_dict(hostname, port),
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


def query_user(handle, token, proxy, api_host):
    _, hostname, port = proxy
    variables = {
        "screen_name": handle,
        "withGrokTranslatedBio": True,
    }
    features = {
        "hidden_profile_subscriptions_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    }
    url = f"https://{api_host}/graphql/IGgvgiOx4QZndDHuD3x9TQ/UserByScreenName"

    response = requests.get(
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
        proxies=proxy_dict(hostname, port),
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
    proxy = next(proxy_cycle)
    acc["last_proxy"] = proxy_label(proxy)
    try:
        token, api_host = get_guest_token(proxy)
        status = query_user(handle, token, proxy, api_host)
        return status, api_host
    except requests.exceptions.Timeout as err:
        raise RuntimeError(f"proxy timeout via {acc['last_proxy']}: {err}") from err
    except requests.exceptions.RequestException as err:
        raise RuntimeError(f"proxy request failed via {acc['last_proxy']}: {err}") from err


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
    response = requests.post(
        f"{BARK_SERVER}/{BARK_KEY}",
        json={
            "title": title,
            "body": message,
            "url": url,
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


def send_ntfy(title, message, click_url="", tags="tada,fire,bird", priority="default"):
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": title,
        "Tags": tags,
        "Priority": priority,
    }

    response = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def send_ntfy_safe(title, message, click_url="", tags="tada,fire,bird", priority="default"):
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
        f"Watcher started: {', '.join('@' + h for h in HANDLES)}",
        (
            f"Diploi started at {started_at}.\n"
            f"Checking Twitter api every {INTERVAL_SECONDS}s\n"
            f"Watching: {', '.join('@' + h for h in HANDLES)}\n"
            "Twitter API health notification every 20 minutes."
        ),
        profile_url(HANDLES[0]),
        tags="rocket,bell",
        priority="high",
    )


def notify_health():
    lines = [
        "Everything is OK. No current errors.",
        "Still working and X/Twitter API responded successfully.",
        "",
        f"Time: {now_text()}",
        f"Checks completed: {state['check_count']}",
    ]

    for handle in HANDLES:
        acc = state["accounts"][handle]
        watching_for = "deactivation" if acc["last_state"] == "active" else "activation"
        lines.append(
            "\n"
            f"@{handle}\n"
            f"  Current: {acc['last_state']}\n"
            f"  Watching for: {watching_for}\n"
            f"  Last response: {acc['last_status']}\n"
            f"  Healthy since: {acc['healthy_since']}\n"
            f"  API host: {acc['last_api_host']}\n"
            f"  Last proxy: {acc['last_proxy']}"
        )

    send_ntfy_safe(
        f"Watcher OK: {', '.join('@' + h for h in HANDLES)}",
        "\n".join(lines),
        profile_url(HANDLES[0]),
        tags="white_check_mark,bell",
        priority="default",
    )


def notify_error(handle, error_text):
    send_ntfy_safe(
        f"Watcher ERROR: @{handle}",
        (
            "Error detected immediately sent.\n\n"
            f"Time: {now_text()}\n"
            f"Check: #{state['check_count']}\n"
            f"Error: {error_text}"
        ),
        profile_url(handle),
        tags="warning,rotating_light",
        priority="urgent",
    )


def notify_activated(handle, detected_at):
    url = profile_url(handle)
    title = f"@{handle} "
    message = f"Online!!\n\n{url}\n\nDetected at: {detected_at}"
    send_ntfy_safe(title, message, url, tags="tada,fire,bird", priority="urgent")
    send_bark_safe(
        f"@{handle}",
        f"\n😎😎 Online!!\n\n{url}\n\nDetected at: {detected_at}",
        url,
    )


def notify_deactivated(handle, status, detected_at):
    url = profile_url(handle)
    title = f"@{handle} "
    message = (
        "❌❌ Offline!!\n\n"
        f"Response: {status}\n"
        f"Detected at: {detected_at}"
    )
    send_ntfy_safe(title, message, url, tags="warning,bell", priority="urgent")
    send_bark_safe(
        f"@{handle}",
        f"\n❌❌ Offline!!\n\nResponse: {status}\nDetected at: {detected_at}",
        url,
    )


def handle_error(handle, acc, error_text):
    signature = f"Exception: {error_text}"
    if signature != acc["last_error_signature"]:
        notify_error(handle, signature)
        acc["last_error_signature"] = signature
        acc["healthy_since"] = None
        state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS
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
            if signature != acc["last_error_signature"]:
                notify_error(handle, signature)
                acc["last_error_signature"] = signature
                acc["healthy_since"] = None
                state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS
            log(f"[{handle}] Unknown API response: {status}")
            return

        if acc["last_error_signature"] is not None or acc["healthy_since"] is None:
            acc["healthy_since"] = now_text()
            state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS
        acc["last_error_signature"] = None

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
    state["running"] = True
    state["started_at"] = now_text()
    state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS
    for handle in HANDLES:
        acc = state["accounts"][handle]
        acc["healthy_since"] = state["started_at"]
    notify_startup(state["started_at"])
    log("Watcher loop started.")

    while True:
        state["check_count"] += 1
        state["last_check_at"] = now_text()

        for handle in HANDLES:
            check_one_handle(handle)

        if state["next_health_at"] is not None and time.monotonic() >= state["next_health_at"]:
            notify_health()
            state["next_health_at"] = time.monotonic() + NTFY_HEALTH_INTERVAL_SECONDS

        time.sleep(INTERVAL_SECONDS)


def ensure_watcher_started():
    global watcher_started
    with watcher_lock:
        if watcher_started:
            return
        watcher_started = True
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
    return jsonify({"ok": True, "running": state["running"], "has_error": any_error})


ensure_watcher_started()


if __name__ == "__main__":
    ensure_watcher_started()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
