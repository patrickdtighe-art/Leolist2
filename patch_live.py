from pathlib import Path
p=Path('/mnt/data/livepatch/main.py')
s=p.read_text()
# imports
s=s.replace('import tempfile\n', 'import tempfile\nimport asyncio\nimport time\nimport uuid\n')
s=s.replace('from fastapi.responses import HTMLResponse, JSONResponse\n', 'from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse, RedirectResponse\n')
# add Job after app
marker='app = FastAPI(title=APP_TITLE)\n'
insert=r'''

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
'''
s=s.replace(marker, marker+insert)
# update discover signature and add job logs
s=s.replace('async def discover_city_listing_pages(context, start_url: str, max_links: int | None, max_city_pages: int = 500) -> tuple[list[str], int]:', 'async def discover_city_listing_pages(context, start_url: str, max_links: int | None, max_city_pages: int = 500, job: ScanJob | None = None) -> tuple[list[str], int]:')
s=s.replace('        seen_city_pages.add(city_url)\n        pagination_pages_visited += 1\n\n        before_count = len(listing_links)', '        seen_city_pages.add(city_url)\n        pagination_pages_visited += 1\n        set_job_message(job, f"Scanning city/page {pagination_pages_visited}: {city_url}")\n\n        before_count = len(listing_links)')
s=s.replace('            await auto_scroll(page, steps=28)\n\n            for link in await extract_links(page, city_url, None):', '            await auto_scroll(page, steps=28)\n            try:\n                write_debug_text(job, "latest_url.txt", city_url)\n                write_debug_text(job, "latest.html", await page.content())\n                write_debug_text(job, "latest_text.txt", await page.evaluate("document.body ? document.body.innerText : \'\'"))\n                write_debug_bytes(job, "latest-page.jpg", await page.screenshot(full_page=True, type="jpeg", quality=70))\n            except Exception as dbg_e:\n                log_job(job, f"Debug capture failed: {dbg_e}")\n\n            extracted_now = await extract_links(page, city_url, None)\n            write_debug_text(job, "links.txt", "\\n".join(extracted_now))\n            for link in extracted_now:')
s=s.replace('                        return listing_links, pagination_pages_visited', '                        write_debug_text(job, "all_listing_links.txt", "\\n".join(listing_links))\n                        return listing_links, pagination_pages_visited')
s=s.replace('    return listing_links, pagination_pages_visited', '    write_debug_text(job, "all_listing_links.txt", "\\n".join(listing_links))\n    return listing_links, pagination_pages_visited')
# scan_site signature
s=s.replace('    screenshot_fallback: bool = False,\n) -> tuple[Diagnostics, list[dict[str, Any]]]:', '    screenshot_fallback: bool = False,\n    job: ScanJob | None = None,\n) -> tuple[Diagnostics, list[dict[str, Any]]]:')
# add initial log after diag
s=s.replace('    diag = Diagnostics(mode=mode, selected_city=selected_city, target_url=target_url)\n', '    diag = Diagnostics(mode=mode, selected_city=selected_city, target_url=target_url)\n    set_job_message(job, f"Starting scan: {target_url}")\n')
# replace discovery calls
s=s.replace('links, pagination_count = await discover_city_listing_pages(context, target_url, effective_max_links)', 'links, pagination_count = await discover_city_listing_pages(context, target_url, effective_max_links, job=job)')
s=s.replace('links, pagination_count = await discover_city_listing_pages(context, parent, effective_max_links)', 'links, pagination_count = await discover_city_listing_pages(context, parent, effective_max_links, job=job)')
# add log after candidate count
s=s.replace('            diag.candidate_links_found = len(links)\n            diag.listing_pages_discovered = len(links)', '            diag.candidate_links_found = len(links)\n            diag.listing_pages_discovered = len(links)\n            set_job_message(job, f"Found {len(links)} candidate listing/detail links across {pagination_count} city pages")')
# scan page log and debug images
s=s.replace('            pg = await context.new_page()\n            try:\n                await pg.goto(link, wait_until="domcontentloaded", timeout=45000)', '            pg = await context.new_page()\n            try:\n                set_job_message(job, f"Opening ad/page {diag.pages_opened + 1}/{len(pages)}: {link}")\n                await pg.goto(link, wait_until="domcontentloaded", timeout=45000)')
s=s.replace('                urls = await extract_image_urls(pg, link)\n                diag.images_found += len(urls)', '                try:\n                    write_debug_text(job, "latest_ad_url.txt", link)\n                    write_debug_text(job, "latest_ad.html", await pg.content())\n                    write_debug_bytes(job, "latest-ad-page.jpg", await pg.screenshot(full_page=True, type="jpeg", quality=70))\n                except Exception as dbg_e:\n                    log_job(job, f"Ad debug capture failed: {dbg_e}")\n                urls = await extract_image_urls(pg, link)\n                write_debug_text(job, "images.txt", "\\n".join(urls))\n                diag.images_found += len(urls)')
s=s.replace('                    diag.images_scanned += 1\n                    scanned_on_page += 1', '                    diag.images_scanned += 1\n                    scanned_on_page += 1\n                    set_job_message(job, f"AI scanning image {diag.images_scanned}; page {diag.pages_opened + 1}/{len(pages)}")')
s=s.replace('        await context.close()\n        await browser.close()', '        await context.close()\n        await browser.close()\n        set_job_message(job, "Browser closed")')
s=s.replace('    return diag, results', '    set_job_message(job, diag.likely_problem)\n    return diag, results')
# replace form action text maybe no change needed.
# replace scan endpoint body to create job
old_start='''@app.post("/scan", response_class=HTMLResponse)\nasync def scan(\n    mode: str = Form("leolist_city"),\n    selected_city: str = Form("Northern Alberta / Grande Prairie"),\n    target_url: str = Form(""),\n    max_links: int = Form(0),\n    max_images: int = Form(0),\n    screenshot_fallback: str | None = Form(None),\n):\n    diag, results = await scan_site(\n        mode=mode,\n        selected_city=selected_city,\n        target_url=target_url,\n        max_links=max_links,\n        max_images=max_images,\n        screenshot_fallback=bool(screenshot_fallback),\n    )\n\n    rows = ['''
idx=s.find(old_start)
end=s.find('@app.get("/scan.json")', idx)
assert idx!=-1 and end!=-1
new_endpoint=r'''@app.post("/scan")
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
        job.status = "done"
        log_job(job, f"Finished: {len(results)} unique verification sign(s) found")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        log_job(job, f"Fatal error: {e}")


def render_results_html(job: ScanJob) -> str:
    diag = job.diagnostics or {}
    rows = [
        ("Job status", html_escape(job.status)),
        ("Current step", html_escape(job.message)),
        ("Mode", html_escape(diag.get("mode", job.mode))),
        ("Selected city", html_escape(diag.get("selected_city", job.selected_city))),
        ("Target URL", html_escape(diag.get("target_url", job.target_url))),
        ("Pages scanned", diag.get("pages_scanned", 0)),
        ("Candidate links found", diag.get("candidate_links_found", 0)),
        ("City pagination pages visited", diag.get("pagination_pages_visited", 0)),
        ("Listing/detail pages discovered", diag.get("listing_pages_discovered", 0)),
        ("Pages opened", diag.get("pages_opened", 0)),
        ("Images found", diag.get("images_found", 0)),
        ("Images scanned", diag.get("images_scanned", 0)),
        ("Screenshot fallbacks scanned", diag.get("screenshot_fallbacks_scanned", 0)),
        ("OpenAI vision calls", diag.get("openai_vision_calls", 0)),
        ("OpenAI/API errors", "none" if not diag.get("openai_api_errors") else "<br>".join(map(html_escape, diag.get("openai_api_errors", [])[:5]))),
        ("Extraction errors", "none" if not diag.get("extraction_errors") else "<br>".join(map(html_escape, diag.get("extraction_errors", [])[:5]))),
        ("Duplicate images skipped before AI", diag.get("duplicate_images_skipped_before_ai", 0)),
        ("Duplicate signs skipped", diag.get("duplicate_signs_skipped", 0)),
        ("Likely problem", html_escape(diag.get("likely_problem", job.error or "Still running"))),
    ]
    row_html = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    result_html = ""
    for r in job.results:
        v = r.get("verdict", {})
        img = f'<img src="{r.get("preview", "")}" style="max-width:100%;border-radius:14px">' if r.get("preview") else ""
        result_html += f"""
        <div class="result">
            {img}
            <p><b>Page:</b> <a href="{html_escape(r.get('page_url',''))}">{html_escape(r.get('page_url',''))}</a></p>
            <p><b>Image:</b> {html_escape(r.get('image_url',''))}</p>
            <p><b>Type:</b> {html_escape(v.get('sign_type',''))}</p>
            <p><b>Text:</b> {html_escape(v.get('text_visible',''))}</p>
            <p><b>Description:</b> {html_escape(v.get('description',''))}</p>
            <p><b>Confidence:</b> {html_escape(v.get('confidence',''))}</p>
        </div>
        """
    debug_links = f"""
    <p>
      <a href="/logs/{job.id}">Live logs</a> |
      <a href="/debug/{job.id}/latest-page.jpg">Latest city screenshot</a> |
      <a href="/debug/{job.id}/latest.html">Latest city HTML</a> |
      <a href="/debug/{job.id}/all_listing_links.txt">All listing links</a> |
      <a href="/debug/{job.id}/images.txt">Latest image URLs</a> |
      <a href="/job/{job.id}.json">Job JSON</a>
    </p>
    """
    auto_refresh = '<meta http-equiv="refresh" content="3">' if job.status in {"queued", "running"} else ""
    return f"""
<!doctype html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
{auto_refresh}
<title>Scan Status</title>
<style>
body{{font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:22px;color:#111}}
.card{{background:white;border-radius:22px;padding:24px;margin:0 auto 24px;max-width:900px;box-shadow:0 8px 24px #0001}}
h1{{font-size:34px;margin:0 0 16px}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;vertical-align:top;border-bottom:1px solid #ddd;padding:12px;font-size:17px}}
th{{width:44%;font-weight:900}}
.result{{border-top:1px solid #ddd;padding:18px 0}}
a{{color:#551a8b}}
pre{{white-space:pre-wrap;background:#111;color:#eee;border-radius:14px;padding:14px;max-height:360px;overflow:auto}}
</style></head><body>
<div class="card"><h1>Live scan status</h1>{debug_links}<table>{row_html}</table></div>
<div class="card"><h1>Recent log</h1><pre>{html_escape(chr(10).join(job.logs[-60:]))}</pre></div>
<div class="card"><h1>{len(job.results)} unique verification sign(s) found</h1>
{result_html if result_html else "<p>No physical verification signs found yet.</p>"}
<p><a href="/">Run another scan</a></p></div>
</body></html>
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


@app.get("/debug/{job_id}/{filename}")
async def debug_file(job_id: str, filename: str):
    job = get_job(job_id)
    if not job:
        return PlainTextResponse("Job not found", status_code=404)
    allowed = {"latest-page.jpg","latest-ad-page.jpg","latest.html","latest_text.txt","latest_url.txt","latest_ad.html","latest_ad_url.txt","links.txt","images.txt","all_listing_links.txt","live.log"}
    if filename not in allowed:
        return PlainTextResponse("File not allowed", status_code=400)
    path = Path(job.debug_dir) / filename
    if not path.exists():
        return PlainTextResponse("Debug file not created yet", status_code=404)
    return FileResponse(path)


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
        "debug_files": [p.name for p in Path(job.debug_dir).glob("*")] if job.debug_dir else [],
    })


'''
s=s[:idx]+new_endpoint+s[end:]
# update scan_json call signature maybe OK now accepts job optional no issue
p.write_text(s)
