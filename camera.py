import os
import json
import base64
import uuid
import threading
import discord
import requests
import tempfile
import asyncio
import smtplib
import concurrent.futures
from queue import Queue
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from dotenv import load_dotenv
import fal_client
from openai import OpenAI
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from flask import Flask, request as flask_request, jsonify, Response, send_from_directory

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FAL_KEY = os.getenv("FAL_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")
GMAIL_SENDER = os.getenv("GMAIL_SENDER")
GMAIL_RECIPIENT = os.getenv("GMAIL_RECIPIENT")
GMAIL_SMTP_PASSWORD = os.getenv("GMAIL_SMTP_PASSWORD")
GOOGLE_SERVICE_ACCOUNT_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")
DRIVE_FOLDER_ID = os.getenv("FOLDER_ID")
GOOGLE_CLIENT_ID_DISCORD_BOT = os.getenv("GOOGLE_CLIENT_ID_DISCORD_BOT")
GOOGLE_CLIENT_SECRET_DISCORD_BOT = os.getenv("GOOGLE_CLIENT_SECRET_DISCORD_BOT")
GOOGLE_REFRESH_TOKEN_DISCORD_BOT = os.getenv("GOOGLE_REFRESH_TOKEN_DISCORD_BOT")
PWA_API_TOKEN = os.getenv("PWA_API_TOKEN")

IMAGE_MODEL = "fal-ai/flux-pro/kontext/max"
CINEMA_MODEL = "fal-ai/kling-video/v3/pro/image-to-video"
VIDEO_MODEL = "fal-ai/ltx-2.3/image-to-video/fast"

DEFAULT_PROMPT_IMAGE = "in the style of the Simpson's cartoon animation illustration"
DEFAULT_PROMPT_VIDEO = "zoom, camera pan, details in focus"

CINEMA_DURATIONS = {"short": "5", "default": "10", "long": "30"}
VIDEO_DURATIONS = {"short": 6, "default": 10, "long": 20}

# --- Google API setup ---

def _get_sa_creds(scopes):
    sa_info = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_B64))
    return service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)

drive_creds = OAuthCredentials(
    token=None,
    refresh_token=GOOGLE_REFRESH_TOKEN_DISCORD_BOT,
    client_id=GOOGLE_CLIENT_ID_DISCORD_BOT,
    client_secret=GOOGLE_CLIENT_SECRET_DISCORD_BOT,
    token_uri="https://oauth2.googleapis.com/token",
)
drive_service = build("drive", "v3", credentials=drive_creds)
calendar_service = build("calendar", "v3", credentials=_get_sa_creds(
    ["https://www.googleapis.com/auth/calendar"]
))
CALENDAR_ID = GMAIL_RECIPIENT

openai_client = OpenAI(api_key=OPENAI_API_KEY)
executor = concurrent.futures.ThreadPoolExecutor()

# --- Shared processing functions ---

def parse_user_input(user_text):
    text = user_text.strip().lower()
    modifier = "long" if "long" in text else ("short" if "short" in text else None)
    if "ocr" in text:
        mode = "ocr"
    elif "cinema" in text:
        mode = "cinema"
    elif "video" in text:
        mode = "video"
    else:
        mode = "image"
    prompt = text
    for kw in ["long", "short", "ocr", "cinema", "video"]:
        prompt = prompt.replace(kw, "")
    return mode, modifier, prompt.strip()


def find_or_create_daily_folder():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    query = (
        f"name='{today}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{DRIVE_FOLDER_ID}' in parents and trashed=false"
    )
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {"name": today, "mimeType": "application/vnd.google-apps.folder", "parents": [DRIVE_FOLDER_ID]}
    folder = drive_service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_to_drive(file_path, filename, mime_type="image/jpeg"):
    folder_id = find_or_create_daily_folder()
    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type)
    uploaded = drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Uploaded to Drive: {filename} ({uploaded['id']})")
    return uploaded["id"]


