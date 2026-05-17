
import asyncio
import base64
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI
from PIL import Image
from playwright.async_api import async_playwright

APP_TITLE = "Any Website Sign Scanner"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

VISION_PROMPT = """
Detect ANY visible sign or intentionally displayed text in this image.

A sign can be:
- paper, card, note, poster, board, placard, label, sticker
- handwritten or printed text
- username/date/verification sign
- text on a wall, mirror, object, screen, package, ad image, or background
- any readable displayed message

The sign does NOT need to be held by a person.

Return JSON only:
{
  "sign_detected": true or false,
  "sign_type": "paper/card/poster/label/screen/wall/object/other",
  "text_visible": "readable text if any, otherwise empty",
  "description": "short description",
  "confidence": 0.0 to 1.0
}
"""

app = FastAPI(title=APP_TITLE)


@dataclass
class Diagnostics:
    target_url: str = ""
    pages_scanned: int = 0
    candidate_links_found: int = 0
    listings_opened: int = 0
    images_found: int = 0
    images_scanned: int = 0
    screenshot_fallbacks_scanned: int = 0
    openai_vision_calls: int = 0
    openai_api_errors: list[str] = field(default_factory=list)
    extraction_errors: list[str] = field(default_factory=list)
    duplicate_images_skipped_before_ai: int = 0
    duplicate_signs_skipped: int = 0
    signs_found: int = 0
    likely_problem: str = ""


def html_escape(s: Any) -> str:
    import html
    return html.escape(str(s or ""))


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def same_site(url: str, base: str) -> bool:
    try:
        return urlparse(url).netloc.replace("www.", "") == urlparse(base).netloc.replace("www.", "")
    except Exception:
        return False


def looks_like_asset(url: str) -> bool:
    return bool(re.search(r"\.(jpg|jpeg|png|webp|gif|avif)(\?|$)", url, re.I))


def likely_listing_link(url: str, base: str) -> bool:
    if not same_site(url, base):
        return False
    p = urlparse(url).path.lower()
    if not p or p in ["/", ""]:
        return False
    bad = ["login", "signup", "register", "privacy", "terms", "contact", "help", "faq", "about", "javascript:"]
    if any(x in p for x in bad):
        return False
    return True


async def auto_scroll(page, steps: int = 5):
    for _ in range(steps):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(700)


async def extract_links(page, base_url: str, max_links: int) -> list[str]:
    raw = await page.eval_on_selector_all(
        "a[href]",
        """els => els.map(a => a.href).filter(Boolean)"""
    )
    links = []
    seen = set()
    for href in raw:
        href = urljoin(base_url, href)
        href = href.split("#")[0]
        if href in seen:
            continue
        if likely_listing_link(href, base_url):
            seen.add(href)
            links.append(href)
        if len(links) >= max_links:
            break
    return links


async def extract_image_urls(page, base_url: str) -> list[str]:
    urls = set()

    # img attributes and srcset
    img_data = await page.eval_on_selector_all(
        "img",
        """imgs => imgs.flatMap(img => {
            const vals = [];
            for (const a of ["src","data-src","data-lazy-src","data-original","data-url"]) {
                const v = img.getAttribute(a);
                if (v) vals.push(v);
            }
            const srcset = img.getAttribute("srcset") || img.getAttribute("data-srcset");
            if (srcset) {
                for (const part of srcset.split(",")) vals.push(part.trim().split(" ")[0]);
            }
            return vals;
        })"""
    )
    for v in img_data:
        if v:
            urls.add(urljoin(base_url, v))

    # source tags
    source_data = await page.eval_on_selector_all(
        "source",
        """els => els.flatMap(el => {
            const vals = [];
            const srcset = el.getAttribute("srcset") || el.getAttribute("data-srcset");
            if (srcset) {
                for (const part of srcset.split(",")) vals.push(part.trim().split(" ")[0]);
            }
            return vals;
        })"""
    )
    for v in source_data:
        if v:
            urls.add(urljoin(base_url, v))

    # OpenGraph/Twitter images
    meta_data = await page.eval_on_selector_all(
        "meta",
        """els => els.map(m => m.getAttribute("content")).filter(Boolean)"""
    )
    for v in meta_data:
        if looks_like_asset(v):
            urls.add(urljoin(base_url, v))

    # CSS background images
    bg_data = await page.evaluate(
        """() => {
            const out = [];
            for (const el of document.querySelectorAll("*")) {
                const s = getComputedStyle(el);
                const bg = s.backgroundImage || "";
                if (bg && bg.includes("url(")) out.push(bg);
            }
            return out;
        }"""
    )
    for style in bg_data:
        for match in re.findall(r'url\(["\\\']?(.*?)["\\\']?\)', style):
            if match and not match.startswith("data:"):
                urls.add(urljoin(base_url, match))

    # Filter tiny known assets
    cleaned = []
    for u in urls:
        if not u.startswith("http"):
            continue
        low = u.lower()
        if any(x in low for x in ["logo", "favicon", "sprite", "icon"]):
            continue
        cleaned.append(u)

    return list(dict.fromkeys(cleaned))


