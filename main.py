
import base64
import hashlib
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from openai import AsyncOpenAI
from PIL import Image, ImageDraw
from playwright.async_api import async_playwright

APP_TITLE = "Verification Sign Scanner"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

LEOLIST_CITIES = {
    "Northern Alberta / Grande Prairie": "https://www.leolist.cc/personals/female-escorts/northern_alberta/grande_prairie",
    "Northern Alberta / Fort McMurray": "https://www.leolist.cc/personals/female-escorts/northern_alberta/fort_mcmurray",
    "Northern Alberta / Peace River": "https://www.leolist.cc/personals/female-escorts/northern_alberta/peace_river",
    "Edmonton": "https://www.leolist.cc/personals/female-escorts/edmonton",
    "Calgary": "https://www.leolist.cc/personals/female-escorts/calgary",
    "Red Deer": "https://www.leolist.cc/personals/female-escorts/red_deer",
    "Lethbridge": "https://www.leolist.cc/personals/female-escorts/lethbridge",
    "Medicine Hat": "https://www.leolist.cc/personals/female-escorts/medicine_hat",
    "Vancouver": "https://www.leolist.cc/personals/female-escorts/vancouver",
    "Victoria": "https://www.leolist.cc/personals/female-escorts/victoria",
    "Kelowna": "https://www.leolist.cc/personals/female-escorts/kelowna",
    "Kamloops": "https://www.leolist.cc/personals/female-escorts/kamloops",
    "Prince George": "https://www.leolist.cc/personals/female-escorts/prince_george",
    "Saskatoon": "https://www.leolist.cc/personals/female-escorts/saskatoon",
    "Regina": "https://www.leolist.cc/personals/female-escorts/regina",
    "Winnipeg": "https://www.leolist.cc/personals/female-escorts/winnipeg",
    "Toronto": "https://www.leolist.cc/personals/female-escorts/toronto",
    "Ottawa": "https://www.leolist.cc/personals/female-escorts/ottawa",
    "Hamilton": "https://www.leolist.cc/personals/female-escorts/hamilton",
    "London": "https://www.leolist.cc/personals/female-escorts/london",
    "Windsor": "https://www.leolist.cc/personals/female-escorts/windsor",
    "Montreal": "https://www.leolist.cc/personals/female-escorts/montreal",
    "Quebec City": "https://www.leolist.cc/personals/female-escorts/quebec_city",
    "Halifax": "https://www.leolist.cc/personals/female-escorts/halifax",
    "St. John's": "https://www.leolist.cc/personals/female-escorts/st_johns",
}

VISION_PROMPT = """
You are detecting ONLY physical verification signs inside photos.

TARGET EXAMPLES:
- a handwritten paper note/card held by a person
- a paper/card/poster/sign inside the actual photo
- a verification note with a website name, username, date, phone number, or custom text
- a physical label, note, or placard placed in the scene

STRICTLY IGNORE AND RETURN sign_detected=false FOR:
- website UI text
- browser screenshots
- menus, headers, footers, buttons, forms
- disclaimers, modals, popups, cookie notices
- category pages or landing pages
- ordinary webpage text
- logos/watermarks/site names overlaid on the page
- text that is not on a physical object inside a photo

Important distinction:
If the image is a screenshot of a webpage with text, that is NOT a sign.
Only count text on a physical object present in a real photograph.

Return JSON only:
{
  "sign_detected": true or false,
  "sign_type": "paper_note/card/poster/label/placard/other",
  "text_visible": "readable text on the physical sign, otherwise empty",
  "description": "short description of the physical sign and where it appears",
  "confidence": 0.0 to 1.0
}
"""

app = FastAPI(title=APP_TITLE)


@dataclass
class Diagnostics:
    mode: str = ""
    selected_city: str = ""
    target_url: str = ""
    pages_scanned: int = 0
    candidate_links_found: int = 0
    pages_opened: int = 0
    listing_pages_discovered: int = 0
    pagination_pages_visited: int = 0
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


def bad_link_path(path: str) -> bool:
    bad = [
        "login", "signup", "register", "privacy", "terms", "contact", "help", "faq", "about",
        "advertise", "support", "report", "javascript:", "mailto:", "tel:", "policy"
    ]
    return any(x in path for x in bad)


