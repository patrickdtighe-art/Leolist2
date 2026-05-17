
import base64
import hashlib
import io
import json
import os
import re
import tempfile
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from zipfile import ZipFile, ZIP_DEFLATED

import httpx
from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageOps, ImageFilter, ImageStat
from playwright.async_api import async_playwright

APP_TITLE = "Verification Sign Scanner"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SCAN_NAV_TIMEOUT_MS = int(os.getenv("SCAN_NAV_TIMEOUT_MS", "25000"))
SCAN_STEP_TIMEOUT_SEC = int(os.getenv("SCAN_STEP_TIMEOUT_SEC", "90"))
SCAN_MAX_CITY_PAGES = int(os.getenv("SCAN_MAX_CITY_PAGES", "80"))
SCAN_MAX_EMPTY_PAGES = int(os.getenv("SCAN_MAX_EMPTY_PAGES", "4"))
REVIEW_MODE_INCLUDE_ALL_AI_IMAGES = os.getenv("REVIEW_MODE_INCLUDE_ALL_AI_IMAGES", "1") == "1"
HANDWRITING_MATCH_THRESHOLD = float(os.getenv("HANDWRITING_MATCH_THRESHOLD", "0.72"))
HANDWRITING_POSSIBLE_THRESHOLD = float(os.getenv("HANDWRITING_POSSIBLE_THRESHOLD", "0.58"))

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
You are reviewing ad photos for physical verification signs.

A physical verification sign is any real-world paper, card, note, placard, poster, or written object intentionally shown to the camera by a person or placed near a person.

Be intentionally INCLUSIVE:
- If you see a person holding a paper/card/note, set sign_detected=true.
- If you are unsure but there might be a held paper/card/note, set possible_sign=true.
- Do not require readable text.
- Do not require the word verification.
- Count mirror selfies, cropped images, small cards, handwritten notes, date/username papers, and partly visible papers.

Return false only when there is clearly no physical paper/card/sign-like object.

Do NOT count website UI text, captions, menus, watermarks, tattoos, printed shirt logos, or ordinary background posters.

