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

DEFAULT_PROMPT = "in the style of the Simpson's cartoon animation illustration"

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

executor = concurrent.futures.ThreadPoolExecutor()
processed_messages = set()  # Track processed message IDs

async def get_fal_result(request_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, lambda: fal_client.result("fal-ai/flux-pro/kontext/max", request_id))

@client.event
async def on_ready():
    print(f'✅ Bot connected as {client.user}')
    print(f'✅ Connected to {len(client.guilds)} server(s)')

@client.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"sending images...: {message.id} | {message.author} | {message.content}")

    # Prevent processing the same message multiple times
    if message.id in processed_messages:
        print(f" {message.id}, skipping...")
        return
    processed_messages.add(message.id)

    # Process any message with an attachment
    if message.attachments:
        user_prompt = message.content.strip() or DEFAULT_PROMPT
        attachment = message.attachments[0]

        # Optional: Only process image attachments (not PDFs etc.)
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            await message.channel.send("⚠️ Please attach an image file.")
            return

        image_url = attachment.url
        await message.channel.send(f"sending image...")

        try:
            # Step 1: Download the Discord image to a temp file
            image_response = requests.get(image_url)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_file:
                temp_file.write(image_response.content)
                temp_file_path = temp_file.name

            # Step 2: Upload the image to Fal using fal_client
            fal_upload_url = fal_client.upload_file(temp_file_path)
            print(f"sending image... {fal_upload_url}")

            # Step 3: Submit to Fal model
            fal_response = fal_client.submit(
                "fal-ai/flux-pro/kontext/max",
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
            result = await get_fal_result(request_id)
            if "images" in result:
                result_url = result["images"][0]["url"]
                await message.channel.send(f"image:\n{result_url}")
            else:
                await message.channel.send("⚠️ No image returned. Check your input or Fal API.")

            os.remove(temp_file_path)  # Clean up temp file

        except Exception as e:
            await message.channel.send(f"❌ Error: {str(e)}")

client.run(DISCORD_TOKEN)
