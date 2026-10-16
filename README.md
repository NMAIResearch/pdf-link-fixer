# PDF link fixer

`pdf_relative_links.py` finds links in a rendered PDF that point at your own disk, and can rewrite them as relative links
without changing any other byte of the file.

## The problem it addresses

When Chromium prints a page to PDF, it makes relative links absolute (Puppeteer issue #3682,
https://github.com/puppeteer/puppeteer/issues/3682). For a page opened from a local HTML file, a Markdown link such as
`NOTES.md` can end up in the PDF as `file:///home/<you>/<project>/NOTES.md`: the PDF then names a folder on your disk, and
the link works only on a machine that has the same file at the same path. The same has been reported for the VS Code
extension Markdown PDF (https://github.com/yzane/vscode-markdown-pdf/issues/142) and for the gutterpress book builder
(https://github.com/dimm-city/gutterpress/issues/263).

## Commands

- `check PDF [PDF ...]` reads each PDF with a strict parser that accepts one narrow kind of file (see Limits), and reads
  every link target held in a `/URI` entry. It fails a PDF when a target is local (a `file:` link, a Windows drive or UNC
  path, an absolute or `~` path, or a relative path that climbs into a home folder) or when an action can open another
  file (Launch, GoToR, GoToE, ImportData, SubmitForm, Thread, Sound, Movie, Rendition). It cross-checks its own reading
  with poppler's `pdfinfo -url`, and fails when `pdfinfo` cannot run, reports any error, or finds more local targets than
  its own reading. It prints one JSON report, which gives for each PDF the number of link targets and how many are local,
  of which kinds, without the targets themselves; it exits 0 only when every PDF passes.
- `fix IN.pdf OUT.pdf RECORD.json EXPECTED_COUNT` rewrites each `file://` link as a relative link of exactly the same byte
  length (`./` padding, then the file name, query and fragment), so no object moves and the cross-reference table stays
  valid. It refuses to overwrite files, refuses a PDF it cannot read fully and any link it cannot rewrite exactly, fails
  unless exactly `EXPECTED_COUNT` links change, checks that no byte outside the rewritten links changed, re-checks the
  result and writes a record.
- `vocabulary` prints the fixed words the report may use.

A PDF that the parser does not fully accept, or that `pdfinfo` reports an error for, is reported `incomplete`, which also
fails.

## Requirements

- Python 3, standard library only.
- poppler-utils (`pdfinfo` for the cross-check; the tests also use `pdftotext`).

Tested on Linux with Python 3.14.8 and poppler 26.01.0.

## Use

```text
python3 pdf_relative_links.py check paper.pdf
```

If the check fails with local targets, rewrite them, giving the count the report shows as `local_targets`:

```text
python3 pdf_relative_links.py fix paper.pdf paper.fixed.pdf fix-record.json 2
python3 pdf_relative_links.py check paper.fixed.pdf
```

Exit status: 0 when every PDF passes (or a fix succeeds), 1 when a check fails or a fix is refused, 2 on an unexpected
error, which prints only the error's class.

The report names the PDFs as you gave them; the fix record names them by file name. Review both before sharing them.

## Limits

- It accepts one narrow kind of file: a single revision with a classic cross-reference table and direct stream lengths. It
  read every one of 117 PDFs printed by Chromium (Producer Skia/PDF m149 to m155) that it was checked against; its tests
  use synthetic PDFs. PDFs that use object or cross-reference streams, incremental updates, encryption or indirect stream
  lengths are reported `incomplete`.
- It also reports `incomplete` when `pdfinfo` reports an error that has nothing to do with links, such as an annotation
  without a rectangle.
- It reads link targets and action types only. It does not inspect JavaScript, metadata, embedded files or page text, and
  it does not classify a link's query or fragment.
- `fix` rewrites only `file://` links held in direct literal strings without escapes. When the padding is odd it adds an
  empty fragment `#` to a link that has none, and refuses a link that already has a fragment; whether every PDF viewer
  follows such a link has not been established.
- `fix` writes the new PDF and its record as two files, one after the other, so an interruption can leave the PDF without
  its record.
- It is not a general PDF sanitiser; use a metadata tool and your own review for the rest of the file.

## Alternatives

- **Fix it before printing.** Drop or rewrite relative links in the HTML before the browser prints it. The 0.10.9 release
  branch of gutterpress does this in its PDF builds: it drops the `href` of each relative link and keeps the text, leaves
  `#anchor`, web and `mailto:` links as they are, and warns with the file, line and target (issue #263).
- **Edit the PDF with a general library.** pikepdf maps a PDF's dictionaries, arrays and streams directly to Python types;
  pypdf can read and create annotations; PyMuPDF can read, write and annotate PDFs; qpdf's QDF mode makes a PDF editable,
  with some restrictions, in an ordinary text editor, and if the edits are kept within certain constraints its fix-qdf
  program can restore the file to a correct state, recomputing parts of it.
- **Check other actions.** veraPDF's rules for PDF/A-2 and PDF/A-3 forbid Launch and several other actions (they allow URI
  and GoToR); Didier Stevens' `pdfid` counts risky keywords for triage.

What this tool adds is narrow: a standard-library check that fails a PDF it cannot read, or that poppler reports an error
for, and is cross-checked by poppler; and a repair that changes no byte outside the rewritten links.

## Tests

```text
python3 -m unittest discover -s tests
```

32 tests on synthetic PDFs, writing only into temporary folders.

## Review status

An OpenAI GPT-6 model's independent review in October 2026 found one defect; a GPT-6.1 model's re-check found it fixed.

## AI assistance

Written with AI assistance (Anthropic Claude Opus 5.5 in Claude Code) under the author's direction.

## Licence

Code under the MIT licence, documentation under CC BY 4.0; see `LICENSE.md`.