Return JSON only:
{
  "sign_detected": true or false,
  "possible_sign": true or false,
  "needs_human_review": true or false,
  "sign_type": "paper_note/card/poster/label/placard/other/none",
  "text_visible": "readable text on the physical sign, otherwise empty",
  "description": "short description of any physical paper/card/sign-like object",
  "confidence": 0.0 to 1.0
}
"""



def _load_image_for_analysis(path: Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None

def _image_to_ink_mask(img: Image.Image) -> tuple[Image.Image, dict[str, float]]:
    """Convert a sign image into a rough handwriting/ink mask.

    This is not forensic proof. It gives visual similarity features for review.
    """
    # Normalize size while keeping enough detail.
    img = ImageOps.exif_transpose(img)
    img.thumbnail((900, 900))
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    # Emphasize dark writing on light paper.
    blurred = gray.filter(ImageFilter.GaussianBlur(radius=1.2))
    # Use adaptive-ish threshold based on mean.
    stat = ImageStat.Stat(blurred)
    mean = stat.mean[0] if stat.mean else 180
    threshold = max(70, min(190, mean * 0.78))
    mask = blurred.point(lambda p: 255 if p < threshold else 0)
    # Clean a bit.
    mask = mask.filter(ImageFilter.MedianFilter(size=3))
    w, h = mask.size
    pixels = mask.load()
    xs, ys = [], []
    ink = 0
    for y in range(h):
        for x in range(w):
            if pixels[x, y] > 0:
                ink += 1
                xs.append(x)
                ys.append(y)
    area = max(1, w * h)
    stats = {
        "ink_density": ink / area,
        "width": float(w),
        "height": float(h),
    }
    if xs and ys:
        stats.update({
            "bbox_x1": min(xs) / w,
            "bbox_y1": min(ys) / h,
            "bbox_x2": max(xs) / w,
            "bbox_y2": max(ys) / h,
            "bbox_w": (max(xs) - min(xs) + 1) / w,
            "bbox_h": (max(ys) - min(ys) + 1) / h,
        })
    else:
        stats.update({"bbox_x1":0, "bbox_y1":0, "bbox_x2":0, "bbox_y2":0, "bbox_w":0, "bbox_h":0})
    return mask, stats

def _projection_features(mask: Image.Image, bins: int = 24) -> list[float]:
    w, h = mask.size
    pix = mask.load()
    feats = []
    # vertical projection
    for i in range(bins):
        x1 = int(i * w / bins)
        x2 = int((i + 1) * w / bins)
        total = max(1, (x2 - x1) * h)
        ink = 0
        for y in range(h):
            for x in range(x1, x2):
                if pix[x, y] > 0:
                    ink += 1
        feats.append(ink / total)
    # horizontal projection
    for i in range(bins):
        y1 = int(i * h / bins)
        y2 = int((i + 1) * h / bins)
        total = max(1, (y2 - y1) * w)
        ink = 0
        for y in range(y1, y2):
            for x in range(w):
                if pix[x, y] > 0:
                    ink += 1
        feats.append(ink / total)
    return feats

def _zone_density_features(mask: Image.Image, grid: int = 8) -> list[float]:
    w, h = mask.size
    pix = mask.load()
    feats = []
    for gy in range(grid):
        y1 = int(gy * h / grid)
        y2 = int((gy + 1) * h / grid)
        for gx in range(grid):
            x1 = int(gx * w / grid)
            x2 = int((gx + 1) * w / grid)
            total = max(1, (x2 - x1) * (y2 - y1))
            ink = 0
            for y in range(y1, y2):
                for x in range(x1, x2):
                    if pix[x, y] > 0:
                        ink += 1
            feats.append(ink / total)
    return feats

def _stroke_orientation_features(mask: Image.Image) -> list[float]:
    # Simple directional edge histogram.
    small = mask.resize((180, 180))
    edges = small.filter(ImageFilter.FIND_EDGES)
    pix = edges.load()
    w, h = edges.size
    buckets = [0.0] * 8
    for y in range(1, h - 1, 2):
        for x in range(1, w - 1, 2):
            c = pix[x, y]
            if c < 20:
                continue
            gx = pix[x + 1, y] - pix[x - 1, y]
            gy = pix[x, y + 1] - pix[x, y - 1]
            if gx == 0 and gy == 0:
                continue
            import math
            angle = (math.atan2(gy, gx) + math.pi) / (2 * math.pi)
            bi = min(7, int(angle * 8))
            buckets[bi] += c / 255.0
    total = sum(buckets) or 1.0
    return [b / total for b in buckets]

def handwriting_feature_vector(image_path: Path) -> dict[str, Any] | None:
    img = _load_image_for_analysis(image_path)
    if img is None:
        return None
    mask, stats = _image_to_ink_mask(img)
    feats = []
    feats.extend(_projection_features(mask, bins=24))
    feats.extend(_zone_density_features(mask, grid=8))
    feats.extend(_stroke_orientation_features(mask))
    feats.extend([
        stats["ink_density"],
        stats["bbox_w"],
        stats["bbox_h"],
        stats["bbox_x1"],
        stats["bbox_y1"],
        stats["bbox_x2"],
        stats["bbox_y2"],
    ])
    # Normalize vector.
    import math
    norm = math.sqrt(sum(float(x) * float(x) for x in feats)) or 1.0
    vec = [float(x) / norm for x in feats]
    return {
        "path": str(image_path),
        "filename": image_path.name,
        "features": vec,
        "stats": stats,
    }

def handwriting_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    va = a.get("features") or []
    vb = b.get("features") or []
    if not va or not vb or len(va) != len(vb):
        return 0.0
    dot = sum(float(x) * float(y) for x, y in zip(va, vb))
    # Cosine similarity already roughly 0..1 for nonnegative features.
    return max(0.0, min(1.0, dot))

def handwriting_label(score: float) -> str:
    if score >= HANDWRITING_MATCH_THRESHOLD:
        return "high visual handwriting similarity"
    if score >= HANDWRITING_POSSIBLE_THRESHOLD:
        return "possible handwriting similarity"
    return "different / low similarity"

def collect_handwriting_source_images(job) -> list[Path]:
    root = Path(job.debug_dir)
    folders = ["signs", "possible_signs", "rendered_gallery_captures", "all_scanned_images"]
    paths: list[Path] = []
    seen = set()
    for folder in folders:
        d = root / folder
        if not d.exists():
            continue
        for p in sorted(d.glob("*.jpg")):
            # Prefer likely sign/review folders but include samples so zero-confirmed scans can still be analyzed.
            key = p.read_bytes()[:64] if p.exists() else str(p)
            ident = hashlib.sha256((str(p.stat().st_size) + str(key)).encode(errors="ignore")).hexdigest()
            if ident in seen:
                continue
            seen.add(ident)
            paths.append(p)
            if len(paths) >= 500:
                return paths
    return paths

def build_handwriting_report(job) -> dict[str, Any]:
    paths = collect_handwriting_source_images(job)
    items = []
    for p in paths:
        fv = handwriting_feature_vector(p)
        if fv:
            rel = str(p.relative_to(Path(job.debug_dir)))
            fv["relative_path"] = rel
            fv["url"] = f"/debug/{job.id}/{rel}"
            items.append(fv)

    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            score = handwriting_similarity(items[i], items[j])
            if score >= HANDWRITING_POSSIBLE_THRESHOLD:
                pairs.append({
                    "a": items[i]["relative_path"],
                    "b": items[j]["relative_path"],
                    "a_url": items[i]["url"],
                    "b_url": items[j]["url"],
                    "score": round(score, 4),
                    "label": handwriting_label(score),
                })
    pairs.sort(key=lambda x: x["score"], reverse=True)

    # Union-find clusters for high/possible similarity.
    parent = list(range(len(items)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    index = {it["relative_path"]: n for n, it in enumerate(items)}
    for p in pairs:
        if p["score"] >= HANDWRITING_POSSIBLE_THRESHOLD:
            union(index[p["a"]], index[p["b"]])
    clusters_map: dict[int, list[dict[str, Any]]] = {}
    for n, it in enumerate(items):
        clusters_map.setdefault(find(n), []).append({
            "relative_path": it["relative_path"],
            "url": it["url"],
            "stats": it["stats"],
        })
    clusters = [v for v in clusters_map.values() if len(v) >= 2]
    clusters.sort(key=len, reverse=True)

    report = {
        "job_id": job.id,
        "warning": "Handwriting similarity is probabilistic visual matching for review only, not forensic identification.",
        "threshold_high": HANDWRITING_MATCH_THRESHOLD,
        "threshold_possible": HANDWRITING_POSSIBLE_THRESHOLD,
        "image_count": len(items),
        "pair_count": len(pairs),
        "clusters": clusters,
        "pairs": pairs[:300],
    }

    try:
        root = Path(job.debug_dir)
        (root / "handwriting_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        pass
    return report


def safe_write_jsonl(path: Path, obj: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass

def save_candidate_image(job, data: bytes, verdict: dict, page_url: str, image_url: str, prefix: str):
    try:
        root = Path(job.debug_dir) if job else Path("/tmp/verification_sign_scanner_debug")
        out_dir = root / prefix
        out_dir.mkdir(parents=True, exist_ok=True)
        n = len(list(out_dir.glob("*.jpg"))) + 1
        img_path = out_dir / f"{prefix}_{n:04d}.jpg"
        meta_path = out_dir / f"{prefix}_{n:04d}.json"
        img_path.write_bytes(jpeg_bytes(data))
        meta_path.write_text(json.dumps({
            "page_url": page_url,
            "image_url": image_url,
            "verdict": verdict,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(img_path)
    except Exception:
        return None


app = FastAPI(title=APP_TITLE)


@dataclass
class ScanJob:
    id: str
    created_at: float
    status: str = "queued"
    message: str = "Waiting to start"
    mode: str = ""
    selected_city: str = ""
    target_url: str = ""
    max_links: int = 0
    max_images: int = 0
    screenshot_fallback: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)
    results: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    error: str = ""
    debug_dir: str = ""
    cancel_requested: bool = False
    pause_requested: bool = False

JOBS: dict[str, ScanJob] = {}
JOBS_ROOT = Path(os.getenv("SCANNER_DEBUG_DIR", "/tmp/verification_sign_scanner_jobs"))
JOBS_ROOT.mkdir(parents=True, exist_ok=True)


def get_job(job_id: str) -> ScanJob | None:
    return JOBS.get(job_id)


def log_job(job: ScanJob | None, msg: str):
    if not job:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    job.logs.append(line)
    job.logs = job.logs[-1000:]
    try:
        d = Path(job.debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "live.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def set_job_message(job: ScanJob | None, msg: str):
    if job:
        job.message = msg
        log_job(job, msg)


def write_debug_text(job: ScanJob | None, name: str, text: str):
    if not job:
        return
    try:
        d = Path(job.debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text or "", encoding="utf-8", errors="ignore")
    except Exception as e:
        log_job(job, f"Debug write failed for {name}: {e}")


def write_debug_bytes(job: ScanJob | None, name: str, data: bytes):
    if not job:
        return
    try:
        d = Path(job.debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data or b"")
    except Exception as e:
        log_job(job, f"Debug write failed for {name}: {e}")


def safe_result_for_json(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out=[]
    for r in results:
        rr=dict(r)
        if isinstance(rr.get("preview"), str) and len(rr["preview"]) > 2000:
            rr["preview"] = rr["preview"][:2000] + "..."
        out.append(rr)
    return out


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
    ads_opened: int = 0
    all_image_urls_written: int = 0
    likely_problem: str = ""



def job_age(job: ScanJob) -> str:
    secs = max(0, int(time.time() - job.created_at))
    h, rem = divmod(secs, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

def count_files(job: ScanJob, folder: str, suffix: str = ".jpg") -> int:
    try:
        return len(list((Path(job.debug_dir) / folder).glob(f"*{suffix}")))
    except Exception:
        return 0

def safe_int(v, default=0):
    try:
        return int(v or 0)
    except Exception:
        return default

def pct(done, total):
    done = safe_int(done)
    total = safe_int(total)
    if total <= 0:
        return 0
    return max(0, min(100, int(done * 100 / total)))

def public_debug_files(job: ScanJob) -> list[str]:
    root = Path(job.debug_dir)
    if not root.exists():
        return []
    names = []
    allowed_suffixes = {".txt", ".log", ".jsonl", ".json", ".html", ".jpg", ".png"}
    for p in sorted(root.glob("*")):
        if p.is_file() and (p.suffix.lower() in allowed_suffixes):
            names.append(p.name)
    return names[:200]


async def run_step_with_timeout(coro, label: str, job: ScanJob | None = None, timeout: int | None = None):
    timeout = timeout or SCAN_STEP_TIMEOUT_SEC
    set_job_message(job, label)
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        msg = f"Timed out after {timeout}s: {label}"
        log_job(job, msg)
        raise TimeoutError(msg)



def looks_like_cloudflare_block(text: str, html: str = "") -> bool:
    blob = ((text or "") + "\n" + (html or "")).lower()
    indicators = [
        "performing security verification",
        "checking if the site connection is secure",
        "verify you are human",
        "cloudflare",
        "cf-ray",
        "security service to protect against malicious bots",
        "challenge-platform",
    ]
    return any(x in blob for x in indicators)


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


def strip_pagination_url(url: str) -> str:
    """Return URL without common pagination markers so category pages are not mistaken for ads."""
    try:
        parsed = urlparse(url.split('#')[0])
        q = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in {'page','p','pg','offset'}]
        path = re.sub(r"/page/\d+/?$", "", parsed.path.rstrip('/'), flags=re.I)
        # Do not strip a bare trailing number here; Leolist ad URLs may end in a numeric id.
        return urlunparse(parsed._replace(path=path, query=urlencode(q), fragment='')).rstrip('/')
    except Exception:
        return (url or '').split('#')[0].rstrip('/')


def likely_ad_detail_link(url: str, city_base: str) -> bool:
    """True only for actual ad/detail pages, not city/category/pagination pages.

    The previous crawler used likely_detail_link() directly. On Leolist that is too broad
    because every city page contains words like personals/female-escorts, so pagination
    and category pages were counted as ads. This function requires a real detail shape.
    """
    if not likely_detail_link(url, city_base):
        return False
    if not same_site(url, city_base):
        return False
    u = urlparse(url.split('#')[0])
    b = urlparse(city_base.split('#')[0])
    upath = u.path.rstrip('/').lower()
    bpath = b.path.rstrip('/').lower()
    if not upath or upath == '/':
        return False
    if bad_link_path(upath):
        return False
    # Reject the selected city/category page and common pagination/query variants.
    if strip_pagination_url(url).lower() == strip_pagination_url(city_base).lower():
        return False
    if re.search(r"(^|&)(page|p|pg|offset)=\d+", u.query.lower()):
        # Pagination URLs are city pages, not ads.
        # Keep only if they also have a strong ad id parameter.
        if not re.search(r"(^|&)(id|ad_id|listing_id|post_id|pid|aid)=\d{3,}", u.query.lower()):
            return False
    base_parts = [x for x in bpath.split('/') if x]
    url_parts = [x for x in upath.split('/') if x]
    if 'leolist.' in u.netloc.lower():
        # Real ads are normally deeper than the city category or have a long numeric id/slug.
        if len(url_parts) <= len(base_parts):
            return False
        extra = '/'.join(url_parts[len(base_parts):])
        if re.search(r"\d{4,}", extra):
            return True
        if len(extra) >= 8 and not extra.isdigit():
            return True
        return False

    # CanadaEscorts and similar sites often use profile URLs/buttons rather than
    # obvious ad IDs. Treat profile/view-profile links as individual ads.
    host = u.netloc.lower()
    if "canadaescorts" in host or "escort" in host:
        strong_profile = any(token in upath for token in [
            "profile", "view-profile", "viewprofile", "escort", "model", "girls", "ad", "listing"
        ])
        weak_city_page = any(token in upath for token in [
            "city", "cities", "search", "category", "contact", "privacy", "terms", "login", "register"
        ])
        if strong_profile and not weak_city_page:
            return True
        if re.search(r"\d{4,}", upath + "?" + u.query):
            return True

    return True


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



async def extract_links(page, base_url: str, max_links: int | None = None, city_base_url: str | None = None) -> list[str]:
    """Extract real ad/profile links from listing pages.

    Includes special handling for sites where the clickable target is a
    "View Profile" button or a card onclick rather than a normal obvious ad URL.
    """
    raw = await page.evaluate(
        """() => {
            const out = [];
            const attrs = ["href","data-href","data-url","data-link","data-target","to","onclick"];
            const nodes = document.querySelectorAll(
              "a, button, [role='button'], [href], [data-href], [data-url], [data-link], [onclick], article, .card, .listing, [class*='listing'], [class*='card'], [class*='ad'], [class*='profile']"
            );
            for (const el of nodes) {
                const text = (el.innerText || el.textContent || "").trim();
                for (const a of attrs) {
                    const v = el.getAttribute && el.getAttribute(a);
                    if (v) out.push({value:v, text});
                }
                if (el.href) out.push({value:el.href, text});
                const innerLinks = el.querySelectorAll && el.querySelectorAll("a[href]");
                if (innerLinks) {
                    for (const a of innerLinks) out.push({value:a.href || a.getAttribute("href"), text:(a.innerText || text || "").trim()});
                }
            }
            return out;
        }"""
    )

    links = []
    seen = set()
    url_pat = re.compile(r"""https?://[^\s'"<>]+|/[A-Za-z0-9_./?=&%-]+""")

    def add_link(href: str, text: str = ""):
        href = urljoin(base_url, href).split("#")[0].rstrip(")")
        if not href or href in seen:
            return
        low_text = (text or "").lower()
        is_profile_text = any(t in low_text for t in ["view profile", "profile", "view ad", "details", "more info"])
        is_detail = likely_ad_detail_link(href, city_base_url or base_url) if city_base_url else likely_detail_link(href, base_url)
        # CanadaEscorts can have bland URLs but obvious profile button text.
        if is_profile_text and same_site(href, base_url) and not bad_link_path(urlparse(href).path.lower()):
            is_detail = True
        if is_detail:
            seen.add(href)
            links.append(href)

    for item in raw:
        value = str(item.get("value", "") if isinstance(item, dict) else item)
        text = str(item.get("text", "") if isinstance(item, dict) else "")
        for m in url_pat.findall(value):
            add_link(m, text)
            if max_links and len(links) >= max_links:
                return links

    # Stronger click fallback: click View Profile/profile buttons and record navigated URL.
    if (not max_links) or len(links) < max_links:
        selectors = [
            "a:has-text('View Profile')",
            "button:has-text('View Profile')",
            "[role='button']:has-text('View Profile')",
            "a:has-text('Profile')",
            "button:has-text('Profile')",
            "a:has-text('View Ad')",
            "a:has-text('Details')",
            ".listing a",
            "[class*='listing'] a",
            "[class*='profile'] a",
            "article a",
        ]
        original = page.url
        for sel in selectors:
            try:
                handles = await page.query_selector_all(sel)
            except Exception:
                continue
            for h in handles[:250]:
                if max_links and len(links) >= max_links:
                    return links
                try:
                    href = await h.get_attribute("href")
                    text = await h.inner_text(timeout=500)
                except Exception:
                    href, text = None, ""
                if href:
                    add_link(href, text)
                    if max_links and len(links) >= max_links:
                        return links
                    continue
                try:
                    await h.scroll_into_view_if_needed(timeout=1000)
                    await h.click(timeout=1500, force=True)
                    await page.wait_for_timeout(900)
                    new_url = page.url.split("#")[0]
                    if new_url != original:
                        add_link(new_url, text or "View Profile")
                        await page.goto(original, wait_until="domcontentloaded", timeout=20000)
                        await dismiss_common_modals(page)
                        await page.wait_for_timeout(600)
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




