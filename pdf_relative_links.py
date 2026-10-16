#!/usr/bin/env python3
"""pdf_relative_links.py - find and rewrite local file links in a rendered PDF, so a deposit PDF carries no local path.

A browser that prints from a local HTML file (Chromium, through Puppeteer or a similar driver) turns a relative link such as
NOTES.md into a /URI action holding an absolute file:// path, which names the home folder. A render pipeline can run check
and, when it fails, fix; a publication gate can run check on every shipped PDF. Three modes:

  check PDF [PDF ...]
      Reads each PDF as a strict validator for the one kind of file it accepts: a single revision with a classic
      cross-reference table that agrees with the objects found (every in-use entry points at the object it names, and every
      object has an entry), each object closed by endobj, each stream read by its direct /Length and closed by endstream, a
      %PDF- header and a %%EOF marker. Tokens: comments, names with #xx escapes, literal strings with nested parentheses and
      every escape, hexadecimal strings, numbers of at most 32 characters, indirect references. A stream length may carry a
      plus sign; the cross-reference table may not list an object twice; the %%EOF marker must be the file's last line. It lists every link target
      in a /URI entry, following one indirect reference to a string; the catalogue's /URI dictionary gives the document's
      base, and a /URI dictionary anywhere else cannot be read. An /S or /Type given by an indirect reference is resolved.
      A second reader, poppler's `pdfinfo -url`, lists the link annotations' targets as a conforming reader resolves them.
      A PDF fails when a target is local (a file: link in any letter case; a Windows drive or UNC path; or, for a relative
      target with no document base, an absolute path, a path starting with ~, or a path that climbs with .. into a home or
      Users folder, read after backslashes become slashes and empty and . segments are dropped; a relative target under a
      document base is local exactly when the base is) or an action can open another file or resource
      (Launch, GoToR, GoToE, ImportData, SubmitForm, Thread, Sound, Movie, Rendition). Queries and fragments are not
      classified. A PDF is incomplete, which also fails, when it holds anything the validator does not accept, when the
      second reader cannot run or writes anything to stderr (poppler reports syntax errors there, such as a page it cannot
      read, while still exiting 0), or when the second reader finds more local targets than the first. Prints one JSON
      report that names each PDF as given on the command line and reports reasons and kinds only in the fixed words of
      VOCABULARY. Exit 0 only when every PDF passes.

  fix IN.pdf OUT.pdf RECORD.json EXPECTED_COUNT
      Rewrites each file:// target held in a direct literal string without escapes as a relative reference of the same byte
      length: "./" repeated, then the last segment of the link's path as written (still percent-encoded), then its query and
      fragment unchanged. When the padding is odd, an empty fragment "#" is added if the link has none; a link that already
      has a fragment and odd padding is refused. No object moves, so the cross-reference table stays valid. Refuses to
      overwrite OUT.pdf or RECORD.json; refuses an incomplete PDF, a file-opening action, a PDF that sets a document base and
      any local target it cannot rewrite; and fails unless exactly EXPECTED_COUNT links are rewritten, every replacement keeps
      its length and its parentheses balanced, no byte outside the rewritten strings changes and the result checks clean.

  vocabulary
      Prints the fixed words that check may report (VOCABULARY), as JSON.

The report names PDFs as the caller gave them and the rewrite record names them by file name; review both before
sharing them. An unexpected error prints only its class, never its message or a path, and exits 2.
Limits: PDFs with object or cross-reference streams, encryption, incremental updates or indirect stream lengths are
reported incomplete; JavaScript is not inspected."""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%"
ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f", ord("("): b"(", ord(")"): b")",
           ord("\\"): b"\\"}
NUMBER = re.compile(rb"[+-]?(\d+\.?\d*|\.\d+)")
INTEGER = re.compile(rb"\d+")
LENGTH = re.compile(rb"\+?\d+")
MAX_NUMBER = 32
MAX_DEPTH = 100
MAX_CHAIN = 8
SCHEME = re.compile(rb"[A-Za-z][A-Za-z0-9+.-]*:")
DRIVE = re.compile(rb"[A-Za-z]:[\\/]")
WEB_SCHEMES = (b"http:", b"https:")
FILE_ACTIONS = ("GoToE", "GoToR", "ImportData", "Launch", "Movie", "Rendition", "Sound", "SubmitForm", "Thread")
STREAM_TYPES = ("ObjStm", "XRef")
READER_TYPES = ("Annotation", "Widget")
UNSUPPORTED = (
    "not a PDF", "no end-of-file marker", "more than one revision", "no classic cross-reference table",
    "a cross-reference table that disagrees with the objects", "an object defined more than once", "an object without endobj",
    "a stream length that is not a direct integer", "a stream whose length does not match", "malformed syntax", "a number too long",
    "nesting too deep", "an object or cross-reference stream", "encryption", "a /URI value that is not a string",
    "a /URI reference to a missing object", "an action or type given by a missing or unreadable object",
    "the file could not be read", "the independent link reader could not run", "the two link readers disagree",
    "the independent link reader reported an error")
