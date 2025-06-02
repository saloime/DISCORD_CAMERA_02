import os
import discord
import requests
import tempfile
import asyncio
import concurrent.futures
from dotenv import load_dotenv
import fal_client

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
FAL_KEY = os.getenv("FAL_KEY")

DEFAULT_PROMPT_IMAGE = "in the style of the Simpson's cartoon animation illustration"
DEFAULT_PROMPT_VIDEO = "zoom, camera pan, details in focus"

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

executor = concurrent.futures.ThreadPoolExecutor()
processed_messages = set()

async def get_fal_result(model, request_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: fal_client.result(model, request_id))

@client.event
async def on_ready():
    print(f'✅ Bot connected as {client.user}')
    print(f'✅ Connected to {len(client.guilds)} server(s)')

@client.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"🔥 on_message triggered by: {message.id} | {message.author} | {message.content}")

    if message.id in processed_messages:
        print(f"🔄 Already processed message {message.id}, skipping...")
        return
    processed_messages.add(message.id)

    if message.attachments:
        user_text = message.content.strip().lower()
        is_video = "video" in user_text

        if is_video:
            other_text = user_text.replace("video", "").strip()
            user_prompt = other_text if other_text else DEFAULT_PROMPT_VIDEO
            model = "fal-ai/kling-video/v2/master/image-to-video"
        else:
            user_prompt = message.content.strip() or DEFAULT_PROMPT_IMAGE
            model = "fal-ai/flux-pro/kontext/max"

        attachment = message.attachments[0]
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            await message.channel.send("⚠️ Please attach an image file.")
            return

        image_url = attachment.url
        await message.channel.send(f"sending {'video' if is_video else 'image'}...")

        try:
            # Step 1: Download Discord image to temp file
            image_response = requests.get(image_url)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                temp_file.write(image_response.content)
                temp_file_path = temp_file.name

            # Step 2: Upload the image to Fal
            fal_upload_url = fal_client.upload_file(temp_file_path)
            print(f"✅ Uploaded to Fal: {fal_upload_url}")

            # Step 3: Submit to the appropriate Fal model
            if is_video:
                fal_response = fal_client.submit(
                    model,
                    arguments={
                        "prompt": user_prompt,
                        "image_url": fal_upload_url,
                        "duration": "5",
                        "aspect_ratio": "16:9",
                        "negative_prompt": "blur, distort, and low quality",
                        "cfg_scale": 0.5
                    }
                )
            else:
                fal_response = fal_client.submit(
                    model,
                    arguments={
                        "prompt": user_prompt,
                        "image_url": fal_upload_url,
                        "guidance_scale": 3.5,
                        "num_images": 1,
                        "safety_tolerance": "2",
                        "output_format": "jpeg"
                    }
                )

            request_id = fal_response.request_id

            # Step 4: Poll for result asynchronously
            result = await get_fal_result(model, request_id)

            if is_video and "video" in result:
                result_url = result["video"]["url"]
                await message.channel.send(f"video:\n{result_url}")
            elif not is_video and "images" in result:
                result_url = result["images"][0]["url"]
                await message.channel.send(f"image:\n{result_url}")
            else:
                await message.channel.send("⚠️ No result returned. Check your input or Fal API.")

            os.remove(temp_file_path)

        except Exception as e:
            await message.channel.send(f"❌ Error: {str(e)}")

client.run(DISCORD_TOKEN)
