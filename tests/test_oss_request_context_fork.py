"""Request metadata and access logs work without OpenTelemetry instrumentation."""

import logging
import logging.config
import unittest
from uuid import UUID
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from keep.api.logging import CONFIG
from keep.api.middlewares import LoggingMiddleware


class OssRequestContextTest(unittest.TestCase):
    def app(self, supplied_trace=None):
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)
        if supplied_trace:
            @app.middleware("http")
            async def tracing(request, call_next):
                request.state.trace_id = supplied_trace
                return await call_next(request)

        @app.post("/ingest")
        def ingest(request: Request):
            return {"trace_id": request.state.trace_id, "tenant_id": request.state.tenant_id}
        return app

    def test_without_instrumentation_each_request_has_a_trace(self):
        with patch("keep.api.middlewares._extract_identity", return_value="keep"):
            client = TestClient(self.app())
            first, second = client.post("/ingest").json(), client.post("/ingest").json()
        self.assertEqual(first["tenant_id"], "keep")
        self.assertEqual(str(UUID(first["trace_id"])), first["trace_id"])
        self.assertNotEqual(first["trace_id"], second["trace_id"])

    def test_instrumentation_trace_is_preserved(self):
        with patch("keep.api.middlewares._extract_identity", return_value="keep"):
            result = TestClient(self.app("otel-trace")).post("/ingest").json()
        self.assertEqual(result["trace_id"], "otel-trace")

    def test_access_formatter_accepts_record_without_otel_fields(self):
        formatter = logging.config.DictConfigurator(CONFIG).configure_formatter(CONFIG["formatters"]["uvicorn_access"])
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "GET /alerts 202", (), None)
        self.assertIn("GET /alerts 202", formatter.format(record))


if __name__ == "__main__":
    unittest.main()