LOCAL_KINDS = ("absolute path", "drive path", "file link", "home path", "UNC path")
VOCABULARY = {"unsupported": UNSUPPORTED, "local_kinds": LOCAL_KINDS, "file_opening_actions": FILE_ACTIONS}


class Unreadable(Exception):
    """A structure or syntax this tool does not read; its argument is one of UNSUPPORTED."""

    def __init__(self, reason):
        if reason not in UNSUPPORTED:
            raise ValueError("unknown reason")
        super().__init__(reason)


def literal_string(data, i):
    """Decode the literal string whose "(" is at i; return (value, raw content span, index after the closing ")")."""
    out, depth, j = bytearray(), 1, i + 1
    while j < len(data):
        c = data[j]
        if c == 0x5C:
            j += 1
            if j >= len(data):
                break
            e = data[j]
            if e in ESCAPES:
                out += ESCAPES[e]
                j += 1
            elif 0x30 <= e <= 0x37:
                k = j
                while k < len(data) and k < j + 3 and 0x30 <= data[k] <= 0x37:
                    k += 1
                out.append(int(data[j:k], 8) & 0xFF)
                j = k
            elif e == 0x0D:
                j += 2 if data[j + 1:j + 2] == b"\n" else 1
            elif e == 0x0A:
                j += 1
            else:
                out.append(e)
                j += 1
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out), (i + 1, j), j + 1
        out.append(c)
        j += 1
    raise Unreadable("malformed syntax")


def stream_length(out):
    """The direct /Length of the dictionary that ends the token list, which must precede the stream keyword."""
    if not out or out[-1][0] != "dict_close":
        raise Unreadable("malformed syntax")
    depth, k = 0, len(out) - 1
    while k >= 0:
        depth += {"dict_close": 1, "dict_open": -1}.get(out[k][0], 0)
        if depth == 0:
            break
        k -= 1
    if k < 0:
        raise Unreadable("malformed syntax")
    (kind, pairs), _ = parse(out, k)
    value = dict(pairs).get("Length")
    if value is None or value[0] != "number" or not LENGTH.fullmatch(value[1].encode("ascii")):
        raise Unreadable("a stream length that is not a direct integer")
    return int(value[1])


