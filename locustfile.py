import os
import time
import uuid
from locust import HttpUser, between, task


class VideoProcessingUser(HttpUser):
    # Wait between 1 and 2 seconds between tasks
    wait_time = between(1, 2)

    def on_start(self):
        """Register and login at start of user session."""
        self.email = f"load_{uuid.uuid4().hex[:8]}@example.com"
        self.password = "LoadPass123!"

        # Register
        self.client.post(
            "/api/auth/register",
            json={"email": self.email, "password": self.password},
            name="/api/auth/register",
        )

        # Login to receive HttpOnly JWT cookie
        self.client.post(
            "/api/auth/login",
            json={"email": self.email, "password": self.password},
            name="/api/auth/login",
        )

    @task
    def process_video_lifecycle(self):
        """Upload video and poll until processing is completed."""
        video_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "test_short.mp4"))
        if not os.path.exists(video_path):
            # Fallback
            video_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "uploads", "short.mp4"))

        if not os.path.exists(video_path):
            return

        with open(video_path, "rb") as video_file:
            files = {"file": ("test_short.mp4", video_file, "video/mp4")}
            data = {"operation_type": "convert"}

            with self.client.post(
                "/api/tasks",
                files=files,
                data=data,
                catch_response=True,
                name="/api/tasks [POST upload]",
            ) as response:
                if response.status_code not in (200, 201):
                    response.failure(f"Upload failed: {response.status_code} - {response.text}")
                    return

                try:
                    task_data = response.json()
                    task_id = task_data["id"]
                except Exception as e:
                    response.failure(f"Invalid JSON response: {e}")
                    return

        # Poll task until done or timeout (max 60 seconds)
        start_poll = time.time()
        timeout_sec = 60
        while time.time() - start_poll < timeout_sec:
            time.sleep(1.0)
            with self.client.get(
                f"/api/tasks/{task_id}",
                catch_response=True,
                name="/api/tasks/[id] [GET poll]",
            ) as poll_resp:
                if poll_resp.status_code != 200:
                    poll_resp.failure(f"Poll returned status {poll_resp.status_code}")
                    break

                poll_data = poll_resp.json()
                status = poll_data.get("status")

                if status == "done":
                    poll_resp.success()
                    break
                elif status in ("failed", "cancelled"):
                    poll_resp.failure(f"Task finished with status '{status}'")
                    break
