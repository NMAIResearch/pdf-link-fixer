"""Purpose: pdf_relative_links.py reads PDF link targets through a strict validator and a second reader (poppler's pdfinfo)
and fails closed on local targets, file-opening actions and PDFs it cannot read fully; it rewrites file:// links in place
without changing their destination. Synthetic PDFs only; writes only into temporary folders. Needs poppler-utils."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOL = Path(os.environ.get("PDF_LINKS_TEST_SOURCE", str(ROOT / "pdf_relative_links.py")))
spec = importlib.util.spec_from_file_location("pdf_links_under_test", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def link(uri_token, key=b"/URI"):
    return b"<< /Type /Annot /Subtype /Link /Rect [72 700 300 712] /Border [0 0 0] /A << /S /URI " + key + b" " + uri_token + b" >> >>"


def make_pdf(annots, extra=(), trailer_extra=b"", pad=0, catalogue_extra=b""):
    """A small valid one-page PDF carrying the given annotation dictionaries, then the extra objects, with a correct
    cross-reference table; pad adds spaces to the page content so the file can pass a size floor."""
    content = b"BT /F1 12 Tf 72 720 Td (Hello links) Tj ET" + b" " * pad
    objects = [b"<< /Type /Catalog /Pages 2 0 R" + catalogue_extra + b" >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", None,
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
    first = len(objects) + 1
    objects += list(annots) + list(extra)
    objects[2] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R /Annots ["
                  + b" ".join(b"%d 0 R" % (first + i) for i in range(len(annots))) + b"] >>")
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R" % (len(objects) + 1) + trailer_extra + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def minimal_pdf(uris, uri_form="literal", pad=0):
    tokens = [(b"(" + u + b")") if uri_form == "literal" else (b"<" + u.hex().encode("ascii") + b">") for u in uris]
    return make_pdf([link(t) for t in tokens], pad=pad)


def broken_page(pdf):
    """The PDF with its page tree pointing at an object that does not exist: the validator accepts it (the cross-reference
    table still agrees with the objects), while poppler reports syntax errors on stderr and still exits 0."""
    assert pdf.count(b"/Kids [3 0 R]") == 1
    return pdf.replace(b"/Kids [3 0 R]", b"/Kids [9 0 R]")


def pdftotext(path):
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, check=True).stdout


def run_check(folder, *names):
    done = subprocess.run([sys.executable, "-B", str(TOOL), "check", *names], cwd=folder, capture_output=True, text=True, timeout=60)
    return done.returncode, json.loads(done.stdout), done.stderr


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def check_one(self, data):
        self.write("c.pdf", data)
        code, report, _ = run_check(self.dir, "c.pdf")
        return code, report["results"][0]


class Rewrite(Folder):
    def run_fix(self, uris, expected):
        src = self.write("in.pdf", minimal_pdf(uris))
        out, record = self.dir / "out.pdf", self.dir / "record.json"
        with redirect_stdout(io.StringIO()):
            tool.fix(src, out, record, expected)
        return src, out, json.loads(record.read_text(encoding="utf-8"))

    def target(self, out):
        data = out.read_bytes()
        start = data.index(b"/URI (", data.index(b"/S /URI")) + 6
        return data[start:data.index(b")", start)]

    def test_even_padding(self):
        uri = b"file:///var/home/someone/Desk/top/FORECASTS.md"
        self.assertEqual((len(uri) - len(b"FORECASTS.md")) % 2, 0)
        src, out, record = self.run_fix([uri], 1)
        data = out.read_bytes()
        self.assertEqual(len(data), len(src.read_bytes()))
        self.assertEqual(self.target(out), b"./" * ((len(uri) - 12) // 2) + b"FORECASTS.md")
        self.assertNotIn(b"someone", data)
        self.assertEqual(record["links_rewritten"], ["FORECASTS.md"])

    def test_odd_padding_without_a_fragment_adds_an_empty_one(self):
        uri = b"file:///var/home/someone/Desktop/RESULTS.json"
        self.assertEqual((len(uri) - len(b"RESULTS.json")) % 2, 1)
        src, out, _ = self.run_fix([uri], 1)
        self.assertTrue(self.target(out).endswith(b"/RESULTS.json#"))
        self.assertEqual(len(out.read_bytes()), len(src.read_bytes()))

    def test_query_and_fragment_are_kept(self):
        for uri, tail in ((b"file:///tmpxy/A.pdf#section", b"/A.pdf#section"), (b"file:///tmpxy/A.pdf#section/x", b"/A.pdf#section/x"),
                          (b"file:///tmpx/A.pdf?page=2", b"/A.pdf?page=2#"), (b"file://host/dir/A.pdf", b"/A.pdf")):
            with self.subTest(uri=uri):
                (self.dir / "out.pdf").unlink(missing_ok=True)
                (self.dir / "record.json").unlink(missing_ok=True)
                _, out, _ = self.run_fix([uri], 1)
                self.assertTrue(self.target(out).endswith(tail), self.target(out))

    def test_odd_padding_with_a_fragment_is_refused(self):
        src = self.write("in.pdf", minimal_pdf([b"file:///tmpx/A.pdf#section"]))
        with self.assertRaisesRegex(SystemExit, "existing fragment"):
            tool.fix(src, self.dir / "out.pdf", self.dir / "record.json", 1)
        self.assertFalse((self.dir / "out.pdf").exists())

    def test_text_and_validity_unchanged(self):
        src, out, _ = self.run_fix([b"file:///var/home/someone/x/FORECASTS.md", b"file:///var/home/someone/x/RESULTS.json"], 2)
        self.assertEqual(pdftotext(src), pdftotext(out))
        info = subprocess.run(["pdfinfo", str(out)], capture_output=True, text=True)
        self.assertEqual((info.returncode, info.stderr), (0, ""))

    def test_only_link_bytes_change(self):
        src, out, record = self.run_fix([b"file:///var/home/someone/x/FORECASTS.md"], 1)
        a, b = src.read_bytes(), out.read_bytes()
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        (start, end), = record["byte_spans"]
        self.assertTrue(diff)
        self.assertTrue(all(start <= i < end for i in diff))

    def test_percent_encoding_is_kept(self):
        _, out, record = self.run_fix([b"file:///var/home/someone/My%20Docs/My%20File.md"], 1)
        self.assertIn(b"My%20File.md", out.read_bytes())
        self.assertEqual(record["links_rewritten"], ["My%20File.md"])

    def test_refusals(self):
        cases = [
            ("wrong count", "expected 2 local file links, found 1", minimal_pdf([b"file:///var/home/someone/x/FORECASTS.md"]), 2),
            ("hexadecimal string", "cannot be rewritten", minimal_pdf([b"file:///var/home/someone/x/FORECASTS.md"], uri_form="hex"), 1),
            ("escaped literal", "cannot be rewritten", make_pdf([link(b"(file:///tmp/\\101.md)")]), 1),
            ("indirect string", "cannot be rewritten", make_pdf([link(b"7 0 R")], [b"(file:///tmp/A.md)"]), 1),
            ("unbalanced result", "unbalanced parentheses", make_pdf([link(b"(file:///tmpa/(b/c)d.md)")]), 1),
            ("host without a path", "no file name to keep", make_pdf([link(b"(file://server)")]), 1),
            ("file-opening action", "action that opens another file",
             make_pdf([link(b"(https://example.com/)"), b"<< /Type /Annot /Subtype /Link /Rect [72 600 300 612] /A << /S /Launch /F (x.pdf) >> >>"]), 0),
            ("unreadable structure", "cannot read",
             make_pdf([link(b"(file:///tmp/A.md)")], [b"<< /Type /ObjStm /N 0 /First 0 /Length 0 >>\nstream\n\nendstream"]), 1),
            ("document base", "document base", make_pdf([link(b"(file:///tmp/A.md)")], catalogue_extra=b" /URI << /Base (https://example.org/) >>"), 1),
            ("page poppler cannot read", "cannot read", broken_page(make_pdf([link(b"(file:///tmp/A.md)")])), 1),
        ]
        for name, message, data, count in cases:
            with self.subTest(case=name):
                src = self.write("in.pdf", data)
                with self.assertRaisesRegex(SystemExit, message):
                    tool.fix(src, self.dir / "out.pdf", self.dir / "record.json", count)
                self.assertFalse((self.dir / "out.pdf").exists())

    def test_refuses_to_overwrite(self):
        src = self.write("in.pdf", minimal_pdf([b"file:///var/home/someone/x/FORECASTS.md"]))
        out = self.write("out.pdf", b"existing")
        with self.assertRaisesRegex(SystemExit, "refusing to overwrite"):
            tool.fix(src, out, self.dir / "record.json", 1)
        self.assertEqual(out.read_bytes(), b"existing")


class Check(Folder):
    def test_local_targets_fail_in_every_spelling(self):
        cases = [link(b"(file:///var/home/someone/x/FORECASTS.md)"), link(b"(/home/someone/notes.md)"), link(b"(notes/../home/someone/x.md)"),
                 link(b"<" + b"file:///tmp/x.md".hex().encode() + b">"), link(b"(FILE:///tmp/A.pdf)"), link(b"(C:\\\\Users\\\\someone\\\\A.pdf)"),
                 link(b"(D:/data/A.pdf)"), link(b"(\\\\\\\\server\\\\share\\\\A.pdf)"), link(b"(file:///home/someone/A(1).md)"),
                 link(b"(\\146ile:///tmp/A.md)"), link(b"(fi\\\nle:///tmp/A.md)"), link(b"(file:///tmp/A.md)", key=b"/U#52I"),
                 link(b"%a comment\n(file:///tmp/A.md)")]
        for annot in cases:
            with self.subTest(annot=annot):
                code, result = self.check_one(make_pdf([annot]))
                self.assertEqual((code, result["status"], result["local_targets"]), (1, "fail", 1))

    def test_indirect_and_base_targets_are_followed(self):
        code, result = self.check_one(make_pdf([link(b"7 0 R")], [b"(file:///tmp/A.md)"]))
        self.assertEqual((code, result["status"], result["local_targets"]), (1, "fail", 1))
        code, result = self.check_one(make_pdf([link(b"(A.md)")], catalogue_extra=b" /URI << /Base (file:///tmp/) >>"))
        self.assertEqual((code, result["status"], result["local_targets"], result["local_kinds"]), (1, "fail", 2, ["file link"]))
        action_dictionary = b"<< /Type /Annot /Subtype /Link /A << /S /URI /URI << /Base (file:///tmp/) >> >> >>"
        code, result = self.check_one(make_pdf([action_dictionary]))
        self.assertEqual((code, result["status"], result["unsupported"]), (1, "incomplete", ["a /URI value that is not a string"]))

    def test_relative_targets_follow_the_document_base(self):
        web = b" /URI << /Base (https://example.org/) >>"
        for uri, base, expected in ((b"/report.pdf", web, (0, "pass", 0)), (b"docs/home/manual.pdf", web, (0, "pass", 0)),
                                    (b"../../home/someone/x.md", web, (0, "pass", 0)), (b"search.html?next=/home/page", b"", (0, "pass", 0)),
                                    (b"docs/home/manual.pdf", b"", (0, "pass", 0)), (b"~/notes.md", b"", (1, "fail", 1)),
                                    (b"../../home/someone/x.md", b"", (1, "fail", 1)), (b"../Users/someone/x.md", b"", (1, "fail", 1)),
                                    (b"/report.pdf", b"", (1, "fail", 1))):
            with self.subTest(uri=uri, base=base):
                code, result = self.check_one(make_pdf([link(b"(" + uri + b")")], catalogue_extra=base))
                self.assertEqual((code, result["status"], result["local_targets"]), expected)

    def test_each_local_kind_is_named(self):
        for uri, kind in ((b"file:///tmp/A.md", "file link"), (b"/home/someone/A.md", "absolute path"), (b"~/A.md", "home path"),
                          (b"../home/someone/A.md", "home path"), (b"C:\\\\Users\\\\A.md", "drive path"),
                          (b"\\\\\\\\server\\\\share\\\\A.md", "UNC path")):
            with self.subTest(uri=uri):
                code, result = self.check_one(make_pdf([link(b"(" + uri + b")")]))
                self.assertEqual((code, result["status"], result["local_kinds"]), (1, "fail", [kind]))

    def test_redundant_segments_and_backslashes_still_climb_into_home(self):
        for uri, expected in ((b".././home/someone/a.pdf", (1, "fail", 1)), (b"..//home/someone/a.pdf", (1, "fail", 1)),
                              (b"..\\\\home\\\\someone\\\\a.pdf", (1, "fail", 1)), (b"./../var/home/someone/a.pdf", (1, "fail", 1)),
                              (b"docs/./home/manual.pdf", (0, "pass", 0)), (b"../docs/a.pdf", (0, "pass", 0))):
            with self.subTest(uri=uri):
                code, result = self.check_one(make_pdf([link(b"(" + uri + b")")]))
                self.assertEqual((code, result["status"], result["local_targets"]), expected)

    def test_a_document_base_decides_relative_targets(self):
        for base, expected in ((b"ftp://example.org/docs/", (0, "pass", 0)), (b"file:///tmp/", (1, "fail", 2)),
                               (b"https://example.org/", (0, "pass", 0))):
            with self.subTest(base=base):
                code, result = self.check_one(make_pdf([link(b"(A.pdf)")], catalogue_extra=b" /URI << /Base (" + base + b") >>"))
                self.assertEqual((code, result["status"], result["local_targets"]), expected)

    def test_signed_stream_lengths(self):
        text = b"BT (signed) Tj ET"
        for length, expected in ((b"+%d" % len(text), (0, "pass", [])), (b"-1", (1, "incomplete", ["a stream length that is not a direct integer"]))):
            with self.subTest(length=length):
                stream = b"<< /Length " + length + b" >>\nstream\n" + text + b"\nendstream"
                code, result = self.check_one(make_pdf([link(b"(https://example.org/)")], [stream]))
                self.assertEqual((code, result["status"], result["unsupported"]), expected)

    def test_an_empty_target_is_read_and_malformed_reader_output_is_not(self):
        code, result = self.check_one(make_pdf([link(b"()")]))
        self.assertEqual((code, result["status"], result["uri_targets"]), (0, "pass", 1))
        for stdout in (b"Page  Type          URL\ngarbage\n", b"Page  Type          URL\n   x  Annotation    https://example.org/\n", b"nonsense\n",
                       b"Page  Type          URL\n   1  Annotation\n", b"Page  Type          URL\n   1  Bogus         https://example.org/\n"):
            with self.subTest(stdout=stdout):
                fake = subprocess.CompletedProcess(["pdfinfo"], 0, stdout=stdout, stderr=b"")
                with mock.patch.object(tool.subprocess, "run", return_value=fake):
                    self.assertIsNone(tool.second_reader(minimal_pdf([b"https://example.org/"])))
        for stdout, urls in ((b"Page  Type          URL\n   1  Annotation    \n", [b""]),
                             (b"Page  Type          URL\n   1  Widget        https://example.org/\n", [b"https://example.org/"])):
            with self.subTest(stdout=stdout):
                fake = subprocess.CompletedProcess(["pdfinfo"], 0, stdout=stdout, stderr=b"")
                with mock.patch.object(tool.subprocess, "run", return_value=fake):
                    self.assertEqual(tool.second_reader(minimal_pdf([b"https://example.org/"])), urls)

    def test_cross_reference_subsections_may_not_overlap(self):
        good = make_pdf([link(b"(https://example.org/)")])
        start, end = good.index(b"xref\n") + 5, good.index(b"trailer\n")
        lines = good[start:end].split(b"\n")
        size, entries = int(lines[0].split()[1]), lines[1:int(lines[0].split()[1]) + 1]
        tables = {"split": (b"0 1\n" + entries[0] + b"\n1 %d\n" % (size - 1) + b"\n".join(entries[1:]) + b"\n", (0, "pass", [])),
                  "repeated in-use entry": (b"0 1\n" + entries[0] + b"\n1 1\n0000000000 00000 n \n1 %d\n" % (size - 1) + b"\n".join(entries[1:]) + b"\n",
                                            (1, "incomplete", ["a cross-reference table that disagrees with the objects"])),
                  "in-use then free": (b"0 %d\n" % size + b"\n".join(entries) + b"\n1 1\n0000000000 65535 f \n",
                                       (1, "incomplete", ["a cross-reference table that disagrees with the objects"]))}
        for name, (table, expected) in tables.items():
            with self.subTest(case=name):
                code, result = self.check_one(good[:start] + table + good[end:])
                self.assertEqual((code, result["status"], result["unsupported"]), expected)

    def test_the_end_marker_must_be_the_last_line(self):
        good = make_pdf([link(b"(https://example.org/)")])
        self.assertTrue(good.endswith(b"\n%%EOF\n"))
        code, result = self.check_one(good[:-6] + b"% extra text %%EOF\n")
        self.assertEqual((code, result["status"], result["unsupported"]), (1, "incomplete", ["no end-of-file marker"]))
        code, result = self.check_one(good + b"\r\n")
        self.assertEqual((code, result["status"]), (0, "pass"))

    def test_stream_data_is_skipped_by_its_length(self):
        hidden = b"endstream\n9 0 obj << /S /URI /URI (https://example.org/) >> endobj"
        decoy = b"<< /Length %d >>\nstream\n" % len(hidden) + hidden + b"\nendstream"
        code, result = self.check_one(make_pdf([link(b"(file:///tmp/A.pdf)")], [decoy]))
        self.assertEqual((code, result["status"], result["local_targets"]), (1, "fail", 1))
        text = b"BT (endstream) Tj ET"
        ordinary = b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream"
        code, result = self.check_one(make_pdf([link(b"(https://example.org/)")], [ordinary]))
        self.assertEqual((code, result["status"]), (0, "pass"))

    def test_indirect_action_and_type_names_are_resolved(self):
        for action in (b"Launch", b"GoToR", b"SubmitForm"):
            with self.subTest(action=action):
                annot = b"<< /Type /Annot /Subtype /Link /Rect [72 600 300 612] /A << /S 8 0 R /F (notes.pdf) /D [0 /Fit] >> >>"
                code, result = self.check_one(make_pdf([link(b"(https://example.org/)"), annot], [b"/" + action]))
                self.assertEqual((code, result["status"], result["file_opening_actions"]), (1, "fail", [action.decode()]))
        stream = b"<< /Type 7 0 R /N 0 /First 0 /Length 0 >>\nstream\n\nendstream"
        code, result = self.check_one(make_pdf([link(b"(https://example.org/)")], [b"/ObjStm", stream]))
        self.assertEqual((code, result["status"], result["unsupported"]), (1, "incomplete", ["an object or cross-reference stream"]))
        annot = b"<< /Type /Annot /Subtype /Link /A << /S 99 0 R /F (notes.pdf) >> >>"
        code, result = self.check_one(make_pdf([annot]))
        self.assertEqual((code, result["status"], result["unsupported"]),
                         (1, "incomplete", ["an action or type given by a missing or unreadable object"]))

    def test_malformed_files_are_incomplete_without_a_traceback(self):
        good = make_pdf([link(b"(https://example.org/)")])
        xref = good.index(b"xref\n")
        first_entry = good.index(b" 00000 n", xref) - 10
        cases = {
            "empty file": (b"", "not a PDF"),
            "object without endobj": (b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n%%EOF\n", "an object without endobj"),
            "number too long": (b"%PDF-1.4\n" + b"9" * 5000 + b" 0 obj\n<< >>\nendobj\n%%EOF\n", "a number too long"),
            "empty /URI dictionary": (make_pdf([link(b"<< >>")]), "a /URI value that is not a string"),
            "/URI dictionary without /Base": (make_pdf([link(b"<< /Wrong (file:///tmp/A.pdf) >>")]), "a /URI value that is not a string"),
            "no end marker": (good[:good.rindex(b"%%EOF")], "no end-of-file marker"),
            "no cross-reference table": (good[:xref] + b"%%EOF\n", "no classic cross-reference table"),
            "a moved object": (good[:first_entry] + b"%010d" % (int(good[first_entry:first_entry + 10]) + 1) + good[first_entry + 10:],
                               "a cross-reference table that disagrees with the objects"),
            "a second revision": (good + b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\nxref\n1 1\n0000000000 00000 n \n"
                                  b"trailer\n<< /Size 9 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n", "an object defined more than once"),
            "a stream of the wrong length": (good.replace(b"<< /Length 42 >>", b"<< /Length 41 >>"), "a stream whose length does not match"),
            "an indirect stream length": (good.replace(b"<< /Length 42 >>", b"<< /Length 4 0 R >>"), "a stream length that is not a direct integer"),
        }
        self.assertIn(b"<< /Length 42 >>", good)
        for name, (data, reason) in cases.items():
            with self.subTest(case=name):
                self.write("c.pdf", data)
                code, report, stderr = run_check(self.dir, "c.pdf")
                result = report["results"][0]
                self.assertEqual((code, result["status"], result["unsupported"], stderr), (1, "incomplete", [reason], ""))

    def test_the_second_reader_is_required_and_compared(self):
        self.write("c.pdf", minimal_pdf([b"https://example.org/"]))
        done = subprocess.run([sys.executable, "-B", str(TOOL), "check", "c.pdf"], cwd=self.dir, capture_output=True, text=True,
                              timeout=60, env={"PATH": str(self.dir)})
        result = json.loads(done.stdout)["results"][0]
        self.assertEqual((done.returncode, result["status"], result["unsupported"]),
                         (1, "incomplete", ["the independent link reader could not run"]))
        with mock.patch.object(tool, "second_reader", return_value=[b"file:///tmp/A.pdf"]):
            found = tool.analyse(minimal_pdf([b"https://example.org/"]))
        self.assertEqual(found["unsupported"], {"the two link readers disagree"})

    def test_poppler_errors_make_the_check_incomplete(self):
        broken = broken_page(minimal_pdf([b"https://example.org/"]))
        self.write("broken.pdf", broken)
        poppler = subprocess.run(["pdfinfo", "-url", str(self.dir / "broken.pdf")], capture_output=True, text=True, timeout=60)
        self.assertEqual(poppler.returncode, 0)
        self.assertIn("Syntax Error", poppler.stderr)
        code, result = self.check_one(broken)
        self.assertEqual((code, result["status"], result["unsupported"]),
                         (1, "incomplete", ["the independent link reader reported an error"]))
        code, result = self.check_one(minimal_pdf([b"https://example.org/"]))
        self.assertEqual((code, result["status"]), (0, "pass"))

    def test_any_reader_diagnostic_fails_closed(self):
        table = b"Page  Type          URL\n   1  Annotation    https://example.org/\n"
        for stderr, expected in ((b"", set()), (b"\n", set()),
                                 (b"Syntax Warning: an unusual but readable object\n", {"the independent link reader reported an error"}),
                                 (b"Syntax Error: Invalid XRef entry 9\n", {"the independent link reader reported an error"})):
            with self.subTest(stderr=stderr):
                fake = subprocess.CompletedProcess(["pdfinfo"], 0, stdout=table, stderr=stderr)
                with mock.patch.object(tool.subprocess, "run", return_value=fake):
                    found = tool.analyse(minimal_pdf([b"https://example.org/"]))
                self.assertEqual(found["unsupported"], expected)

    def test_vocabulary_is_printed_and_closed(self):
        done = subprocess.run([sys.executable, "-B", str(TOOL), "vocabulary"], capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(done.stdout), {k: list(v) for k, v in tool.VOCABULARY.items()})
        with self.assertRaises(ValueError):
            tool.Unreadable("/synthetic-private/home/id")

    def test_unexpected_errors_print_only_their_class(self):
        src = self.write("in.pdf", minimal_pdf([b"file:///var/home/someone/x/FORECASTS.md"]))
        done = subprocess.run([sys.executable, "-B", str(TOOL), "fix", str(src), str(self.dir / "missing" / "out.pdf"),
                               str(self.dir / "record.json"), "1"], capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stderr.strip()), (2, "pdf_relative_links.py: unexpected error (FileNotFoundError)"))

    def test_web_and_relative_targets_pass(self):
        uris = [b"https://orcid.org/0000-0000-0000-0000", b"https://example.org/home/page", b"mailto:someone@example.com", b"./FORECASTS.md",
                b"https://en.wikipedia.org/wiki/A_(B)", b"RESULTS.json#", b"https://example.org/a/../home/someone/page"]
        self.write("ok.pdf", minimal_pdf(uris))
        code, report, _ = run_check(self.dir, "ok.pdf")
        self.assertEqual((code, report["status"], report["results"][0]["uri_targets"], report["results"][0]["local_targets"]), (0, "pass", 7, 0))

    def test_file_opening_actions_fail(self):
        # Each action carries the keys poppler needs to read it, so the action alone decides the result.
        required = {b"GoToR": b" /D [0 /Fit]", b"Movie": b" /T (clip)", b"Rendition": b" /OP 1 /AN 3 0 R"}
        for action in (b"Launch", b"GoToR", b"GoToE", b"ImportData", b"SubmitForm", b"Thread", b"Sound", b"Movie", b"Rendition", b"La#75nch"):
            with self.subTest(action=action):
                annot = (b"<< /Type /Annot /Subtype /Link /Rect [72 600 300 612] /A << /S /" + action + b" /F (notes.pdf)"
                         + required.get(action, b"") + b" >> >>")
                code, result = self.check_one(make_pdf([link(b"(https://example.org/)"), annot]))
                self.assertEqual((code, result["status"]), (1, "fail"))
                self.assertEqual(result["file_opening_actions"], ["Launch" if action == b"La#75nch" else action.decode()])

    def test_an_annotation_poppler_rejects_is_incomplete_and_its_action_still_listed(self):
        # Poppler reports each of these on stderr: an annotation without /Rect ("Bad bounding box for annotation"), and
        # GoToR, Movie and Rendition actions without the keys it needs to read them.
        for action, annot in ((b"Launch", b"<< /Type /Annot /Subtype /Link /A << /S /Launch /F (notes.pdf) >> >>"),
                              (b"GoToR", b"<< /Type /Annot /Subtype /Link /Rect [72 600 300 612] /A << /S /GoToR /F (notes.pdf) >> >>"),
                              (b"Movie", b"<< /Type /Annot /Subtype /Link /Rect [72 600 300 612] /A << /S /Movie /F (notes.pdf) >> >>"),
                              (b"Rendition", b"<< /Type /Annot /Subtype /Link /Rect [72 600 300 612] /A << /S /Rendition /F (notes.pdf) >> >>")):
            with self.subTest(action=action):
                code, result = self.check_one(make_pdf([link(b"(https://example.org/)"), annot]))
                self.assertEqual((code, result["status"], result["unsupported"], result["file_opening_actions"]),
                                 (1, "incomplete", ["the independent link reader reported an error"], [action.decode()]))

    def test_unreadable_structures_are_incomplete(self):
        cases = {"object stream": (make_pdf([link(b"(https://example.org/)")], [b"<< /Type /ObjStm /N 0 /First 0 /Length 0 >>\nstream\n\nendstream"]),
                                   "an object or cross-reference stream"),
                 "cross-reference stream": (make_pdf([link(b"(https://example.org/)")], [b"<< /Type /XRef /Size 1 /W [1 1 1] /Length 0 >>\nstream\n\nendstream"]),
                                            "an object or cross-reference stream"),
                 "encryption": (make_pdf([link(b"(https://example.org/)")], trailer_extra=b" /Encrypt << /Filter /Standard >>"), "encryption"),
                 "missing object": (make_pdf([link(b"99 0 R")]), "a /URI reference to a missing object"),
                 "not a string": (make_pdf([link(b"42")]), "a /URI value that is not a string"),
                 "malformed hexadecimal": (make_pdf([link(b"<file:x>")]), "malformed syntax")}
        for name, (data, reason) in cases.items():
            with self.subTest(case=name):
                code, result = self.check_one(data)
                self.assertEqual((code, result["status"], result["unsupported"]), (1, "incomplete", [reason]))

    def test_report_names_each_pdf_as_given_and_kinds_without_text(self):
        self.write("clean.pdf", minimal_pdf([b"./A.md"]))
        self.write("local.pdf", minimal_pdf([b"file:///var/home/someone/A.md"]))
        code, report, stderr = run_check(self.dir, "clean.pdf", "local.pdf", "missing.pdf")
        self.assertEqual(code, 1)
        self.assertEqual([(r["pdf"], r["status"]) for r in report["results"]], [("clean.pdf", "pass"), ("local.pdf", "fail"), ("missing.pdf", "incomplete")])
        self.assertEqual(report["results"][1]["local_kinds"], ["file link"])
        self.assertNotIn("someone", json.dumps(report))
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
