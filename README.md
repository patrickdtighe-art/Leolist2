# Any Website Sign Scanner

Railway-ready FastAPI app.

## What it does

- Lets you enter any website URL, not only Leolist
- Crawls links from the page
- Opens listing/detail pages
- Extracts images from:
  - img src
  - data-src / data-lazy-src / srcset
  - CSS background-image
  - OpenGraph image tags
- Takes page screenshots as fallback
- Sends images/screenshots to OpenAI Vision
- Detects ANY visible sign, label, note, poster, card, board, printed/handwritten text, or displayed text
- Skips duplicate images before AI calls
- Skips duplicate detected signs after AI calls

## Railway

1. Upload this repo to GitHub.
2. Deploy on Railway.
3. Add Railway variable:
   OPENAI_API_KEY=your_new_key
4. Delete any custom Railway Start Command.
5. Let the Dockerfile run the app.

## Health check

Open:

/health



## Runtime self-test

After Railway deploy, open:

/selftest

Expected:
```json
"ok": true
```

This tests Playwright/Chromium and extraction without spending OpenAI credits.
