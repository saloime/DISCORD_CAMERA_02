import os
import json
import base64
import discord
import requests
import tempfile
import asyncio
import smtplib
import concurrent.futures
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from dotenv import load_dotenv
import fal_client
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FAL_KEY = os.getenv("FAL_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN")
GMAIL_SENDER = os.getenv("GMAIL_SENDER")
GMAIL_RECIPIENT = os.getenv("GMAIL_RECIPIENT")
GMAIL_SMTP_PASSWORD = os.getenv("GMAIL_SMTP_PASSWORD")
GOOGLE_SERVICE_ACCOUNT_B64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_B64")

IMAGE_MODEL = "fal-ai/flux-pro/kontext/max"
CINEMA_MODEL = "fal-ai/kling-video/v3/pro/image-to-video"
VIDEO_MODEL = "fal-ai/ltx-2.3/image-to-video/fast"
OCR_MODEL = "fal-ai/got-ocr/v2"

DEFAULT_PROMPT_IMAGE = "in the style of the Simpson's cartoon animation illustration"
DEFAULT_PROMPT_VIDEO = "zoom, camera pan, details in focus"

CINEMA_DURATIONS = {"short": "5", "default": "10", "long": "30"}
VIDEO_DURATIONS = {"short": 6, "default": 10, "long": 20}

DRIVE_ROOT_FOLDER = "discord_bot"

# Google Drive setup
def get_drive_service():
    sa_info = json.loads(base64.b64decode(GOOGLE_SERVICE_ACCOUNT_B64))
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

drive_service = get_drive_service()


def find_or_create_folder(name, parent_id=None):
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = drive_service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def get_daily_folder_id():
    root_id = find_or_create_folder(DRIVE_ROOT_FOLDER)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return find_or_create_folder(today, root_id)


def upload_to_drive(file_path, filename, mime_type="image/jpeg"):
    folder_id = get_daily_folder_id()
    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype=mime_type)
    uploaded = drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
    print(f"Uploaded to Drive: {filename} ({uploaded['id']})")
    return uploaded["id"]


def backup_result_url(result_url, filename, mime_type="image/jpeg"):
    """Download a result URL from fal and upload it to Google Drive."""
    resp = requests.get(result_url)
    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as f:
        f.write(resp.content)
        tmp = f.name
    try:
        return upload_to_drive(tmp, filename, mime_type)
    finally:
        os.remove(tmp)


intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

executor = concurrent.futures.ThreadPoolExecutor()
processed_messages = set()


async def get_fal_result(model, request_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: fal_client.result(model, request_id))


