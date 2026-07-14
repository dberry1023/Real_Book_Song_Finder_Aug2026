# Real Book Song Finder

A local web app that finds any song across your Real Book collection and
displays the sheet music instantly in your browser.

**1,780 songs · 2,491 charts · 7 books**

> **This repo is private.** It contains code and an index (song titles +
> page numbers) only — no sheet music PDFs. Keep it private: the Quick
> Start link below embeds an access token, so treat repo access the same
> as you'd treat a password.

## 🚀 Quick start — open the app right now

Once Tailscale is set up on this PC and your device (one-time, see
[below](#using-it-from-other-devices-tailscale)), your personal link is:

```
http://YOUR-TAILSCALE-DEVICE-NAME:8765/?key=YOUR-ACCESS-TOKEN
```

- `YOUR-TAILSCALE-DEVICE-NAME` — this PC's Tailscale name, shown in the
  Tailscale app's system tray icon (commonly something like
  `statsias-pc` or `statsias-desktop`).
- `YOUR-ACCESS-TOKEN` — open `data/.access_token` in Notepad on this PC
  to see it. **This file is intentionally excluded from git** (see
  `.gitignore`) — never paste the real token into this README or any
  other tracked file, even here in a private repo, since it would stay
  in the git history permanently.

Build the real link once using the two values above, then save *that
filled-in link* as a bookmark / home-screen icon on your phone — not
committed to the repo, just kept locally on your device.

It only works from a device signed into your own Tailscale account —
that's intentional, see [Using it from other devices](#using-it-from-other-devices-tailscale).

## Setup (first time, or on a new PC)

```bash
git clone https://github.com/dberry1023/real-book-song-finder.git
cd real-book-song-finder
pip install -r requirements.txt
```

Then edit the `BOOKS_DIR` path near the top of `server.py` and
`build_database.py` to point at wherever your Real Book PDFs actually
live — **this repo does not include the PDFs themselves** (they're
copyrighted sheet music). See [The books](#the-books) below for exactly
which files it expects to find there.

## How to start it

**On this PC only:** double-click **`Start_Song_Finder.bat`** (or run
`python server.py`). Your browser opens automatically at
<http://127.0.0.1:8765>.

**From your phone/laptop too:** see [Using it from other devices](#using-it-from-other-devices-tailscale)
below — it's already set to auto-start in that mode every time you log
into this PC, so you usually don't need to launch anything manually.

## How to use it

- **Type a song name** in the search box — matching is forgiving
  (word order and articles don't matter), or scroll the full list.
- **Click a song** (or press `Enter` for the top match) to display it.
- If a song is in several books, **tabs above the page** let you switch
  editions (e.g. the 6th Edition vs. the original hand-written chart).
- Use **◀ ▶** or the **arrow keys** to flip pages for multi-page tunes.
- **− / Fit / +** control zoom · **/** jumps to the search box ·
  **🎲 Random song** picks one for practice.

## The books

| Badge | Book | PDF file |
|---|---|---|
| 6th Ed | Real Book 6th Edition (Hal Leonard) | Real-Book-6.pdf |
| RB4 | Real Book Vol 4 (Hal Leonard) | Real-Book-4.pdf |
| 5th Ed | Real Book 5th Edition | Real Book-5.pdf |
| RB1 | Real Book Vol 1 (original) | Real Book-I.pdf |
| RB2 | Real Book Vol 2 | Real-Book-2.pdf |
| RB3 | Real Book Vol 3 | Real-Book-3.pdf |
| Xmas | Christmas Real Book | Christmas-Real-Book.pdf |

The PDFs stay where they are, in
`C:\Users\stats\OneDrive\Desktop\Jazz Night School\Real_Books`
(path configured at the top of `server.py` and `build_database.py`).

## How the index was built (`data/songs.json`)

- **Vols 1–3**: parsed from `MASTER Index.pdf` (song → book → page),
  using each PDF's page-label offset (verified against the actual pages).
- **Vol 4 & 6th Edition**: their in-book tables of contents were OCR'd
  (RapidOCR) and cleaned against the official Hal Leonard
  `Real-Book-Song-Finder.pdf` title list.
- **5th Edition**: its own 10-page in-book index was OCR'd.
- **Christmas book**: used its built-in PDF bookmarks.

To rebuild after changing anything: `python build_database.py`
(needs `pip install rapidocr-onnxruntime` only if you re-run the OCR step
in `ocr_tocs.py`; the OCR results are already saved in `data/`).

## Using it from other devices (Tailscale)

The Real Books are copyrighted sheet music, so this app is built to stay
**private to your own devices** — never exposed to the public internet.
[Tailscale](https://tailscale.com) is a free private network that makes
that possible without any router/port-forwarding setup.

### One-time setup

1. On **this PC**: install Tailscale from tailscale.com, sign in (e.g.
   with your Google account), and leave it running. It sits quietly in
   the system tray.
2. On **your phone/laptop**: install the Tailscale app, sign in with the
   **same account**.
3. That's it — the two devices can now reach each other directly and
   privately, wherever they are, even on different WiFi/cellular networks.

The song finder is **already configured to auto-start in this mode**
every time you log into this PC (a shortcut was added to your Windows
Startup folder), so after the one-time Tailscale setup above, it's
always running and ready.

### Finding your link

1. Open the Tailscale app on this PC (system tray icon) → it shows this
   machine's Tailscale name, something like `statsias-pc`.
2. Get the access token — open
   `real_book_song_finder\data\.access_token` in Notepad (it's a short
   random string, generated once and reused).
3. Your personal link is:

   ```
   http://statsias-pc:8765/?key=PASTE_YOUR_TOKEN_HERE
   ```

   (swap in your actual Tailscale device name and token). Test it from
   your phone first, with Tailscale running on both devices, before
   adding it to Wix.

### Adding the link on statsias.com (Wix)

1. In the Wix Editor, add a **Button** or **Text link** element wherever
   you want it (e.g. your homepage or a "Tools" page).
2. Select it → **Link** → choose **Web Address** → paste the URL from
   above → set it to open in a **New Tab**.
3. Publish the site.

Because the link points at your private Tailscale address, it will only
ever work from a device that's signed into your Tailscale account — if
someone else clicks it, it just won't load. That's expected and fine;
it means the sheet music stays private while still being one click away
for you, from anywhere.

**Optional — a friendlier name:** in the Tailscale admin console
(login.tailscale.com/admin/machines) you can rename this PC to something
tidier than the default, which changes the hostname in the link above
(e.g. `song-finder` instead of `statsias-desktop-3`).

## Notes

- A handful of index entries were dropped where the source index itself
  was corrupt (e.g. *Alfie* has a garbled page number in the MASTER Index —
  it is still findable via the 5th/6th Edition sources).
- **Local mode** (`Start_Song_Finder.bat` / plain `python server.py`)
  binds to `127.0.0.1` only — nothing beyond this PC can reach it, no
  token needed.
- **LAN mode** (`--lan` flag, used by the autostart shortcut and
  `Start_Song_Finder_LAN.bat`) binds to all network interfaces so your
  other devices can reach it over Tailscale, and requires the access
  token in `data/.access_token` — keep that file private, it's your
  password. If it's ever compromised, just delete
  `data/.access_token` and restart the server to generate a new one
  (you'll need to update your saved link/Wix button afterward).
- To stop the always-on background copy: open Task Manager → find
  `pythonw.exe` → End Task. To stop it from auto-starting at all,
  delete the shortcut from your Startup folder (press `Win+R`, type
  `shell:startup`, delete "Real Book Song Finder.lnk").
