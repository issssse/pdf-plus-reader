from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import requests


class MathpixError(RuntimeError):
    pass


class MathpixClient:
    def __init__(
        self,
        app_id: str,
        app_key: str,
        base_url: str = "https://api.mathpix.com",
        session: requests.Session | None = None,
        request_timeout: float = 90,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {"app_id": app_id, "app_key": app_key}
        self.request_timeout = request_timeout

    def _json(self, response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = {"body": response.text[:1000]}
        if not response.ok:
            raise MathpixError(f"Mathpix HTTP {response.status_code}: {data}")
        if not isinstance(data, dict):
            raise MathpixError(f"Unexpected Mathpix response: {data!r}")
        return data

    def submit(self, document: Path, options: dict[str, Any]) -> str:
        with document.open("rb") as stream:
            response = self.session.post(
                f"{self.base_url}/v3/pdf",
                headers=self.headers,
                files={"file": (document.name, stream, "application/pdf")},
                data={"options_json": json.dumps(options)},
                timeout=self.request_timeout,
            )
        data = self._json(response)
        pdf_id = data.get("pdf_id")
        if not pdf_id:
            raise MathpixError(f"Submission response has no pdf_id: {data}")
        return str(pdf_id)

    def status(self, pdf_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/v3/pdf/{pdf_id}",
            headers=self.headers,
            timeout=self.request_timeout,
        )
        return self._json(response)

    def wait(
        self,
        pdf_id: str,
        requested_formats: list[str],
        poll_seconds: float = 5,
        max_wait_seconds: float = 1800,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_seconds
        while True:
            state = self.status(pdf_id)
            if progress:
                progress(state)
            status = state.get("status")
            if status == "error":
                raise MathpixError(f"Mathpix processing failed: {state}")
            conversions = state.get("conversion_status") or {}
            formats_done = all(
                isinstance(conversions.get(fmt), dict)
                and conversions[fmt].get("status") == "completed"
                for fmt in requested_formats
            )
            if status == "completed" and formats_done:
                return state
            failed = {
                fmt: conversions.get(fmt)
                for fmt in requested_formats
                if isinstance(conversions.get(fmt), dict)
                and conversions[fmt].get("status") == "error"
            }
            if failed:
                raise MathpixError(f"Mathpix conversion failed: {failed}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Mathpix job {pdf_id} did not finish within {max_wait_seconds}s")
            time.sleep(poll_seconds)

    def download(self, pdf_id: str, extension: str, destination: Path) -> None:
        response = self.session.get(
            f"{self.base_url}/v3/pdf/{pdf_id}.{extension}",
            headers=self.headers,
            timeout=self.request_timeout,
        )
        if response.status_code == 202:
            raise MathpixError(f"Output {extension} is still processing")
        if not response.ok:
            self._json(response)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(response.content)
        temporary.replace(destination)

    def submit_conversion(
        self,
        mmd: str,
        formats: dict[str, bool],
        conversion_options: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {"mmd": mmd, "formats": formats}
        if conversion_options:
            payload["conversion_options"] = conversion_options
        if metadata:
            payload["metadata"] = metadata
        response = self.session.post(
            f"{self.base_url}/v3/converter",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            timeout=self.request_timeout,
        )
        data = self._json(response)
        conversion_id = data.get("conversion_id")
        if not conversion_id:
            raise MathpixError(f"Submission response has no conversion_id: {data}")
        return str(conversion_id)

    def conversion_status(self, conversion_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/v3/converter/{conversion_id}",
            headers=self.headers,
            timeout=self.request_timeout,
        )
        return self._json(response)

    def wait_conversion(
        self,
        conversion_id: str,
        requested_formats: list[str],
        poll_seconds: float = 5,
        max_wait_seconds: float = 1800,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max_wait_seconds
        while True:
            state = self.conversion_status(conversion_id)
            if progress:
                progress(state)
            conversions = state.get("conversion_status") or {}
            failed = {
                fmt: conversions.get(fmt)
                for fmt in requested_formats
                if isinstance(conversions.get(fmt), dict)
                and conversions[fmt].get("status") == "error"
            }
            if state.get("status") == "error" or failed:
                raise MathpixError(f"Mathpix conversion failed: {state}")
            if all(
                isinstance(conversions.get(fmt), dict)
                and conversions[fmt].get("status") == "completed"
                for fmt in requested_formats
            ):
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Mathpix conversion {conversion_id} did not finish within {max_wait_seconds}s"
                )
            time.sleep(poll_seconds)

    def download_conversion(
        self, conversion_id: str, extension: str, destination: Path
    ) -> None:
        response = self.session.get(
            f"{self.base_url}/v3/converter/{conversion_id}.{extension}",
            headers=self.headers,
            timeout=self.request_timeout,
        )
        if response.status_code == 202:
            raise MathpixError(f"Output {extension} is still processing")
        if not response.ok:
            self._json(response)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(response.content)
        temporary.replace(destination)
