from pathlib import Path
p=Path('/mnt/data/adpatch/main.py')
s=p.read_text()
insert = r'''

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
    return True
'''
# Insert after likely_detail_link function before auto_scroll
marker='\n\nasync def auto_scroll'
s=s.replace(marker, insert+marker)
# Change extract_links signature and filtering
s=s.replace('async def extract_links(page, base_url: str, max_links: int | None = None) -> list[str]:',
            'async def extract_links(page, base_url: str, max_links: int | None = None, city_base_url: str | None = None) -> list[str]:')
s=s.replace('if likely_detail_link(href, base_url):\n                seen.add(href)\n                links.append(href)',
            'is_detail = likely_ad_detail_link(href, city_base_url or base_url) if city_base_url else likely_detail_link(href, base_url)\n            if is_detail:\n                seen.add(href)\n                links.append(href)')
s=s.replace('if new_url != original and new_url not in seen and likely_detail_link(new_url, base_url):\n                        seen.add(new_url)\n                        links.append(new_url)',
            'is_detail = likely_ad_detail_link(new_url, city_base_url or base_url) if city_base_url else likely_detail_link(new_url, base_url)\n                    if new_url != original and new_url not in seen and is_detail:\n                        seen.add(new_url)\n                        links.append(new_url)')
# Need fix call and for loop filter
s=s.replace('extracted_now = await extract_links(page, city_url, None)', 'extracted_now = await extract_links(page, city_url, None, start_url)')
s=s.replace('if link not in seen_listing_links and likely_detail_link(link, start_url):', 'if link not in seen_listing_links and likely_ad_detail_link(link, start_url):')
# Add debug rejected links by replacing after raw_next retrieval? Add simple all anchors debug
s=s.replace('write_debug_text(job, "links.txt", "\\n".join(extracted_now))', 'write_debug_text(job, "links.txt", "\\n".join(extracted_now))\n            log_job(job, f"Ad/detail links extracted from this city page: {len(extracted_now)}")')
# Add latest pages list maybe no
p.write_text(s)
