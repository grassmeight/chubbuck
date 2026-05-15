"""Lambda entry point.

Two event shapes land here:

1. Function URL HTTP events (forwarded by CloudFront for /api/*):
       {"version": "2.0", "rawPath": "/api/convert", "headers": {...}, ...}
   → Wrapped through Mangum into the FastAPI app in app/main.py.

2. Keepalive events from EventBridge Scheduler:
       {"warm": true}
   → Short-circuit response in ~50 ms (no body parsing, no FastAPI dispatch).
   → On the first warm tick of each 5-min cycle, self-invoke
     (WARM_POOL_SIZE - 1) parallel copies tagged {"warm": true,
     "fanout_origin": true} so multiple execution environments stay warm in
     parallel. Children skip their own fanout to avoid recursion.

The module body runs once per cold start. We use that to pre-warm
LibreOffice (the first soffice invocation after a cold start spends ~5-10s
creating its user profile in /tmp), so the first real request doesn't pay
that cost.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Mapping

import boto3
from mangum import Mangum

# Defense-in-depth: also set in the Dockerfile, but a misconfigured Lambda
# env block could drop them. LibreOffice MUST write its profile to /tmp.
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("TMPDIR", "/tmp")

from app.main import app  # noqa: E402 — must come after HOME/TMPDIR are set


# ---------------------------------------------------------------------------
# Cold-start pre-warm: convert the template once into /tmp, just to seed
# LibreOffice's user profile + font cache. The output PDF is discarded.
# ---------------------------------------------------------------------------

def _prewarm_libreoffice() -> None:
    task_root = os.environ.get("LAMBDA_TASK_ROOT", "/var/task")
    template = os.path.join(task_root, "טבלת ניתוחים ריקה.xlsx")
    if not os.path.exists(template):
        print(f"prewarm: template not found at {template}", file=sys.stderr)
        return
    started = time.monotonic()
    try:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf",
             "--outdir", "/tmp/_prewarm", template],
            # Full libreoffice install + Hebrew fonts can push the first
            # cold-start conversion to ~50s. Keep this generous; the
            # function-level timeout is the real ceiling.
            timeout=75,
            check=False,
            capture_output=True,
        )
    except Exception as exc:
        print(f"prewarm: failed: {exc}", file=sys.stderr)
        return
    elapsed = time.monotonic() - started
    print(f"prewarm: done in {elapsed:.1f}s", file=sys.stderr)


# Lambda's INIT phase has a hard 10s ceiling (separate from the function
# timeout). The LibreOffice pre-warm takes 5-15s, which would blow that
# budget and cause the env to be killed before serving its first request.
# Defer to the first invocation instead — runs once per execution env, in
# the keepalive path (which has no user waiting on it) when possible.
_lo_warmed = False


def _ensure_lo_warmed() -> None:
    global _lo_warmed
    if _lo_warmed:
        return
    _lo_warmed = True
    _prewarm_libreoffice()


# ---------------------------------------------------------------------------
# Mangum adapter. lifespan="off" because the FastAPI app doesn't have any
# startup/shutdown handlers (and Lambda's per-request lifecycle wouldn't
# hold an ASGI lifespan context across invocations anyway).
# ---------------------------------------------------------------------------

_mangum = Mangum(app, lifespan="off")


# ---------------------------------------------------------------------------
# Warm-pool fan-out
# ---------------------------------------------------------------------------

_WARM_POOL_SIZE = max(1, int(os.environ.get("WARM_POOL_SIZE", "1")))

# boto3.client is expensive (~200ms first call). Build it lazily on first
# fanout, then reuse across invocations within the same execution env.
_lambda_client: Any = None


def _get_lambda_client():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


def _fanout_warm(context) -> None:
    """Self-invoke (WARM_POOL_SIZE - 1) copies in parallel.

    Each child gets fanout_origin=true so it knows NOT to fan out further.
    Without that flag we'd recurse exponentially every 5 minutes.
    """
    if _WARM_POOL_SIZE <= 1:
        return
    client = _get_lambda_client()
    payload = json.dumps({"warm": True, "fanout_origin": True}).encode("utf-8")
    for _ in range(_WARM_POOL_SIZE - 1):
        try:
            client.invoke(
                FunctionName=context.function_name,
                InvocationType="Event",  # async, fire-and-forget
                Payload=payload,
            )
        except Exception as exc:
            print(f"fanout: invoke failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Event-shape detection
# ---------------------------------------------------------------------------

def _is_warm_ping(event: Mapping[str, Any]) -> bool:
    return bool(event.get("warm"))


def _is_function_url_event(event: Mapping[str, Any]) -> bool:
    # Function URL and API Gateway HTTP API v2 share this shape.
    return event.get("version") == "2.0" and "rawPath" in event


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    if _is_warm_ping(event):
        # Keepalive is the right place to amortize the LibreOffice pre-warm:
        # there's no user waiting on this invocation, and after the first
        # keepalive the env is fully warm for real requests.
        _ensure_lo_warmed()
        if not event.get("fanout_origin"):
            # Top-level keepalive from the scheduler — fan out warmth to
            # the rest of the warm pool.
            _fanout_warm(context)
        return {"statusCode": 200, "body": "warm"}

    if _is_function_url_event(event):
        # Best-effort: if a real request lands on a freshly-cold env before
        # the first keepalive ticked, this user pays the LO cold-start tax.
        _ensure_lo_warmed()
        return _mangum(event, context)

    # Unknown event shape. Surface it loudly in CloudWatch instead of
    # silently 200-ing with an empty body.
    return {
        "statusCode": 400,
        "body": json.dumps({
            "error": "unrecognized event shape",
            "keys": sorted(event.keys()) if isinstance(event, Mapping) else None,
        }),
    }
