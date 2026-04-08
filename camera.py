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
SMS_GATEWAY = os.getenv("SMS_GATEWAY") or os.getenv("PHONE")

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
    # For OCR mode, preserve original casing of the user note
    if mode == "ocr":
        original = user_text.strip()
        # Find where "ocr" ends (case-insensitive) and take the rest as the note
        idx = original.lower().find("ocr")
        if idx >= 0:
            prompt = original[idx + 3:].strip()
        else:
            prompt = ""
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


def parse_image_with_openai(image_url, user_note=""):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_prompt = (
        "You read and extract ALL text and structured information from images. "
        "This may be a photo of a screen, document, flyer, sign, or handwritten note. "
        f"Today's date is {today}. "
        "Return JSON with these fields:\n"
        '  "confidence": 0.0 to 1.0 — how confident you are in the OCR quality '
        "(1.0 = perfectly clear text, 0.5 = partially readable, below 0.3 = mostly guessing),\n"
        '  "full_text": "all text visible in the image, transcribed faithfully",\n'
        '  "summary": "1 sentence plain-language summary of the content",\n'
        '  "who": "person or organization, or null",\n'
        '  "what": "the action, event, or item",\n'
        '  "when": "date/time if found, or null",\n'
        '  "where": "location if found, or null",\n'
        '  "why": "purpose or context, or null",\n'
        '  "web_links": ["any URLs visible in the image"],\n'
        '  "tasks": [{"title": "...", "due_string": "...", "priority": 1-4, "create": true/false}]\n'
        "\n"
        "TASK RULES:\n"
        "- Task titles MUST be actionable verbs: 'Attend ...', 'Buy ...', 'Reply to ...', "
        "'Schedule ...', 'Look up ...', 'RSVP for ...'. Never use descriptions or taglines.\n"
        "- If the image is an event flyer, the task is 'Attend [event name]' or 'RSVP for [event name]'.\n"
        "- If the image is a conversation/chat, the task is the action implied: 'Reply to ...', "
        "'Meet ... at ...', 'Follow up with ...'.\n"
        "- If the image is a list (todo, shopping, checklist), return EACH item as a separate task.\n"
        "- If the image is a business sign, storefront, or business card, the task is "
        "'Save contact for [Business Name]' with any phone number, address, website, or hours "
        "included in the title (e.g. 'Save contact for Thompson\\'s Carpet — 718-399-3400'). "
        "Always set create: true for these.\n"
        '- "create": false for items that are purely informational with no action needed.\n'
        '- "create": true for items the user should act on.\n'
        '- "priority": 1=normal, 2=medium, 3=high, 4=urgent. Use urgency signals '
        "(deadlines, words like 'urgent', 'ASAP', 'deadline', proximity of dates).\n"
        "- Always return at least one task.\n"
        "\n"
        "DATE/TIME RULES (critical — read carefully):\n"
        "- Extract EVERY date and time visible in the image. Be precise.\n"
        "- If the image says 'Sunday, April 13' and today is " + today + ", compute the correct year.\n"
        "- If a time is shown as '6:30 PM' or '18:30', include it in event_start as ISO 8601.\n"
        "- If only a date with no time is visible, set event_start to 09:00 and event_end to 10:00.\n"
        "- If doors open at one time but the event starts at another, use the event start time.\n"
        "- For multi-day events, set event_end to the last day.\n"
        "- NEVER leave event_start as null if any date/time is visible and is_event is true.\n"
        "\n"
        '  "is_event": true if this describes a calendar event with a specific date/time,\n'
        '  "event_title": "short descriptive name for the event itself (e.g. \'Brooklyn For Peace Talk\', '
        "'Coffee with Zach'). Use a plain name, NEVER a tagline, subtitle, or marketing copy. "
        'If unsure, use the summary.",\n'
        '  "event_start": "ISO 8601 datetime like 2026-04-13T18:30:00, or null",\n'
        '  "event_end": "ISO 8601 datetime like 2026-04-13T20:30:00, or null (default 1 hour after start)",\n'
        '  "event_location": "full address string, or null",\n'
        '  "reminder_minutes": number of minutes before the event to send a reminder (default 30), '
        "or null if no event. If the user requests a specific reminder like 'remind me 2 weeks before', "
        "convert to minutes (2 weeks = 20160)."
    )

    user_content = [
        {"type": "image_url", "image_url": {"url": image_url}},
        {"type": "text", "text": "Extract all information from this image."},
    ]
    if user_note:
        user_content.append({"type": "text", "text": f"User note: {user_note}"})

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return json.loads(response.choices[0].message.content)


