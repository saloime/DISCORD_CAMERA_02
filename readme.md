# Simpsonify Discord Bot

A Discord bot that converts uploaded images into Simpsons-style cartoon illustrations, generates cinematic or fast video animations, and extracts text from images with smart routing to Todoist and email. Also includes a batch processing script for local folders.

## Features

### Discord Bot (`camera.py`)

Upload an image to Discord with a keyword to trigger different modes:

| Keyword | What it does |
|---------|-------------|
| *(no keyword)* | Applies Simpsons cartoon style to the image |
| `video` | Generates a 10s video at 1080p (LTX 2.3 Fast) |
| `cinema` | Generates a 10s cinematic video (Kling v3 Pro) |
| `short video` / `short cinema` | Shortest duration (6s / 5s) |
| `long video` / `long cinema` | Longest duration (20s / 30s) |
| `ocr` | Extracts text from image → creates Todoist task + emails full text and image |

- Add custom text alongside any video keyword to use as a prompt (e.g. `cinema slow dolly zoom`)
- OCR uses OpenAI to extract who/what/when/where/why, web links, and a summary

### Batch Processor (`simpsonify_batch.py`)
- Watches a local folder for new images
- Simpsonifies each image, then generates an animated video from the result
- Tracks processed files in `processed.json` to avoid re-processing
- Skips its own output files (`simpsons_*`, `video_*`)
- Designed to run on a recurring schedule (e.g. every 10 minutes)

## Requirements

- Python 3.10+
- Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications))
- Fal.ai API Key ([Fal.ai dashboard](https://fal.ai/))
- OpenAI API Key (for OCR text interpretation)
- Todoist API Token ([Todoist Developer Settings](https://todoist.com/app/settings/integrations/developer))
- Gmail App Password (for emailing OCR results)

## Setup

1. Clone this repository:
```bash
git clone https://github.com/yourusername/simpsonify-discord-bot.git
cd simpsonify-discord-bot
```

2. Create a virtual environment and install dependencies:
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Create a `.env` file:
```
DISCORD_TOKEN=your_discord_bot_token
FAL_KEY=your_fal_api_key
OPENAI_API_KEY=your_openai_api_key
TODOIST_API_TOKEN=your_todoist_api_token
GMAIL_SENDER=your_email@gmail.com
GMAIL_RECIPIENT=your_email@gmail.com
GMAIL_SMTP_PASSWORD=your_gmail_app_password
```

4. Run the Discord bot:
```bash
python camera.py
```

5. Run the batch processor:
```bash
python simpsonify_batch.py
```

## Models Used

| Model | Purpose |
|-------|---------|
| `fal-ai/flux-pro/kontext/max` | Image style transfer (Simpsons look) |
| `fal-ai/kling-video/v3/pro/image-to-video` | Cinematic video from stills (`cinema`) |
| `fal-ai/ltx-2.3/image-to-video/fast` | Fast 1080p video from stills (`video`) |
| `fal-ai/got-ocr/v2` | Text extraction from images (`ocr`) |