def tokens(data):
    """Tokens as (kind, value, raw content span or None, byte offset); kinds: name, string, hex, number, keyword, stream,
    dict_open, dict_close, array_open, array_close. A stream's data is skipped by its direct /Length and must be followed by
    an end-of-line marker and endstream; the stream is one token."""
    out, i, n = [], 0, len(data)
    while i < n:
        c, at = data[i], i
        if c in WHITESPACE:
            i += 1
        elif c == 0x25:
            while i < n and data[i] not in b"\r\n":
                i += 1
        elif c == 0x2F:
            j = i + 1
            while j < n and data[j] not in WHITESPACE and data[j] not in DELIMITERS:
                j += 1
            name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), data[i + 1:j])
            out.append(("name", name.decode("latin-1"), None, at))
            i = j
        elif c == 0x28:
            value, span, i = literal_string(data, i)
            out.append(("string", value, span, at))
        elif data[i:i + 2] == b"<<":
            out.append(("dict_open", None, None, at))
            i += 2
        elif data[i:i + 2] == b">>":
            out.append(("dict_close", None, None, at))
            i += 2
        elif c == 0x3C:
            j = data.find(b">", i)
            digits = re.sub(rb"\s", b"", data[i + 1:j]) if j > 0 else b"?"
            if not re.fullmatch(rb"[0-9A-Fa-f]*", digits):
                raise Unreadable("malformed syntax")
            out.append(("hex", bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii")), None, at))
            i = j + 1
        elif c == 0x5B:
            out.append(("array_open", None, None, at))
            i += 1
        elif c == 0x5D:
            out.append(("array_close", None, None, at))
            i += 1
        elif c in b"{})>":
            raise Unreadable("malformed syntax")
        else:
            j = i
            while j < n and data[j] not in WHITESPACE and data[j] not in DELIMITERS:
                j += 1
            word = data[i:j]
            if word == b"stream":
                length = stream_length(out)
                if data[j:j + 2] == b"\r\n":
                    start = j + 2
                elif data[j:j + 1] == b"\n":
                    start = j + 1
                else:
                    raise Unreadable("malformed syntax")
                end = start + length
                after = end + (2 if data[end:end + 2] == b"\r\n" else 1 if data[end:end + 1] in (b"\n", b"\r") else 0)
                if end > n or data[after:after + 9] != b"endstream":
                    raise Unreadable("a stream whose length does not match")
                out.append(("stream", None, (start, end), at))
                j = after + 9
            elif NUMBER.fullmatch(word):
                if len(word) > MAX_NUMBER:
                    raise Unreadable("a number too long")
                out.append(("number", word.decode("ascii"), None, at))
            else:
                out.append(("keyword", word.decode("latin-1"), None, at))
            i = j
    return out


def is_integer(tok):
    return tok[0] == "number" and tok[1].isdigit()


def is_reference(toks, k):
    return k + 2 < len(toks) and is_integer(toks[k]) and is_integer(toks[k + 1]) and toks[k + 2][:2] == ("keyword", "R")


def parse(toks, k, depth=0):
    """One value from the token list at k, and the next k. A dictionary becomes ("dict", [(key, value), ...])."""
    if depth > MAX_DEPTH:
        raise Unreadable("nesting too deep")
    if k >= len(toks):
        raise Unreadable("malformed syntax")
    kind, value, span, _ = toks[k]
    if kind == "dict_open":
        pairs, k = [], k + 1
        while True:
            if k >= len(toks):
                raise Unreadable("malformed syntax")
            if toks[k][0] == "dict_close":
                return ("dict", pairs), k + 1
            if toks[k][0] != "name":
                raise Unreadable("malformed syntax")
            key = toks[k][1]
            item, k = parse(toks, k + 1, depth + 1)
            pairs.append((key, item))
    if kind == "array_open":
        items, k = [], k + 1
        while True:
            if k >= len(toks):
                raise Unreadable("malformed syntax")
            if toks[k][0] == "array_close":
                return ("array", items), k + 1
            item, k = parse(toks, k, depth + 1)
            items.append(item)
    if kind in ("dict_close", "array_close", "stream"):
        raise Unreadable("malformed syntax")
    if is_reference(toks, k):
        return ("ref", (int(value), int(toks[k + 1][1]))), k + 3
    if kind == "string":
        return ("string", (value, span)), k + 1
    return (kind, value), k + 1


def xref_table(toks, k):
    """The entries of a classic cross-reference table that starts after the xref keyword at k: {(number, generation): offset}
    for in-use entries, and the next k."""
    entries, seen, k = {}, set(), k + 1
    while k + 1 < len(toks) and is_integer(toks[k]) and is_integer(toks[k + 1]):
        if k + 2 < len(toks) and toks[k + 2][:2] in (("keyword", "n"), ("keyword", "f")):
            raise Unreadable("a cross-reference table that disagrees with the objects")
        first, count, k = int(toks[k][1]), int(toks[k + 1][1]), k + 2
        if count > (len(toks) - k) // 3:
            raise Unreadable("a cross-reference table that disagrees with the objects")
        for number in range(first, first + count):
            if not (is_integer(toks[k]) and is_integer(toks[k + 1]) and toks[k + 2][:2] in (("keyword", "n"), ("keyword", "f"))):
                raise Unreadable("a cross-reference table that disagrees with the objects")
            if number in seen:
                raise Unreadable("a cross-reference table that disagrees with the objects")
            seen.add(number)
            if toks[k + 2][1] == "n":
                entries[(number, int(toks[k + 1][1]))] = int(toks[k][1])
            k += 3
    return entries, k


def read_pdf(data):
    """The PDF's objects by (number, generation) and its trailer, after the structural checks in the docstring."""
    if not data.startswith(b"%PDF-"):
        raise Unreadable("not a PDF")
    tail = data.rstrip(b"\r\n\t\x0c\x00 ")
    if not tail.endswith(b"%%EOF") or tail[-6:-5] not in (b"\n", b"\r"):
        raise Unreadable("no end-of-file marker")
    toks = tokens(data)
    objects, offsets, entries, trailer, startxref, xref_at, k = {}, {}, None, None, None, None, 0
    while k < len(toks):
        kind, value, _, at = toks[k]
        if is_integer(toks[k]) and k + 2 < len(toks) and is_integer(toks[k + 1]) and toks[k + 2][:2] == ("keyword", "obj"):
            key = (int(value), int(toks[k + 1][1]))
            if key in objects:
                raise Unreadable("an object defined more than once")
            item, k = parse(toks, k + 3)
            if k < len(toks) and toks[k][0] == "stream":
                if item[0] != "dict":
                    raise Unreadable("malformed syntax")
                k += 1
            if k >= len(toks) or toks[k][:2] != ("keyword", "endobj"):
                raise Unreadable("an object without endobj")
            objects[key], offsets[key], k = item, at, k + 1
        elif toks[k][:2] == ("keyword", "xref"):
            if entries is not None:
                raise Unreadable("more than one revision")
            xref_at = at
            entries, k = xref_table(toks, k)
        elif toks[k][:2] == ("keyword", "trailer"):
            if trailer is not None:
                raise Unreadable("more than one revision")
            trailer, k = parse(toks, k + 1)
            if trailer[0] != "dict":
                raise Unreadable("malformed syntax")
        elif toks[k][:2] == ("keyword", "startxref"):
            if startxref is not None:
                raise Unreadable("more than one revision")
            if k + 1 >= len(toks) or not is_integer(toks[k + 1]):
                raise Unreadable("malformed syntax")
            startxref, k = int(toks[k + 1][1]), k + 2
        else:
            raise Unreadable("malformed syntax")
    if entries is None or trailer is None or startxref is None:
        raise Unreadable("no classic cross-reference table")
    if startxref != xref_at or entries != offsets:
        raise Unreadable("a cross-reference table that disagrees with the objects")
    if "Encrypt" in dict(trailer[1]):
        raise Unreadable("encryption")
    return objects, trailer


def resolve(item, objects):
    """Follow indirect references to a direct value, or None if an object is missing or the chain is too long or cyclic."""
    seen = set()
    while item is not None and item[0] == "ref":
        if item[1] in seen or len(seen) >= MAX_CHAIN:
            return None
        seen.add(item[1])
        item = objects.get(item[1])
    return item


def _name(item, objects):
    """The name an /S or /Type value gives, directly or through references; "" for none; None if it cannot be read."""
    if item is None:
        return ""
    target = resolve(item, objects)
    if target is None or target[0] != "name":
        return None
    return target[1]


def uri_value(item, objects, found):
    """Record the link target that a /URI entry's value holds, following indirect references to a string."""
    target = resolve(item, objects)
    if target is None:
        found["unsupported"].add("a /URI reference to a missing object")
    elif target[0] == "string":
        text, span = target[1]
        found["targets"].append({"value": text, "span": span, "direct": item[0] == "string", "form": "literal"})
    elif target[0] == "hex":
        found["targets"].append({"value": target[1], "span": None, "direct": False, "form": "hex"})
    else:
        found["unsupported"].add("a /URI value that is not a string")


def walk(item, objects, found, catalogue=False, depth=0):
    """Visit every dictionary and array of an object: /URI entries, action types and stream types."""
    if depth > MAX_DEPTH:
        raise Unreadable("nesting too deep")
    kind, value = item
    if kind == "dict":
        for key, sub in value:
            if key in ("S", "Type"):
                name = _name(sub, objects)
                if name is None:
                    found["unsupported"].add("an action or type given by a missing or unreadable object")
                elif key == "S" and name in FILE_ACTIONS:
                    found["actions"].add(name)
                elif key == "Type" and name in STREAM_TYPES:
                    found["unsupported"].add("an object or cross-reference stream")
            elif key == "URI" and catalogue:
                base_dict = resolve(sub, objects)
                if base_dict is None or base_dict[0] != "dict":
                    found["unsupported"].add("a /URI value that is not a string")
                    continue
                base = dict(base_dict[1]).get("Base")
                if base is not None:
                    before = len(found["targets"])
                    uri_value(base, objects, found)
                    if len(found["targets"]) > before:
                        found["base"] = found["targets"][-1]["value"]
                continue
            elif key == "URI":
                uri_value(sub, objects, found)
            walk(sub, objects, found, depth=depth + 1)
    elif kind == "array":
        for sub in value:
            walk(sub, objects, found, depth=depth + 1)


def local_kind(value, base=None):
    """The kind of local target a link value is, or None. A relative value resolves against the document's base, so it is
    local exactly when the base is; with no base, its path alone is classified, after backslashes become slashes and empty
    and . segments are dropped (.. segments are kept)."""
    low = value.lower()
    if low.startswith(b"file:"):
        return "file link"
    if DRIVE.match(value):
        return "drive path"
    if value.startswith(b"\\\\"):
        return "UNC path"
    if SCHEME.match(value):
        return None
    if base is not None:
        return local_kind(base)
    path = re.split(rb"[?#]", value, maxsplit=1)[0].replace(b"\\", b"/")
    if path.startswith(b"/"):
        return "absolute path"
    segments = [segment for segment in path.split(b"/") if segment not in (b"", b".")]
    if segments and segments[0].startswith(b"~"):
        return "home path"
    for i, segment in enumerate(segments):
        if segment == b"..":
            rest = segments[i + 1:]
            if rest[:2] == [b"var", b"home"]:
                rest = rest[1:]
            if len(rest) >= 2 and rest[0] in (b"home", b"Users"):
                return "home path"
    return None


def second_reader(data):
    """Link annotation targets as poppler's pdfinfo -url resolves them (the catalogue base applied), or None if it cannot run
    or prints anything but its table. Each row must reach the column where the header's URL starts and name a page and a type
    pdfinfo prints (Annotation or Widget); the URL is read from that column, so a padded empty URL stays. Raises Unreadable
    when pdfinfo writes anything to stderr: poppler reports syntax errors there, such as a page it cannot read, while still
    exiting 0 and printing its table."""
    try:
        done = subprocess.run(["pdfinfo", "-url", "-"], input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.stderr.strip():
        raise Unreadable("the independent link reader reported an error")
    lines = done.stdout.decode("latin-1").splitlines()
    if done.returncode != 0 or not lines or lines[0].split()[:3] != ["Page", "Type", "URL"]:
        return None
    column, urls = lines[0].index("URL"), []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line[:column].split()
        if len(line) < column or len(fields) != 2 or not fields[0].isdigit() or fields[1] not in READER_TYPES:
            return None
        urls.append(line[column:].encode("latin-1"))
    return urls


def analyse(data):
    """Link targets, file-opening actions, the document base and unreadable structures of one PDF's bytes."""
    found = {"targets": [], "actions": set(), "unsupported": set(), "base": None, "local": []}
    try:
        objects, trailer = read_pdf(data)
        root = dict(trailer[1]).get("Root")
        catalogue_key = root[1] if root is not None and root[0] == "ref" else None
        for key, item in objects.items():
            walk(item, objects, found, catalogue=key == catalogue_key)
    except Unreadable as error:
        found["unsupported"].add(str(error))
    except RecursionError:
        found["unsupported"].add("nesting too deep")
    if found["unsupported"]:
        return found
    base = found["base"]
    found["local"] = [k for k in (local_kind(t["value"], None if t["value"] == base else base) for t in found["targets"]) if k]
    try:
        urls = second_reader(data)
    except Unreadable as error:
        found["unsupported"].add(str(error))
        return found
    if urls is None:
        found["unsupported"].add("the independent link reader could not run")
    elif sum(1 for u in urls if local_kind(u)) > len(found["local"]):
        found["unsupported"].add("the two link readers disagree")
    return found


def check(paths):
    report = []
    for path in paths:
        try:
            found = analyse(Path(path).read_bytes())
        except OSError:
            found = {"targets": [], "actions": set(), "unsupported": {"the file could not be read"}, "local": []}
        except Exception:
            found = {"targets": [], "actions": set(), "unsupported": {"malformed syntax"}, "local": []}
        kinds = found["local"]
        status = "incomplete" if found["unsupported"] else "fail" if kinds or found["actions"] else "pass"
        report.append({"pdf": str(path), "status": status, "uri_targets": len(found["targets"]), "local_targets": len(kinds),
                       "local_kinds": sorted(set(kinds)), "file_opening_actions": sorted(found["actions"]),
                       "unsupported": sorted(found["unsupported"])})
    status = "pass" if all(r["status"] == "pass" for r in report) else "fail"
    print(json.dumps({"check": "pdf link targets", "status": status, "results": report}, indent=1))
    return 0 if status == "pass" else 1


def replacement_for(old):
    """A same-length relative reference to the last segment of a file link's path, with its query and fragment unchanged."""
    rest = old[old.index(b":") + 1:]
    hash_at = rest.find(b"#")
    fragment = rest[hash_at:] if hash_at >= 0 else b""
    rest = rest[:hash_at] if hash_at >= 0 else rest
    query_at = rest.find(b"?")
    query = rest[query_at:] if query_at >= 0 else b""
    path = rest[:query_at] if query_at >= 0 else rest
    if path.startswith(b"//"):
        slash = path.find(b"/", 2)
        path = path[slash:] if slash >= 0 else b""
    segment = path.rsplit(b"/", 1)[-1]
    if not segment:
        raise ValueError("the link's path ends in a slash, so there is no file name to keep")
    tail = segment + query + fragment
    pad = len(old) - len(tail)
    if pad % 2:
        if fragment:
            raise ValueError("odd padding and an existing fragment: a same-length rewrite would change the fragment")
        new = b"./" * ((pad - 1) // 2) + tail + b"#"
    else:
        new = b"./" * (pad // 2) + tail
    depth = 0
    for c in new:
        depth += (c == 0x28) - (c == 0x29)
        if depth < 0:
            break
    if len(new) != len(old) or depth != 0:
        raise ValueError("the rewritten link would change length or leave unbalanced parentheses")
    return new, segment.decode("latin-1")


def fix(src, out, record_path, expected):
    if out.exists() or record_path.exists():
        raise SystemExit("refusing to overwrite the output or record")
    data = src.read_bytes()
    found = analyse(data)
    if found["unsupported"]:
        raise SystemExit("the PDF holds a structure this tool cannot read: " + "; ".join(sorted(found["unsupported"])))
    if found["actions"]:
        raise SystemExit("the PDF holds an action that opens another file: " + ", ".join(sorted(found["actions"])))
    if found["base"] is not None:
        raise SystemExit("the PDF sets a document base, so a relative rewrite would resolve against it")
    local = [t for t in found["targets"] if local_kind(t["value"])]
    rewritable = [t for t in local if t["direct"] and t["form"] == "literal" and t["value"].lower().startswith(b"file://")
                  and data[t["span"][0]:t["span"][1]] == t["value"]]
    if len(rewritable) != len(local):
        raise SystemExit("a local target cannot be rewritten: only file:// links in direct literal strings without escapes can be")
    if len(rewritable) != expected:
        raise SystemExit(f"expected {expected} local file links, found {len(rewritable)}")
    new, spans, names = bytearray(data), [], []
    for target in rewritable:
        start, end = target["span"]
        try:
            replacement, name = replacement_for(target["value"])
        except ValueError as error:
            raise SystemExit(str(error)) from None
        new[start:end] = replacement
        spans.append((start, end))
        names.append(name)
    changed = [i for i in range(len(data)) if data[i] != new[i]]
    if any(not any(s <= i < e for s, e in spans) for i in changed):
        raise SystemExit("a byte outside the rewritten links changed")
    after = analyse(bytes(new))
    if after["unsupported"] or after["actions"] or len(after["targets"]) != len(found["targets"]) or after["local"]:
        raise SystemExit("the rewritten PDF does not check clean")
    with out.open("xb") as handle:
        handle.write(bytes(new))
    record = {"purpose": "Local file links in a rendered PDF rewritten as same-length relative links.", "input": src.name,
              "input_sha256": hashlib.sha256(data).hexdigest(), "output": out.name,
              "output_sha256": hashlib.sha256(bytes(new)).hexdigest(), "bytes": len(data),
              "links_rewritten": names, "byte_spans": spans, "bytes_changed": len(changed)}
    with record_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=1) + "\n")
    print(json.dumps({k: record[k] for k in ("input_sha256", "output_sha256", "links_rewritten", "bytes_changed")}))
    return 0


def main(argv):
    if argv == ["vocabulary"]:
        print(json.dumps(VOCABULARY, indent=1))
        return 0
    if len(argv) >= 2 and argv[0] == "check":
        return check(argv[1:])
    if len(argv) == 5 and argv[0] == "fix" and argv[4].isdigit():
        return fix(Path(argv[1]), Path(argv[2]), Path(argv[3]), int(argv[4]))
    raise SystemExit(__doc__)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as error:
        print(f"pdf_relative_links.py: unexpected error ({type(error).__name__})", file=sys.stderr)
        sys.exit(2)