def _build_task_description(parsed, user_note=""):
    """Build shared description string from parsed OCR data."""
    parts = [f"Summary: {parsed.get('summary', '')}"]
    for field in ["who", "what", "when", "where", "why"]:
        val = parsed.get(field)
        if val:
            parts.append(f"{field.capitalize()}: {val}")
    if parsed.get("web_links"):
        parts.append("Links: " + ", ".join(parsed["web_links"]))
    if user_note:
        parts.append(f"Note: {user_note}")
    return "\n".join(parts)


def create_todoist_tasks(parsed, task_overrides=None, user_note=""):
    """Create Todoist tasks. If task_overrides provided, use those instead of parsed tasks.
    Each override: {"title": str, "due_string": str|null, "priority": int|null, "create": bool}
    Returns (created_count, task_urls)."""
    if task_overrides is not None:
        tasks = [t for t in task_overrides if t.get("create", True)]
    else:
        tasks = [t for t in parsed.get("tasks", []) if t.get("create", True)]
    if not tasks:
        return 0, []

    shared_desc = _build_task_description(parsed, user_note)

    created = 0
    task_urls = []
    for task in tasks:
        body = {"content": task.get("title", "Untitled"), "description": shared_desc}
        if task.get("due_string"):
            body["due_string"] = task["due_string"]
        priority = task.get("priority")
        if priority and priority in (1, 2, 3, 4):
            body["priority"] = priority
        try:
            resp = requests.post(
                "https://api.todoist.com/api/v1/tasks",
                headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            created += 1
            task_url = data.get("url", "")
            if task_url:
                task_urls.append(task_url)
        except Exception as e:
            print(f"Todoist task failed ({task.get('title')}): {e}", flush=True)
    return created, task_urls


def create_calendar_event(parsed, user_note=""):
    # Build rich description with context + links
    desc_parts = []
    if parsed.get("summary"):
        desc_parts.append(parsed["summary"])
    for field in ["who", "what", "where", "why"]:
        val = parsed.get(field)
        if val:
            desc_parts.append(f"{field.capitalize()}: {val}")
    if parsed.get("web_links"):
        desc_parts.append("")
        desc_parts.append("Links:")
        for link in parsed["web_links"]:
            desc_parts.append(f"  {link}")
    if user_note:
        desc_parts.append("")
        desc_parts.append(f"Note: {user_note}")
    if parsed.get("full_text"):
        desc_parts.append("")
        desc_parts.append("--- Original Text ---")
        desc_parts.append(parsed["full_text"][:1000])

    event_body = {
        "summary": parsed.get("event_title") or parsed.get("tasks", [{}])[0].get("title", "Event"),
        "description": "\n".join(desc_parts),
        "start": {"dateTime": parsed["event_start"], "timeZone": "America/New_York"},
        "end": {"dateTime": parsed["event_end"], "timeZone": "America/New_York"},
    }
    if parsed.get("event_location"):
        event_body["location"] = parsed["event_location"]

    # Reminders: use parsed reminder_minutes, or default 30
    reminder_minutes = parsed.get("reminder_minutes", 30)
    if reminder_minutes:
        overrides = [{"method": "email", "minutes": reminder_minutes}]
        # Also add a popup reminder at 30 min if the email reminder is far out
        if reminder_minutes > 60:
            overrides.append({"method": "popup", "minutes": 30})
        else:
            overrides.append({"method": "popup", "minutes": reminder_minutes})
        event_body["reminders"] = {"useDefault": False, "overrides": overrides}

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


def send_sms(message, image_bytes=None, image_filename="result.jpg"):
    """Send MMS (with image) or SMS via AT&T email gateway."""
    if not SMS_GATEWAY:
        return
    # Use mms.att.net for picture messages, txt.att.net for text-only
    gateway = SMS_GATEWAY.replace("@txt.att.net", "@mms.att.net") if image_bytes else SMS_GATEWAY

    if image_bytes:
        msg = MIMEMultipart()
        msg.attach(MIMEText(message[:160]))
        img = MIMEImage(image_bytes)
        img.add_header("Content-Disposition", "attachment", filename=image_filename)
        msg.attach(img)
    else:
        msg = MIMEText(message[:160])

    msg["From"] = GMAIL_SENDER
    msg["To"] = gateway
    msg["Subject"] = ""
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_SENDER, GMAIL_SMTP_PASSWORD)
        server.send_message(msg)
    print(f"MMS/SMS sent: {message[:60]}...")


