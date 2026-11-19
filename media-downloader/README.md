# Media Grabber

A small website that takes a link and gives you a file:

| Paste a link to… | You get |
|---|---|
| A YouTube (incl. Shorts) or other video-site link | **MP4** (choose up to 1080p / best) or **MP3** audio — optionally only a **clip** between a start and end time |
| A direct **PDF** link | The PDF, untouched |
| **Google Drive** / any **web page that shows a PDF or PPT** (PDF.js, slide viewers, embedded PDFs) | Opened in a headless browser, **scrolled to the end**, and **every page/slide image is saved at full resolution** into one PDF |
| **Google Slides / Docs / Sheets**, **Dropbox** share links | Exported directly as **PDF** |
| A direct **.ppt / .pptx / .doc / .docx** file link | Opened in Google's online viewer and captured page by page into a PDF |

## Clipping a video

Fill in **Start** and/or **End** (seconds, `MM:SS` or `HH:MM:SS`) to download only that part.
This is the same as running:

```bash
yt-dlp -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]" \
  --download-sections "*00:00:08-00:00:33" --force-keyframes-at-cuts \
  --merge-output-format mp4 <link>
```

Leave Start empty to begin at 0:00; leave End empty to go to the end of the video.

## How the document capture works

1. Known share links are rewritten to their direct export URL (e.g. Google Slides → `/export/pdf`).
   If the link is a PDF file, it is returned as-is.
2. Otherwise the link is opened in headless Chromium (raw Office files go through Google's
   online viewer; Google Drive files use Drive's own viewer) and the same approach as the
   "Download pdf or ppt from chrome" console script runs:
   - find the scrolling area (the tallest scrollable panel, or the page itself);
   - scroll it top to bottom in half-screen steps, and at every step collect each `<img>` / `<canvas>`
     that is at least 300 px — at its **native resolution**, not a screenshot;
   - viewers that recycle elements or re-render canvases are handled (each page is keyed by its image
     URL or its position, and a canvas is re-taken if it changes after it first appears);
   - cross-origin page images that can't be read in the page are downloaded with the page's cookies;
   - stop when the bottom is reached and no new pages appear for a few rounds; sort by position.
3. Fallbacks for viewers without page images: screenshot repeated page elements, and finally the
   scrolling area one screen at a time. An embedded PDF (`<iframe>/<embed>/<object>`) is downloaded directly.

The response header `X-Capture-Method` (shown in the UI) tells you which path was used.

## Run locally

**Windows (easiest):** install Python from https://www.python.org/downloads/ (tick *Add python.exe to PATH*),
download this repo (green **Code** button → *Download ZIP*, then unzip), edit the `FFMPEG_LOCATION` line in
`start-windows.bat` if your ffmpeg folder is different, and double-click `start-windows.bat`.
The first run installs everything (a few minutes); then your browser opens http://localhost:8000.

**Mac / Linux / manual:**

Requires Python 3.11+ and `ffmpeg`.

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

## Host it: Vercel (website) + Render (download server)

Vercel serves the web page; Render runs the download server (yt-dlp, ffmpeg and Chromium are too
big / too slow for Vercel functions). The page calls the Render server directly.

**1. Render — the download server (do this first)**
1. Sign up at https://render.com with GitHub.
2. **New → Blueprint** → choose this repository → **Apply**. `render.yaml` builds the `Dockerfile`.
3. Wait for the first deploy (~5–10 min), then copy the URL, e.g. `https://media-grabber.onrender.com`.
   Opening `<that URL>/api/health` should show `{"ok":true,...}`. (The Render URL also serves the
   full website by itself.)

**2. Vercel — the website**
1. Sign up at https://vercel.com with GitHub → **Add New → Project** → import this repository.
2. Under **Environment Variables** add `API_BASE` = your Render URL from step 1 (no trailing slash).
3. **Deploy.** `vercel.json` builds a static site from `app/static` and writes `API_BASE` into `config.js`.

**3. Lock the server to your site (recommended)**
In Render → your service → **Environment**, set `ALLOWED_ORIGINS` to your Vercel address,
e.g. `https://mydownloader.vercel.app`, and save (it redeploys).

Notes:
- Render's free plan sleeps after ~15 minutes idle; the first request then takes up to a minute.
  The page wakes the server as soon as it opens and shows its status at the bottom.
- Free plan memory is 512 MB. Very long documents may need a paid instance.
- Any other Docker host (Railway, Fly.io, Cloud Run, a VPS) works for the server too — it listens on `$PORT`.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8000` | Port the server listens on |
| `ALLOWED_ORIGINS` | – | Comma-separated sites allowed to call the server from the browser (your Vercel URL), or `*` |
| `API_BASE` (Vercel only) | – | URL of the download server, written into the website at build time |
| `MAX_VIDEO_SECONDS` | `7200` | Reject videos longer than this |
| `MAX_DOC_BYTES` | `314572800` | Max size of a downloaded document (300 MB) |
| `MAX_CAPTURE_PAGES` | `500` | Max pages captured from a viewer page |
| `YTDLP_COOKIES` | – | Path to a Netscape `cookies.txt` for YouTube (see below) |
| `FFMPEG_LOCATION` | – | Folder containing ffmpeg, if it is not on PATH |
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
