import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("dispatcher.client")


class WorkerClient:
    """HTTP client for communicating with worker servers."""

    def __init__(
        self,
        worker_token: str,
        timeout: float = 30.0,
        async_client: Optional[httpx.AsyncClient] = None,
    ):
        self.worker_token = worker_token
        self.timeout = timeout
        self._custom_client = async_client

    def _get_headers(self) -> Dict[str, str]:
        return {"X-Worker-Token": self.worker_token}

    def _client(self) -> httpx.AsyncClient:
        if self._custom_client:
            return self._custom_client
        return httpx.AsyncClient(timeout=self.timeout)

    async def execute(
        self,
        worker_url: str,
        task_id: str,
        operation_type: str,
        params: dict,
        duration_sec: Optional[float],
        input_file_path: str,
        logo_path: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Stream video and optional logo to worker's /execute endpoint.
        """
        url = f"{worker_url.rstrip('/')}/execute"
        headers = self._get_headers()

        data = {
            "task_id": task_id,
            "operation_type": operation_type,
            "params": json.dumps(params) if params else "{}",
            "duration_sec": str(duration_sec or 0.0),
        }

        # Open input file streams
        files = {}
        file_handles = []
        try:
            f_video = open(input_file_path, "rb")
            file_handles.append(f_video)
            filename = os.path.basename(input_file_path)
            files["file"] = (filename, f_video, "video/mp4")

            if logo_path and os.path.exists(logo_path):
                f_logo = open(logo_path, "rb")
                file_handles.append(f_logo)
                files["logo"] = (os.path.basename(logo_path), f_logo, "image/png")

            client = self._client()
            should_close = client != self._custom_client
            try:
                res = await client.post(url, data=data, files=files, headers=headers)
                try:
                    res_data = res.json()
                except Exception:
                    res_data = {"text": res.text}
                return res.status_code, res_data
            finally:
                if should_close:
                    await client.aclose()
        finally:
            for fh in file_handles:
                try:
                    fh.close()
                except Exception:
                    pass

    async def get_progress(self, worker_url: str, task_id: str) -> Tuple[int, Dict[str, Any]]:
        url = f"{worker_url.rstrip('/')}/tasks/{task_id}/progress"
        client = self._client()
        should_close = client != self._custom_client
        try:
            res = await client.get(url, headers=self._get_headers())
            try:
                res_data = res.json()
            except Exception:
                res_data = {"text": res.text}
            return res.status_code, res_data
        finally:
            if should_close:
                await client.aclose()

    async def cancel(self, worker_url: str, task_id: str) -> Tuple[int, Dict[str, Any]]:
        url = f"{worker_url.rstrip('/')}/tasks/{task_id}/cancel"
        client = self._client()
        should_close = client != self._custom_client
        try:
            res = await client.post(url, headers=self._get_headers())
            try:
                res_data = res.json()
            except Exception:
                res_data = {"text": res.text}
            return res.status_code, res_data
        finally:
            if should_close:
                await client.aclose()

    async def download_result(self, worker_url: str, task_id: str, target_file_path: str) -> bool:
        url = f"{worker_url.rstrip('/')}/tasks/{task_id}/result"
        client = self._client()
        should_close = client != self._custom_client
        try:
            os.makedirs(os.path.dirname(os.path.abspath(target_file_path)), exist_ok=True)
            async with client.stream("GET", url, headers=self._get_headers()) as res:
                if res.status_code != 200:
                    logger.warning("Download result for %s returned status %d", task_id, res.status_code)
                    return False

                with open(target_file_path, "wb") as out_f:
                    async for chunk in res.aiter_bytes(chunk_size=1024 * 1024):
                        out_f.write(chunk)
            return True
        except Exception as exc:
            logger.error("Failed downloading result for %s from %s: %s", task_id, worker_url, exc)
            if os.path.exists(target_file_path):
                try:
                    os.remove(target_file_path)
                except OSError:
                    pass
            return False
        finally:
            if should_close:
                await client.aclose()

    async def delete_task(self, worker_url: str, task_id: str) -> bool:
        url = f"{worker_url.rstrip('/')}/tasks/{task_id}"
        client = self._client()
        should_close = client != self._custom_client
        try:
            res = await client.delete(url, headers=self._get_headers())
            return res.status_code == 200
        except Exception as exc:
            logger.warning("Failed to delete task %s on %s: %s", task_id, worker_url, exc)
            return False
        finally:
            if should_close:
                await client.aclose()

    async def get_status(self, worker_url: str, timeout: float = 2.0) -> Tuple[int, Dict[str, Any]]:
        url = f"{worker_url.rstrip('/')}/status"
        if self._custom_client:
            client = self._custom_client
            should_close = False
        else:
            client = httpx.AsyncClient(timeout=timeout)
            should_close = True

        try:
            res = await client.get(url, headers=self._get_headers())
            try:
                res_data = res.json()
            except Exception:
                res_data = {"text": res.text}
            return res.status_code, res_data
        finally:
            if should_close:
                await client.aclose()

