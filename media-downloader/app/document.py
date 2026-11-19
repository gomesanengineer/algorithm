"""Turn a PDF / PPT / document-viewer link into a single PDF file.

Strategy, in order:
1. Rewrite well-known share links (Google Slides/Docs/Sheets/Drive, Dropbox) to direct export URLs.
2. Fetch the URL. If it is a PDF, return it as-is (lossless).
3. Otherwise open a page in headless Chromium:
   - a web page / viewer link is opened directly;
   - a raw .ppt/.pptx/.doc/.docx file is opened in Google's online document viewer
     (or, for Google Drive links, in Drive's own viewer).
   Then the viewer is scrolled from top to bottom while every page image / canvas
   (anything >= 300 px) is collected at its native resolution, the same way the
   "Download pdf or ppt from chrome" console script works. The pages are put into one PDF.
   Fallbacks when a viewer has no page images: screenshot repeated page elements, and
   finally the scrolling area one screen at a time.
"""
import asyncio
import base64
import io
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse, unquote, urljoin, quote

import httpx
import img2pdf
from PIL import Image

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MAX_BYTES = int(os.getenv("MAX_DOC_BYTES", str(300 * 1024 * 1024)))
MAX_PAGES = int(os.getenv("MAX_CAPTURE_PAGES", "500"))
OFFICE_EXT = {".ppt", ".pptx", ".pps", ".ppsx", ".odp", ".doc", ".docx", ".odt", ".rtf",
              ".xls", ".xlsx", ".ods", ".key"}
OFFICE_CT = ("powerpoint", "presentation", "msword", "wordprocessing", "officedocument",
             "opendocument", "ms-excel", "spreadsheet", "rtf")


# ------------------------------------------------------------------ link rewriting
def rewrite_share_link(url: str) -> str:
    u = urlparse(url)
    host = u.netloc.lower()
    m = re.search(r"/d/([a-zA-Z0-9_-]{10,})", u.path)
    if "docs.google.com" in host and m:
        fid = m.group(1)
        if "/presentation/" in u.path:
            return f"https://docs.google.com/presentation/d/{fid}/export/pdf"
        if "/document/" in u.path:
            return f"https://docs.google.com/document/d/{fid}/export?format=pdf"
        if "/spreadsheets/" in u.path:
            return f"https://docs.google.com/spreadsheets/d/{fid}/export?format=pdf"
    if "drive.google.com" in host:
        fid = m.group(1) if m else None
        if not fid:
            q = re.search(r"[?&]id=([a-zA-Z0-9_-]{10,})", url)
            fid = q.group(1) if q else None
        if fid:
            return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    if "dropbox.com" in host:
        return re.sub(r"([?&])dl=0", r"\1dl=1", url) if "dl=0" in url else url + ("&" if "?" in url else "?") + "dl=1"
    return url


def name_from(url: str, headers: httpx.Headers | None = None) -> str:
    if headers:
        cd = headers.get("content-disposition", "")
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
        if m:
            return Path(unquote(m.group(1))).stem
    stem = Path(unquote(urlparse(url).path)).stem
    return stem or urlparse(url).netloc


