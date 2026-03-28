import os
import json
from pathlib import Path
from dotenv import load_dotenv
import fal_client
import requests

load_dotenv()

SOURCE_DIR = "/Users/chrisevans/Desktop/scripting_projects/website_update_strategy/assets/discord-camera/2026"
OUTPUT_DIR = SOURCE_DIR  # save results back into the same folder
TRACKER_FILE = os.path.join(OUTPUT_DIR, "processed.json")

SIMPSONS_PROMPT = "in the style of the Simpson's cartoon animation illustration"
VIDEO_PROMPT = "zoom, camera pan, details in focus"

IMAGE_MODEL = "fal-ai/flux-pro/kontext/max"
VIDEO_MODEL = "fal-ai/kling-video/v2/master/image-to-video"


def load_processed():
    if os.path.exists(TRACKER_FILE):
        with open(TRACKER_FILE) as f:
            return json.load(f)
    return {}


def save_processed(tracker):
    with open(TRACKER_FILE, "w") as f:
        json.dump(tracker, f, indent=2)


def simpsonify(image_path):
    """Upload image, run through Simpsons style filter, return result URL."""
    fal_url = fal_client.upload_file(image_path)
    print(f"  Uploaded to fal: {fal_url}")

    handle = fal_client.submit(
        IMAGE_MODEL,
        arguments={
            "prompt": SIMPSONS_PROMPT,
            "image_url": fal_url,
            "guidance_scale": 3.5,
            "num_images": 1,
            "safety_tolerance": "2",
            "output_format": "jpeg",
        },
    )
    result = fal_client.result(IMAGE_MODEL, handle.request_id)
    if "images" in result and result["images"]:
        return result["images"][0]["url"]
    raise RuntimeError("No image returned from simpsonify")


def animate(image_url):
    """Take a simpsonified image URL and generate a video clip."""
    handle = fal_client.submit(
        VIDEO_MODEL,
        arguments={
            "prompt": VIDEO_PROMPT,
            "image_url": image_url,
            "duration": "5",
            "aspect_ratio": "16:9",
            "negative_prompt": "blur, distort, and low quality",
            "cfg_scale": 0.5,
        },
    )
    result = fal_client.result(VIDEO_MODEL, handle.request_id)
    if "video" in result:
        return result["video"]["url"]
    raise RuntimeError("No video returned from animate")


def download(url, dest_path):
    resp = requests.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)


def process_folder():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tracker = load_processed()

    source = Path(SOURCE_DIR)
    image_extensions = {".jpg", ".jpeg", ".png", ".tiff", ".bmp", ".webp"}
    files = sorted(
        f for f in source.iterdir()
        if f.suffix.lower() in image_extensions
    )

    new_count = 0
    for filepath in files:
        name = filepath.name
        if name in tracker:
            continue
        if name.startswith("simpsons_") or name.startswith("video_"):
            continue

        print(f"\nProcessing: {name}")
        try:
            # Step 1: Simpsonify
            print("  Simpsonifying...")
            simpsons_url = simpsonify(str(filepath))
            simpsons_path = os.path.join(OUTPUT_DIR, f"simpsons_{filepath.stem}.jpg")
            download(simpsons_url, simpsons_path)
            print(f"  Saved: {simpsons_path}")

            # Step 2: Animate
            print("  Animating...")
            video_url = animate(simpsons_url)
            video_path = os.path.join(OUTPUT_DIR, f"video_{filepath.stem}.mp4")
            download(video_url, video_path)
            print(f"  Saved: {video_path}")

            tracker[name] = {
                "status": "done",
                "simpsons_image": simpsons_path,
                "video": video_path,
            }
            new_count += 1

        except Exception as e:
            print(f"  Error processing {name}: {e}")
            tracker[name] = {"status": "error", "error": str(e)}

        save_processed(tracker)

    print(f"\nDone. Processed {new_count} new landscape image(s).")


if __name__ == "__main__":
    process_folder()