def likely_detail_link(url: str, base: str) -> bool:
    if not same_site(url, base):
        return False
    parsed = urlparse(url)
    p = parsed.path.lower()
    q = parsed.query.lower()
    if not p or p == "/":
        return False
    if bad_link_path(p):
        return False

    detail_words = [
        "ad", "ads", "post", "posting", "listing", "profile", "gallery",
        "personals", "escort", "escorts", "female", "service"
    ]
    if any(x in p for x in detail_words):
        return True
    if re.search(r"/[a-z0-9_-]*\d{3,}[a-z0-9_-]*(/|$)", p):
        return True
    if re.search(r"(^|&)(id|ad|post|listing|item|pid|aid)=", q):
        return True

    return p.count("/") >= 2


async def auto_scroll(page, steps: int = 12):
    for _ in range(steps):
        await page.mouse.wheel(0, 1600)
        await page.wait_for_timeout(650)


async def dismiss_common_modals(page):
    checkbox_selectors = [
        "input[type='checkbox']",
        "label:has-text('I have read')",
        "label:has-text('I agree')",
        "label:has-text('agree')",
    ]
    for sel in checkbox_selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els[:3]:
                try:
                    await el.click(timeout=1200)
                    await page.wait_for_timeout(300)
                except Exception:
                    pass
        except Exception:
            pass

    button_selectors = [
        "button:has-text('Continue')",
        "button:has-text('Accept')",
        "button:has-text('I Agree')",
        "button:has-text('Agree')",
        "button:has-text('Enter')",
        "button:has-text('Close')",
        "input[type='submit']",
        "[role='button']:has-text('Continue')",
        "[role='button']:has-text('Agree')",
        "a:has-text('Continue')",
        "a:has-text('Enter')",
    ]
    for sel in button_selectors:
        try:
            els = await page.query_selector_all(sel)
            for el in els[:3]:
                try:
                    await el.click(timeout=1500)
                    await page.wait_for_timeout(900)
                except Exception:
                    pass
        except Exception:
            pass


async def extract_links(page, base_url: str, max_links: int | None = None) -> list[str]:
    raw = await page.evaluate(
        """() => {
            const out = [];
            const attrs = ["href","data-href","data-url","data-link","data-target","to"];
            for (const el of document.querySelectorAll("a, [href], [data-href], [data-url], [data-link], [onclick], article, .card, .listing, [class*='listing'], [class*='card'], [class*='ad']")) {
                for (const a of attrs) {
                    const v = el.getAttribute && el.getAttribute(a);
                    if (v) out.push(v);
                }
                const oc = el.getAttribute && el.getAttribute("onclick");
                if (oc) out.push(oc);
                const inner = el.querySelector && el.querySelector("a[href]");
                if (inner && inner.href) out.push(inner.href);
            }
            return out;
        }"""
    )

    links = []
    seen = set()
    url_pat = re.compile(r"""https?://[^\s'"<>]+|/[A-Za-z0-9_./?=&%-]+""")
    for item in raw:
        for m in url_pat.findall(str(item)):
            href = urljoin(base_url, m).split("#")[0].rstrip(")")
            if href in seen:
                continue
            if likely_detail_link(href, base_url):
                seen.add(href)
                links.append(href)
            if max_links and len(links) >= max_links:
                return links

    if (not max_links) or len(links) < max_links:
        try:
            handles = await page.query_selector_all("article, .card, .listing, [class*='listing'], [class*='card'], [class*='ad']")
            original = page.url
            click_limit = (max_links * 3) if max_links else min(len(handles), 250)
            for h in handles[:click_limit]:
                if max_links and len(links) >= max_links:
                    break
                try:
                    await h.click(timeout=1000)
                    await page.wait_for_timeout(1000)
                    new_url = page.url.split("#")[0]
                    if new_url != original and new_url not in seen and likely_detail_link(new_url, base_url):
                        seen.add(new_url)
                        links.append(new_url)
                    if page.url != original:
                        await page.goto(original, wait_until="domcontentloaded", timeout=20000)
                        await dismiss_common_modals(page)
                        await page.wait_for_timeout(1000)
                except Exception:
                    pass
        except Exception:
            pass

    return links[:max_links] if max_links else links