# ------------------------------------------------------------------ direct download
async def fetch(url: str, dest_dir: Path, validate) -> tuple[Path | None, str, str, str]:
    """Download url. Returns (path_or_None_if_html, kind, title, final_url). kind: pdf|office|html|other."""

    async def check(request: httpx.Request):
        validate(str(request.url))  # re-check every redirect hop

    async with httpx.AsyncClient(follow_redirects=True, timeout=60, headers={"User-Agent": UA},
                                 event_hooks={"request": [check]}) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()
            final = str(r.url)
            title = name_from(final, r.headers)
            if "text/html" in ct or "xhtml" in ct:
                return None, "html", title, final
            ext = Path(urlparse(final).path).suffix.lower()
            cd_ext = Path(title).suffix.lower()
            path = dest_dir / f"source-{uuid.uuid4().hex}"
            size = 0
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError(f"File is larger than {MAX_BYTES // (1024 * 1024)} MB.")
                    f.write(chunk)
    head = path.read_bytes()[:8]
    if head.startswith(b"%PDF"):
        return path, "pdf", title, final
    disp = r.headers.get("content-disposition", "").lower()
    disp_ext = next((e for e in OFFICE_EXT if e in disp), "")
    if (ext in OFFICE_EXT or cd_ext in OFFICE_EXT or disp_ext or any(k in ct for k in OFFICE_CT)
            or head.startswith(b"PK\x03\x04") or head.startswith(b"\xd0\xcf\x11\xe0")):
        real_ext = next(e for e in (ext, cd_ext, disp_ext, ".pptx") if e in OFFICE_EXT)
        renamed = path.with_suffix(real_ext)
        path.rename(renamed)
        return renamed, "office", title, final
    if head.lstrip().startswith(b"<"):
        return None, "html", title, final
    return path, "other", title, final


# ------------------------------------------------------------------ browser capture
FIND_SCROLLER_JS = """
() => {
  const doc = document.scrollingElement || document.documentElement;
  let best = null, bestArea = 0;
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (!/(auto|scroll|overlay)/.test(s.overflowY)) continue;
    if (el.scrollHeight <= el.clientHeight + 50) continue;
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (r.width < 300 || r.height < 200) continue;
    if (area > bestArea || (area === bestArea && best && el.scrollHeight > best.scrollHeight)) { best = el; bestArea = area; }
  }
  const docScrolls = doc.scrollHeight > window.innerHeight + 50;
  if (best && (!docScrolls || bestArea > window.innerWidth * window.innerHeight * 0.4)) {
    best.setAttribute('data-mg-scroller', '1');
    return true;
  }
  return false;
}
"""

SCROLL_JS = """
([useEl, y]) => {
  const el = useEl ? document.querySelector('[data-mg-scroller]') : (document.scrollingElement || document.documentElement);
  if (y !== null && y !== undefined) el.scrollTop = y;
  return { top: el.scrollTop, height: el.scrollHeight, view: useEl ? el.clientHeight : window.innerHeight };
}
"""

FIND_PAGES_JS = """
() => {
  const sel = 'canvas, img, svg, .page, [data-page-number], [data-page], [class*="slide"], [class*="page"], [id*="page"]';
  const set = new Set(document.querySelectorAll(sel));
  // also: blocks that repeat as same-class siblings (custom viewers with arbitrary class names)
  for (const el of document.querySelectorAll('div, section, article, figure, li')) {
    const p = el.parentElement;
    if (p && p.children.length >= 2 && el.className &&
        [...p.children].filter(c => c.className === el.className).length >= 2) set.add(el);
  }
  const cands = [];
  for (const el of set) {
    const r = el.getBoundingClientRect();
    if (r.width < 350 || r.height < 200) continue;
    if (r.height > 6000) continue;
    const cls = typeof el.className === 'string' ? el.className : '';
    const sig = el.tagName + '|' + cls.split(/\\s+/).filter(c => !/\\d/.test(c)).sort().join('.');
    cands.push({ el, sig, w: r.width, h: r.height, area: r.width * r.height });
  }
  const groups = {};
  for (const c of cands) (groups[c.sig] = groups[c.sig] || []).push(c);
  let best = null, score = 0;
  for (const g of Object.values(groups)) {
    // drop elements nested inside another member of the same group
    let top = g.filter(c => !g.some(o => o !== c && o.el.contains(c.el)));
    if (top.length < 2) continue;
    // pages/slides have (nearly) the same size; keep the dominant size
    const mw = top.map(c => c.w).sort((a, b) => a - b)[top.length >> 1];
    const mh = top.map(c => c.h).sort((a, b) => a - b)[top.length >> 1];
    top = top.filter(c => Math.abs(c.w - mw) < mw * 0.15 && Math.abs(c.h - mh) < mh * 0.35);
    if (top.length < 2) continue;
    const s = top.length * Math.sqrt(mw * mh);
    if (s > score) { score = s; best = top; }
  }
  if (!best) return 0;
  best.sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
  best.forEach((c, i) => c.el.setAttribute('data-mg-page', String(i)));
  return best.length;
}
"""

