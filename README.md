# DJ Library Review

A local review desk for filename duplicate **candidates** in a saved DJ-library inventory. This first milestone is review-only. It helps a DJ inspect a CSV snapshot without treating a similar filename as the same audio, and without deleting or rewriting anything.

The filename rules come from `dj_find_dupes_v2.py`. The desk does not weaken them. Clean, Dirty, and Super Clean stay apart. Intro and Hype Intro stay apart. Instrumental and Acapella stay apart. Radio Edit and Extended stay apart. VIP and Dub stay apart. Named editors, named remixes, BPM transition ranges, and unknown words stay in the identity. A file with no version tag is unlabeled and uncertain, not Original. A trailing `(2)` is only a copy hint and is not stripped. The matcher would rather miss a candidate than collapse different edits.

## Quickstart

Tested runtime: **CPython 3.12.3 on Linux**.

Minimum supported version: **Python 3.11**, based on the standard library used (`zoneinfo`, `list[str]`, `X | None`). Python 3.11 was **not** executed in this workspace. macOS was **not** tested. This is not an installed Mac app, and it does not integrate with Serato.

From the source folder:

```bash
python3 -m djlibreview
```

Then open [http://127.0.0.1:8741](http://127.0.0.1:8741). The process binds **127.0.0.1 only**. `--host 0.0.0.0` is refused. Another port is `--port 8741` (or any other port).

```bash
python3 -m djlibreview --port 8741
```

This milestone uses the Python standard library only. `djlibreview`, `dj_find_dupes_v2.py`, and the tests do not import third-party packages. The repository `requirements.txt` is the existing setup file. It pins audio, tagging, and Serato packages for later work, including `librosa`, `mutagen`, `serato-tools`, and their dependencies. Do not install, import, or execute those packages to run or test this milestone. A virtualenv is not required.

Click **Load synthetic example**. The banner says it is example data, not your library and not a scan of this computer. To review your own snapshot, fill in the dataset label, scope, and snapshot date, then choose a CSV. Scope and snapshot date are required. The desk does not assume today and does not read file dates.

Choosing a CSV sends that file from the browser to the review process on this computer (`127.0.0.1`). That is not a hosted upload. The path text inside the CSV is not opened.

## CSV schema

UTF-8, with or without a BOM. One header row. Paths are absolute POSIX text, such as `/Users/you/Music/...` or `/Volumes/Library/...`. They are not Windows paths, and they are not looked up on disk.

| Column | Required | Meaning |
| --- | --- | --- |
| `path`, or `full_path`, or `filepath`, or `file_path` | One of these, unless you type another name in Path column | Inventory path text |
| `status` | No | Blank or `ok` is accepted. Any other value is skipped and reported. It is not opened. |

Extra columns are ignored. They are not audio metadata. A repeated header is a fatal error. An empty path, a null, or a line break inside a path cell is a skipped row. `..` and paths outside the scope are skipped. Extensions other than the audio set are counted and skipped.

Audio extensions, compared case-insensitively: `.mp3`, `.m4a`, `.wav`, `.flac`, `.aiff`, `.aif`, `.aac`, `.ogg`, `.opus`, `.wma`, `.alac`.

Example, also at `djlibreview/examples/synthetic_inventory.csv`:

```csv
path,status
/synthetic/dj-library/house/Nova - Midnight (Clean Intro).mp3,ok
/synthetic/dj-library/usb/Nova - Midnight [Intro Clean].flac,ok
```

`djlibreview/examples/row_problems.csv` is a second synthetic file with an empty row, a bad status, an out-of-scope path, and untrusted text. Review it with scope `/synthetic/dj-library` and any real snapshot date. `bad_headers.csv` has two path columns and is rejected until you name one.

Limits: 48 MB per upload, 200,000 data rows, 8,192 characters per path cell. The in-memory review is dropped when the process stops. Nothing is written next to your music.

## What the screen means

- **Dataset** is the label you typed.
- **Source file** is the uploaded file's display name, or `synthetic_inventory.csv` for the example.
- **Scope** is the path prefix you typed. It is not a folder the desk scanned.
- **Snapshot date** is the day you said the inventory was saved. Source text: `operator-supplied; not inferred from file mtime`.
- **Snapshot age note** is the gap between that date and the analysis calendar day in `America/New_York`. It is not file age and does not mean the files still exist.
- **Analysis time** is when this process read the CSV text. It is not a library scan.
- **Engine** `dj_find_dupes_v2` **2.1.0** is the filename rule set. **App** `0.1.0` is this desk.
- **Mode** is saved inventory text only.
- **Example data** is yes only when the bytes match the packaged sample, or when you use Load synthetic example.
- **Byte evidence** is Not checked. **Audio evidence** is Not checked. **Current files** are Not verified. **Deletion** is Not authorized.

Groups are labeled **filename candidates**, never confirmed audio duplicates. Side-by-side filenames and paths are the imported strings. They are not links. A matching name, and any later byte hash outside this desk, does not authorize deletion, moving, renaming, or retagging.

## Report export

**Download review report** builds a zip in memory and the browser saves it. The suggested name is `filename-candidate-review.zip`. The desk does not choose a folder inside the music scope and does not silently overwrite a file on disk.

| File | Spreadsheet |
| --- | --- |
| `README_FIRST.txt` | Plain-text limits |
| `filename_candidates.csv` | Yes. Formula-like cells get a leading apostrophe. |
| `row_problems.csv` | Yes, same guard |
| `review_notes.txt` | Plain text |
| `summary.json` | No. Exact structured summary. |
| `errors.json`, `paths.json` | No. Exact untrusted text. Do not open these in a spreadsheet. |

A leading apostrophe is a spreadsheet guard, not part of the filename.

## Architecture

```text
Browser on 127.0.0.1
    |  CSV text, same-origin POST with a CSRF token
    v
djlibreview/server.py     loopback HTTP, size cap, host and origin checks
    |
    v
djlibreview/review.py     provenance, explanations, in-memory zip
    |
    v
dj_find_dupes_v2.py       filename grouping only
```

`server.py` and `review.py` do not call `enumerate_local`, `hash_selected`, or `safe_local_path`. Those helpers remain in the engine for the original tests. They are Linux-only: if `/proc/self/mountinfo` is missing they stop and say to use a saved CSV. This desk does not make them work on a Mac.

The page, CSS, and script are served by the process. There is no CDN, no telemetry, and no cloud library. The cookie is `HttpOnly` and `SameSite=Strict`. It is not marked `Secure` because the desk is plain HTTP on loopback; a Secure cookie would not be stored. Cross-origin posts are rejected. User text is inserted with DOM text nodes, not HTML.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

`.github/workflows/stdlib-tests.yml` runs that command on Ubuntu with Python 3.12. It does not install `requirements.txt`. There is no deploy step and no auto-merge.

Import verification in this repository reported `Ran 62 tests in 6.908s` and `OK` on CPython 3.12.3 on Linux. That includes the original 35 engine tests, unchanged in `tests/test_dj_find_dupes_v2.py`, plus regressions for editors, remixes, BPM transitions, and the packaged example. The same run printed `100k synthetic analyze seconds: 2.264`.

The source draft that produced these files reported `Ran 62 tests in 6.697s` and `OK` on CPython 3.12.3, with a 100,000-path synthetic analyze at 2.077 seconds and an earlier analyze in that draft at 2.274 seconds. Python 3.11 was not executed. macOS was not tested. These timings are observations from the test process, not a performance guarantee and not a scan of a real library.

The tests use synthetic temp files and in-memory CSV text. They check that imported paths are not opened, that the app does not walk directories or hash audio, that malformed and formula-like text is contained, and that the HTTP desk stays on loopback.

## Screenshots

Taken in headless Chrome against the local process, using the synthetic example and `row_problems.csv`:

- `docs/screenshots/example-desktop.png`
- `docs/screenshots/example-group.png`
- `docs/screenshots/example-mobile.png`
- `docs/screenshots/example-mobile-group.png`
- `docs/screenshots/filter-beyonce.png`
- `docs/screenshots/row-problems.png`

## Source handoff

```bash
python3 scripts/make_source_zip.py /path/to/dj-library-review-source-only.zip
```

The zip contains source, tests, the synthetic CSVs, screenshots, and this README. It skips `.git`, virtualenvs, caches, `uploads/`, and other zip files. It does not contain a personal library.

## Deferred

Not in this milestone: folder scans, byte hashes, audio fingerprints, tags, keep/delete, Serato crates or databases, repair, Mac packaging, accounts, and saved sessions after the process stops. The pinned packages in `requirements.txt` are unused here and are not a way to turn those features on. A hash of file bytes, if it is ever offered, would still not authorize deletion.
