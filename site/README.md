# Site

One static page for Architect. It leads with what Architect is (an agent factory) and the
rule that an agent only runs if it passed its exam, shown in three steps (passed, changed
and stopped, retake or undo). Then come how an agent gets its exam with the receipt reader's
results, the retake trial's counts, what the rule does not do, the video and how to try it.
It is built for GitHub Pages at echoo19.github.io/architect-nemotron.

## Build and preview

```
node site/build.mjs
```

The build needs Node 20 or later and nothing else. It writes `site/dist`, which you can open
straight from disk, serve with `npx serve site/dist` or `python -m http.server -d
site/dist`, or publish with the Pages workflow below.

`node site/build.mjs --release` is the build for publishing. It refuses placeholder data and
the private markers the release scanner looks for, such as local paths.

Add `?theme=dark` to start in the dark theme, and `?os=windows`, `?os=mac-arm64`,
`?os=mac-x64` or `?os=none` to choose which download link is filled. Without it, the page
fills the link for the visitor's system.

## What the build does

1. Copies Lectern's `tokens.css`, `fonts.css` and `motion.css` unchanged from
   `desktop/src/styles`, with the woff2 files from `desktop/src/assets/fonts`, and
   `viz.css` from `design/`.
2. Wraps `design/components.js` as a classic script, because module scripts do not load from
   a local file.
3. Renders the video's poster (the bar view of the build with rule checks, with no words),
   the download links, the terminal commands, the results paragraphs, the two tables and the
   notes under them into `index.html`, so the page reads with script turned off.
   `src/site.js` then adds the tooltips, the copy buttons, the theme switch and the video.

## Layout

| Path | What it holds |
|---|---|
| `build.mjs` | the build |
| `src/` | the page template, its styles and script, and the build-time renderers |
| `design/` | the shared visual spec: `SPEC.md`, `components.js`, `viz.css` and the prototype |
| `data/` | everything the page shows; `data/README.md` documents both files |
| `tools/shoot.mjs` | screenshots and layout checks with one headless Chrome |
| `vendor/` | the licenses of the fonts and of Tabler Icons |
| `shots/` | screenshots taken with `tools/shoot.mjs` |

## Publishing

`.github/workflows/pages.yml` builds `site/dist` and deploys it to GitHub Pages. It runs only
when started by hand from the Actions tab, and it runs the `--release` build unless the
person starting it ticks the box for placeholder data.

## Screenshots

`tools/shoot.mjs` needs puppeteer-core, which this repository does not install:

```
npm install --prefix /tmp/shoot puppeteer-core
PUPPETEER_DIR=/tmp/shoot node site/tools/shoot.mjs
```

It uses Chrome at its usual Windows path unless `CHROME` names another, opens one browser for
every capture and closes it at the end, and writes `shots/desktop-*.png` (1440 by 900) and
`shots/phone-*.png` (390 wide). Add `--file` to load the page from disk instead of a local
server. It also reports console errors, sideways scrolling, text outside its chart and fonts
that failed to load.
