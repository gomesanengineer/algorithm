# Media Grabber

A small website that takes a link and gives you a file:

| Paste a link to… | You get |
|---|---|
| A YouTube (or other video-site) video | **MP4** (choose up to 1080p / best) or **MP3** audio |
| A direct **PDF** link | The PDF |
| A **PPT / PPTX / DOC / DOCX / ODP** link | The file converted to **PDF** (LibreOffice) |
| **Google Slides / Docs / Sheets / Drive**, **Dropbox** share links | The document as **PDF** |
| Any **web page that shows a document** (PDF.js viewers, slide viewers, embedded PDFs) | The page is opened in a headless browser, **scrolled to the end** so every lazy-loaded page renders, then **every page/slide is captured** into one PDF |

## How the document capture works

1. Known share links are rewritten to their direct export URL (e.g. Google Slides → `/export/pdf`).
2. The link is fetched. A PDF is returned as-is; an Office file is converted to PDF.
3. If it is a web page, headless Chromium opens it and:
   - downloads an embedded PDF directly if the page has one (`<iframe>/<embed>/<object>`);
   - finds the scrolling area (the window or an inner viewer panel) and scrolls it to the bottom until no more content loads;
   - detects repeated page/slide elements and screenshots each one as a PDF page;
   - if no page elements are found, captures the scrolling area one screen at a time.

The response header `X-Capture-Method` (shown in the UI) tells you which path was used.

## Run locally

Requires Python 3.11+, `ffmpeg`, and LibreOffice (`soffice`) for PPT→PDF.

```bash
cd media-downloader
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --port 8000
# open http://localhost:8000
```

Or with Docker:

```bash
cd media-downloader
docker build -t media-grabber .
docker run -p 8000:8000 media-grabber
```

## Host it

The app needs a real server (yt-dlp, ffmpeg, LibreOffice and Chromium), so static hosts like
GitHub Pages or Netlify will not work. Any Docker host does:

- **Render** (free tier): New → *Blueprint* → pick this repo. `render.yaml` at the repo root
  builds `media-downloader/Dockerfile`. Free instances sleep when idle and have 512 MB RAM, which is
  tight for big presentations; a paid instance is smoother.
- **Hugging Face Spaces** (free, 16 GB RAM): create a *Docker* Space, upload the contents of
  `media-downloader/`, and add `ENV PORT=7860` to the Dockerfile (Spaces expects port 7860).
- **Railway / Fly.io / Google Cloud Run / any VPS**: point them at `media-downloader/Dockerfile`;
  the container listens on `$PORT` (default 8000).

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | Port the server listens on |
| `MAX_VIDEO_SECONDS` | `7200` | Reject videos longer than this |
| `MAX_DOC_BYTES` | `314572800` | Max size of a downloaded document (300 MB) |
| `MAX_CAPTURE_PAGES` | `500` | Max pages captured from a viewer page |
| `YTDLP_COOKIES` | – | Path to a Netscape `cookies.txt` for YouTube (see below) |
| `CHROMIUM_PATH` | – | Use a specific Chromium binary instead of Playwright's |

### YouTube on cloud servers

YouTube often blocks requests from data-centre IPs with *"Sign in to confirm you're not a bot"*.
If you see that error on your host:

1. Export cookies for youtube.com from a browser where you are signed in (e.g. with the
   "Get cookies.txt LOCALLY" extension) using a throwaway account.
2. Make the file available in the container (a secret file on Render / Spaces) and set
   `YTDLP_COOKIES=/path/to/cookies.txt`.

Running it on your own computer or a home server avoids this entirely.
Keep `yt-dlp` up to date (`pip install -U yt-dlp`, or redeploy), since YouTube changes often.

## Safety

- Links that resolve to private/local network addresses are refused (prevents the server being
  used to reach internal services). Every redirect and browser sub-request is checked too.
- Temporary files are deleted as soon as the download is sent.
- Only download content you own or have permission to save.
