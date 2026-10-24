"""
Script to trigger heavy cluster load for live demonstration.
Submits 4 real video tasks with different operations simultaneously.
Demonstrates:
- Multiple workers processing in parallel at 100% CPU.
- Queue formation with live ETA and queue position.
- Automatic scaling up of workers to the maximum limit.
"""

import os
import sys
import time
import requests
import urllib3

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://localhost"
SAMPLE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "samples"))

FRACTAL_VIDEO = os.path.join(SAMPLE_DIR, "fractal_universe_30s.mp4")
CYBER_VIDEO = os.path.join(SAMPLE_DIR, "cyber_particles_20s.mp4")
MELODY_VIDEO = os.path.join(SAMPLE_DIR, "synthesizer_melody_25s.mp4")
LOGO_FILE = os.path.join(SAMPLE_DIR, "watermark_logo.png")


def main():
    session = requests.Session()
    session.verify = False

    user_email = f"demo_load_{int(time.time())}@example.com"
    user_pw = "DemoPass123!"

    print(f"[DEMO LOAD] Registering test user: {user_email}...")
    session.post(f"{BASE_URL}/api/auth/register", json={"email": user_email, "password": user_pw})
    session.post(f"{BASE_URL}/api/auth/login", json={"email": user_email, "password": user_pw})

    tasks_to_create = [
        {
            "name": "1. Fractal Universe -> Convert (H.264 High Quality)",
            "file": FRACTAL_VIDEO,
            "operation": "convert",
            "params": '{"preset":"medium"}',
            "logo": None,
        },
        {
            "name": "2. Fractal Universe -> Watermark with Glowing Logo",
            "file": FRACTAL_VIDEO,
            "operation": "watermark",
            "params": '{"position":"bottom_right","opacity":0.85,"preset":"medium"}',
            "logo": LOGO_FILE,
        },
        {
            "name": "3. Cyber Particles -> Scale 720p HD",
            "file": CYBER_VIDEO,
            "operation": "scale",
            "params": '{"resolution":"720","preset":"medium"}',
            "logo": None,
        },
        {
            "name": "4. Synthesizer Melody -> Extract Audio Track (MP3)",
            "file": MELODY_VIDEO,
            "operation": "audio",
            "params": '{"action":"extract"}',
            "logo": None,
        },
    ]

    print("\n" + "=" * 70)
    print(">>> SUBMITTING 4 HEAVY TASKS TO CLUSTER SIMULTANEOUSLY")
    print("=" * 70)

    task_ids = []
    for t in tasks_to_create:
        print(f"\n[+] Submitting: {t['name']}")
        files = {"file": open(t["file"], "rb")}
        data = {"operation_type": t["operation"], "params": t["params"]}

        if t["logo"] and os.path.exists(t["logo"]):
            files["logo"] = open(t["logo"], "rb")

        res = session.post(f"{BASE_URL}/api/tasks", files=files, data=data)
        if res.status_code in (200, 201):
            t_data = res.json()
            task_ids.append(t_data["id"])
            print(f"    [OK] Created Task ID: {t_data['id']}")
            print(f"    [OK] Status: {t_data['status']}")
            if t_data.get("queue_position"):
                print(f"    [OK] Queue Position: #{t_data['queue_position']} | ETA: {t_data.get('eta_sec')}s")
        else:
            print(f"    [FAIL] Error: {res.status_code} {res.text}")

    print("\n" + "=" * 70)
    print("[LOAD] CLUSTER IS NOW UNDER PEAK LOAD!")
    print("1. Open https://localhost/admin to watch active workers, CPU load, and Autoscaler scaling up!")
    print("2. Open https://localhost in your browser to watch live progress bars!")
    print("=" * 70)


if __name__ == "__main__":
    main()