async def expand_ad_gallery_and_collect_images(page, base_url: str, job: ScanJob | None = None) -> list[str]:
    """Collect images from an individual ad page, including lazy galleries.

    This deliberately runs only after an ad/detail URL is opened. It does not scan
    the city listing page as an ad. It also clicks visible gallery thumbnails so
    photos that only load in a modal/carousel get captured.
    """
    collected: list[str] = []

    async def add_current_images():
        try:
            for u in await extract_image_urls(page, base_url):
                if u not in collected:
                    collected.append(u)
        except Exception as e:
            log_job(job, f"Image extraction pass failed: {e}")

    await add_current_images()

    # Leolist and similar sites often lazy-load full photos after scrolling.
    try:
        await auto_scroll(page, steps=18)
    except Exception:
        pass
    await add_current_images()

    # Click likely thumbnails / gallery controls. Keep this bounded so full-city
    # scans do not get stuck on one ad.
    selectors = [
        "img",
        "picture img",
        "[class*='gallery'] img",
        "[class*='photo'] img",
        "[class*='image'] img",
        "[class*='thumb'] img",
        "a[href*='.jpg'], a[href*='.jpeg'], a[href*='.png'], a[href*='.webp']",
        "button[aria-label*='next' i]",
        "button:has-text('Next')",
        "a:has-text('Next')",
        "[class*='next']",
    ]
    clicked = 0
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
        except Exception:
            continue
        for el in els[:30]:
            if clicked >= 60:
                break
            try:
                await el.scroll_into_view_if_needed(timeout=1000)
                await el.click(timeout=1200, force=True)
                clicked += 1
                await page.wait_for_timeout(550)
                await add_current_images()
            except Exception:
                pass
        if clicked >= 60:
            break

    # One more pass after possible modal/carousel opens.
    try:
        await page.keyboard.press('Escape')
    except Exception:
        pass
    await add_current_images()

    return list(dict.fromkeys(collected))


async def save_full_ad_screenshot_review(pg, job: ScanJob | None, page_url: str, reason: str):
    """Always keep visual proof of opened ads so zero-result scans can be inspected."""
    if not job:
        return
    try:
        root = Path(job.debug_dir)
        d = root / "opened_ad_screenshots"
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob("ad_*.jpg"))) + 1
        data = await pg.screenshot(full_page=True, type="jpeg", quality=70)
        (d / f"ad_{n:04d}.jpg").write_bytes(data)
        (d / f"ad_{n:04d}.json").write_text(json.dumps({"page_url": page_url, "reason": reason}, indent=2), encoding="utf-8")
    except Exception as e:
        log_job(job, f"Could not save opened ad screenshot: {e}")