def process_job(image_bytes, user_text, image_filename, events_queue, defer_tasks=False):
    """Core processing function used by both Discord and PWA.
    defer_tasks=True (PWA): skip Todoist/Calendar, let user confirm in the app.
    defer_tasks=False (Discord): auto-create everything immediately."""
    mode, modifier, prompt_text = parse_user_input(user_text)
    events_queue.put({"status": "processing", "mode": mode})

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
        f.write(image_bytes)
        temp_path = f.name

    try:
        if mode == "ocr":
            fal_upload_url = fal_client.upload_file(temp_path)
            parsed = parse_image_with_openai(fal_upload_url, user_note=prompt_text)
            ocr_text = parsed.get("full_text", "")
            confidence = parsed.get("confidence", 1.0)
            results = {"type": "ocr", "parsed": parsed, "confidence": confidence, "user_note": prompt_text}

            if defer_tasks:
                results["todoist"] = "deferred"
                results["task_count"] = 0
                results["calendar"] = "deferred"
            else:
                # Auto-create Todoist tasks
                try:
                    count, task_urls = create_todoist_tasks(parsed, user_note=prompt_text)
                    results["todoist"] = True
                    results["task_count"] = count
                    results["task_urls"] = task_urls
                except Exception as e:
                    results["todoist"] = False
                    results["task_count"] = 0
                    print(f"Todoist failed: {e}", flush=True)

                # Auto-create calendar event
                results["calendar"] = False
                if parsed.get("is_event") and parsed.get("event_start"):
                    try:
                        cal_event = create_calendar_event(parsed, user_note=prompt_text)
                        results["calendar"] = True
                        results["calendar_link"] = cal_event.get("htmlLink", "")
                    except Exception as e:
                        print(f"Calendar failed: {e}", flush=True)

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
                if prompt_text:
                    email_lines.append(f"\nNote: {prompt_text}")
                email_lines.append(f"\n--- Full Text ---\n{ocr_text}")
                send_email(
                    f"OCR Capture: {parsed.get('summary', '')[:50]}",
                    "\n".join(email_lines),
                    image_bytes,
                    image_filename,
                )
                results["email"] = True
            except Exception as e:
                results["email"] = False
                print(f"Email failed: {e}", flush=True)

            # Drive
            try:
                upload_to_drive(temp_path, f"ocr_{uuid.uuid4().hex[:8]}.jpg", "image/jpeg")
                results["drive"] = True
            except Exception as e:
                results["drive"] = False
                print(f"Drive failed: {e}", flush=True)

            # SMS
            try:
                task_count = results.get("task_count", 0)
                summary = parsed.get("summary", "")[:80]
                sms_msg = f"[ocr] {task_count} task(s) | {summary}" if not defer_tasks else f"[ocr] {summary}"
                send_sms(sms_msg, image_bytes, "ocr_source.jpg")
            except Exception as e:
                print(f"SMS failed: {e}", flush=True)

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
                        print(f"Drive backup failed: {e}", flush=True)
                    try:
                        prompt_label = prompt_text[:30] if prompt_text else "default"
                        send_sms(f"[cinema {prompt_label}] Done: {result_url}")
                    except Exception as e:
                        print(f"SMS failed: {e}", flush=True)
                    events_queue.put({"status": "complete", "type": "cinema", "result_url": result_url})
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
                        print(f"Drive backup failed: {e}", flush=True)
                    try:
                        prompt_label = prompt_text[:30] if prompt_text else "default"
                        send_sms(f"[video {prompt_label}] Done: {result_url}")
                    except Exception as e:
                        print(f"SMS failed: {e}", flush=True)
                    events_queue.put({"status": "complete", "type": "video", "result_url": result_url})
                else:
                    events_queue.put({"status": "error", "message": "No result from model"})

            else:  # image (simpsons)
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
                        print(f"Drive backup failed: {e}", flush=True)
                    try:
                        # Download result image and send as MMS
                        result_img = requests.get(result_url).content
                        prompt_label = prompt_text[:30] if prompt_text else "simpsons"
                        send_sms(f"[{prompt_label}]", result_img, "simpsons.jpg")
                    except Exception as e:
                        print(f"SMS failed: {e}", flush=True)
                    events_queue.put({"status": "complete", "type": "image", "result_url": result_url})
                else:
                    events_queue.put({"status": "error", "message": "No result from model"})

    except Exception as e:
        print(f"process_job error ({mode}): {e}", flush=True)
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
    mode, _, _ = parse_user_input(user_text)

    status_msg = {"ocr": "scanning image...", "cinema": "sending cinema...", "video": "sending video...", "image": "sending image..."}
    await message.channel.send(status_msg.get(mode, "processing..."))

    try:
        image_response = requests.get(attachment.url)
        q = Queue()

        def run():
            process_job(image_response.content, user_text, attachment.filename or "image.jpg", q)

        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(executor, run),
            timeout=300  # 5 minute timeout
        )

        result = q.get()
        while result["status"] not in ("complete", "error"):
            result = q.get()

        if result["status"] == "error":
            await message.channel.send(f"Error: {result['message']}")
            return
    except asyncio.TimeoutError:
        print(f"on_message timeout: mode={mode}, user={message.author}", flush=True)
        await message.channel.send("Error: processing timed out after 5 minutes.")
        return
    except Exception as e:
        print(f"on_message error: {e}", flush=True)
        await message.channel.send(f"Error: {e}")
        return

    if result["type"] == "ocr":
        parsed = result["parsed"]
        task_count = result.get("task_count", 0)
        task_urls = result.get("task_urls", [])
        user_note = result.get("user_note", "")

        # Color: green = tasks created, yellow = partial/no tasks, red = failure
        color = 0x34c759 if task_count > 0 else (0xffcc00 if result.get("todoist") != False else 0xff453a)
        embed = discord.Embed(title=parsed.get("summary", "OCR Result"), color=color)

        for field in ["who", "when", "where"]:
            val = parsed.get(field)
            if val:
                embed.add_field(name=field.capitalize(), value=val, inline=True)

        if user_note:
            embed.add_field(name="Note", value=user_note, inline=False)

        # Task list
        tasks = parsed.get("tasks", [])
        if tasks:
            task_lines = []
            for i, t in enumerate(tasks):
                line = f"{i+1}. {t.get('title', 'Untitled')}"
                if t.get("due_string"):
                    line += f" *({t['due_string']})*"
                if not t.get("create", True):
                    line += " (info only)"
                task_lines.append(line)
            embed.add_field(name=f"Tasks ({task_count} created)", value="\n".join(task_lines), inline=False)

        if task_urls:
            embed.add_field(name="Todoist", value=" | ".join(f"[task {i+1}]({u})" for i, u in enumerate(task_urls)), inline=False)

        # Calendar
        if result.get("calendar"):
            cal_line = parsed.get("event_title", "Event")
            if parsed.get("event_start"):
                cal_line += f"\n{parsed['event_start']}"
            if parsed.get("event_location"):
                cal_line += f"\n{parsed['event_location']}"
            reminder = parsed.get("reminder_minutes", 30)
            if reminder and reminder >= 1440:
                cal_line += f"\nReminder: {reminder // 1440}d before"
            elif reminder and reminder > 60:
                cal_line += f"\nReminder: {reminder // 60}h before"
            if result.get("calendar_link"):
                cal_line += f"\n[Open in Calendar]({result['calendar_link']})"
            embed.add_field(name="Calendar", value=cal_line, inline=False)

        # Footer status
        status = []
        if result.get("todoist") == True:
            status.append(f"Tasks: {task_count}")
        if result.get("calendar") == True:
            status.append("Calendar")
        if result.get("email"):
            status.append("Email")
        if result.get("drive"):
            status.append("Drive")
        fails = [s for s, ok in [("Todoist", result.get("todoist")), ("Email", result.get("email")), ("Drive", result.get("drive"))] if ok == False]
        if fails:
            status.append(f"Failed: {', '.join(fails)}")
        embed.set_footer(text=" | ".join(status) if status else "Done")

        await message.channel.send(embed=embed)
    else:
        result_url = result.get("result_url", "")
        await message.channel.send(f"{result['type']}:\n{result_url}")


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
    job_record = {"events": q, "event_log": [], "done": False, "created": datetime.now(timezone.utc)}
    jobs[job_id] = job_record

    image_bytes = image.read()
    filename = image.filename or "capture.jpg"

    def run_and_log():
        process_job(image_bytes, text, filename, q, defer_tasks=True)
        # Drain queue into event_log so late-connecting SSE can read
        while not q.empty():
            evt = q.get_nowait()
            job_record["event_log"].append(evt)
            if evt["status"] in ("complete", "error"):
                job_record["done"] = True

    threading.Thread(target=run_and_log, daemon=True).start()

    return jsonify({"job_id": job_id})


