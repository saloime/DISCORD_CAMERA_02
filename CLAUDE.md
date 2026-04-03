# Discord Camera 2

## Project overview

A Discord bot + PWA that processes phone camera images through AI models. Four modes: Simpsons style transfer, OCR with smart routing, and two video generation modes. Backend is Python (Flask + discord.py), frontend is a vanilla JS PWA.

## Architecture

- `camera.py` — main server: Discord bot, Flask API, all processing logic
- `static/index.html` — PWA with camera capture, job queue, result panels
- `static/sw.js` — service worker for offline shell caching
- `simpsonify_batch.py` — cron-based batch processor for local folders

## Key integration flow (OCR)

### Discord path (auto-create)
Image -> fal.ai upload -> OpenAI gpt-4o-mini vision -> structured JSON -> auto-create Todoist tasks + Calendar events + Email + Drive + SMS -> Discord embed reply

### PWA path (confirm-then-create)
Image -> fal.ai upload -> OpenAI vision -> structured JSON -> Email + Drive + SMS (immediate) -> user sees editable task preview -> user confirms -> POST /api/confirm-tasks -> Todoist + Calendar

## API endpoints

- `POST /api/process` — submit image for processing (returns job_id)
- `GET /api/status/<job_id>` — SSE stream for job progress
- `POST /api/confirm-tasks` — create Todoist tasks + calendar after user review (PWA only)

## OCR prompt structure

The OpenAI prompt returns:
- `confidence` (0.0-1.0) — OCR quality score; below 0.5 skips auto-creation in Discord
- `tasks[]` — each with `title` (actionable verb), `due_string`, `priority` (1-4), `create` (bool)
- `is_event`, `event_title` (plain name, never a tagline), `event_start/end`, `event_location`
- Standard fields: `full_text`, `summary`, `who/what/when/where/why`, `web_links`

## PWA features

- **One-tap scan**: blue OCR button next to shutter captures + submits as OCR instantly
- **Sticky mode**: last-used mode saved to localStorage, auto-selected on next capture
- **Task preview**: OCR results show editable checkboxes for each task; user confirms before creation
- **Calendar toggle**: detected events shown with a checkbox to add to Google Calendar

## Future improvements

- Todoist project routing (map OCR-detected categories to Todoist project IDs)
- Discord reaction-based undo (react with X to delete just-created tasks)

## Commands

```bash
# Run the bot + API server
python camera.py

# Run batch processor
python simpsonify_batch.py

# Install dependencies
pip install -r requirements.txt
```

## Environment

All secrets in `.env`. Never commit `.env` or `client_secret_*.json`.

## Code conventions

- No type annotations unless already present
- Print statements for logging (no logging module)
- Try/except around each integration (Todoist, Calendar, Email, Drive, SMS) so one failure doesn't block others
- Flask + discord.py run in the same process
- PWA is vanilla JS, no build step, no frameworks
