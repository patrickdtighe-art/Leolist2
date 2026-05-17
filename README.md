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


FINAL WORKING DEBUG BUILD
-------------------------
This build saves evidence even when zero confirmed signs are found.

After starting a scan, open:
- /status/<job_id>
- /debug/<job_id>/ai_verdicts.jsonl
- /candidates/<job_id>.zip

The candidates ZIP contains:
- confirmed signs
- possible signs
- sampled scanned ad images

If all_scanned_images contains sign photos but confirmed signs is empty, the AI detector is rejecting them.
If all_scanned_images does not contain ad photos, the crawler/gallery extraction is the problem.



PRO GUI FINAL BUILD
===================

What changed:
- Polished mobile-friendly dashboard UI.
- City selector and custom URL scan modes.
- Full-city scanning with 0 = no cap.
- Opens individual ad/detail pages instead of only city pages.
- Live scan dashboard at /status/<job_id>.
- Live logs at /logs/<job_id>.
- Job JSON at /job/<job_id>.json.
- Debug files are downloadable from the dashboard.
- Confirmed signs ZIP at /signs/<job_id>.zip.
- Possible signs + scanned sample images ZIP at /candidates/<job_id>.zip.
- Saves AI verdict audit trail to ai_verdicts.jsonl.
- Saves sampled scanned images so zero-result runs can be diagnosed.
- Uses duplicate image/sign filtering.
- Includes compile, backend smoke, and GUI smoke tests.

Important:
If confirmed signs are still zero:
1. Download "Possible signs + scanned images ZIP".
2. If sign photos are in all_scanned_images but not possible_signs, the AI rejected them.
3. If sign photos are not in all_scanned_images, the crawler is not getting those gallery images from the site/Railway.
4. Check latest-ad-page.jpg, opened_ad_urls.txt, all_image_urls.txt, and ai_verdicts.jsonl from the dashboard.

Railway:
Set OPENAI_API_KEY in Railway variables.
Start command should be similar to:
uvicorn main:app --host 0.0.0.0 --port $PORT


NO-HANG BUILD
=============
This build adds:
- hard timeout around Chromium launch
- hard timeout around city-page navigation
- hard timeout around individual-ad navigation
- shorter lazy-load scroll loops
- heartbeat logs every 20 seconds
- pause/resume/cancel controls
- Railway-safe crawl defaults

Optional Railway variables:
SCAN_NAV_TIMEOUT_MS=25000
SCAN_STEP_TIMEOUT_SEC=90
SCAN_MAX_CITY_PAGES=80
SCAN_MAX_EMPTY_PAGES=4

If it appears stuck, open the live status page and click Live logs. The last Heartbeat line tells you exactly what step is stuck.


RAILWAY PORT FIX
================
This build fixes:
Error: Invalid value for '--port': '$PORT' is not a valid integer.

Use the included Dockerfile/railway.json/Procfile. If Railway still shows the same error,
go to Railway > Service > Settings > Deploy > Start Command and set it exactly to:

sh -c 'uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}'

Do NOT use:
uvicorn main:app --host 0.0.0.0 --port $PORT

because Railway may pass $PORT literally unless it runs through a shell.

VISIBLE REVIEW MODE BUILD
=========================
This build changes zero-result behavior.

Instead of only showing "0 signs", it now:
- saves every opened ad screenshot in opened_ad_screenshots/
- saves all scanned image samples in all_scanned_images/
- saves possible/review images in possible_signs/
- adds /review/<job_id> to visually inspect what the app actually scanned
- includes opened ad screenshots in the candidates ZIP
- uses a more inclusive physical verification signs prompt
- treats possible_sign / needs_human_review as visible review hits

How to diagnose:
1. Start a scan.
2. Open the dashboard.
3. Click "Review images".
4. If sign photos appear there, the app is scanning correctly.
5. If sign photos do not appear there, the crawler is not reaching/extracting those ad gallery images from Railway.


HANDWRITING SIMILARITY BUILD
============================
Added:
- Rendered gallery fallback for Leolist/question-mark image issues.
- Review images page.
- Handwriting similarity comparison.
- Automatic handwriting report after each scan.
- /handwriting/<job_id> visual match dashboard.
- /handwriting/<job_id>.json and /debug/<job_id>/handwriting_report.json report download.
- Clusters likely similar handwriting.
- Pairwise side-by-side comparisons.

Important:
Handwriting matching is probabilistic visual similarity, not forensic proof.
Use it as a lead/review tool only.

Railway variables:
OPENAI_API_KEY=your key
Optional:
HANDWRITING_MATCH_THRESHOLD=0.72
HANDWRITING_POSSIBLE_THRESHOLD=0.58
REVIEW_MODE_INCLUDE_ALL_AI_IMAGES=1
SCAN_NAV_TIMEOUT_MS=25000
SCAN_MAX_CITY_PAGES=80


CLOUDFLARE-AWARE BUILD
======================
This build does not try to bypass Cloudflare/security verification.

It detects Cloudflare/security verification pages and stops wasting scans on them.
It also adds /manual so you can upload saved ad/sign images for review and handwriting comparison.

If Railway receives Cloudflare instead of listings, the app writes:
BLOCKED_BY_CLOUDFLARE.txt

Manual fallback:
1. Open /manual
2. Upload saved ad/sign photos
3. Review images and handwriting matches from the generated job pages


PROFILE OPENING FIX
===================
Fixes sites such as CanadaEscorts where ads are opened through "View Profile" buttons.

Changes:
- treats View Profile / Profile / View Ad / Details buttons as individual ads
- extracts href/data-url/onclick/card links
- click-fallback records the navigated profile URL
- avoids staying on the city results page and scanning only thumbnails