@app.route("/api/confirm-tasks", methods=["POST"])
def api_confirm_tasks():
    token = flask_request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != PWA_API_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    data = flask_request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400

    parsed = data.get("parsed", {})
    tasks = data.get("tasks", [])
    create_cal = data.get("create_calendar", False)

    results = {}

    # Create confirmed tasks in Todoist
    try:
        count, task_urls = create_todoist_tasks(parsed, task_overrides=tasks)
        results["todoist"] = True
        results["task_count"] = count
        results["task_urls"] = task_urls
    except Exception as e:
        results["todoist"] = False
        results["task_count"] = 0
        print(f"Todoist confirm failed: {e}", flush=True)

    # Create calendar event if confirmed
    results["calendar"] = False
    if create_cal and parsed.get("is_event") and parsed.get("event_start"):
        try:
            # Allow overriding event title from client
            if data.get("event_title"):
                parsed["event_title"] = data["event_title"]
            create_calendar_event(parsed)
            results["calendar"] = True
        except Exception as e:
            print(f"Calendar confirm failed: {e}", flush=True)

    return jsonify(results)


@app.route("/api/status/<job_id>")
def job_status(job_id):
    import time

    def stream():
        job = jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'status': 'error', 'message': 'not found'})}\n\n"
            return

        # First replay any events already logged (for late-connecting clients)
        sent = 0
        for evt in job["event_log"]:
            yield f"data: {json.dumps(evt)}\n\n"
            sent += 1
            if evt["status"] in ("complete", "error"):
                jobs.pop(job_id, None)
                return

        # Then wait for new events
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                event = job["events"].get(timeout=2)
                job["event_log"].append(event)
                yield f"data: {json.dumps(event)}\n\n"
                if event["status"] in ("complete", "error"):
                    break
            except Exception:
                # Check if job finished while we weren't listening
                if job.get("done"):
                    for evt in job["event_log"][sent:]:
                        yield f"data: {json.dumps(evt)}\n\n"
                    break
                continue
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