def backup_result_url(result_url, filename, mime_type="image/jpeg"):
    resp = requests.get(result_url)
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as f:
        f.write(resp.content)
        tmp = f.name
    try:
        return upload_to_drive(tmp, filename, mime_type)
    finally:
        os.remove(tmp)


def parse_image_with_openai(image_url):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You read and extract ALL text and structured information from images. "
                    "This may be a photo of a screen, a document, a flyer, a sign, etc. "
                    f"Today's date is {today}. "
                    "Return JSON with these fields:\n"
                    '  "full_text": "all text visible in the image, transcribed faithfully",\n'
                    '  "summary": "1 sentence summary of the content",\n'
                    '  "who": "person or organization, or null",\n'
                    '  "what": "the action, event, or item",\n'
                    '  "when": "date/time if found, or null",\n'
                    '  "where": "location if found, or null",\n'
                    '  "why": "purpose or context, or null",\n'
                    '  "web_links": ["any URLs visible in the image"],\n'
                    '  "todoist_title": "short actionable title for a task",\n'
                    '  "due_string": "natural language date for scheduling, or null",\n'
                    '  "is_event": true if this describes a calendar event with a specific date/time,\n'
                    '  "event_title": "short event title, or null",\n'
                    '  "event_start": "ISO 8601 datetime like 2026-03-28T14:00:00, or null",\n'
                    '  "event_end": "ISO 8601 datetime like 2026-03-28T15:00:00, or null (default 1 hour after start)",\n'
                    '  "event_location": "location string, or null"'
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": "Extract all information from this image."},
                ],
            },
        ],
    )
    return json.loads(response.choices[0].message.content)


def create_todoist_task(parsed):
    description_parts = [f"Summary: {parsed['summary']}"]
    for field in ["who", "what", "when", "where", "why"]:
        val = parsed.get(field)
        if val:
            description_parts.append(f"{field.capitalize()}: {val}")
    if parsed.get("web_links"):
        description_parts.append("Links: " + ", ".join(parsed["web_links"]))
    body = {"content": parsed["todoist_title"], "description": "\n".join(description_parts)}
    if parsed.get("due_string"):
        body["due_string"] = parsed["due_string"]
    resp = requests.post(
        "https://api.todoist.com/api/v1/tasks",
        headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def create_calendar_event(parsed):
    event_body = {
        "summary": parsed.get("event_title") or parsed.get("todoist_title"),
        "description": parsed.get("summary", ""),
        "start": {"dateTime": parsed["event_start"], "timeZone": "America/New_York"},
        "end": {"dateTime": parsed["event_end"], "timeZone": "America/New_York"},
    }
    if parsed.get("event_location"):
        event_body["location"] = parsed["event_location"]
    event = calendar_service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
    print(f"Calendar event created: {event.get('htmlLink')}")
    return event


def send_email(subject, body_text, image_bytes, image_filename):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_SENDER
    msg["To"] = GMAIL_RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain"))
    img_attachment = MIMEImage(image_bytes)
    img_attachment.add_header("Content-Disposition", "attachment", filename=image_filename)
    msg.attach(img_attachment)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_SENDER, GMAIL_SMTP_PASSWORD)
        server.send_message(msg)