def parse_ocr_with_openai(ocr_text):
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract structured information from OCR text. "
                    "Return JSON with these fields:\n"
                    '  "summary": "1 sentence summary",\n'
                    '  "who": "person or organization, or null",\n'
                    '  "what": "the action, event, or item",\n'
                    '  "when": "date/time if found, or null",\n'
                    '  "where": "location if found, or null",\n'
                    '  "why": "purpose or context, or null",\n'
                    '  "web_links": ["any URLs found in text"],\n'
                    '  "todoist_title": "short actionable title for a task",\n'
                    '  "due_string": "natural language date for scheduling, or null"'
                ),
            },
            {"role": "user", "content": ocr_text},
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
        "https://api.todoist.com/rest/v2/tasks",
        headers={"Authorization": f"Bearer {TODOIST_API_TOKEN}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


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


@client.event
async def on_ready():
    print(f'Bot connected as {client.user}')
    print(f'Connected to {len(client.guilds)} server(s)')


@client.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"on_message triggered by: {message.id} | {message.author} | {message.content}")

    if message.id in processed_messages:
        print(f"Already processed message {message.id}, skipping...")
        return
    processed_messages.add(message.id)

    if not message.attachments:
        return

    attachment = message.attachments[0]
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        await message.channel.send("Please attach an image file.")
        return

    user_text = message.content.strip().lower()
    image_url = attachment.url

    # Detect mode and duration modifier
    modifier = "long" if "long" in user_text else ("short" if "short" in user_text else None)

    if "ocr" in user_text:
        mode = "ocr"
    elif "cinema" in user_text:
        mode = "cinema"
    elif "video" in user_text:
        mode = "video"
    else:
        mode = "image"

    # Build prompt: strip keywords from user text, remainder is prompt
    prompt_text = user_text
    for keyword in ["long", "short", "ocr", "cinema", "video"]:
        prompt_text = prompt_text.replace(keyword, "")
    prompt_text = prompt_text.strip()

    try:
        # Download Discord image to temp file
        image_response = requests.get(image_url)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
            temp_file.write(image_response.content)
            temp_file_path = temp_file.name

        # Upload image to Fal
        fal_upload_url = fal_client.upload_file(temp_file_path)
        print(f"Uploaded to Fal: {fal_upload_url}")

        if mode == "ocr":
            await message.channel.send("scanning text...")

            fal_response = fal_client.submit(
                OCR_MODEL,
                arguments={"input_image_urls": [fal_upload_url], "do_format": True},
            )
            result = await get_fal_result(OCR_MODEL, fal_response.request_id)
            ocr_text = "\n".join(result.get("outputs", []))

            if not ocr_text.strip():
                await message.channel.send("No text found in image.")
                os.remove(temp_file_path)
                return

            # Interpret with OpenAI
            parsed = await asyncio.get_event_loop().run_in_executor(
                executor, lambda: parse_ocr_with_openai(ocr_text)
            )

            # Create Todoist task
            await asyncio.get_event_loop().run_in_executor(
                executor, lambda: create_todoist_task(parsed)
            )

            # Build email body
            email_lines = [f"Summary: {parsed['summary']}", ""]
            for field in ["who", "what", "when", "where", "why"]:
                val = parsed.get(field)
                if val:
                    email_lines.append(f"{field.capitalize()}: {val}")
            if parsed.get("web_links"):
                email_lines.append("\nLinks:")
                for link in parsed["web_links"]:
                    email_lines.append(f"  {link}")
            email_lines.append(f"\n--- Full OCR Text ---\n{ocr_text}")

            # Send email with image attached
            await asyncio.get_event_loop().run_in_executor(
                executor,
                lambda: send_email(
                    f"OCR Capture: {parsed['todoist_title']}",
                    "\n".join(email_lines),
                    image_response.content,
                    attachment.filename or "ocr_image.jpg",
                ),
            )

            # Backup source image to Drive
            await asyncio.get_event_loop().run_in_executor(
                executor, lambda: upload_to_drive(temp_file_path, f"ocr_{message.id}.jpg", "image/jpeg")
            )

            # Discord response
            reply_parts = [
                f"**Task created:** {parsed['todoist_title']}",
                f"**Email sent** with full text + image",
                f"\n**Summary:** {parsed['summary']}",
            ]
            for field in ["who", "when", "where"]:
                val = parsed.get(field)
                if val:
                    reply_parts.append(f"**{field.capitalize()}:** {val}")
            await message.channel.send("\n".join(reply_parts))

        elif mode == "cinema":
            duration = CINEMA_DURATIONS.get(modifier, CINEMA_DURATIONS["default"])
            user_prompt = prompt_text if prompt_text else DEFAULT_PROMPT_VIDEO
            await message.channel.send(f"sending cinema ({duration}s)...")

            fal_response = fal_client.submit(
                CINEMA_MODEL,
                arguments={
                    "prompt": user_prompt,
                    "start_image_url": fal_upload_url,
                    "duration": duration,
                    "aspect_ratio": "16:9",
                    "negative_prompt": "blur, distort, and low quality",
                    "cfg_scale": 0.5,
                },
            )
            result = await get_fal_result(CINEMA_MODEL, fal_response.request_id)

            if "video" in result:
                result_url = result["video"]["url"]
                await message.channel.send(f"cinema:\n{result_url}")
                await asyncio.get_event_loop().run_in_executor(
                    executor, lambda: backup_result_url(result_url, f"cinema_{message.id}.mp4", "video/mp4")
                )
            else:
                await message.channel.send("No result returned. Check your input or Fal API.")

        elif mode == "video":
            duration = VIDEO_DURATIONS.get(modifier, VIDEO_DURATIONS["default"])
            user_prompt = prompt_text if prompt_text else DEFAULT_PROMPT_VIDEO
            await message.channel.send(f"sending video ({duration}s)...")

            fal_response = fal_client.submit(
                VIDEO_MODEL,
                arguments={
                    "prompt": user_prompt,
                    "image_url": fal_upload_url,
                    "duration": duration,
                    "resolution": "1080p",
                    "fps": 25,
                    "generate_audio": True,
                },
            )
            result = await get_fal_result(VIDEO_MODEL, fal_response.request_id)

            if "video" in result:
                result_url = result["video"]["url"]
                await message.channel.send(f"video:\n{result_url}")
                await asyncio.get_event_loop().run_in_executor(
                    executor, lambda: backup_result_url(result_url, f"video_{message.id}.mp4", "video/mp4")
                )
            else:
                await message.channel.send("No result returned. Check your input or Fal API.")

        else:
            user_prompt = message.content.strip() or DEFAULT_PROMPT_IMAGE
            await message.channel.send("sending image...")

            fal_response = fal_client.submit(
                IMAGE_MODEL,
                arguments={
                    "prompt": user_prompt,
                    "image_url": fal_upload_url,
                    "guidance_scale": 3.5,
                    "num_images": 1,
                    "safety_tolerance": "2",
                    "output_format": "jpeg",
                },
            )
            result = await get_fal_result(IMAGE_MODEL, fal_response.request_id)

            if "images" in result:
                result_url = result["images"][0]["url"]
                await message.channel.send(f"image:\n{result_url}")
                await asyncio.get_event_loop().run_in_executor(
                    executor, lambda: backup_result_url(result_url, f"image_{message.id}.jpg", "image/jpeg")
                )
            else:
                await message.channel.send("No result returned. Check your input or Fal API.")

        os.remove(temp_file_path)

    except Exception as e:
        await message.channel.send(f"Error: {str(e)}")


client.run(DISCORD_TOKEN)
