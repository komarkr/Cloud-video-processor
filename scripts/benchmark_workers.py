"""
Script for automated load testing and benchmarking with Locust against live Docker Compose cluster.
Runs real tests on 2 workers vs 4 workers and generates plots and evidence CSVs.
"""

import os
import subprocess
import sys
import time

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT_DIR)

VIDEO_PATH = os.path.join(ROOT_DIR, "data", "test_short.mp4")


def ensure_test_video():
    """Ensure test video exists in data/test_short.mp4."""
    if not os.path.exists(VIDEO_PATH):
        print(f"Generating test video at {VIDEO_PATH}...")
        os.makedirs(os.path.dirname(VIDEO_PATH), exist_ok=True)
        cmd = [
            "docker", "compose", "exec", "api1", "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=20:size=1280x720:rate=25",
            "-pix_fmt", "yuv420p", "/tmp/test_short.mp4"
        ]
        subprocess.run(cmd, check=True)
        subprocess.run(["docker", "cp", "cloud-video-processor-api1-1:/tmp/test_short.mp4", VIDEO_PATH], check=True)
        print("Generated test video successfully.")


def run_locust_benchmark(workers: int, run_time_sec: int = 40):
    """Scale Docker cluster to N workers and run headless Locust benchmark."""
    print(f"\n{'='*70}\n[BENCHMARK] Scaling cluster to {workers} workers...\n{'='*70}")
    subprocess.run(["docker", "compose", "up", "-d", f"--scale", f"worker={workers}"], check=True)
    # Stop autoscaler so it doesn't alter worker count during controlled benchmark
    subprocess.run(["docker", "compose", "stop", "autoscaler"], check=True)
    time.sleep(3)

    csv_prefix = f"docs/evidence/locust_{workers}_workers"
    print(f"[BENCHMARK] Running Locust on {workers} workers for {run_time_sec}s...")
    locust_cmd = [
        sys.executable, "-m", "locust",
        "-f", "locustfile.py",
        "--host", "https://localhost",
        "--headless",
        "-u", "10",
        "-r", "2",
        "--run-time", f"{run_time_sec}s",
        "--csv", csv_prefix,
    ]
    subprocess.run(locust_cmd, check=True)
    print(f"[BENCHMARK] {workers} workers run completed. Saved to {csv_prefix}_stats.csv")


def main():
    print("Starting automated real load testing for Cloud Video Processor...")
    ensure_test_video()

    # 1. Benchmark on 2 workers
    run_locust_benchmark(workers=2, run_time_sec=40)

    # 2. Benchmark on 4 workers
    run_locust_benchmark(workers=4, run_time_sec=40)

    # 3. Restore autoscaler
    subprocess.run(["docker", "compose", "start", "autoscaler"], check=True)

    # 4. Generate comparison plots
    plot_script = os.path.join(ROOT_DIR, "scripts", "plot_locust_results.py")
    if os.path.exists(plot_script):
        subprocess.run([sys.executable, plot_script], check=True)

    print("\n[SUCCESS] Real Locust benchmarks executed and evidence updated successfully!")


if __name__ == "__main__":
    main()
