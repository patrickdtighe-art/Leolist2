from pathlib import Path
p=Path('/mnt/data/workscan/main.py')
s=p.read_text()
# Add opened_ad_urls and all_image_urls diagnostics
s=s.replace('    signs_found: int = 0\n    likely_problem: str = ""', '    signs_found: int = 0\n    ads_opened: int = 0\n    all_image_urls_written: int = 0\n    likely_problem: str = ""')
# Insert helper after extract_image_urls function
marker='''async def fetch_image_bytes(url: str, referer: str) -> bytes | None:\n'''
helper=r'''
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

'''
s=s.replace(marker, helper+marker)
# Replace pages including target with links only and debug no ads
s=s.replace('''        # scan start page too, but screenshot fallback remains off by default
        pages = [target_url] + links
        pages = list(dict.fromkeys(pages))
        effective_max_images = max_images if max_images and max_images > 0 else None

        for link in pages:''', '''        # Open and scan ONLY individual ad/detail pages. Do not scan the city/category
        # page as an ad because that creates false results and hides crawler bugs.
        pages = [u for u in dict.fromkeys(links) if likely_ad_detail_link(u, target_url)]
        write_debug_text(job, "opened_ad_urls.txt", "")
        effective_max_images = max_images if max_images and max_images > 0 else None
        all_image_urls: list[str] = []

        for link in pages:''')
# Replace image extraction line and add cumulative debug
s=s.replace('''                urls = await extract_image_urls(pg, link)
                write_debug_text(job, "images.txt", "\n".join(urls))
                diag.images_found += len(urls)
                scanned_on_page = 0
''', '''                # The important part: after opening the individual ad, harvest that
                # ad's photos, including lazy-loaded gallery/modal photos.
                urls = await expand_ad_gallery_and_collect_images(pg, link, job)
                for u in urls:
                    if u not in all_image_urls:
                        all_image_urls.append(u)
                write_debug_text(job, "images.txt", "\n".join(urls))
                write_debug_text(job, "all_image_urls.txt", "\n".join(all_image_urls))
                try:
                    with (Path(job.debug_dir) / "opened_ad_urls.txt").open("a", encoding="utf-8") as f:
                        f.write(link + "\n")
                except Exception:
                    pass
                diag.images_found += len(urls)
                diag.ads_opened = diag.pages_opened
                diag.all_image_urls_written = len(all_image_urls)
                scanned_on_page = 0
''')
# Fix progress off-by-one
s=s.replace('f"AI scanning image {diag.images_scanned}; page {diag.pages_opened + 1}/{len(pages)}"', 'f"AI scanning image {diag.images_scanned}; ad {diag.pages_opened}/{len(pages)}"')
# Add status rows
s=s.replace('''        ("Pages opened", diag.get("pages_opened", 0)),
        ("Images found", diag.get("images_found", 0)),''', '''        ("Pages opened", diag.get("pages_opened", 0)),
        ("Individual ads opened", diag.get("ads_opened", diag.get("pages_opened", 0))),
        ("Images found", diag.get("images_found", 0)),
        ("Cumulative image URLs written", diag.get("all_image_urls_written", 0)),''')
# Add debug links
s=s.replace('''      <a href="/debug/{job.id}/all_listing_links.txt">All listing links</a> |
      <a href="/debug/{job.id}/images.txt">Latest image URLs</a> |''', '''      <a href="/debug/{job.id}/all_listing_links.txt">All listing links</a> |
      <a href="/debug/{job.id}/opened_ad_urls.txt">Opened ad URLs</a> |
      <a href="/debug/{job.id}/images.txt">Latest image URLs</a> |
      <a href="/debug/{job.id}/all_image_urls.txt">All image URLs</a> |''')
# Allow files
s=s.replace('''"links.txt","images.txt","all_listing_links.txt","live.log"}''', '''"links.txt","images.txt","all_listing_links.txt","opened_ad_urls.txt","all_image_urls.txt","live.log"}''')
# Update likely_problem no ads wording
s=s.replace('''    if diag.candidate_links_found == 0 and diag.pages_opened <= 1:
        diag.likely_problem = "No detail/listing links were found after aggressive extraction. Try Custom URL with the exact listings page."
    elif diag.images_found == 0:''', '''    if diag.candidate_links_found == 0 or not pages:
        diag.likely_problem = "No real individual ad/detail links were found. The city page may be blocked or the ad URL selector needs another patch. Check all_listing_links.txt and latest-page.jpg."
    elif diag.images_found == 0:''')
p.write_text(s)
print('patched')
