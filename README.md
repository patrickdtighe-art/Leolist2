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


Patched version: improved Leolist compatibility, anti-bot handling, JS loading, and lazy image extraction.

Full-city scanning update:
- The old default cap of 40 images has been removed.
- In the web UI, set "Max listing/detail pages to open" to 0 to scan all ads discovered in the selected city/category.
- Set "Max images to scan" to 0 to scan every usable image found.
- The scanner now walks pagination/next pages and records city pagination pages visited plus listing/detail pages discovered in diagnostics.

## Live scan/debug dashboard patch

This version no longer blocks the browser while a full city scan runs. Press **Start scan** and the app creates a background job, then redirects to `/status/<job_id>`.

Live pages/files:

- `/status/<job_id>` - auto-refreshing progress dashboard
- `/logs/<job_id>` - live text log
- `/job/<job_id>.json` - machine-readable job status
- `/debug/<job_id>/latest-page.jpg` - what Railway's browser saw on the latest city/listing page
- `/debug/<job_id>/latest.html` - HTML Railway received
- `/debug/<job_id>/all_listing_links.txt` - all listing/detail links discovered
- `/debug/<job_id>/images.txt` - latest image URLs found

Use these debug files if Leolist returns zero links or zero images. The screenshot/HTML will show whether Railway is blocked, redirected, or seeing a different page than Safari.

## Individual-ad scanning patch

This version does not scan the city/category page as an ad. It first discovers real ad/detail URLs, then opens each individual ad page, expands/scrolls/clicks likely galleries, extracts the ad photos, hashes images to skip duplicates, and runs the verification-sign detector only on those pictures.

Useful debug links during a scan:
- `/status/<job_id>` live status
- `/debug/<job_id>/all_listing_links.txt` discovered ad/detail links
- `/debug/<job_id>/opened_ad_urls.txt` ads actually opened
- `/debug/<job_id>/all_image_urls.txt` cumulative image URLs found from opened ads
- `/debug/<job_id>/latest-ad-page.jpg` screenshot of the latest opened ad