async def extract_image_urls(page, base_url: str) -> list[str]:
    urls = set()

    img_data = await page.eval_on_selector_all(
        "img",
        """imgs => imgs.flatMap(img => {
            const vals = [];
            for (const a of ["src","data-src","data-lazy-src","data-original","data-url","data-full","data-image"]) {
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

    meta_data = await page.eval_on_selector_all(
        "meta[property='og:image'],meta[name='twitter:image'],meta[itemprop='image']",
        "els => els.map(m => m.getAttribute('content')).filter(Boolean)"
    )
    for v in meta_data:
        if v:
            urls.add(urljoin(base_url, v))

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

    cleaned = []
    for u in urls:
        if not u.startswith("http"):
            continue
        low = u.lower()
        if any(x in low for x in ["favicon", "sprite", "icon", "logo", "avatar-default"]):
            continue
        cleaned.append(u)
    
    # Extra Leolist-specific image harvesting
    try:
        extra_imgs = await page.eval_on_selector_all(
            "img, a[href*='jpg'], a[href*='jpeg'], a[href*='png']",
            """els => els.flatMap(el => {
                const vals = [];
                if (el.src) vals.push(el.src);
                if (el.href) vals.push(el.href);
                for (const a of ["data-src","data-full","data-original"]) {
                    const v = el.getAttribute && el.getAttribute(a);
                    if (v) vals.push(v);
                }
                return vals;
            })"""
        )

        for v in extra_imgs:
            if v and v.startswith("http"):
                cleaned.append(v)
    except Exception:
        pass

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


def image_fingerprint(data: bytes) -> str:
    try:
        im = Image.open(io.BytesIO(data))
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=75)
        return hashlib.sha256(buf.getvalue()).hexdigest()
    except Exception:
        return hashlib.sha256(data).hexdigest()


def jpeg_bytes(data: bytes) -> bytes:
    im = Image.open(io.BytesIO(data))
    im.thumbnail((1600, 1600))
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=82)
    return out.getvalue()


def to_data_url(data: bytes) -> str:
    try:
        data = jpeg_bytes(data)
    except Exception:
        pass
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


async def analyze_image(client: AsyncOpenAI, data: bytes) -> dict[str, Any]:
    jpg = jpeg_bytes(data)
    b64 = base64.b64encode(jpg).decode("ascii")
    resp = await client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "You detect only physical verification signs inside photos. Reject webpage UI/screenshots."},
            {"role": "user", "content": [
                {"type": "text", "text": VISION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def sign_key(verdict: dict[str, Any]) -> str:
    text = re.sub(r"\s+", " ", (verdict.get("text_visible") or "").lower()).strip()
    desc = re.sub(r"\s+", " ", (verdict.get("description") or "").lower()).strip()
    return hashlib.sha256((text + "|" + desc).encode()).hexdigest()



def increment_url_page(url: str, page_num: int) -> str:
    parsed = urlparse(url)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("page", "p"):
        if key in q:
            q[key] = str(page_num)
            return urlunparse(parsed._replace(query=urlencode(q)))
    sep = "&" if parsed.query else "?"
    return url + f"{sep}page={page_num}"


async def discover_city_listing_pages(context, start_url: str, max_links: int | None, max_city_pages: int = 250) -> tuple[list[str], int]:
    """Walk a city/category page and common pagination links until no new ads are found."""
    listing_links: list[str] = []
    seen_listing_links: set[str] = set()
    seen_city_pages: set[str] = set()
    city_queue: list[str] = [start_url]
    pagination_pages_visited = 0
    empty_page_streak = 0

    while city_queue and len(seen_city_pages) < max_city_pages:
        city_url = city_queue.pop(0).split("#")[0]
        if city_url in seen_city_pages:
            continue
        seen_city_pages.add(city_url)
        pagination_pages_visited += 1

        before_count = len(listing_links)
        page = await context.new_page()
        try:
            await page.goto(city_url, wait_until="domcontentloaded", timeout=60000)
            await dismiss_common_modals(page)
            await page.wait_for_timeout(2500)
            await auto_scroll(page, steps=24)

            for link in await extract_links(page, city_url, None):
                if link not in seen_listing_links and likely_detail_link(link, start_url):
                    seen_listing_links.add(link)
                    listing_links.append(link)
                    if max_links and len(listing_links) >= max_links:
                        await page.close()
                        return listing_links, pagination_pages_visited

            # Find explicit next/page-number links.
            raw_next = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(a => ({href:a.href, text:(a.innerText||a.getAttribute('aria-label')||a.rel||'').trim().toLowerCase()}))"""
            )
            for item in raw_next:
                href = (item.get("href") or "").split("#")[0]
                text = item.get("text") or ""
                if not href or not same_site(href, start_url) or href in seen_city_pages or href in city_queue:
                    continue
                low = href.lower()
                looks_like_page = (
                    "next" in text or "more" in text or text in {">", "›", "»"} or
                    re.search(r"(^|\?|&)(page|p)=\d+", low) or
                    re.search(r"/page/\d+", low)
                )
                if looks_like_page and not bad_link_path(urlparse(href).path.lower()):
                    city_queue.append(href)
        finally:
            await page.close()

        if len(listing_links) == before_count:
            empty_page_streak += 1
        else:
            empty_page_streak = 0

        # Also try ?page=N for sites that do not expose pagination links until later.
        # Stop after several consecutive empty guessed pages so it does not crawl forever.
        if len(seen_city_pages) < max_city_pages and empty_page_streak < 4:
            next_guess = increment_url_page(start_url, len(seen_city_pages) + 1)
            if next_guess not in seen_city_pages and next_guess not in city_queue:
                city_queue.append(next_guess)

        if empty_page_streak >= 4:
            break

    return listing_links, pagination_pages_visited