def process_job(image_bytes, user_text, image_filename, events_queue):
    """Core processing function used by both Discord and PWA. Pushes status events to queue."""
    mode, modifier, prompt_text = parse_user_input(user_text)
    events_queue.put({"status": "processing", "mode": mode})

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        f.write(image_bytes)
        temp_path = f.name

    try:
        if mode == "ocr":
            # Upload to fal for Drive backup, use Discord/direct URL for vision
            fal_upload_url = fal_client.upload_file(temp_path)
            parsed = parse_image_with_openai(fal_upload_url)
            ocr_text = parsed.get("full_text", "")
            results = {"type": "ocr", "parsed": parsed}

            # Todoist
            try:
                create_todoist_task(parsed)
                results["todoist"] = True
            except Exception as e:
                results["todoist"] = False
                print(f"Todoist failed: {e}")

            # Calendar
            results["calendar"] = False
            if parsed.get("is_event") and parsed.get("event_start"):
                try:
                    create_calendar_event(parsed)
                    results["calendar"] = True
                except Exception as e:
                    print(f"Calendar failed: {e}")

            # Email
            try:
                email_lines = [f"Summary: {parsed['summary']}", ""]
                for field in ["who", "what", "when", "where", "why"]:
                    val = parsed.get(field)
                    if val:
                        email_lines.append(f"{field.capitalize()}: {val}")
                if parsed.get("web_links"):
                    email_lines.append("\nLinks:")
                    for link in parsed["web_links"]:
                        email_lines.append(f"  {link}")
                email_lines.append(f"\n--- Full Text ---\n{ocr_text}")
                send_email(
                    f"OCR Capture: {parsed['todoist_title']}",
                    "\n".join(email_lines),
                    image_bytes,
                    image_filename,
                )
                results["email"] = True
            except Exception as e:
                results["email"] = False
                print(f"Email failed: {e}")

            # Drive
            try:
                upload_to_drive(temp_path, f"ocr_{uuid.uuid4().hex[:8]}.jpg", "image/jpeg")
                results["drive"] = True
            except Exception as e:
                results["drive"] = False
                print(f"Drive failed: {e}")

            events_queue.put({"status": "complete", **results})

        else:
            fal_upload_url = fal_client.upload_file(temp_path)
            events_queue.put({"status": "submitted"})

            if mode == "cinema":
                duration = CINEMA_DURATIONS.get(modifier, CINEMA_DURATIONS["default"])
                user_prompt = prompt_text if prompt_text else DEFAULT_PROMPT_VIDEO
                fal_response = fal_client.submit(CINEMA_MODEL, arguments={
                    "prompt": user_prompt, "start_image_url": fal_upload_url,
                    "duration": duration, "aspect_ratio": "16:9",
                    "negative_prompt": "blur, distort, and low quality", "cfg_scale": 0.5,
                })
                result = fal_client.result(CINEMA_MODEL, fal_response.request_id)
                if "video" in result:
                    result_url = result["video"]["url"]
                    try:
                        backup_result_url(result_url, f"cinema_{uuid.uuid4().hex[:8]}.mp4", "video/mp4")
                    except Exception as e:
                        print(f"Drive backup failed: {e}")
                    events_queue.put({"status": "complete", "type": "video", "result_url": result_url})
                else:
                    events_queue.put({"status": "error", "message": "No result from model"})

            elif mode == "video":
                duration = VIDEO_DURATIONS.get(modifier, VIDEO_DURATIONS["default"])
                user_prompt = prompt_text if prompt_text else DEFAULT_PROMPT_VIDEO
                fal_response = fal_client.submit(VIDEO_MODEL, arguments={
                    "prompt": user_prompt, "image_url": fal_upload_url,
                    "duration": duration, "resolution": "1080p", "fps": 25, "generate_audio": True,
                })
                result = fal_client.result(VIDEO_MODEL, fal_response.request_id)
                if "video" in result:
                    result_url = result["video"]["url"]
                    try:
                        backup_result_url(result_url, f"video_{uuid.uuid4().hex[:8]}.mp4", "video/mp4")
                    except Exception as e:
                        print(f"Drive backup failed: {e}")
                    events_queue.put({"status": "complete", "type": "video", "result_url": result_url})
                else:
                    events_queue.put({"status": "error", "message": "No result from model"})

            else:  # image
                user_prompt = prompt_text if prompt_text else DEFAULT_PROMPT_IMAGE
                fal_response = fal_client.submit(IMAGE_MODEL, arguments={
                    "prompt": user_prompt, "image_url": fal_upload_url,
                    "guidance_scale": 3.5, "num_images": 1, "safety_tolerance": "2", "output_format": "jpeg",
                })
                result = fal_client.result(IMAGE_MODEL, fal_response.request_id)
                if "images" in result:
                    result_url = result["images"][0]["url"]
                    try:
                        backup_result_url(result_url, f"image_{uuid.uuid4().hex[:8]}.jpg", "image/jpeg")
                    except Exception as e:
                        print(f"Drive backup failed: {e}")
                    events_queue.put({"status": "complete", "type": "image", "result_url": result_url})
                else:
                    events_queue.put({"status": "error", "message": "No result from model"})

    except Exception as e:
        events_queue.put({"status": "error", "message": str(e)})
    finally:
        os.remove(temp_path)