# Port of the "Download pdf or ppt from chrome" console script: collect every big <img>/<canvas>
# at native resolution. Runs repeatedly while scrolling; returns only new/changed pages.
COLLECT_JS = """
async ([useEl, MIN]) => {
  const st = window.__mg || (window.__mg = { seen: new Map() });
  const scroller = useEl ? document.querySelector('[data-mg-scroller]')
                         : (document.scrollingElement || document.documentElement);
  const isDoc = !useEl;
  const top0 = isDoc ? 0 : scroller.getBoundingClientRect().top;
  const out = [];
  const els = [...document.querySelectorAll('img, canvas')].filter(el => {
    const w = el.tagName === 'IMG' ? el.naturalWidth : el.width;
    const h = el.tagName === 'IMG' ? el.naturalHeight : el.height;
    return w >= MIN && h >= MIN && (el.tagName !== 'IMG' || el.complete);
  });
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) continue;            // hidden / offscreen copies
    const order = Math.round(r.top - top0 + scroller.scrollTop);
    const isImg = el.tagName === 'IMG';
    const key = isImg ? el.currentSrc || el.src : 'canvas@' + order;
    const w = isImg ? el.naturalWidth : el.width, h = isImg ? el.naturalHeight : el.height;
    // tiny thumbnail = blank check + change detection (viewers re-render / recycle canvases)
    let sig = '';
    try {
      const t = document.createElement('canvas'); t.width = 16; t.height = 16;
      const x = t.getContext('2d'); x.drawImage(el, 0, 0, 16, 16);
      const d = x.getImageData(0, 0, 16, 16).data;
      let alpha = 0, sum = 0;
      for (let i = 0; i < d.length; i += 4) { alpha += d[i + 3]; sum += d[i] * 3 + d[i + 1] * 5 + d[i + 2] * 7; }
      if (!isImg && alpha === 0) continue;                    // canvas not rendered yet
      sig = alpha + ':' + sum;
    } catch (e) {
      sig = 'tainted';                                         // cross-origin image
    }
    if (st.seen.get(key) === sig) continue;
    if (isImg && st.seen.has(key)) continue;                   // same image already taken
    st.seen.set(key, sig);
    if (sig === 'tainted') { out.push({ key, order, w, h, src: el.currentSrc || el.src }); continue; }
    const c = document.createElement('canvas'); c.width = w; c.height = h;
    const ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, w, h);
    ctx.drawImage(el, 0, 0, w, h);
    out.push({ key, order, w, h, data: c.toDataURL('image/jpeg', 0.92) });
  }
  return out;
}
"""