async def scan_site(
    mode: str,
    target_url: str,
    selected_city: str,
    max_links: int = 0,
    max_images: int = 0,
    screenshot_fallback: bool = False,
) -> tuple[Diagnostics, list[dict[str, Any]]]:
    if mode == "leolist_city":
        target_url = LEOLIST_CITIES.get(selected_city, "")
    else:
        target_url = normalize_url(target_url)

    diag = Diagnostics(mode=mode, selected_city=selected_city, target_url=target_url)

    if not target_url:
        diag.likely_problem = "No website URL or city was selected."
        return diag, []

    if not OPENAI_API_KEY:
        diag.likely_problem = "OPENAI_API_KEY is missing in Railway variables."
        return diag, []

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    results = []
    seen_images = set()
    seen_signs = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1365, "height": 1800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            ignore_https_errors=True,
            locale="en-CA",
            java_script_enabled=True,
        )

        start = await context.new_page()
        try:
            # Discover listing/detail pages across the entire selected city, including pagination.
            # max_links=0 means no intentional listing cap; max_images=0 means no intentional image cap.
            effective_max_links = max_links if max_links and max_links > 0 else None
            links, pagination_count = await discover_city_listing_pages(context, target_url, effective_max_links)
            diag.pagination_pages_visited = pagination_count
            diag.pages_scanned += pagination_count

            if not links and "leolist" in target_url.lower():
                parsed = urlparse(target_url)
                parts = [x for x in parsed.path.split("/") if x]
                parent_urls = []
                for cut in range(len(parts) - 1, 1, -1):
                    parent_urls.append(parsed.scheme + "://" + parsed.netloc + "/" + "/".join(parts[:cut]))
                for parent in parent_urls[:3]:
                    try:
                        links, pagination_count = await discover_city_listing_pages(context, parent, effective_max_links)
                        diag.pagination_pages_visited += pagination_count
                        diag.pages_scanned += pagination_count
                        if links:
                            target_url = parent
                            diag.target_url = parent
                            break
                    except Exception:
                        pass

            diag.candidate_links_found = len(links)
            diag.listing_pages_discovered = len(links)
        except Exception as e:
            diag.extraction_errors.append(f"Could not open start URL: {e}")
            links = []
        finally:
            await start.close()

        # scan start page too, but screenshot fallback remains off by default
        pages = [target_url] + links
        pages = list(dict.fromkeys(pages))
        effective_max_images = max_images if max_images and max_images > 0 else None

        for link in pages:
            if effective_max_images and diag.images_scanned >= effective_max_images:
                break

            pg = await context.new_page()
            try:
                await pg.goto(link, wait_until="domcontentloaded", timeout=45000)
                await dismiss_common_modals(pg)
                await pg.wait_for_timeout(2500)
                await auto_scroll(pg)
                diag.pages_opened += 1

                urls = await extract_image_urls(pg, link)
                diag.images_found += len(urls)
                scanned_on_page = 0

                for img_url in urls:
                    if effective_max_images and diag.images_scanned >= effective_max_images:
                        break
                    data = await fetch_image_bytes(img_url, link)
                    if not data or len(data) < 3500:
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
                        if verdict.get("sign_detected") and float(verdict.get("confidence", 0) or 0) >= 0.55:
                            key = sign_key(verdict)
                            if key in seen_signs:
                                diag.duplicate_signs_skipped += 1
                                continue
                            seen_signs.add(key)
                            results.append({
                                "page_url": link,
                                "image_url": img_url,
                                "preview": to_data_url(data),
                                "verdict": verdict,
                            })
                    except Exception as e:
                        diag.openai_api_errors.append(str(e)[:300])

                if screenshot_fallback and scanned_on_page == 0 and (not effective_max_images or diag.images_scanned < effective_max_images):
                    # Off by default because it causes false positives from website UI.
                    try:
                        shot = await pg.screenshot(full_page=True, type="jpeg", quality=75)
                        fp = image_fingerprint(shot)
                        if fp not in seen_images:
                            seen_images.add(fp)
                            diag.images_scanned += 1
                            diag.screenshot_fallbacks_scanned += 1
                            diag.openai_vision_calls += 1
                            verdict = await analyze_image(client, shot)
                            if verdict.get("sign_detected") and float(verdict.get("confidence", 0) or 0) >= 0.70:
                                key = sign_key(verdict)
                                if key not in seen_signs:
                                    seen_signs.add(key)
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
    if diag.candidate_links_found == 0 and diag.pages_opened <= 1:
        diag.likely_problem = "No detail/listing links were found after aggressive extraction. Try Custom URL with the exact listings page."
    elif diag.images_found == 0:
        diag.likely_problem = "Pages opened, but no usable image URLs were found."
    elif diag.openai_vision_calls == 0:
        diag.likely_problem = "Images were found but none were scanned. They may be blocked or too small."
    elif diag.signs_found == 0:
        diag.likely_problem = "Photos were scanned, but no physical verification signs were detected."
    else:
        diag.likely_problem = "Scan completed."

    return diag, results


