import logging
import os
import sys
import time

# Ensure backend root is in sys.path
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import docker
from autoscaler.scaler import Autoscaler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (autoscaler) %(message)s",
)
logger = logging.getLogger("autoscaler")


def run_autoscaler():
    """Main autoscaler service daemon loop."""
    min_workers = int(os.getenv("MIN_WORKERS", "2"))
    max_workers = int(os.getenv("MAX_WORKERS", "4"))
    per_worker = int(os.getenv("PER_WORKER", "2"))
    idle_for = int(os.getenv("IDLE_FOR", "60"))
    sleep_interval = int(os.getenv("AUTOSCALER_SLEEP", "5"))

    logger.info(
        "Starting CVP Autoscaler (MIN_WORKERS=%d, MAX_WORKERS=%d, PER_WORKER=%d, IDLE_FOR=%ds, SLEEP=%ds)",
        min_workers,
        max_workers,
        per_worker,
        idle_for,
        sleep_interval,
    )

    client = None
    try:
        client = docker.from_env()
        # Verify Docker connection
        client.ping()
        logger.info("Connected to Docker daemon successfully.")
    except Exception as exc:
        logger.error(
            "Failed to connect to Docker daemon via /var/run/docker.sock: %s. "
            "Ensure /var/run/docker.sock is mounted into the container.",
            exc,
        )

    scaler = Autoscaler(
        docker_client=client,
        min_workers=min_workers,
        max_workers=max_workers,
        per_worker=per_worker,
        idle_for=idle_for,
    )

    while True:
        try:
            result = scaler.step()
            if result.get("action") not in ["none", "no_docker_client"]:
                logger.info("Step result: %s", result)
        except Exception as exc:
            logger.error("Error during autoscaler step: %s", exc)

        time.sleep(sleep_interval)


if __name__ == "__main__":
    run_autoscaler()
