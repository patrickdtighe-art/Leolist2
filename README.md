# Verification Sign Scanner

Railway-ready app for scanning websites for physical verification signs inside photos.

## It detects

Physical signs like:
- handwritten paper verification notes
- username/date cards
- paper signs, notes, cardboard, posters, labels inside photos
- signs like "LeoList.cc 28/09/25"

## It ignores

- website UI text
- menus
- headers/footers
- disclaimers
- modals/popups
- category pages
- normal webpage text
- watermarks/logos unless they appear on a physical sign

## Features

- Custom website URL input
- Leolist city selector presets
- Custom Leolist URL option
- scans internal listing/detail pages
- extracts image URLs from img/srcset/data-src/background-image/meta tags
- screenshot fallback is OFF by default to avoid false positives from website UI
- duplicate image skipping before OpenAI calls
- duplicate sign skipping after detection
- `/health` endpoint
- `/selftest` endpoint that costs $0 OpenAI credits

## Railway

1. Upload to GitHub.
2. Deploy on Railway.
3. Add variable:
   OPENAI_API_KEY=your_key
4. Delete any custom Start Command. Let Dockerfile run the app.
5. Test `/health`
6. Test `/selftest`

Expected `/selftest`:

```json
"ok": true
```