@app.get("/health")
async def health():
    return {"ok": True, "openai_key_present": bool(OPENAI_API_KEY), "model": OPENAI_MODEL, "cities": len(LEOLIST_CITIES)}


@app.get("/selftest")
async def selftest():
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
        <a href="https://example.com/listing/123">Mock listing</a>
        <img src="https://example.com/sign.jpg">
        <div style="background-image:url('https://example.com/sign.jpg');width:640px;height:360px"></div>
        </body></html>
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
            report["playwright_browser_launch"] = True
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            report["mock_page_loaded"] = True
            links = await extract_links(page, "https://example.com", 10)
            imgs = await extract_image_urls(page, "https://example.com")
            shot = await page.screenshot(type="jpeg", quality=75)
            report["links_extracted"] = len(links)
            report["images_extracted"] = len(imgs)
            report["screenshot_captured"] = bool(shot and len(shot) > 1000)
            report["image_fingerprint"] = bool(image_fingerprint(shot))
            await page.close()
            await browser.close()
        report["ok"] = all([
            report["playwright_browser_launch"],
            report["mock_page_loaded"],
            report["links_extracted"] >= 1,
            report["images_extracted"] >= 1,
            report["screenshot_captured"],
            report["image_fingerprint"],
        ])
    except Exception as e:
        report["error"] = str(e)
    finally:
        tmp.cleanup()
    return report


@app.get("/", response_class=HTMLResponse)
async def index():
    city_options = "\n".join(
        f'<option value="{html_escape(name)}">{html_escape(name)}</option>'
        for name in LEOLIST_CITIES
    )
    return f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verification Sign Scanner</title>