async def fetch_image_bytes(url: str, referer: str) -> bytes | None:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)
            if r.status_code >= 400:
                return None
            ct = r.headers.get("content-type", "")
            if "image" not in ct and not looks_like_asset(url):
                return None
            return r.content
    except Exception:
        return None


def image_fingerprint(data: bytes) -> str | None:
    try:
        im = Image.open(io.BytesIO(data))
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=75)
        return hashlib.sha256(buf.getvalue()).hexdigest()
    except Exception:
        return hashlib.sha256(data).hexdigest()


def to_data_url(data: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


async def analyze_image(client: AsyncOpenAI, data: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You are a strict visual sign/text detector. Return JSON only."},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    text = resp.choices[0].message.content or "{}"
    return json.loads(text)


async def scan_site(target_url: str, max_pages: int = 1, max_links: int = 10, max_images: int = 40) -> tuple[Diagnostics, list[dict[str, Any]]]:
    target_url = normalize_url(target_url)
    diag = Diagnostics(target_url=target_url)

    if not target_url:
        diag.likely_problem = "No website URL was provided."
        return diag, []

    if not OPENAI_API_KEY:
        diag.likely_problem = "OPENAI_API_KEY is missing in Railway variables."
        return diag, []

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    results = []
    seen_images = set()
    seen_sign_keys = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            viewport={"width": 1365, "height": 1800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        )

        page = await context.new_page()
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            await auto_scroll(page)
            diag.pages_scanned += 1
            links = await extract_links(page, target_url, max_links)
            diag.candidate_links_found = len(links)
        except Exception as e:
            diag.extraction_errors.append(f"Could not open start URL: {e}")
            links = []
        finally:
            await page.close()

        # Include homepage too, because many websites put images directly there.
        pages_to_scan = [target_url] + links[:max_links]
        pages_to_scan = list(dict.fromkeys(pages_to_scan))[: max_links + 1]

        for link in pages_to_scan:
            if diag.images_scanned >= max_images:
                break
            pg = await context.new_page()
            try:
                await pg.goto(link, wait_until="domcontentloaded", timeout=45000)
                await pg.wait_for_timeout(3000)
                await auto_scroll(pg)
                diag.listings_opened += 1

                urls = await extract_image_urls(pg, link)
                diag.images_found += len(urls)

                scanned_on_page = 0
                for img_url in urls:
                    if diag.images_scanned >= max_images:
                        break
                    data = await fetch_image_bytes(img_url, link)
                    if not data or len(data) < 2500:
                        continue
                    fp = image_fingerprint(data)
                    if fp in seen_images:
                        diag.duplicate_images_skipped_before_ai += 1
                        continue
                    seen_images.add(fp)

                    diag.images_scanned += 1
                    scanned_on_page += 1
                    try:
                        diag.openai_vision_calls += 1
                        verdict = await analyze_image(client, data)
                        if verdict.get("sign_detected"):
                            text_key = re.sub(r"\s+", " ", (verdict.get("text_visible") or "").lower()).strip()
                            desc_key = re.sub(r"\s+", " ", (verdict.get("description") or "").lower()).strip()
                            sign_key = hashlib.sha256((text_key + "|" + desc_key).encode()).hexdigest()
                            if sign_key in seen_sign_keys:
                                diag.duplicate_signs_skipped += 1
                                continue
                            seen_sign_keys.add(sign_key)
                            results.append({
                                "page_url": link,
                                "image_url": img_url,
                                "preview": to_data_url(data[:]) if len(data) < 4_000_000 else "",
                                "verdict": verdict,
                            })
                    except Exception as e:
                        diag.openai_api_errors.append(str(e)[:300])

                # Fallback: if no downloadable images, scan screenshot of rendered page.
                if scanned_on_page == 0 and diag.images_scanned < max_images:
                    try:
                        shot = await pg.screenshot(full_page=True, type="jpeg", quality=75)
                        fp = image_fingerprint(shot)
                        if fp not in seen_images:
                            seen_images.add(fp)
                            diag.images_scanned += 1
                            diag.screenshot_fallbacks_scanned += 1
                            diag.openai_vision_calls += 1
                            verdict = await analyze_image(client, shot)
                            if verdict.get("sign_detected"):
                                text_key = re.sub(r"\s+", " ", (verdict.get("text_visible") or "").lower()).strip()
                                desc_key = re.sub(r"\s+", " ", (verdict.get("description") or "").lower()).strip()
                                sign_key = hashlib.sha256((text_key + "|" + desc_key).encode()).hexdigest()
                                if sign_key not in seen_sign_keys:
                                    seen_sign_keys.add(sign_key)
                                    results.append({
                                        "page_url": link,
                                        "image_url": "rendered page screenshot",
                                        "preview": to_data_url(shot),
                                        "verdict": verdict,
                                    })
                    except Exception as e:
                        diag.extraction_errors.append(f"Screenshot fallback failed: {e}")

            except Exception as e:
                diag.extraction_errors.append(f"Page failed {link}: {e}")
            finally:
                await pg.close()

        await context.close()
        await browser.close()

    diag.signs_found = len(results)

    if diag.candidate_links_found == 0 and diag.listings_opened <= 1:
        diag.likely_problem = "No internal listing/detail links were found. The app scanned the submitted page directly using screenshot fallback."
    elif diag.images_found == 0 and diag.screenshot_fallbacks_scanned == 0:
        diag.likely_problem = "No usable images or screenshots were found."
    elif diag.openai_vision_calls == 0:
        diag.likely_problem = "Nothing reached OpenAI Vision."
    elif diag.signs_found == 0:
        diag.likely_problem = "Images/screenshots were scanned, but no signs or displayed text were detected."
    else:
        diag.likely_problem = "Scan completed."

    return diag, results


@app.get("/health")
async def health():
    return {
        "ok": True,
        "openai_key_present": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL,
    }


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Any Website Sign Scanner</title>
<style>
body{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:24px;color:#111}
.card{background:white;border-radius:22px;padding:24px;margin:0 auto 24px;max-width:760px;box-shadow:0 8px 24px #0001}
h1{font-size:36px;margin:0 0 16px}
label{font-weight:700;display:block;margin-top:14px}
input,select{font-size:18px;width:100%;box-sizing:border-box;padding:14px;border:1px solid #ccc;border-radius:12px}
button{font-size:20px;font-weight:800;padding:16px 22px;border:0;border-radius:14px;background:#111;color:#fff;margin-top:18px;width:100%}
.small{color:#555;font-size:14px}
</style>
</head>
<body>
<div class="card">
<h1>Any Website Sign Scanner</h1>
<form method="post" action="/scan">
<label>Website URL</label>
<input name="target_url" placeholder="https://example.com or https://leolist.cc" required>

<label>Max internal pages/listings to open</label>
<input name="max_links" type="number" value="10" min="0" max="50">

<label>Max images/screenshots to scan</label>
<input name="max_images" type="number" value="40" min="1" max="100">

<button type="submit">Start scan</button>
<p class="small">Scans any website URL you enter. Duplicate images are skipped before OpenAI calls.</p>
</form>
</div>
</body>
</html>
"""


@app.post("/scan", response_class=HTMLResponse)
async def scan(
    target_url: str = Form(...),
    max_links: int = Form(10),
    max_images: int = Form(40),
):
    diag, results = await scan_site(target_url, max_links=max_links, max_images=max_images)

    rows = [
        ("Target URL", html_escape(diag.target_url)),
        ("Pages scanned", diag.pages_scanned),
        ("Candidate links found", diag.candidate_links_found),
        ("Pages/listings opened", diag.listings_opened),
        ("Images found", diag.images_found),
        ("Images/screenshots scanned", diag.images_scanned),
        ("Screenshot fallbacks scanned", diag.screenshot_fallbacks_scanned),
        ("OpenAI vision calls", diag.openai_vision_calls),
        ("OpenAI/API errors", "none" if not diag.openai_api_errors else "<br>".join(map(html_escape, diag.openai_api_errors[:5]))),
        ("Extraction errors", "none" if not diag.extraction_errors else "<br>".join(map(html_escape, diag.extraction_errors[:5]))),
        ("Duplicate images skipped before AI", diag.duplicate_images_skipped_before_ai),
        ("Duplicate signs skipped", diag.duplicate_signs_skipped),
        ("Likely problem", html_escape(diag.likely_problem)),
    ]
    row_html = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

    result_html = ""
    for r in results:
        v = r["verdict"]
        img = f'<img src="{r["preview"]}" style="max-width:100%;border-radius:14px">' if r.get("preview") else ""
        result_html += f"""
        <div class="result">
            {img}
            <p><b>Page:</b> <a href="{html_escape(r['page_url'])}">{html_escape(r['page_url'])}</a></p>
            <p><b>Image:</b> {html_escape(r['image_url'])}</p>
            <p><b>Type:</b> {html_escape(v.get('sign_type',''))}</p>
            <p><b>Text:</b> {html_escape(v.get('text_visible',''))}</p>
            <p><b>Description:</b> {html_escape(v.get('description',''))}</p>
            <p><b>Confidence:</b> {html_escape(v.get('confidence',''))}</p>
        </div>
        """

    return f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scan Results</title>
<style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:24px;color:#111}}
.card{{background:white;border-radius:22px;padding:24px;margin:0 auto 24px;max-width:860px;box-shadow:0 8px 24px #0001}}
h1{{font-size:34px;margin:0 0 16px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:12px;font-size:17px}}
th{{width:45%;font-weight:800}}
.result{{border-top:1px solid #ddd;padding:18px 0}}
a{{color:#551a8b}}
button{{font-size:18px;font-weight:800;padding:14px 18px;border:0;border-radius:14px;background:#111;color:#fff}}
</style>
</head>
<body>
<div class="card">
<h1>Scan diagnostics</h1>
<table>{row_html}</table>
</div>
<div class="card">
<h1>{len(results)} unique sign(s) found</h1>
{result_html if result_html else "<p>No signs found. Try a different website URL, more pages, or more images.</p>"}
<p><a href="/">Run another scan</a></p>
</div>
</body>
</html>
"""


@app.get("/scan.json")
async def scan_json(target_url: str, max_links: int = 10, max_images: int = 40):
    diag, results = await scan_site(target_url, max_links=max_links, max_images=max_images)
    return JSONResponse({"diagnostics": diag.__dict__, "results": results})


@app.get("/selftest")
async def selftest():
    """
    Runtime test that does not call OpenAI and does not need internet.
    It verifies FastAPI, Playwright browser launch, link extraction,
    image extraction, screenshot fallback path, and duplicate handling helpers.
    """
    import tempfile
    from pathlib import Path
    from PIL import Image, ImageDraw

    report = {
        "fastapi": True,
        "playwright_browser_launch": False,
        "mock_page_loaded": False,
        "links_extracted": 0,
        "images_extracted": 0,
        "screenshot_captured": False,
        "image_fingerprint": False,
        "ok": False,
        "error": "",
    }

    tmp = tempfile.TemporaryDirectory()
    try:
        site = Path(tmp.name)
        img = Image.new("RGB", (640, 360), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle((60, 100, 580, 250), outline="black", width=5)
        draw.text((110, 160), "SELFTEST SIGN 123", fill="black")
        img.save(site / "sign.jpg", "JPEG")

        html = """
        <html><body>
        <a href="/listing/123">Mock listing</a>
        <img src="/sign.jpg">
        <div style="background-image:url('/sign.jpg');width:640px;height:360px"></div>
        </body></html>
        """

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            report["playwright_browser_launch"] = True
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            report["mock_page_loaded"] = True

            base_url = "https://example.com"
            links = await extract_links(page, base_url, 10)
            imgs = await extract_image_urls(page, base_url)
            shot = await page.screenshot(type="jpeg", quality=75)

            report["links_extracted"] = len(links)
            report["images_extracted"] = len(imgs)
            report["screenshot_captured"] = bool(shot and len(shot) > 1000)
            report["image_fingerprint"] = bool(image_fingerprint(shot))

            await page.close()
            await browser.close()

        report["ok"] = (
            report["playwright_browser_launch"]
            and report["mock_page_loaded"]
            and report["links_extracted"] >= 1
            and report["images_extracted"] >= 1
            and report["screenshot_captured"]
            and report["image_fingerprint"]
        )
    except Exception as e:
        report["error"] = str(e)
    finally:
        tmp.cleanup()

    return report