EMBEDDED_PDF_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('iframe, embed, object')) {
    const src = el.getAttribute('src') || el.getAttribute('data') || '';
    if (!src) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 300 || r.height < 200) continue;
    out.push(src);
  }
  return out;
}
"""


def launch_args() -> dict:
    exe = os.getenv("CHROMIUM_PATH")
    args = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if exe:
        args["executable_path"] = exe
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
    if proxy:
        args["proxy"] = {"server": proxy,
                         "bypass": os.getenv("NO_PROXY") or os.getenv("no_proxy") or "localhost,127.0.0.1"}
    return args


async def scroll_through(page, use_el: bool) -> None:
    """Scroll the page/viewer to the bottom step by step until its height stops growing."""
    y, stable, last_h = 0, 0, -1
    for _ in range(2000):
        st = await page.evaluate(SCROLL_JS, [use_el, y])
        await page.wait_for_timeout(250)
        if st["top"] + st["view"] >= st["height"] - 2:
            if st["height"] == last_h:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            await page.wait_for_timeout(600)
        last_h = st["height"]
        y = st["top"] + int(st["view"] * 0.8)
    await page.evaluate(SCROLL_JS, [use_el, 0])
    await page.wait_for_timeout(500)


async def wait_images(page, locator) -> None:
    try:
        await locator.evaluate(
            """el => Promise.race([
                 Promise.all([...(el.tagName === 'IMG' ? [el] : el.querySelectorAll('img'))]
                   .map(i => i.complete ? 0 : new Promise(r => { i.onload = i.onerror = r; }))),
                 new Promise(r => setTimeout(r, 4000))])"""
        )
    except Exception:
        pass


async def capture_elements(page, count: int) -> list[bytes]:
    shots = []
    for i in range(min(count, MAX_PAGES)):
        loc = page.locator(f'[data-mg-page="{i}"]')
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
            await page.wait_for_timeout(700)  # let lazy renderers (pdf.js, slide viewers) paint
            await wait_images(page, loc)
            shots.append(await loc.screenshot(timeout=15000, animations="disabled"))
        except Exception:
            continue
    return shots


async def capture_screens(page, use_el: bool) -> list[bytes]:
    shots, y, prev_top = [], 0, -1
    clip = None
    if use_el:
        box = await page.locator("[data-mg-scroller]").bounding_box()
        if box:
            clip = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
    for _ in range(MAX_PAGES):
        st = await page.evaluate(SCROLL_JS, [use_el, y])
        await page.wait_for_timeout(500)
        if st["top"] == prev_top:
            break
        png = await page.screenshot(clip=clip, animations="disabled") if clip else await page.screenshot(animations="disabled")
        if prev_top >= 0 and st["top"] < prev_top + st["view"]:
            # last screen overlaps the previous one: keep only the new part
            img = Image.open(io.BytesIO(png))
            new_px = int((st["top"] + st["view"] - (prev_top + st["view"])) / st["view"] * img.height)
            if new_px > 10:
                buf = io.BytesIO()
                img.crop((0, img.height - new_px, img.width, img.height)).save(buf, "PNG")
                shots.append(buf.getvalue())
        else:
            shots.append(png)
        prev_top = st["top"]
        if st["top"] + st["view"] >= st["height"] - 2:
            break
        y = st["top"] + st["view"]
    return shots


async def collect_page_images(page, ctx, use_el: bool) -> list[bytes]:
    """Scroll top -> bottom, collecting page images/canvases until nothing new appears."""
    pages: dict[str, dict] = {}

    async def grab():
        for item in await page.evaluate(COLLECT_JS, [use_el, 300]):
            if "src" in item:  # cross-origin image: fetch it with the page's cookies
                try:
                    r = await ctx.request.get(item["src"], timeout=30000)
                    if r.ok:
                        item["bytes"] = await r.body()
                except Exception:
                    continue
            else:
                item["bytes"] = base64.b64decode(item["data"].split(",", 1)[1])
            if item.get("bytes"):
                pages[item["key"]] = item

    await page.evaluate(SCROLL_JS, [use_el, 0])
    await page.wait_for_timeout(1500)
    stable, last_count, last_height = 0, -1, -1
    for _ in range(3000):
        await grab()
        st = await page.evaluate(SCROLL_JS, [use_el, None])
        at_bottom = st["top"] + st["view"] >= st["height"] - 5
        if not at_bottom:
            await page.evaluate(SCROLL_JS, [use_el, st["top"] + max(200, int(st["view"] * 0.5))])
            await page.wait_for_timeout(700)
        else:
            await page.wait_for_timeout(1500)
        await grab()
        st = await page.evaluate(SCROLL_JS, [use_el, None])
        now_bottom = st["top"] + st["view"] >= st["height"] - 5
        if now_bottom and len(pages) == last_count and st["height"] == last_height:
            stable += 1
            if stable >= 3:
                break
        elif now_bottom:
            stable = 0
        last_count, last_height = len(pages), st["height"]
        if len(pages) >= MAX_PAGES:
            break
    return [p["bytes"] for p in sorted(pages.values(), key=lambda p: p["order"])][:MAX_PAGES]


def images_to_pdf(shots: list[bytes], out: Path) -> None:
    clean = []
    for s in shots:
        img = Image.open(io.BytesIO(s))
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = bg
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90, dpi=(192, 192))
        clean.append(buf.getvalue())
    out.write_bytes(img2pdf.convert(clean))


async def browser_capture(url: str, outdir: Path, validate) -> tuple[Path, str, str, str | None]:
    """Returns (pdf_path or None, title, method, embedded_pdf_url or None)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_args())
        try:
            ctx = await browser.new_context(viewport={"width": 1280, "height": 1600},
                                            device_scale_factor=2, user_agent=UA)

            async def guard(route):  # block navigation/sub-requests to private networks
                try:
                    validate(route.request.url) if route.request.url.startswith("http") else None
                    await route.continue_()
                except Exception:
                    await route.abort()

            await ctx.route("**/*", guard)
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
            except Exception:
                await page.wait_for_timeout(3000)  # slow pages: work with what loaded
            title = (await page.title()) or name_from(url)

            # close common cookie banners
            for txt in ("Accept all", "Accept", "I agree", "Got it", "Allow all"):
                try:
                    btn = page.get_by_role("button", name=txt, exact=True)
                    if await btn.count():
                        await btn.first.click(timeout=1500)
                        break
                except Exception:
                    pass

            for src in await page.evaluate(EMBEDDED_PDF_JS):
                full = urljoin(page.url, src)
                if re.search(r"\.pdf($|[?#])", full, re.I) or "pdf" in full.lower().split("?")[0][-12:]:
                    return None, title, "embedded", full

            use_el = await page.evaluate(FIND_SCROLLER_JS)
            shots = await collect_page_images(page, ctx, use_el)
            method = f"page images ({len(shots)})"
            n = 0
            if not shots:
                await scroll_through(page, use_el)
                n = await page.evaluate(FIND_PAGES_JS)
            if not shots and n >= 2:
                shots = await capture_elements(page, n)
                method = f"pages ({len(shots)})"
            if not shots:
                shots = await capture_screens(page, use_el)
                method = f"screens ({len(shots)})"
            if not shots:
                raise RuntimeError("Nothing could be captured from the page.")
            out = outdir / "capture.pdf"
            images_to_pdf(shots, out)
            return out, title, method, None
        finally:
            await browser.close()