<style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:22px;color:#111}}
.card{{background:white;border-radius:22px;padding:24px;margin:0 auto 24px;max-width:820px;box-shadow:0 8px 24px #0001}}
h1{{font-size:34px;margin:0 0 12px}}
label{{font-weight:800;display:block;margin-top:16px}}
input,select{{font-size:18px;width:100%;box-sizing:border-box;padding:14px;border:1px solid #ccc;border-radius:12px;background:white}}
button{{font-size:20px;font-weight:900;padding:16px 22px;border:0;border-radius:14px;background:#111;color:#fff;margin-top:20px;width:100%}}
.small{{color:#555;font-size:14px;line-height:1.4}}
.hidden{{display:none}}
</style>
<script>
function updateMode(){{
  const mode = document.querySelector("select[name='mode']").value;
  document.getElementById("cityBox").style.display = mode === "leolist_city" ? "block" : "none";
  document.getElementById("urlBox").style.display = mode === "custom_url" ? "block" : "none";
}}
window.addEventListener("DOMContentLoaded", updateMode);
</script>
</head>
<body>
<div class="card">
<h1>Verification Sign Scanner</h1>
<p class="small">Finds physical handwritten/printed verification signs inside photos. Ignores website UI, disclaimers, menus, and normal webpage text.</p>
<form method="post" action="/scan">
<label>Scan mode</label>
<select name="mode" onchange="updateMode()">
<option value="leolist_city">Leolist city selector</option>
<option value="custom_url">Custom website URL</option>
</select>

<div id="cityBox">
<label>City</label>
<select name="selected_city">{city_options}</select>
</div>

<div id="urlBox">
<label>Website URL</label>
<input name="target_url" placeholder="https://example.com/listings">
</div>

<label>Max listing/detail pages to open</label>
<input name="max_links" type="number" value="0" min="0" max="10000">
<p class="small">Use 0 to scan the entire city/category instead of stopping at a fixed number of ads.</p>

<label>Max images to scan</label>
<input name="max_images" type="number" value="0" min="0" max="50000">
<p class="small">Use 0 to scan every usable image found in the selected city/category.</p>

<label>
<input name="screenshot_fallback" type="checkbox" value="1" style="width:auto">
 Enable screenshot fallback
</label>
<p class="small">Leave screenshot fallback OFF unless image extraction fails. It can cause false positives from webpage text.</p>

<button type="submit">Start scan</button>
</form>
</div>
</body>
</html>
"""


@app.post("/scan", response_class=HTMLResponse)
async def scan(
    mode: str = Form("leolist_city"),
    selected_city: str = Form("Northern Alberta / Grande Prairie"),
    target_url: str = Form(""),
    max_links: int = Form(0),
    max_images: int = Form(0),
    screenshot_fallback: str | None = Form(None),
):
    diag, results = await scan_site(
        mode=mode,
        selected_city=selected_city,
        target_url=target_url,
        max_links=max_links,
        max_images=max_images,
        screenshot_fallback=bool(screenshot_fallback),
    )

    rows = [
        ("Mode", html_escape(diag.mode)),
        ("Selected city", html_escape(diag.selected_city)),
        ("Target URL", html_escape(diag.target_url)),
        ("Pages scanned", diag.pages_scanned),
        ("Candidate links found", diag.candidate_links_found),
        ("City pagination pages visited", diag.pagination_pages_visited),
        ("Listing/detail pages discovered", diag.listing_pages_discovered),
        ("Pages opened", diag.pages_opened),
        ("Images found", diag.images_found),
        ("Images scanned", diag.images_scanned),
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
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:22px;color:#111}}
.card{{background:white;border-radius:22px;padding:24px;margin:0 auto 24px;max-width:900px;box-shadow:0 8px 24px #0001}}
h1{{font-size:34px;margin:0 0 16px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:12px;font-size:17px}}
th{{width:44%;font-weight:900}}
.result{{border-top:1px solid #ddd;padding:18px 0}}
a{{color:#551a8b}}
</style>
</head>
<body>
<div class="card"><h1>Scan diagnostics</h1><table>{row_html}</table></div>
<div class="card">
<h1>{len(results)} unique verification sign(s) found</h1>
{result_html if result_html else "<p>No physical verification signs found.</p>"}
<p><a href="/">Run another scan</a></p>
</div>
</body>
</html>
"""


@app.get("/scan.json")
async def scan_json(
    mode: str = "custom_url",
    target_url: str = "",
    selected_city: str = "Northern Alberta / Grande Prairie",
    max_links: int = 0,
    max_images: int = 0,
    screenshot_fallback: bool = False,
):
    diag, results = await scan_site(mode, target_url, selected_city, max_links, max_images, screenshot_fallback)
    return JSONResponse({"diagnostics": diag.__dict__, "results": results})
