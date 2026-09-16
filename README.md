# Mathpix PDF pipeline

A small, general CLI for running reproducible Mathpix document experiments and
checking the results from several independent angles. It supports any PDF, keeps
the exact request options and source hash beside every run, downloads the editable
sources, and creates machine-readable QA plus side-by-side review sheets.

The standalone reader generator is the core of the public
[PDF++ reader project](https://github.com/issssse/pdf-plus-reader).

## Setup

Python 3.10+, Poppler (`pdftoppm`), and the two existing `.env` entries are required.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

The `.env` file is ignored by git. Values are never copied into a run manifest.

## Try representative pages first

Page selections are 1-based and accept ranges and `last`:

```bash
.venv/bin/mathpix-pdf run "Transformteori för Ingenjörer-1.pdf" \
  --pages 3,8,14 \
  --label baseline \
  --options configs/default.json
```

The default region is EU. Use `--region global` if the API key or workload requires
the global endpoint. A no-cost request preview is available with `--dry-run`.

Each run is isolated under `output/runs/<document>/<timestamp-label>/` and contains:

- `manifest.json`: input SHA-256, selected pages, region, and complete API options
- `status.json`: final Mathpix state and model/status metadata returned by the API
- `outputs/result.latex.pdf` and `outputs/result.pdf`: regenerated PDFs
- `outputs/result.tex.zip`: editable LaTeX and embedded assets
- `outputs/result.mmd` and `outputs/result.lines.json`: OCR/structure artifacts
- `qa/report.json`: comparable measurements
- `qa/compare-*.png`: source/output pages side by side

## Build a portable PDF++ reader

The `html` command keeps the original scanned page as the visible source of truth,
then places the Mathpix OCR lines invisibly on top. The result supports ordinary
browser text selection (including normal double-click word selection), full-document
search, direct page jumps, highlighted matches, and copying prose or formula text.
Plain wheel/trackpad gestures scroll normally. Ctrl/Cmd-wheel and trackpad pinch
zoom around the pointer, while native mobile pinch zoom remains available.

Build directly from a completed run:

```bash
.venv/bin/mathpix-pdf html output/runs/course/TIMESTAMP-baseline \
  --output output/html/course-reader.html \
  --title "Course reader"
```

Or combine any PDF with its matching Mathpix `lines.json`:

```bash
.venv/bin/mathpix-pdf html course.pdf \
  --lines result.lines.json \
  --output output/html/course-reader.html
```

The default output is one standalone `.html` file. All compressed page images,
OCR coordinates, corrections, controls, and QA metadata are embedded in that
file. It makes no network requests and needs no local server, installation,
sidecar files, or unpacking: send the HTML file and open it directly in a browser.
If `--output` has no `.html` suffix, the CLI adds it automatically.

The reader deliberately keeps its UI small. A search icon at the top left expands
into a field when clicked or focused with Ctrl+F / Cmd+F. The current/editable page
number floats at the bottom right on desktop and appears progressively while
scrolling on small screens. OCR-zone, two-page spread, and reader-information
buttons remain at the top right. Search, OCR, and spread controls disappear on
small screens; the page number and information button remain. There is no menu,
toolbar, or backdrop. Search results remain open after a jump and all matching
mounted OCR lines stay highlighted. OCR boxes never flash while pages are mounted;
they appear only when the OCR toggle is enabled or as persistent search highlights.

For exceptionally large books, `--folder` retains the older folder/ZIP format,
which uses less browser memory because page images remain separate:

```bash
.venv/bin/mathpix-pdf html RUN --folder --output output/html/course-reader
```

The reader build writes its own `manifest.json` and `qa.json`. It refuses PDF/OCR
page-count mismatches and treats PDF/image/OCR aspect-ratio disagreement as a hard
failure rather than publishing a shifted text layer. It also validates bounding
boxes, records confidence diagnostics, checks every rendered page, and stores input
hashes. Rendering follows each PDF page's visible CropBox rather than assuming that
the MediaBox is the visible page, so mixed scan crops and page proportions remain
aligned. Use `--dpi` and `--quality` to make explicit size/clarity tradeoffs.

Reviewed OCR corrections can be applied without touching the visible scan. Each
correction is tied to a Mathpix line ID and an exact old string, so stale or
ambiguous edits fail the build. Existing page-level `--checks` can then assert
that important formulas and diacritics are present and known errors are absent:

```bash
.venv/bin/mathpix-pdf html RUN \
  --corrections benchmarks/course-html-corrections.json \
  --checks benchmarks/course-pages.json \
  --output output/html/course-reader.html
```

### Reader compatibility

The generated reader is dependency-free and targets current Chrome, Edge,
Firefox, Safari, iOS Safari, and Android browsers. It works from `file://` as well
as a normal web server. It adapts per page, so one document may mix portrait,
landscape, rotated, square, or unusually wide/tall pages without shifting the OCR
layer. Mobile safe areas, orientation changes, touch/pinch navigation, 200% text
zoom, reduced motion, forced colors, printing, and keyboard navigation are covered.

Only the current page and three neighboring pages in either direction are mounted.
This bounds image decoding and OCR DOM work to at most seven pages, while page
jumps use precomputed geometry and search first consults a compact per-page index.
The implementation does not require `ResizeObserver`, `IntersectionObserver`, or
the Clipboard API. CSS `aspect-ratio` and native async image decoding are optional
enhancements; explicit pixel geometry keeps the reader usable without them.
Like PDF.js, each invisible OCR line is horizontally fitted to its source bounding
box. This substantially improves double-click and drag-selection alignment for
individual prose words even though Mathpix supplies line boxes rather than exact
glyph coordinates. The selection layer also uses PDF.js's end-of-content pattern
to prevent browsers from painting large selection rectangles across empty page
areas. Formulas and unusually spaced text remain approximate.

Headings reported by Mathpix form a structured outline. Each page inherits its
nearest heading, browser titles update as the reader moves, and search results are
real links such as `#page=8&block=...`. These can be opened in a new tab or copied
from the native context menu and still have a page-only fallback when no heading is
available. The title source order is explicit `--title`, PDF metadata, then filename.

Mathpix `table_of_contents_item` and `table_of_contents_number` rows become real
links over the printed contents page. Destinations are resolved by matching the
normalized title to document headings and by inferring the printed-to-PDF page
offset from multiple confirmed matches. Numeric section prefixes must agree, which
prevents similarly named sections in other chapters from stealing a link. Once the
offset is established, printed page numbers disambiguate repeated headings such as
`Problems` or `Exercises`. Unresolved rows remain ordinary selectable OCR text
rather than receiving a guessed target.

Exercise headings also form a conservative structured index. The builder carries
chapter and exercise-group context forward through the OCR stream, then records only
numbered text lines inside a recognized `Testproblem`, `Övningar`, `Exercises`,
`Problem set`, or `Review problems` section. This avoids treating every isolated
number as an exercise. Students can search one item or paste a whole assignment row:

```text
tp kap 3 14ac
öv kap 5 17
Testproblem kap. 1: 10abc, 11ab, 12ab, 13acef
Övningar kap. 2: 1cd, 2abc, 5ab, 8abc, 9
```

The long forms `testproblem`, `övningar`, `exercise`, and `uppgift` work as well as
the short forms `tp` and `öv`. A query's letters are displayed as requested
subparts, while navigation goes to the indexed start of the numbered exercise; that
is more reliable across OCR formats than guessing separate anchors for `a`, `b`, and
`c`. Results are scoped by group and chapter, so exercise 13 in another chapter or
in the wrong problem collection is not returned. Ordinary full-text search remains
the fallback for documents whose headings cannot be recognized.

Numbered answer and solution sections are indexed separately. Prefix the same
structured query with `svar`, `facit`, `lösning`, `answer`, or `solution`:

```text
svar tp kap 3 14ac
lösning öv kap 5 17
answer problem chapter 8 24
```

The index recognizes Swedish and English section headings, chapter-qualified forms
such as `5.14.` and `5-14.`, running answer headers, and OCR that occasionally drops
a chapter-number separator inside a sequential problem list. Sections explicitly
labeled selected, odd, or even stay sparse; missing numbers are not invented there.

Every generated HTML file starts with the complete WTFPL v2 notice for the
reader/wrapper code, copyright © 2026 Isac Carlsson, the build date, an explicit
exclusion for embedded document-derived content, and an as-is warranty disclaimer.

Ctrl+P mounts the complete document into one source page per print page, disables
two-page layout for pagination, hides all controls and OCR/search decorations, and
keeps an almost transparent text layer in the browser-generated PDF. Both
`beforeprint`/`afterprint` and the older print-media listener are used for browser
compatibility. Print dimensions are fitted against both the paper width and height
using each source page's own aspect ratio; this prevents sub-pixel overflow from
creating an extra nearly blank sheet after every page.

A standalone 5,000-page scan can still be several gigabytes: a browser must read
that one physical file before its embedded last page is addressable. Virtualization
prevents 5,000 page images and OCR layers from being decoded/rendered at once, but
cannot remove the file-I/O cost imposed by the one-file format. Use lower `--dpi`
and `--quality` for very large shareable editions; `--folder` is the faster-loading
alternative if the single-file constraint is later relaxed. Internet Explorer is
not supported.

## QA strategy

Do not optimize one number. Re-typesetting intentionally changes pixels and layout,
and Mathpix's PDF and MMD share the same OCR, so visual similarity and MMD agreement
alone can hide recognition errors. The report therefore keeps separate axes:

- page count, page sizes, blank-page detection, content bounds, and ink density;
- page-flow analysis that catches split/merged source pages even if total page count matches;
- selectable character counts per page;
- MMD-to-generated-PDF CER/WER (conversion consistency, not OCR truth);
- optional CER/WER against a manually checked transcript (`--golden`);
- Mathpix line-confidence diagnostics (a useful triage signal, not ground truth);
- side-by-side pages for equations, diagrams, reading order, clipping, headers, and
  footers.

For tuning, manually transcribe a small, fixed benchmark covering prose, equations,
tables/figures, diacritics, and degraded scans. Keep those pages unchanged between
runs and compare with an earlier report:

```bash
.venv/bin/mathpix-pdf run course.pdf --pages 2,7,11,19 \
  --options configs/latex-11pt-noto.json \
  --golden benchmarks/course-pages.txt \
  --checks benchmarks/course-pages.json \
  --baseline output/runs/course/OLDER_RUN/qa/report.json \
  --label noto-11pt
```

Lower CER/WER is better. A negative `*_cer_delta` is an improvement. Baseline deltas
are intentionally not combined into a single score.

For expensive-to-transcribe material, `--checks` accepts page-level `contains` and
`not_contains` assertions against raw `lines.json`. They are ideal semantic tripwires for
critical formulas, names, units, and diacritics. A full verified transcript remains
the stronger test.

When tuning only typography or conversion settings, reuse saved MMD instead of
paying for and confounding the experiment with a second OCR pass:

```bash
.venv/bin/mathpix-pdf rerender output/runs/course/TIMESTAMP-baseline \
  --options configs/latex-11pt-noto.json --label noto-11pt
```

Pass `--mmd edited.mmd` to rerender reviewed/corrected OCR without uploading the
source scan again. The rerender manifest records the exact MMD hash.

For tighter control, compile the downloaded LaTeX locally. Exact corrections are
validated (a stale or multiply matching edit fails instead of silently changing the
wrong text), and the command writes a settings/hash manifest beside the PDF:

```bash
.venv/bin/mathpix-pdf tex-tune RUN/outputs/result.tex.zip \
  --output output/pdf/course-sample-a4.pdf \
  --paper a4paper --margin 18mm --font-size 14pt \
  --replacements benchmarks/course-replacements.json
```

Install the LaTeX packages named by the generated source. `--omit-package NAME` is
available only for packages confirmed unused in a benchmark, and
`--babel-language` can select an installed language for local experiments.

## QA existing PDFs

```bash
.venv/bin/mathpix-pdf qa sample.pdf regenerated-a.pdf regenerated-b.pdf \
  --mmd result.mmd --lines result.lines.json --qa-dir output/qa-manual
```

If a terminal or network interruption happens after submission, resume from the
saved `pdf_id` without paying for another OCR job:

```bash
.venv/bin/mathpix-pdf resume output/runs/course/TIMESTAMP-label
```

## Safe experimentation notes

- `metadata.improve_mathpix` defaults to `false`; this opts the document out of
  Mathpix quality-improvement access. See Mathpix's retention policy for remaining
  artifact retention and use their DELETE endpoint if immediate deletion is needed.
- `mmd` and `lines.json` are always produced by `v3/pdf`; they are downloaded but are
  deliberately not listed in `conversion_formats`.
- The CLI polls top-level OCR status and every requested conversion status, uses
  request and total-job timeouts, writes downloads atomically, and can resume QA from
  the saved local artifacts.
- API processing is billable. Use a stable 3-5 page benchmark before a full book.

## Findings from the included sample

The included benchmark covers source pages 3, 8, and 14. The first Mathpix OCR pass
found the document structure well, but the raw result failed three of four manual
semantic tripwires: `för` became `for`, a three-row coefficient system was merged
incorrectly, and `e^(4t)` became `c^(4t)`. The default HTML PDF also grew to four
pages, while a 14px HTML rerender kept three pages but split source content across
those boundaries. Default LaTeX preserved boundaries but used US Letter and low
visual density.

The final sample under `output/pdf/` applies the three reviewed corrections and
uses 14pt A4 LaTeX with 18mm margins. Its QA report has matching page count and size,
2,154 selectable normalized characters, no blank pages, preserved page boundaries,
and no structural/visual warnings. This validates the workflow, not the untouched
17 pages; a full run should first expand the golden transcript or semantic checks.

The matching portable reader at `output/html/transformteori-sample.html` preserves
the scan pixels, includes 59 positioned OCR lines and 3,637 selectable characters,
and passes page geometry, correction, and semantic QA without warnings.