# --- Discord Bot ---

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)
processed_messages = set()


async def get_fal_result(model, request_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: fal_client.result(model, request_id))


@discord_client.event
async def on_ready():
    print(f'Bot connected as {discord_client.user}')
    print(f'Connected to {len(discord_client.guilds)} server(s)')


@discord_client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.id in processed_messages:
        return
    processed_messages.add(message.id)

    if not message.attachments:
        return

    attachment = message.attachments[0]
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        await message.channel.send("Please attach an image file.")
        return

    user_text = message.content.strip()
    mode, modifier, prompt_text = parse_user_input(user_text)

    status_msg = {"ocr": "scanning image...", "cinema": "sending cinema...", "video": "sending video...", "image": "sending image..."}
    await message.channel.send(status_msg.get(mode, "processing..."))

    image_response = requests.get(attachment.url)
    q = Queue()

    def run():
        process_job(image_response.content, user_text, attachment.filename or "image.jpg", q)

    await asyncio.get_event_loop().run_in_executor(executor, run)

    result = q.get()
    while result["status"] not in ("complete", "error"):
        result = q.get()

    if result["status"] == "error":
        await message.channel.send(f"Error: {result['message']}")
        return

    if result["type"] == "ocr":
        parsed = result["parsed"]
        reply_parts = [f"\n**Summary:** {parsed['summary']}"]
        for field in ["who", "when", "where"]:
            val = parsed.get(field)
            if val:
                reply_parts.append(f"**{field.capitalize()}:** {val}")
        status = []
        if result.get("todoist"):
            status.append(f"Task: {parsed['todoist_title']}")
        if result.get("calendar"):
            status.append(f"Calendar: {parsed.get('event_title', parsed['todoist_title'])}")
        if result.get("email"):
            status.append("Email sent")
        if result.get("drive"):
            status.append("Saved to Drive")
        fails = [s for s, ok in [("Todoist", result.get("todoist")), ("Email", result.get("email")), ("Drive", result.get("drive"))] if not ok]
        if status:
            reply_parts.insert(0, " | ".join(status))
        if fails:
            reply_parts.append(f"*Failed: {', '.join(fails)}*")
        await message.channel.send("\n".join(reply_parts))
    else:
        result_url = result.get("result_url", "")
        label = "cinema" if "cinema" in mode else result["type"]
        await message.channel.send(f"{label}:\n{result_url}")


# --- Flask PWA API ---

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB

jobs = {}  # job_id -> {"events": Queue}


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/process", methods=["POST"])
def api_process():
    token = flask_request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != PWA_API_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    image = flask_request.files.get("image")
    text = flask_request.form.get("text", "")
    if not image:
        return jsonify({"error": "no image"}), 400

    job_id = uuid.uuid4().hex
    q = Queue()
    jobs[job_id] = {"events": q, "created": datetime.now(timezone.utc)}

    image_bytes = image.read()
    filename = image.filename or "capture.jpg"

    threading.Thread(target=process_job, args=(image_bytes, text, filename, q), daemon=True).start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    def stream():
        job = jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'status': 'error', 'message': 'not found'})}\n\n"
            return
        while True:
            event = job["events"].get(timeout=300)
            yield f"data: {json.dumps(event)}\n\n"
            if event["status"] in ("complete", "error"):
                break
        jobs.pop(job_id, None)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- Startup ---

_bot_started = False
def ensure_bot_running():
    global _bot_started
    if not _bot_started:
        _bot_started = True
        threading.Thread(target=discord_client.run, args=(DISCORD_TOKEN,), daemon=True).start()
        print("Discord bot thread started")

ensure_bot_running()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
