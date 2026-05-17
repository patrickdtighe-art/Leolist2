import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import AsyncOpenAI
from PIL import Image
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

APP_NAME = "Leolist Sign Scanner"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_LISTINGS_DEFAULT = int(os.getenv("MAX_LISTINGS", "15"))
MAX_IMAGES_PER_LISTING = int(os.getenv("MAX_IMAGES_PER_LISTING", "30"))
MAX_AI_CALLS = int(os.getenv("MAX_AI_CALLS", "120"))
RESULT_DIR = "static/results"
BASE_URL = "https://www.leolist.cc"

app = FastAPI(title=APP_NAME)
os.makedirs(RESULT_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

CITY_URLS = {
    "Northern Alberta / Grande Prairie": "https://www.leolist.cc/personals/female-escorts/northern_alberta/grande_prairie",
    "Northern Alberta / Fort McMurray": "https://www.leolist.cc/personals/female-escorts/northern_alberta/fort_mcmurray",
    "Edmonton": "https://www.leolist.cc/personals/female-escorts/edmonton",
    "Calgary": "https://www.leolist.cc/personals/female-escorts/calgary",
    "Vancouver": "https://www.leolist.cc/personals/female-escorts/vancouver",
    "Toronto": "https://www.leolist.cc/personals/female-escorts/toronto",
    "Winnipeg": "https://www.leolist.cc/personals/female-escorts/winnipeg",
    "Saskatoon": "https://www.leolist.cc/personals/female-escorts/saskatoon",
    "Regina": "https://www.leolist.cc/personals/female-escorts/regina",
    "Ottawa": "https://www.leolist.cc/personals/female-escorts/ottawa",
    "Montreal": "https://www.leolist.cc/personals/female-escorts/montreal",
    "Halifax": "https://www.leolist.cc/personals/female-escorts/halifax",
}

@dataclass
class FoundSign:
    image_url: str
    saved_path: str
    listing_url: str
    text_visible: str
    sign_type: str
    description: str
    confidence: str
    hash: str


def now_id() -> str:
    return str(int(time.time() * 1000))


def normalize_url(url: str, base: str) -> str | None:
    if not url:
        return None
    url = url.strip().strip('"\'')
    if url.startswith("data:") or url.startswith("blob:"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("/"):
        url = urljoin(base, url)
    if not url.startswith("http"):
        return None
    low = url.lower()
    if any(x in low for x in ["logo", "sprite", "avatar-placeholder", "placeholder", "google", "doubleclick"]):
        return None
    if any(low.endswith(ext) for ext in [".svg", ".gif"]):
        return None
    return url


async def safe_goto(page, url: str):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


async def auto_scroll(page, rounds: int = 6):
    for _ in range(rounds):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(700)


async def collect_listing_links(page, city_url: str, max_listings: int) -> list[str]:
    await safe_goto(page, city_url)
    await auto_scroll(page, 5)
    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)")
    links: list[str] = []
    seen = set()
    for href in hrefs:
        if not href:
            continue
        h = href.split("#")[0]
        low = h.lower()
        # Broad enough for Leolist ad pages, narrow enough to avoid menus/categories.
        looks_like_ad = any(x in low for x in ["/ad/", "classified", "posting", "escorts/"]) and h != city_url
        if looks_like_ad and h.startswith("http") and h not in seen:
            seen.add(h)
            links.append(h)
        if len(links) >= max_listings:
            break
    if not links:
        # Fallback: use same-domain links deeper than category page.
        parsed = urlparse(city_url)
        for href in hrefs:
            if href and urlparse(href).netloc == parsed.netloc and href.rstrip("/") != city_url.rstrip("/") and href not in seen:
                seen.add(href)
                links.append(href)
            if len(links) >= max_listings:
                break
    return links[:max_listings]


async def extract_images(page, base_url: str) -> list[str]:
    urls = set()
    await page.wait_for_timeout(2500)
    await auto_scroll(page, 4)

    js = """
    () => {
      const out = [];
      const attrs = ['src','data-src','data-lazy-src','data-original','data-url','data-full','data-image','href','srcset'];
      for (const el of document.querySelectorAll('*')) {
        for (const a of attrs) {
          const v = el.getAttribute && el.getAttribute(a);
          if (v) out.push(v);
        }
        const st = window.getComputedStyle(el);
        if (st && st.backgroundImage && st.backgroundImage !== 'none') out.push(st.backgroundImage);
        const inline = el.getAttribute && el.getAttribute('style');
        if (inline) out.push(inline);
      }
      return out;
    }
    """
    vals = await page.evaluate(js)
    for val in vals:
        for part in str(val).split(","):
            candidates = re.findall(r'https?://[^\s\)"\']+', part)
            candidates += re.findall(r'url\(["\']?([^"\')]+)', part)
            if not candidates:
                candidates = [part.strip()]
            for c in candidates:
                u = normalize_url(c, base_url)
                if u:
                    low = u.lower()
                    if any(ext in low for ext in [".jpg", ".jpeg", ".png", ".webp", "image"]):
                        urls.add(u)
    return list(urls)[:MAX_IMAGES_PER_LISTING]


async def fetch_image(url: str) -> tuple[bytes | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(url)
            ct = r.headers.get("content-type", "")
            if r.status_code >= 400 or "image" not in ct:
                return None, f"HTTP {r.status_code} {ct}"
            data = r.content
            if len(data) < 2000:
                return None, "image too small"
            return data, None
    except Exception as e:
        return None, str(e)


def image_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare_image(data: bytes) -> tuple[bytes | None, str | None]:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if w < 120 or h < 120:
            return None, "image dimensions too small"
        img.thumbnail((1400, 1400))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=84)
        return out.getvalue(), None
    except Exception as e:
        return None, str(e)


async def analyze_image(client: AsyncOpenAI, jpg: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(jpg).decode("ascii")
    prompt = """Detect ANY visible sign or intentionally displayed text in this image. This includes signs, labels, notes, paper, posters, cards, boards, screens, handwritten text, printed text, verification signs, usernames, dates, or any object containing readable displayed text. It does not need to be held by a person. Return JSON only with: sign_detected boolean, sign_type string, text_visible string, description string, confidence low/medium/high. If none, sign_detected false."""
    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        response_format={"type": "json_object"},
        max_tokens=350,
    )
    txt = resp.choices[0].message.content or "{}"
    return json.loads(txt)


def render_html(body: str) -> str:
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'><title>{APP_NAME}</title>
<style>body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:24px;color:#111}}.card{{background:white;border-radius:22px;padding:22px;margin:0 auto 22px;max-width:880px;box-shadow:0 8px 30px #0001}}select,input,button{{font-size:18px;padding:12px;border-radius:12px;border:1px solid #ccc;max-width:100%}}button{{background:#111;color:white;border:0;font-weight:700}}table{{width:100%;border-collapse:collapse}}td{{border-top:1px solid #ddd;padding:12px;vertical-align:top}}td:first-child{{font-weight:700;width:45%}}img{{max-width:100%;border-radius:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px}}</style></head><body>{body}</body></html>"""


@app.get("/health")
async def health():
    return {"ok": True, "openai_key_present": bool(os.getenv("OPENAI_API_KEY")), "model": OPENAI_MODEL}


@app.get("/", response_class=HTMLResponse)
async def home():
    options = "".join(f"<option>{c}</option>" for c in CITY_URLS)
    body = f"""<div class='card'><h1>{APP_NAME}</h1><form method='post' action='/scan'><p><label>City/location</label><br><select name='city'>{options}</select></p><p><label>Listings to open</label><br><input name='max_listings' type='number' min='1' max='50' value='{MAX_LISTINGS_DEFAULT}'></p><button type='submit'>Start scan</button></form><p><a href='/health'>Health check</a></p></div>"""
    return render_html(body)


@app.post("/scan", response_class=HTMLResponse)
async def scan(request: Request, city: str = Form(...), max_listings: int = Form(MAX_LISTINGS_DEFAULT)):
    diag: dict[str, Any] = {
        "OPENAI_API_KEY present": "yes" if os.getenv("OPENAI_API_KEY") else "no",
        "Selected location": city,
        "Listings found": 0,
        "Listings opened": 0,
        "Images found": 0,
        "Images scanned": 0,
        "OpenAI vision calls": 0,
        "OpenAI/API errors": "none",
        "Duplicate images skipped before AI": 0,
        "Duplicate signs skipped": 0,
        "Extraction errors": "none",
    }
    found: list[FoundSign] = []
    seen_image_hashes = set()
    seen_sign_keys = set()
    errors = []

    if city not in CITY_URLS:
        return JSONResponse({"error": "Invalid city", "valid": list(CITY_URLS)}, status_code=400)
    if not os.getenv("OPENAI_API_KEY"):
        diag["Likely problem"] = "OPENAI_API_KEY is missing in Railway Variables."
        return HTMLResponse(render_result(diag, found))

    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    max_listings = max(1, min(int(max_listings), 50))

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148", viewport={"width": 390, "height": 1200})
            page = await context.new_page()
            links = await collect_listing_links(page, CITY_URLS[city], max_listings)
            diag["Listings found"] = len(links)

            for link in links:
                if diag["OpenAI vision calls"] >= MAX_AI_CALLS:
                    break
                ad = await context.new_page()
                try:
                    await safe_goto(ad, link)
                    diag["Listings opened"] += 1
                    img_urls = await extract_images(ad, link)
                    diag["Images found"] += len(img_urls)
                    for img_url in img_urls:
                        raw, err = await fetch_image(img_url)
                        if not raw:
                            continue
                        h = image_sha(raw)
                        if h in seen_image_hashes:
                            diag["Duplicate images skipped before AI"] += 1
                            continue
                        seen_image_hashes.add(h)
                        jpg, prep_err = prepare_image(raw)
                        if not jpg:
                            continue
                        diag["Images scanned"] += 1
                        try:
                            diag["OpenAI vision calls"] += 1
                            result = await analyze_image(client, jpg)
                        except Exception as e:
                            errors.append(str(e)[:180])
                            continue
                        if result.get("sign_detected"):
                            text = str(result.get("text_visible") or "").strip().lower()
                            desc = str(result.get("description") or "").strip().lower()
                            sign_key = hashlib.sha256((text + "|" + desc).encode()).hexdigest()
                            if sign_key in seen_sign_keys:
                                diag["Duplicate signs skipped"] += 1
                                continue
                            seen_sign_keys.add(sign_key)
                            fname = f"sign_{now_id()}_{len(found)+1}.jpg"
                            path = os.path.join(RESULT_DIR, fname)
                            with open(path, "wb") as f:
                                f.write(jpg)
                            found.append(FoundSign(img_url, "/" + path, link, str(result.get("text_visible") or ""), str(result.get("sign_type") or ""), str(result.get("description") or ""), str(result.get("confidence") or ""), h[:12]))
                except Exception as e:
                    errors.append(str(e)[:180])
                finally:
                    await ad.close()
            await browser.close()
    except Exception as e:
        errors.append(str(e)[:250])

    if errors:
        diag["OpenAI/API errors"] = "; ".join(errors[:4])
    if diag["Listings found"] == 0:
        diag["Likely problem"] = "No listing links were found. The site may have changed its HTML, blocked access, or the selected city has no visible listings."
    elif diag["Images found"] == 0:
        diag["Likely problem"] = "Listings opened, but no usable image URLs were found. The extractor may need a site-specific selector update."
    elif diag["OpenAI vision calls"] == 0:
        diag["Likely problem"] = "Images were found, but none were usable after download/filtering."
    elif not found:
        diag["Likely problem"] = "Images were scanned, but no signs or displayed text were detected. Try more listings."
    else:
        diag["Likely problem"] = "none"

    return HTMLResponse(render_result(diag, found))


def render_result(diag: dict[str, Any], found: list[FoundSign]) -> str:
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in diag.items())
    cards = "".join(f"<div><img src='{s.saved_path}'><p><b>Text:</b> {s.text_visible or 'unknown'}</p><p><b>Type:</b> {s.sign_type}</p><p>{s.description}</p><p><a href='{s.listing_url}'>Listing</a></p></div>" for s in found)
    if not cards:
        cards = "<p>No signs found. Try more listings/pages.</p>"
    body = f"<div class='card'><h1>Scan diagnostics</h1><table>{rows}</table></div><div class='card'><h1>{len(found)} unique sign(s) found</h1><div class='grid'>{cards}</div><p><a href='/'>Run another scan</a></p></div>"
    return render_html(body)
