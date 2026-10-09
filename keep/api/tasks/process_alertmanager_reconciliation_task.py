"""Background worker task for Alertmanager reconciliation."""

import asyncio
import json
import logging
import os
import time

from keep.api.core.alertmanager_reconciliation import AlertmanagerReconciler
from keep.api.core.dependencies import SINGLE_TENANT_UUID


logger = logging.getLogger(__name__)

_RECONCILER: AlertmanagerReconciler | None = None


def get_reconciler() -> AlertmanagerReconciler | None:
    """Return the singleton reconciler instance if configured, or None if disabled."""
    global _RECONCILER
    setting = os.environ.get("KEEP_ALERTMANAGER_RECONCILER_ENABLED", "").lower()
    if setting in ("false", "0", "no"):
        return None
    if _RECONCILER is not None:
        return _RECONCILER

    url = os.environ.get("KEEP_ALERTMANAGER_URL") or os.environ.get("ALERTMANAGER_URL")
    enabled = setting in ("true", "1", "yes")

    if not url and not enabled:
        return None

    url = url or "http://alertmanager:9093"
    grace_period = int(os.environ.get("KEEP_ALERTMANAGER_GRACE_PERIOD_SECONDS", 180))
    consecutive_misses = int(os.environ.get("KEEP_ALERTMANAGER_CONSECUTIVE_MISSES", 2))
    drop_ratio = float(os.environ.get("KEEP_ALERTMANAGER_DROP_RATIO_CIRCUIT_BREAKER", 0.5))
    try:
        team_matchers = json.loads(os.environ.get("KEEP_ALERTMANAGER_TEAM_MATCHERS", "{}"))
    except ValueError:
        raise ValueError("KEEP_ALERTMANAGER_TEAM_MATCHERS must be valid JSON") from None
    if not isinstance(team_matchers, dict):
        raise ValueError("KEEP_ALERTMANAGER_TEAM_MATCHERS must be a JSON object")

    _RECONCILER = AlertmanagerReconciler(
        alertmanager_url=url,
        tenant_id=SINGLE_TENANT_UUID,
        grace_period_seconds=grace_period,
        consecutive_misses_required=consecutive_misses,
        drop_ratio_threshold=drop_ratio,
        team_matchers=team_matchers,
        circuit_grace_seconds=int(os.environ.get("KEEP_ALERTMANAGER_CIRCUIT_GRACE_SECONDS", 180)),
        interval_seconds=int(os.environ.get("KEEP_ALERTMANAGER_RECONCILE_INTERVAL_SECONDS", 60)),
        lease_seconds=int(os.environ.get("KEEP_ALERTMANAGER_LEASE_SECONDS", 120)),
    )
    return _RECONCILER


def process_alertmanager_reconciliation_once() -> dict | None:
    """Run a single pass of Alertmanager reconciliation."""
    reconciler = get_reconciler()
    if reconciler is None:
        return None
    return reconciler.reconcile_once()


async def async_process_alertmanager_reconciliation():
    """Periodic loop for Alertmanager reconciliation."""
    interval = int(os.environ.get("KEEP_ALERTMANAGER_RECONCILE_INTERVAL_SECONDS", 60))
    logger.info("Starting Alertmanager reconciliation background worker (interval: %ds)", interval)
    while True:
        started = time.monotonic()
        try:
            await asyncio.to_thread(process_alertmanager_reconciliation_once)
        except Exception as error:
            # HTTP exception text can contain endpoint credentials.
            logger.error("Unexpected error in Alertmanager reconciliation worker (%s)", type(error).__name__)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval - elapsed))