async def collect_rendered_photo_screenshots(page, job: ScanJob | None, page_url: str) -> list[tuple[str, bytes]]:
    """Capture rendered photo/gallery elements as screenshots.

    This helps when direct image URLs are blocked or show question-mark placeholders.
    """
    captures: list[tuple[str, bytes]] = []
    selectors = [
        "img",
        "picture",
        "[class*='gallery']",
        "[class*='photo']",
        "[class*='image']",
        "[class*='carousel']",
        "[class*='slider']",
        "[style*='background-image']",
    ]
    seen_boxes = set()
    for sel in selectors:
        try:
            els = await page.query_selector_all(sel)
        except Exception:
            continue
        for idx, el in enumerate(els[:80]):
            try:
                box = await el.bounding_box()
                if not box:
                    continue
                w = float(box.get("width") or 0)
                h = float(box.get("height") or 0)
                x = int(box.get("x") or 0)
                y = int(box.get("y") or 0)
                key = (round(x / 20), round(y / 20), round(w / 20), round(h / 20))
                if key in seen_boxes:
                    continue
                seen_boxes.add(key)
                if w < 140 or h < 140 or w * h < 30000:
                    continue
                await el.scroll_into_view_if_needed(timeout=1500)
                await page.wait_for_timeout(250)
                data = await el.screenshot(type="jpeg", quality=82, timeout=5000)
                if data and len(data) > 4000:
                    label = f"rendered:{sel}:{idx}:{page_url}"
                    captures.append((label, data))
                    if job:
                        try:
                            root = Path(job.debug_dir)
                            d = root / "rendered_gallery_captures"
                            d.mkdir(parents=True, exist_ok=True)
                            n = len(list(d.glob("rendered_*.jpg"))) + 1
                            (d / f"rendered_{n:04d}.jpg").write_bytes(data)
                            (d / f"rendered_{n:04d}.json").write_text(json.dumps({
                                "page_url": page_url,
                                "selector": sel,
                                "box": box,
                                "label": label,
                            }, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                if len(captures) >= 40:
                    return captures
            except Exception:
                pass
    return captures


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


async def image_bytes_from_page_element(page, img_url: str, job: ScanJob | None = None) -> bytes | None:
    """Fallback: screenshot the actual rendered image element.

    Some sites block direct hotlink fetches or use lazy/blob images. This captures
    what the browser rendered, which is often the only reliable source.
    """
    try:
        candidates = await page.query_selector_all("img, picture img, [style*='background-image']")
        for el in candidates[:120]:
            try:
                src = await el.evaluate("""el => {
                    const vals = [];
                    if (el.currentSrc) vals.push(el.currentSrc);
                    if (el.src) vals.push(el.src);
                    for (const a of ['src','data-src','data-lazy-src','data-original','data-url','data-full','data-image']) {
                        const v = el.getAttribute && el.getAttribute(a);
                        if (v) vals.push(v);
                    }
                    const bg = getComputedStyle(el).backgroundImage || '';
                    if (bg) vals.push(bg);
                    return vals.join(' ');
                }""")
                if img_url and img_url not in src:
                    continue
                box = await el.bounding_box()
                if not box or box.get("width", 0) < 80 or box.get("height", 0) < 80:
                    continue
                await el.scroll_into_view_if_needed(timeout=1500)
                shot = await el.screenshot(type="jpeg", quality=85, timeout=4000)
                if shot and len(shot) > 2500:
                    return shot
            except Exception:
                pass
    except Exception as e:
        log_job(job, f"Rendered image fallback failed: {e}")
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



def _extract_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}

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
    verdict = json.loads(resp.choices[0].message.content or "{}")
    if verdict.get("sign_detected") or verdict.get("possible_sign"):
        verdict["needs_human_review"] = True
    return verdict


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


def leolist_page_candidates(start_url: str, page_num: int) -> list[str]:
    """Generate common Leolist/category pagination URL shapes.

    Leolist has used more than one page URL style. The old crawler only guessed
    ?page=N, which can stop early even when the UI shows many numbered pages.
    """
    parsed = urlparse(start_url)
    base = start_url.split("#")[0]
    no_query = urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
    candidates = [
        increment_url_page(base, page_num),
        no_query + f"?page={page_num}",
        no_query + f"?p={page_num}",
        no_query + f"/page/{page_num}",
        no_query + f"/{page_num}",
    ]
    out = []
    for u in candidates:
        if u not in out:
            out.append(u)
    return out


async def discover_city_listing_pages(context, start_url: str, max_links: int | None, max_city_pages: int = SCAN_MAX_CITY_PAGES, job: ScanJob | None = None) -> tuple[list[str], int]:
    """Walk a city/category page and pagination until no new ads are found.

    This version does not stop at the first small batch of pages. It harvests
    explicit numbered pagination, estimates the highest page number shown, and
    queues every page up to that number. It also continues guessed pagination
    until several consecutive pages add no listings.
    """
    listing_links: list[str] = []
    seen_listing_links: set[str] = set()
    seen_city_pages: set[str] = set()
    city_queue: list[str] = [start_url]
    pagination_pages_visited = 0
    empty_page_streak = 0
    highest_page_seen = 1
    next_guess_num = 2

    def enqueue(u: str):
        u = (u or "").split("#")[0]
        if u and same_site(u, start_url) and u not in seen_city_pages and u not in city_queue:
            city_queue.append(u)

    while city_queue and len(seen_city_pages) < max_city_pages:
        if job and job.cancel_requested:
            log_job(job, "Scan cancelled before opening next city page")
            break
        while job and job.pause_requested and not job.cancel_requested:
            set_job_message(job, "Paused")
            await asyncio.sleep(1)
        city_url = city_queue.pop(0).split("#")[0]
        if city_url in seen_city_pages:
            continue
        seen_city_pages.add(city_url)
        pagination_pages_visited += 1
        set_job_message(job, f"Scanning city/page {pagination_pages_visited}: {city_url}")

        before_count = len(listing_links)
        page = await context.new_page()
        try:
            await run_step_with_timeout(page.goto(city_url, wait_until="domcontentloaded", timeout=SCAN_NAV_TIMEOUT_MS), f"Opening city page: {city_url}", job, timeout=max(10, SCAN_NAV_TIMEOUT_MS // 1000 + 8))
            await dismiss_common_modals(page)
            await page.wait_for_timeout(1200)
            await run_step_with_timeout(auto_scroll(page, steps=14), "Scrolling city page for lazy-loaded ads", job, timeout=35)
            try:
                write_debug_text(job, "latest_url.txt", city_url)
                latest_html = await page.content()
                latest_text = await page.evaluate("document.body ? document.body.innerText : ''")
                write_debug_text(job, "latest.html", latest_html)
                write_debug_text(job, "latest_text.txt", latest_text)
                write_debug_bytes(job, "latest-page.jpg", await page.screenshot(full_page=True, type="jpeg", quality=70))
                if looks_like_cloudflare_block(latest_text, latest_html):
                    msg = "Blocked by Cloudflare/security verification. Railway is receiving a verification page instead of Leolist listings."
                    log_job(job, msg)
                    write_debug_text(job, "BLOCKED_BY_CLOUDFLARE.txt", msg + "\nUse the manual upload/review workflow or run from an allowed environment.")
                    return listing_links, pagination_pages_visited
            except Exception as dbg_e:
                log_job(job, f"Debug capture failed: {dbg_e}")

            extracted_now = await extract_links(page, city_url, None, start_url)
            write_debug_text(job, "links.txt", "\n".join(extracted_now))
            log_job(job, f"Ad/detail links extracted from this city page: {len(extracted_now)}")
            for link in extracted_now:
                if link not in seen_listing_links and likely_ad_detail_link(link, start_url):
                    seen_listing_links.add(link)
                    listing_links.append(link)
                    if max_links and len(listing_links) >= max_links:
                        await page.close()
                        write_debug_text(job, "all_listing_links.txt", "\n".join(listing_links))
                        return listing_links, pagination_pages_visited

            raw_next = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(a => ({href:a.href, text:(a.innerText||a.getAttribute('aria-label')||a.rel||'').trim().toLowerCase()}))"""
            )

            # Explicit pagination links and max page detection.
            for item in raw_next:
                href = (item.get("href") or "").split("#")[0]
                text = (item.get("text") or "").strip().lower()
                if not href or not same_site(href, start_url):
                    continue
                low = href.lower()
                page_nums = []
                for m in re.finditer(r"(?:[?&](?:page|p)=|/page/|/)(\d{1,4})(?:[/?&#]|$)", low):
                    try:
                        n = int(m.group(1))
                        if 1 < n <= max_city_pages:
                            page_nums.append(n)
                    except Exception:
                        pass
                if text.isdigit():
                    n = int(text)
                    if 1 < n <= max_city_pages:
                        page_nums.append(n)
                if page_nums:
                    highest_page_seen = max(highest_page_seen, max(page_nums))

                looks_like_page = (
                    "next" in text or "more" in text or text in {">", "›", "»"} or
                    bool(page_nums) or
                    re.search(r"(^|\?|&)(page|p)=\d+", low) or
                    re.search(r"/page/\d+", low)
                )
                if looks_like_page and not bad_link_path(urlparse(href).path.lower()):
                    enqueue(href)

            # If the page says things like Page 1 of 39, queue through 39.
            body_text = await page.evaluate("document.body ? document.body.innerText : ''")
            for m in re.finditer(r"(?:page\s+\d+\s+of\s+|of\s+)(\d{1,4})", body_text.lower()):
                try:
                    n = int(m.group(1))
                    if 1 < n <= max_city_pages:
                        highest_page_seen = max(highest_page_seen, n)
                except Exception:
                    pass

            # Queue all numbered pages discovered by the UI, not just the next one.
            if highest_page_seen > 1:
                for n in range(2, min(highest_page_seen, max_city_pages) + 1):
                    for cand in leolist_page_candidates(start_url, n):
                        enqueue(cand)
        finally:
            await page.close()

        if len(listing_links) == before_count:
            empty_page_streak += 1
        else:
            empty_page_streak = 0

        # Continue guessed pagination even when numbered links are missing.
        # This fixes cases where the city has many pages but the first loaded
        # viewport only exposed a limited pagination window.
        while len(city_queue) < 8 and next_guess_num <= max_city_pages and empty_page_streak < SCAN_MAX_EMPTY_PAGES:
            for cand in leolist_page_candidates(start_url, next_guess_num):
                enqueue(cand)
            next_guess_num += 1
            break

        if empty_page_streak >= SCAN_MAX_EMPTY_PAGES and not city_queue:
            break

    write_debug_text(job, "all_listing_links.txt", "\n".join(listing_links))
    return listing_links, pagination_pages_visited


async def scan_site(
    mode: str,
    target_url: str,
    selected_city: str,
    max_links: int = 0,
    max_images: int = 0,
    screenshot_fallback: bool = False,
    job: ScanJob | None = None,
) -> tuple[Diagnostics, list[dict[str, Any]]]:
    if mode == "leolist_city":
        target_url = LEOLIST_CITIES.get(selected_city, "")
    else:
        target_url = normalize_url(target_url)

    diag = Diagnostics(mode=mode, selected_city=selected_city, target_url=target_url)
    set_job_message(job, f"Starting scan: {target_url}")

    if not target_url:
        diag.likely_problem = "No website URL or city was selected."
        set_job_message(job, diag.likely_problem)
        return diag, []

    if not OPENAI_API_KEY:
        diag.likely_problem = "OPENAI_API_KEY is missing in Railway variables."
        set_job_message(job, diag.likely_problem)
        return diag, []

    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    results = []
    seen_images = set()
    seen_signs = set()

    async with async_playwright() as p:
        set_job_message(job, "Launching browser engine")
        browser = await run_step_with_timeout(
            p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            ),
            "Launching Chromium",
            job,
            timeout=45,
        )
        set_job_message(job, "Creating browser context")
        context = await browser.new_context(
            viewport={"width": 1365, "height": 1800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            ignore_https_errors=True,
            locale="en-CA",
            java_script_enabled=True,
        )
        context.set_default_timeout(SCAN_NAV_TIMEOUT_MS)
        context.set_default_navigation_timeout(SCAN_NAV_TIMEOUT_MS)

        start = await context.new_page()
        try:
            # Discover listing/detail pages across the entire selected city, including pagination.
            # max_links=0 means no intentional listing cap; max_images=0 means no intentional image cap.
            effective_max_links = max_links if max_links and max_links > 0 else None
            links, pagination_count = await run_step_with_timeout(
                discover_city_listing_pages(context, target_url, effective_max_links, job=job),
                "Discovering individual ad links from city pages",
                job,
                timeout=max(180, SCAN_MAX_CITY_PAGES * 12),
            )
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
                        links, pagination_count = await run_step_with_timeout(
                            discover_city_listing_pages(context, parent, effective_max_links, job=job),
                            f"Trying parent category fallback: {parent}",
                            job,
                            timeout=120,
                        )
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
            set_job_message(job, f"Found {len(links)} candidate listing/detail links across {pagination_count} city pages")
        except Exception as e:
            diag.extraction_errors.append(f"Could not open start URL: {e}")
            links = []
        finally:
            await start.close()

        # Open and scan ONLY individual ad/detail pages. Do not scan the city/category
        # page as an ad because that creates false results and hides crawler bugs.
        pages = [u for u in dict.fromkeys(links) if likely_ad_detail_link(u, target_url)]
        write_debug_text(job, "opened_ad_urls.txt", "")
        effective_max_images = max_images if max_images and max_images > 0 else None
        all_image_urls: list[str] = []

        for link in pages:
            if effective_max_images and diag.images_scanned >= effective_max_images:
                break

            pg = await context.new_page()
            try:
                set_job_message(job, f"Opening ad/page {diag.pages_opened + 1}/{len(pages)}: {link}")
                await run_step_with_timeout(pg.goto(link, wait_until="domcontentloaded", timeout=SCAN_NAV_TIMEOUT_MS), f"Opening individual ad: {link}", job, timeout=max(10, SCAN_NAV_TIMEOUT_MS // 1000 + 8))
                await dismiss_common_modals(pg)
                await pg.wait_for_timeout(2500)
                await auto_scroll(pg)
                diag.pages_opened += 1

                try:
                    await save_full_ad_screenshot_review(pg, job, link, "opened individual ad")
                    write_debug_text(job, "latest_ad_url.txt", link)
                    write_debug_text(job, "latest_ad.html", await pg.content())
                    write_debug_bytes(job, "latest-ad-page.jpg", await pg.screenshot(full_page=True, type="jpeg", quality=70))
                except Exception as dbg_e:
                    log_job(job, f"Ad debug capture failed: {dbg_e}")
                # Collect images from the opened individual ad, including galleries/modals.
                urls = await expand_ad_gallery_and_collect_images(pg, link, job)
                all_image_urls.extend([u for u in urls if u not in all_image_urls])
                write_debug_text(job, "images.txt", "\n".join(urls))
                write_debug_text(job, "all_image_urls.txt", "\n".join(all_image_urls))
                rendered_captures = await collect_rendered_photo_screenshots(pg, job, link)
                if rendered_captures:
                    log_job(job, f"Rendered gallery screenshots captured from ad: {len(rendered_captures)}")
                    for rendered_label, rendered_data in rendered_captures:
                        if rendered_label not in urls:
                            urls.append(rendered_label)
                    rendered_bytes_by_url = {label: data for label, data in rendered_captures}
                else:
                    rendered_bytes_by_url = {}

                diag.images_found += len(urls)
                diag.all_image_urls_written = len(all_image_urls)
                diag.ads_opened = diag.pages_opened
                scanned_on_page = 0

                for img_url in urls:
                    if effective_max_images and diag.images_scanned >= effective_max_images:
                        break
                    data = rendered_bytes_by_url.get(img_url)
                    if not data:
                        data = await fetch_image_bytes(img_url, link)
                    if not data or len(data) < 3500:
                        data = await image_bytes_from_page_element(pg, img_url, job)
                    if not data or len(data) < 2500:
                        log_job(job, f"Skipped unusable image or placeholder: {img_url}")
                        continue
                    fp = image_fingerprint(data)
                    if fp in seen_images:
                        diag.duplicate_images_skipped_before_ai += 1
                        continue
                    seen_images.add(fp)

                    diag.images_scanned += 1
                    scanned_on_page += 1
                    set_job_message(job, f"AI scanning image {diag.images_scanned}; ad {diag.pages_opened}/{len(pages)}")
                    try:
                        diag.openai_vision_calls += 1
                        verdict = await analyze_image(client, data)
                        audit_root = Path(job.debug_dir) if job else Path("/tmp/verification_sign_scanner_debug")
                        safe_write_jsonl(audit_root / "ai_verdicts.jsonl", {
                            "page_url": link,
                            "image_url": img_url,
                            "verdict": verdict,
                        })
                        try:
                            sample_dir = audit_root / "all_scanned_images"
                            sample_dir.mkdir(parents=True, exist_ok=True)
                            sample_n = len(list(sample_dir.glob("*.jpg"))) + 1
                            if sample_n <= 500:
                                (sample_dir / f"scanned_{sample_n:04d}.jpg").write_bytes(jpeg_bytes(data))
                        except Exception:
                            pass
                        # Save every AI decision so you can see whether the model is rejecting signs.
                        try:
                            d = Path(job.debug_dir) if job else JOBS_ROOT
                            decisions = d / "ai_verdicts.jsonl"
                            with decisions.open("a", encoding="utf-8") as f:
                                f.write(json.dumps({"page_url": link, "image_url": img_url, "verdict": verdict}, ensure_ascii=False) + "\n")
                        except Exception:
                            pass

                        # Lowered from 0.55 to 0.35 so possible verification cards are not missed.
                        # Duplicate filtering still prevents repeated signs from filling results.
                        conf = float(verdict.get("confidence", 0) or 0)
                        reviewable = verdict.get("sign_detected") or verdict.get("possible_sign") or verdict.get("needs_human_review") or conf >= 0.05 or REVIEW_MODE_INCLUDE_ALL_AI_IMAGES
                        confirmed = verdict.get("sign_detected") or verdict.get("possible_sign") or verdict.get("needs_human_review") or conf >= 0.20

                        if reviewable:
                            save_candidate_image(job, data, verdict, link, img_url, "possible_signs")

                        if confirmed:
                            key = sign_key(verdict)
                            if key == hashlib.sha256(("|").encode()).hexdigest():
                                key = fp
                            if key in seen_signs:
                                diag.duplicate_signs_skipped += 1
                                continue
                            seen_signs.add(key)
                            result = {
                                "page_url": link,
                                "image_url": img_url,
                                "preview": to_data_url(data),
                                "verdict": verdict,
                                "review_label": "confirmed_or_review",
                            }
                            results.append(result)
                            save_candidate_image(job, data, verdict, link, img_url, "signs")
                            try:
                                out_dir = (Path(job.debug_dir) / "signs") if job else Path("/tmp/verification_sign_scanner_signs")
                                out_dir.mkdir(parents=True, exist_ok=True)
                                sign_num = len(results)
                                (out_dir / f"sign_{sign_num:04d}.jpg").write_bytes(jpeg_bytes(data))
                                (out_dir / f"sign_{sign_num:04d}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                            except Exception as save_e:
                                log_job(job, f"Could not save sign image: {save_e}")
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
        set_job_message(job, "Browser closed")

    diag.signs_found = len(results)
    if diag.candidate_links_found == 0 or not pages:
        diag.likely_problem = "No real individual ad/detail links were found. The city page may be blocked or the ad URL selector needs another patch. Check all_listing_links.txt and latest-page.jpg."
    elif diag.images_found == 0:
        diag.likely_problem = "Pages opened, but no usable image URLs were found."
    elif diag.openai_vision_calls == 0:
        diag.likely_problem = "Images were found but none were scanned. They may be blocked or too small."
    elif diag.signs_found == 0:
        diag.likely_problem = "Photos were scanned, but no physical verification signs were detected."
    else:
        diag.likely_problem = "Scan completed."

    set_job_message(job, diag.likely_problem)
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
    active_jobs = sorted(JOBS.values(), key=lambda j: j.created_at, reverse=True)[:8]
    active_html = ""
    for j in active_jobs:
        badge = "running" if j.status == "running" else j.status
        active_html += f"""
        <a class="job-row" href="/status/{j.id}">
          <span><b>{html_escape(j.selected_city or j.target_url or 'Custom scan')}</b><small>{html_escape(j.message)}</small></span>
          <em class="pill {html_escape(badge)}">{html_escape(j.status)}</em>
        </a>
        """
    if not active_html:
        active_html = '<p class="muted">No scans have been started since this server booted.</p>'

    return f"""
<!doctype html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{APP_TITLE}</title>
<style>
:root {{
  --bg:#080b12; --panel:#101621; --panel2:#151d2b; --text:#eef4ff; --muted:#9ca8ba;
  --line:#243145; --accent:#66e3ff; --accent2:#a78bfa; --good:#3ee18b; --warn:#ffca57; --bad:#ff6678;
  --shadow:0 18px 60px rgba(0,0,0,.38); --radius:24px;
}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;background:
 radial-gradient(circle at 15% 10%,rgba(102,227,255,.18),transparent 28%),
 radial-gradient(circle at 85% 0%,rgba(167,139,250,.18),transparent 30%),
 linear-gradient(180deg,#070a10,#0a0f18 44%,#070a10);color:var(--text);min-height:100vh}}
a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1180px;margin:0 auto;padding:28px}}
.hero{{display:grid;grid-template-columns:1.15fr .85fr;gap:22px;align-items:stretch}}
.card{{background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.03));border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:24px;backdrop-filter:blur(16px)}}
h1{{font-size:clamp(34px,5vw,64px);line-height:.98;margin:8px 0 14px;letter-spacing:-.05em}}
h2{{font-size:24px;margin:0 0 14px}}
p{{color:var(--muted);line-height:1.55}}
.logo{{display:inline-flex;align-items:center;gap:10px;color:#dff8ff;font-weight:900;letter-spacing:.08em;text-transform:uppercase;font-size:13px}}
.logo span{{width:12px;height:12px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 0 24px var(--accent)}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:22px}}
.stat{{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:18px;padding:16px}}
.stat b{{display:block;font-size:25px;color:white}} .stat small{{color:var(--muted)}}
label{{display:block;font-weight:850;margin:16px 0 8px;color:#dce8f8}}
input,select{{width:100%;font-size:16px;padding:14px 14px;border-radius:15px;border:1px solid #33425b;background:#0b1018;color:var(--text);outline:none}}
input:focus,select:focus{{border-color:var(--accent);box-shadow:0 0 0 4px rgba(102,227,255,.1)}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.help{{font-size:13px;color:var(--muted);margin:8px 0 0}}
.toggle{{display:flex;gap:10px;align-items:flex-start;background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:16px;padding:14px;margin-top:16px}}
.toggle input{{width:auto;margin-top:4px}}
.btn{{width:100%;border:0;border-radius:18px;padding:17px 20px;margin-top:20px;font-size:18px;font-weight:950;color:#051019;background:linear-gradient(135deg,var(--accent),var(--accent2));cursor:pointer;box-shadow:0 14px 40px rgba(102,227,255,.18)}}
.btn:hover{{filter:brightness(1.08)}}
.tool-list{{display:grid;gap:12px;margin-top:16px}}
.tool{{display:flex;gap:12px;align-items:flex-start;padding:14px;border-radius:18px;border:1px solid var(--line);background:rgba(255,255,255,.035)}}
.icon{{width:34px;height:34px;border-radius:12px;background:rgba(102,227,255,.11);display:grid;place-items:center;flex:0 0 auto}}
.job-row{{display:flex;justify-content:space-between;gap:14px;align-items:center;padding:14px;border:1px solid var(--line);background:rgba(255,255,255,.035);border-radius:16px;margin-top:10px;color:var(--text)}}
.job-row small{{display:block;color:var(--muted);margin-top:4px}}
.pill{{font-style:normal;border-radius:999px;padding:7px 10px;font-size:12px;font-weight:900;background:#263247;color:#dce8f8;text-transform:uppercase}}
.pill.running{{background:rgba(102,227,255,.14);color:var(--accent)}} .pill.done{{background:rgba(62,225,139,.14);color:var(--good)}} .pill.error{{background:rgba(255,102,120,.14);color:var(--bad)}}
.footer{{margin-top:22px;color:var(--muted);font-size:13px}}
.hidden{{display:none}}
@media(max-width:880px){{.hero,.form-grid{{grid-template-columns:1fr}}.grid{{grid-template-columns:1fr}}.wrap{{padding:16px}}}}
</style>
<script>
function updateMode(){{
  const mode = document.querySelector("select[name='mode']").value;
  document.getElementById("cityBox").classList.toggle("hidden", mode !== "leolist_city");
  document.getElementById("urlBox").classList.toggle("hidden", mode !== "custom_url");
}}
window.addEventListener("DOMContentLoaded", updateMode);
</script>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <section class="card">
      <div class="logo"><span></span> Verification Sign Scanner Pro</div>
      <h1>Scan city ads. Open every ad. Catch physical signs.</h1>
      <p>This build is designed for long city-wide scans: it follows pagination, opens individual ads, scans gallery photos, skips duplicate images/signs, and keeps evidence when detection is uncertain.</p>
      <div class="grid">
        <div class="stat"><b>Full city</b><small>Use 0 limits to scan all pages found.</small></div>
        <div class="stat"><b>Live debug</b><small>See current ad, images, AI verdicts, and logs.</small></div>
        <div class="stat"><b>Review queue</b><small>Download possible signs and sampled scanned images.</small></div>
      </div>
      <div class="tool-list">
        <div class="tool"><div class="icon">🔎</div><div><b>Individual ad opening</b><p class="help">The crawler avoids counting city/category pages as ads and records every opened ad URL.</p></div></div>
        <div class="tool"><div class="icon">🖼️</div><div><b>Gallery extraction</b><p class="help">Attempts regular image URLs, lazy-loaded images, modal/gallery clicks, and rendered image screenshots.</p></div></div>
        <div class="tool"><div class="icon">🧠</div><div><b>AI verdict audit</b><p class="help">Every AI decision is saved so zero-result runs are diagnosable instead of mysterious.</p></div></div>
      </div>
    </section>

    <section class="card">
      <h2>Start a scan</h2>
      <form method="post" action="/scan">
        <label>Scan mode</label>
        <select name="mode" onchange="updateMode()">
          <option value="leolist_city">Leolist city selector</option>
          <option value="custom_url">Custom website URL</option>
        </select>

        <div id="cityBox">
          <label>City / region</label>
          <select name="selected_city">{city_options}</select>
          <p class="help">Grande Prairie is included under Northern Alberta.</p>
        </div>

        <div id="urlBox" class="hidden">
          <label>Website URL</label>
          <input name="target_url" placeholder="https://example.com/listings">
        </div>

        <div class="form-grid">
          <div>
            <label>Max ads to open</label>
            <input name="max_links" type="number" value="0" min="0" max="100000">
            <p class="help">0 = scan every ad discovered.</p>
          </div>
          <div>
            <label>Max images to scan</label>
            <input name="max_images" type="number" value="0" min="0" max="1000000">
            <p class="help">0 = scan every usable image.</p>
          </div>
        </div>

        <label class="toggle">
          <input name="screenshot_fallback" type="checkbox" value="1">
          <span><b>Enable screenshot fallback</b><br><span class="help">Use only if no images are extracted. It can catch rendered images but may create false positives from webpage text.</span></span>
        </label>

        <button class="btn" type="submit">Start full scanner</button><p class="help"><a href="/manual">Manual upload mode</a> for when Leolist shows Cloudflare verification on Railway.</p>
      </form>
    </section>
  </div>

  <section class="card" style="margin-top:22px">
    <h2>Recent scans</h2>
    {active_html}
  </section>
  <div class="footer">Tip: after starting, keep the status page open for live progress. On Railway, the scan continues server-side unless the service restarts.</div>
</div>
</body>
</html>
"""


@app.post("/scan")
async def scan(
    mode: str = Form("leolist_city"),
    selected_city: str = Form("Northern Alberta / Grande Prairie"),
    target_url: str = Form(""),
    max_links: int = Form(0),
    max_images: int = Form(0),
    screenshot_fallback: str | None = Form(None),
):
    job_id = uuid.uuid4().hex[:12]
    debug_dir = str(JOBS_ROOT / job_id)
    job = ScanJob(
        id=job_id,
        created_at=time.time(),
        status="queued",
        message="Queued",
        mode=mode,
        selected_city=selected_city,
        target_url=target_url,
        max_links=max_links,
        max_images=max_images,
        screenshot_fallback=bool(screenshot_fallback),
        debug_dir=debug_dir,
    )
    Path(debug_dir).mkdir(parents=True, exist_ok=True)
    JOBS[job_id] = job
    asyncio.create_task(run_scan_job(job))
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


async def run_scan_job(job: ScanJob):
    job.status = "running"
    async def heartbeat():
        while job.status == "running":
            log_job(job, f"Heartbeat: still running - {job.message}")
            await asyncio.sleep(20)
    hb = asyncio.create_task(heartbeat())
    try:
        diag, results = await scan_site(
            mode=job.mode,
            selected_city=job.selected_city,
            target_url=job.target_url,
            max_links=job.max_links,
            max_images=job.max_images,
            screenshot_fallback=job.screenshot_fallback,
            job=job,
        )
        job.diagnostics = diag.__dict__
        job.results = results
        try:
            hw = build_handwriting_report(job)
            job.diagnostics["handwriting_images_compared"] = hw.get("image_count", 0)
            job.diagnostics["handwriting_possible_pairs"] = hw.get("pair_count", 0)
            job.diagnostics["handwriting_clusters"] = len(hw.get("clusters", []))
            log_job(job, f"Handwriting comparison complete: {hw.get('pair_count', 0)} possible pair(s), {len(hw.get('clusters', []))} cluster(s)")
        except Exception as hw_e:
            log_job(job, f"Handwriting comparison failed: {hw_e}")
        job.status = "done"
        log_job(job, f"Finished: {len(results)} unique verification sign(s) found")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        log_job(job, f"Fatal error: {e}")
    finally:
        hb.cancel()
        try:
            await hb
        except BaseException:
            pass



def render_results_html(job: ScanJob) -> str:
    diag = job.diagnostics or {}
    pages_scanned = safe_int(diag.get("pages_scanned", 0))
    pages_total = safe_int(diag.get("listing_pages_discovered", 0)) or safe_int(diag.get("candidate_links_found", 0))
    ads_opened = safe_int(diag.get("ads_opened", diag.get("pages_opened", 0)))
    images_found = safe_int(diag.get("images_found", 0))
    images_scanned = safe_int(diag.get("images_scanned", 0))
    signs_found = len(job.results)
    possible_count = count_files(job, "possible_signs")
    scanned_samples = count_files(job, "all_scanned_images")
    confirmed_count = count_files(job, "signs")
    handwriting_pairs = safe_int(diag.get("handwriting_possible_pairs", 0))
    handwriting_clusters = safe_int(diag.get("handwriting_clusters", 0))
    openai_calls = safe_int(diag.get("openai_vision_calls", 0))
    dup_imgs = safe_int(diag.get("duplicate_images_skipped_before_ai", 0))
    dup_signs = safe_int(diag.get("duplicate_signs_skipped", 0))
    likely = diag.get("likely_problem", job.error or ("Scan running" if job.status in {"queued","running"} else "Complete"))
    progress = pct(images_scanned or ads_opened or pages_scanned, images_found or pages_total or max(ads_opened, 1))

    def stat(label, value, hint=""):
        return f'<div class="stat"><b>{value}</b><small>{html_escape(label)}</small>{f"<em>{html_escape(hint)}</em>" if hint else ""}</div>'

    result_cards = ""
    for idx, r in enumerate(job.results, 1):
        v = r.get("verdict", {})
        conf = v.get("confidence", "")
        img = f'<img src="{r.get("preview", "")}" alt="detected sign preview">' if r.get("preview") else '<div class="empty-img">No preview</div>'
        result_cards += f"""
        <article class="result-card">
          <div class="thumb">{img}</div>
          <div class="result-body">
            <div class="result-top"><span class="pill done">Confirmed #{idx}</span><span class="conf">{html_escape(conf)}</span></div>
            <h3>{html_escape(v.get('sign_type','Physical sign'))}</h3>
            <p>{html_escape(v.get('description',''))}</p>
            <dl>
              <dt>Text visible</dt><dd>{html_escape(v.get('text_visible','')) or '—'}</dd>
              <dt>Ad page</dt><dd><a href="{html_escape(r.get('page_url',''))}" target="_blank">open source ad</a></dd>
              <dt>Image URL</dt><dd class="break">{html_escape(r.get('image_url',''))}</dd>
            </dl>
          </div>
        </article>
        """
    if not result_cards:
        result_cards = """
        <div class="empty-state">
          <h3>No confirmed signs yet</h3>
          <p>Use the review/download buttons below. If the sampled scanned images contain signs, then the AI verdict is too strict. If they do not, the crawler is not extracting the right gallery photos.</p>
        </div>
        """

    files = public_debug_files(job)
    file_links = "".join(f'<a href="/debug/{job.id}/{html_escape(name)}">{html_escape(name)}</a>' for name in files[:50])
    if not file_links:
        file_links = '<span class="muted">No debug files written yet.</span>'

    logs = html_escape(chr(10).join(job.logs[-120:]))
    api_errors = diag.get("openai_api_errors", []) or []
    extraction_errors = diag.get("extraction_errors", []) or []
    problem_cards = ""
    if api_errors:
        problem_cards += f'<div class="alert bad"><b>OpenAI/API errors</b><p>{html_escape(str(api_errors[:3]))}</p></div>'
    if extraction_errors:
        problem_cards += f'<div class="alert warn"><b>Extraction errors</b><p>{html_escape(str(extraction_errors[:3]))}</p></div>'
    if not problem_cards:
        problem_cards = '<div class="alert good"><b>No fatal errors recorded</b><p>Check live logs and debug files for crawler/detector details.</p></div>'

    refresh = '<meta http-equiv="refresh" content="4">' if job.status in {"queued", "running"} else ""
    status_class = html_escape(job.status)
    return f"""
<!doctype html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>Scan {html_escape(job.id)} · {APP_TITLE}</title>
<style>
:root{{--bg:#080b12;--panel:#101621;--panel2:#151d2b;--text:#eef4ff;--muted:#9ca8ba;--line:#243145;--accent:#66e3ff;--accent2:#a78bfa;--good:#3ee18b;--warn:#ffca57;--bad:#ff6678;--shadow:0 18px 60px rgba(0,0,0,.38);--radius:24px}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;background:linear-gradient(180deg,#070a10,#0d1420 40%,#070a10);color:var(--text);min-height:100vh}} a{{color:var(--accent);text-decoration:none}}
.wrap{{max-width:1280px;margin:0 auto;padding:22px}} .top{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}} h1{{font-size:clamp(30px,4vw,54px);letter-spacing:-.04em;margin:6px 0}} h2{{margin:0 0 14px}} h3{{margin:8px 0}} p{{color:var(--muted);line-height:1.55}}
.card{{background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.03));border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:22px;margin-bottom:18px}}
.pill{{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:8px 11px;font-size:12px;font-weight:950;background:#263247;color:#dce8f8;text-transform:uppercase}} .pill.running{{background:rgba(102,227,255,.14);color:var(--accent)}} .pill.done{{background:rgba(62,225,139,.14);color:var(--good)}} .pill.error{{background:rgba(255,102,120,.14);color:var(--bad)}} .pill.queued{{background:rgba(255,202,87,.14);color:var(--warn)}}
.actions{{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}} button.btn{{cursor:pointer}} .btn{{border:1px solid var(--line);border-radius:14px;padding:11px 13px;background:rgba(255,255,255,.05);color:var(--text);font-weight:850}} .btn.primary{{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#06101a;border:0}}
.progress{{height:14px;background:#0a1019;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:16px 0}} .bar{{height:100%;width:{progress}%;background:linear-gradient(90deg,var(--accent),var(--accent2))}}
.stats{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}} .stat{{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:18px;padding:14px;min-height:94px}} .stat b{{display:block;font-size:28px;color:white}} .stat small{{display:block;color:var(--muted);margin-top:4px}} .stat em{{display:block;color:#7f8ca0;font-size:12px;margin-top:7px;font-style:normal}}
.grid{{display:grid;grid-template-columns:1fr .85fr;gap:18px}} pre{{white-space:pre-wrap;background:#05080d;border:1px solid var(--line);border-radius:18px;padding:14px;max-height:520px;overflow:auto;color:#d7e6f8;font-size:13px}}
.result-card{{display:grid;grid-template-columns:280px 1fr;gap:18px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.035);padding:14px;margin:12px 0}} .thumb img{{width:100%;max-height:340px;object-fit:contain;border-radius:15px;background:#05080d}} .empty-img{{height:220px;display:grid;place-items:center;background:#05080d;border-radius:15px;color:var(--muted)}} .result-top{{display:flex;justify-content:space-between;gap:10px;align-items:center}} .conf{{color:var(--good);font-weight:900}} dl{{display:grid;grid-template-columns:110px 1fr;gap:8px;margin:12px 0}} dt{{color:var(--muted)}} dd{{margin:0}} .break{{word-break:break-all;color:#b7c4d7}}
.alert{{border-radius:18px;border:1px solid var(--line);padding:14px;margin:10px 0;background:rgba(255,255,255,.04)}} .alert.good{{border-color:rgba(62,225,139,.35)}} .alert.warn{{border-color:rgba(255,202,87,.4)}} .alert.bad{{border-color:rgba(255,102,120,.4)}} .file-grid{{display:flex;flex-wrap:wrap;gap:8px}} .file-grid a{{border:1px solid var(--line);border-radius:999px;padding:8px 10px;background:rgba(255,255,255,.04);font-size:13px}}
.empty-state{{border:1px dashed #33425b;border-radius:20px;padding:24px;text-align:center}} .muted{{color:var(--muted)}} .meta{{color:var(--muted);font-size:14px}} .preview-img{{width:100%;border-radius:18px;border:1px solid var(--line);background:#05080d;max-height:480px;object-fit:contain}}
@media(max-width:1000px){{.stats{{grid-template-columns:repeat(2,1fr)}}.grid,.result-card{{grid-template-columns:1fr}}.top{{display:block}}}} @media(max-width:560px){{.stats{{grid-template-columns:1fr}}.wrap{{padding:14px}}}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div>
      <a href="/">← New scan</a>
      <h1>Live scan dashboard</h1>
      <div class="meta">Job {html_escape(job.id)} · Running for {job_age(job)} · {html_escape(job.selected_city or job.target_url)}</div>
    </div>
    <div><span class="pill {status_class}">{html_escape(job.status)}</span></div>
  </div>

  <section class="card">
    <h2>{html_escape(job.message)}</h2>
    <div class="progress"><div class="bar"></div></div>
    <p><b>Likely problem / current diagnosis:</b> {html_escape(str(likely))}</p>
    <div class="actions">
      <a class="btn primary" href="/job/{job.id}.json">Job JSON</a>
      <a class="btn" href="/logs/{job.id}">Live logs</a>
      <a class="btn" href="/signs/{job.id}.zip">Confirmed signs ZIP</a>
      <a class="btn" href="/review/{job.id}">Review images</a>
      <a class="btn" href="/handwriting/{job.id}">Handwriting matches</a>
      <a class="btn" href="/candidates/{job.id}.zip">Possible signs + scanned images ZIP</a>
      <a class="btn" href="/debug/{job.id}/latest-page.jpg">Latest city screenshot</a>
      <a class="btn" href="/debug/{job.id}/latest-ad-page.jpg">Latest ad screenshot</a>
      <form method="post" action="/job/{job.id}/pause" style="display:inline"><button class="btn" type="submit">Pause</button></form>
      <form method="post" action="/job/{job.id}/resume" style="display:inline"><button class="btn" type="submit">Resume</button></form>
      <form method="post" action="/job/{job.id}/cancel" style="display:inline"><button class="btn" type="submit">Cancel</button></form>
    </div>
  </section>

  <section class="stats">
    {stat("Confirmed signs", signs_found, f"{confirmed_count} saved files")}
    {stat("Possible signs", possible_count, "manual review queue")}
    {stat("Ads opened", ads_opened, "individual detail pages")}
    {stat("Images scanned", images_scanned, f"{images_found} found")}
    {stat("AI calls", openai_calls, f"{dup_imgs} duplicate images skipped")}
    {stat("Pages scanned", pages_scanned, f"{pages_total} discovered/expected")}
    {stat("Scanned samples", scanned_samples, "saved for proof")}
    {stat("Duplicate signs", dup_signs, "ignored")}
    {stat("Writing matches", handwriting_pairs, f"{handwriting_clusters} cluster(s)")}
    {stat("Mode", html_escape(job.mode), "")}
    {stat("Max limits", f"{job.max_links or 'all'} ads / {job.max_images or 'all'} imgs", "")}
  </section>

  <div class="grid">
    <section class="card">
      <h2>Found signs</h2>
      {result_cards}
    </section>
    <aside>
      <section class="card">
        <h2>Debug preview</h2>
        <p>These files show what the server is actually seeing.</p>
        <img class="preview-img" src="/debug/{job.id}/latest-ad-page.jpg" onerror="this.style.display='none'">
        <div class="file-grid" style="margin-top:12px">{file_links}</div>
      </section>
      <section class="card">
        <h2>Health checks</h2>
        {problem_cards}
      </section>
    </aside>
  </div>

  <section class="card">
    <h2>Recent live log</h2>
    <pre>{logs}</pre>
  </section>
</div>
</body>
</html>
"""


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status_page(job_id: str):
    job = get_job(job_id)
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    return render_results_html(job)


@app.get("/logs/{job_id}", response_class=PlainTextResponse)
async def job_logs(job_id: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    return "\n".join(job.logs)



@app.get("/debug/{job_id}/{filename:path}")
async def debug_file(job_id: str, filename: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    root = Path(job.debug_dir).resolve()
    path = (root / filename).resolve()
    if not str(path).startswith(str(root)):
        return PlainTextResponse("Invalid path", status_code=400)
    if not path.exists() or not path.is_file():
        return PlainTextResponse("Debug file not created yet", status_code=404)
    return FileResponse(path)



@app.post("/job/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    job.cancel_requested = True
    log_job(job, "Cancel requested from dashboard")
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)

@app.post("/job/{job_id}/pause")
async def pause_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    job.pause_requested = True
    log_job(job, "Pause requested from dashboard")
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)

@app.post("/job/{job_id}/resume")
async def resume_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    job.pause_requested = False
    log_job(job, "Resume requested from dashboard")
    return RedirectResponse(url=f"/status/{job_id}", status_code=303)



@app.get("/review/{job_id}", response_class=HTMLResponse)
async def review_page(job_id: str):
    job = get_job(job_id)
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    root = Path(job.debug_dir)
    cards = ""
    for folder, title in [("signs", "Confirmed / review hits"), ("possible_signs", "Possible signs"), ("all_scanned_images", "All scanned samples"), ("opened_ad_screenshots", "Opened ad screenshots"), ("rendered_gallery_captures", "Rendered gallery captures")]:
        d = root / folder
        if not d.exists():
            continue
        imgs = sorted([p for p in d.glob("*.jpg")])[:300]
        if not imgs:
            continue
        cards += f"<h2>{html_escape(title)} ({len(imgs)})</h2><div class='grid'>"
        for p in imgs:
            meta = p.with_suffix(".json")
            text = ""
            if meta.exists():
                try:
                    text = html_escape(meta.read_text(encoding="utf-8", errors="ignore")[:1000])
                except Exception:
                    text = ""
            cards += f"""
            <div class='card'>
              <img src='/debug/{job.id}/{folder}/{p.name}'>
              <pre>{text}</pre>
            </div>
            """
        cards += "</div>"
    if not cards:
        cards = "<p>No review images saved yet. Wait until images are scanned, then refresh.</p>"
    return f"""
<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Review images</title>
<style>
body{{font-family:Arial,sans-serif;background:#080b12;color:#eef4ff;margin:0;padding:18px}}
a{{color:#66e3ff}} .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.card{{background:#101621;border:1px solid #243145;border-radius:16px;padding:10px}}
img{{width:100%;border-radius:12px;background:#000}} pre{{white-space:pre-wrap;font-size:11px;color:#cbd5e1;max-height:180px;overflow:auto}}
</style></head><body>
<p><a href='/status/{job.id}'>Back to dashboard</a> | <a href='/candidates/{job.id}.zip'>Download review ZIP</a></p>
<h1>Review images for job {html_escape(job.id)}</h1>
{cards}
</body></html>"""



@app.get("/handwriting/{job_id}", response_class=HTMLResponse)
async def handwriting_page(job_id: str):
    job = get_job(job_id)
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    report_path = Path(job.debug_dir) / "handwriting_report.json"
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            report = build_handwriting_report(job)
    else:
        report = build_handwriting_report(job)

    pairs = report.get("pairs", [])
    clusters = report.get("clusters", [])

    pair_html = ""
    for p in pairs[:100]:
        pair_html += f"""
        <div class="pair">
          <div><img src="{html_escape(p.get('a_url',''))}"><small>{html_escape(p.get('a',''))}</small></div>
          <div><img src="{html_escape(p.get('b_url',''))}"><small>{html_escape(p.get('b',''))}</small></div>
          <div class="score"><b>{int(float(p.get('score',0))*100)}%</b><span>{html_escape(p.get('label',''))}</span></div>
        </div>
        """
    if not pair_html:
        pair_html = "<p>No handwriting-similar pairs found yet. This may mean no sign/review images were captured, or the samples are too visually different.</p>"

    cluster_html = ""
    for ci, cluster in enumerate(clusters[:50], 1):
        imgs = "".join(f"<img src='{html_escape(item.get('url',''))}'><small>{html_escape(item.get('relative_path',''))}</small>" for item in cluster[:12])
        cluster_html += f"<section class='cluster'><h3>Cluster {ci}: {len(cluster)} image(s)</h3><div class='thumbs'>{imgs}</div></section>"
    if not cluster_html:
        cluster_html = "<p>No clusters yet.</p>"

    return f"""
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Handwriting matches</title>
<style>
body{{font-family:Arial,sans-serif;background:#080b12;color:#eef4ff;margin:0;padding:18px}}
a{{color:#66e3ff}} .warn{{background:#2a1d0a;border:1px solid #ffca57;border-radius:14px;padding:12px;color:#ffe0a0}}
.grid{{display:grid;grid-template-columns:1fr;gap:14px}} .pair{{display:grid;grid-template-columns:1fr 1fr 160px;gap:12px;background:#101621;border:1px solid #243145;border-radius:16px;padding:12px;margin:12px 0;align-items:center}}
img{{width:100%;max-height:280px;object-fit:contain;background:#000;border-radius:12px}} small{{display:block;color:#9ca8ba;word-break:break-all;margin-top:6px}}
.score{{text-align:center}} .score b{{font-size:34px;color:#66e3ff;display:block}} .score span{{color:#cbd5e1}}
.cluster{{background:#101621;border:1px solid #243145;border-radius:16px;padding:12px;margin:12px 0}} .thumbs{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
.card{{background:#101621;border:1px solid #243145;border-radius:16px;padding:14px;margin:12px 0}}
@media(max-width:760px){{.pair{{grid-template-columns:1fr}}}}
</style></head><body>
<p><a href="/status/{job.id}">Back to dashboard</a> | <a href="/debug/{job.id}/handwriting_report.json">Download JSON report</a></p>
<h1>Handwriting similarity review</h1>
<div class="warn"><b>Important:</b> This is probabilistic visual matching for review only. It cannot prove the same person wrote two signs. Treat high scores as leads that need human review.</div>
<div class="card">
  <p><b>Images compared:</b> {report.get('image_count',0)}</p>
  <p><b>Possible/high-similarity pairs:</b> {report.get('pair_count',0)}</p>
  <p><b>Clusters:</b> {len(clusters)}</p>
  <p><b>High threshold:</b> {report.get('threshold_high')} · <b>Possible threshold:</b> {report.get('threshold_possible')}</p>
</div>
<h2>Clusters</h2>
{cluster_html}
<h2>Top similar pairs</h2>
<div class="grid">{pair_html}</div>
</body></html>
"""

@app.get("/handwriting/{job_id}.json")
async def handwriting_json(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(build_handwriting_report(job))



@app.get("/manual", response_class=HTMLResponse)
async def manual_upload_page():
    return """
<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Manual image upload</title>
<style>body{font-family:Arial,sans-serif;background:#080b12;color:#eef4ff;padding:22px}a{color:#66e3ff}.card{background:#101621;border:1px solid #243145;border-radius:18px;padding:18px;max-width:780px}input,button{font-size:18px;margin-top:12px}button{padding:12px 16px;border-radius:12px;border:0}</style>
</head><body><div class="card">
<a href="/">Back</a>
<h1>Manual upload scanner</h1>
<p>Use this when Leolist blocks Railway with Cloudflare. Upload saved ad/sign images and the app will place them into a review job for detection/handwriting comparison.</p>
<form method="post" action="/manual/upload" enctype="multipart/form-data">
<input type="file" name="files" multiple accept="image/*"><br>
<button type="submit">Upload images for review</button>
</form>
</div></body></html>
"""

@app.post("/manual/upload")
async def manual_upload(files: list[UploadFile] = File(...)):
    job_id = uuid.uuid4().hex[:12]
    debug_dir = str(JOBS_ROOT / job_id)
    job = ScanJob(
        id=job_id,
        created_at=time.time(),
        status="done",
        message="Manual images uploaded",
        mode="manual_upload",
        selected_city="manual upload",
        target_url="manual upload",
        max_links=0,
        max_images=0,
        screenshot_fallback=False,
        debug_dir=debug_dir,
    )
    Path(debug_dir).mkdir(parents=True, exist_ok=True)
    d = Path(debug_dir) / "all_scanned_images"
    d.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in files:
        data = await f.read()
        if not data:
            continue
        count += 1
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in [".jpg", ".jpeg", ".png", ".webp"]:
            suffix = ".jpg"
        (d / f"manual_{count:04d}{suffix}").write_bytes(data)
    job.diagnostics = {
        "mode": "manual_upload",
        "images_scanned": count,
        "likely_problem": "Manual upload mode: images saved for review/handwriting comparison.",
    }
    JOBS[job_id] = job
    try:
        hw = build_handwriting_report(job)
        job.diagnostics["handwriting_images_compared"] = hw.get("image_count", 0)
        job.diagnostics["handwriting_possible_pairs"] = hw.get("pair_count", 0)
        job.diagnostics["handwriting_clusters"] = len(hw.get("clusters", []))
    except Exception as e:
        log_job(job, f"Handwriting comparison failed: {e}")
    return RedirectResponse(url=f"/review/{job_id}", status_code=303)


@app.get("/job/{job_id}.json")
async def job_json(job_id: str):
    job = get_job(job_id)
    if not job:
        return JSONResponse({"error":"Job not found"}, status_code=404)
    return JSONResponse({
        "id": job.id,
        "status": job.status,
        "message": job.message,
        "error": job.error,
        "diagnostics": job.diagnostics,
        "results": safe_result_for_json(job.results),
        "logs": job.logs[-200:],
        "debug_files": public_debug_files(job),
    })


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



@app.get("/signs/{job_id}.zip")
async def signs_zip(job_id: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    root = Path(job.debug_dir)
    zip_path = root / "confirmed_signs.zip"
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zout:
        wrote = False
        for folder in ["signs"]:
            d = root / folder
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        zout.write(p, arcname=str(Path(folder) / p.name))
                        wrote = True
        if not wrote:
            empty = root / "README_no_confirmed_signs_yet.txt"
            empty.write_text("No confirmed signs have been saved for this job yet. Check candidates ZIP for possible signs and scanned samples.", encoding="utf-8")
            zout.write(empty, arcname=empty.name)
    return FileResponse(zip_path, filename=f"confirmed_signs_{job_id}.zip")


@app.get("/candidates/{job_id}.zip")
async def candidates_zip(job_id: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    root = Path(job.debug_dir)
    zip_path = root / "possible_and_confirmed_signs.zip"
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zout:
        wrote = False
        for folder in ["signs", "possible_signs", "all_scanned_images", "opened_ad_screenshots", "rendered_gallery_captures"]:
            d = root / folder
            if d.exists():
                for p in d.rglob("*"):
                    if p.is_file():
                        zout.write(p, arcname=str(Path(folder) / p.name))
                        wrote = True
        if not wrote:
            empty = root / "README_no_images_yet.txt"
            empty.write_text("No scanned images have been saved for this job yet.", encoding="utf-8")
            zout.write(empty, arcname=empty.name)
    return FileResponse(zip_path, filename=f"possible_and_confirmed_signs_{job_id}.zip")