# ------------------------------------------------------------------ entry point
def office_viewer_url(original: str, direct: str) -> str:
    """Where to *view* a raw Office file so its pages can be captured."""
    if "drive.google.com" in urlparse(original).netloc:
        return original  # Drive's own viewer
    return "https://docs.google.com/viewer?embedded=true&url=" + quote(direct, safe="")


async def capture_document(url: str, outdir: Path, validate) -> tuple[Path, str, str]:
    """Returns (pdf_path, title, method)."""
    direct = rewrite_share_link(url)
    view_url = url
    for attempt_url in dict.fromkeys([direct, url]):  # rewritten link first, then the original
        path, kind, title, final = await fetch(attempt_url, outdir, validate)
        if kind == "pdf":
            return path, title, "direct pdf"
        if kind == "office":
            view_url = office_viewer_url(url, attempt_url)
            break
        if kind == "other":
            raise RuntimeError("The link is not a PDF, presentation or web page.")

    pdf, title, method, embedded = await browser_capture(view_url, outdir, validate)
    if embedded:
        validate(embedded)
        path, kind, etitle, _ = await fetch(embedded, outdir, validate)
        if kind == "pdf":
            return path, etitle or title, "embedded pdf"
        target = office_viewer_url(embedded, embedded) if kind == "office" else embedded
        pdf, title, method, _ = await browser_capture(target, outdir, validate)
        if pdf is None:
            raise RuntimeError("Could not capture the embedded document.")
    title = re.sub(r"\s*-\s*Google Drive$", "", title or "", flags=re.I)
    title = re.sub(r"\.(pdf|pptx?|docx?)$", "", title, flags=re.I).strip() or "document"
    return pdf, title, method
