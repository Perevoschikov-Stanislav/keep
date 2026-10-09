"""DB-backed lifecycle worker; safe to run on multiple existing API workers."""

import asyncio
import logging
import time

from sqlmodel import Session

from keep.api.bl.silences_delivery_bl import materialize_time_transitions
from keep.api.core.incident_notifications import IncidentNotificationWorker
from keep.api.core import db
from keep.api.core.incident_configuration import configuration_scope
from keep.api.core.silence_integrations import get_silence_integrations

logger = logging.getLogger(__name__)


def process_silences_once(settings):
    if settings is not None:
        return IncidentNotificationWorker(db.engine, settings).run_once()
    with Session(db.engine) as session:
        return {"transitions": materialize_time_transitions(session), "delivered": 0}


async def async_process_silences():
    while True:
        started = time.monotonic()
        interval = 5
        try:
            with configuration_scope():
                settings = get_silence_integrations()
                interval = settings.dispatch["scan_interval_seconds"] if settings else 5
                await asyncio.to_thread(process_silences_once, settings)
        except Exception:
            # Exception text can contain HTTP credential values or database event contents.
            logger.error("Silence lifecycle worker failed; retained deliveries will be retried")
        await asyncio.sleep(max(0.1, interval - (time.monotonic() - started)))
