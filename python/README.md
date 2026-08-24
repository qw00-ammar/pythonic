# Diploi Hardcoded X Watcher

Use this for Diploi's 14-day no-card credit window.

## Which Diploi Component To Pick

Pick **Flask** if you can.

If you already picked **Python**, this package still includes a `Dockerfile`, so you can use it as a Python backend service too.

## What To Upload

Put these files in your Diploi component folder:

- `main.py`
- `requirements.txt`
- `Dockerfile`

Everything important is hardcoded inside `main.py`.

## What It Does

- Runs a Flask web service so Diploi keeps it online.
- Starts the watcher in a background thread.
- Checks every 4 seconds.
- Rotates through the hardcoded top 10 Windscribe proxies.
- Sends ntfy on startup.
- Sends ntfy every 30 minutes when everything is OK.
- Sends ntfy immediately on errors.
- Sends ntfy + Pushover on activation and deactivation.

## Health URLs

After deploy, open:

```text
https://YOUR_DIPLOI_URL/
```

or:

```text
https://YOUR_DIPLOI_URL/health
```

The root URL returns the current watcher state as JSON.

## Important

This file is hardcoded with tokens and credentials. Keep the repository private.
