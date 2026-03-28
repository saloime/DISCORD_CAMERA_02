# Simpsonify Discord Bot

A Discord bot that converts uploaded images into Simpsons-style cartoon illustrations, with optional video animation. Also includes a batch processing script for local folders.

## Features

### Discord Bot (`camera.py`)
- Upload an image to Discord — bot applies a Simpsons cartoon style via Fal.ai
- Add custom text with the image to use as a custom prompt
- Type "video" with an image to generate a 5-second animated clip instead
- Uses `fal-ai/flux-pro/kontext/max` for image stylization
- Uses `fal-ai/kling-video/v2/master/image-to-video` for animation

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
| `fal-ai/kling-video/v2/master/image-to-video` | 5-second video animation from stills |
