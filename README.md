## iCloudPhotoScraper

Export your entire iCloud Photos library to a local drive with a clean year/month folder structure, EXIF-aware dating, Live Photo pairing, and a resumable state so you can safely stop and restart without re-downloading.

### Features
- **Resumable exports**: Uses a lightweight SQLite manifest to skip already-processed assets across runs.
- **Bootstrap existing exports**: Optionally scan an existing destination to populate the state without re-downloading.
- **Clean folder layout**: Files are saved under `YYYY/MM Month/filename`.
- **Preserve dates**: Prefers EXIF DateTimeOriginal; falls back to iCloud metadata, then file mtime.
- **Live Photo pairing**: Ensures paired videos sit next to stills as `<photo_stem>_LIVE.mov` (or original video extension).
- **Atomic writes**: Downloads to a temp file and renames into place to avoid partial files.
- **Dry-run mode**: Preview exactly where files would land without downloading.
- **Throttle and retry**: Tune network behavior and handle transient errors.

### Requirements
- **Python**: 3.9+ recommended
- **Packages**:
  - `pyicloud` (iCloud API access)
  - `Pillow` (EXIF reading)
  - `pillow-heif` (optional; enables EXIF reading for HEIC/HEIF)

Install dependencies:

```bash
pip install pyicloud Pillow pillow-heif
```

If you don’t need HEIC EXIF support, you can skip `pillow-heif`.

### Usage
Run from the project directory:

```bash
python photoScraper.py -o D:/iCloud
```

You’ll be prompted for your Apple ID and password if not provided via flags. Two‑factor (2FA/2SA) flow is supported interactively.

#### Common options
- `--username, -u` Apple ID email.
- `--password, -p` Apple ID password. For security, prefer entering at the prompt over passing on the CLI.
- `--output, -o` Destination folder, e.g. `D:/iCloud`.
- `--max` Limit number of assets for testing.
- `--dry-run` Show where files would be saved without downloading.
- `--throttle` Seconds to sleep between downloads (default `0.2`).
- `--retries` Retry attempts per asset (default `3`).
- `--state` Path to the resume database. Defaults to `<output>/.state/icloud.sqlite`.
- `--bootstrap-state` Scan `--output` and populate the state DB from existing files (no downloads), then exit.

#### Examples
- Basic export to an external drive:

```bash
python photoScraper.py -o D:/iCloud
```

- Quick preview without downloading, limited to 25 assets:

```bash
python photoScraper.py -o D:/iCloud --dry-run --max 25
```

- Use a custom resume database location:

```bash
python photoScraper.py -o D:/iCloud --state D:/iCloud/.state/icloud.sqlite
```

- Bootstrap state from an already-populated folder (then exit):

```bash
python photoScraper.py -o D:/iCloud --bootstrap-state
```

### Output layout
Files are organized by year and month, for example:

```text
D:/iCloud/
  2021/
    01 January/
      IMG_0001.HEIC
      IMG_0001_LIVE.MOV
    02 February/
      DSC_1234.JPG
  2022/
    08 August/
      Vacation.png
```

Live Photos will have their video companion renamed to sit next to the still as `<still_name>_LIVE<ext>`.

### Resuming and bootstrapping
- The tool records processed assets in a SQLite DB (the “state”) so repeated runs skip already-downloaded items.
- If you already have files in your destination, run with `--bootstrap-state` once to populate the state without downloads. The bootstrap process:
  - Matches by asset ID (legacy exports) or filename (case-insensitive, with handling for `_LIVE`).
  - Disambiguates same-name files by capture date proximity where possible.

### 2FA/2SA authentication
When iCloud requires verification:
- You’ll be prompted for a code sent to your devices; enter it when asked.
- The session is trusted when possible so subsequent runs may not require a code.

### HEIC/HEIF support
If you install `pillow-heif`, EXIF reading will work for HEIC/HEIF files as well:

```bash
pip install pillow-heif
```

### Testing Live Photo pairing locally (optional)
A helper script `test_pairing.py` will scan a directory and pair/rename Live Photo components in-place. It currently points to `D:/icloud_test` by default—edit the path in the script to your target folder before running.

```bash
python test_pairing.py
```

### Notes and troubleshooting
- The script does not delete or overwrite existing files; existing paths are skipped.
- If you see frequent failures, increase `--throttle` and/or `--retries`.
- If `len(photos)` is slow or unavailable, the script will stream assets lazily regardless.
- For security, avoid passing `--password` on shared machines; use the prompt instead.

### Disclaimer
This project relies on third-party libraries and iCloud’s private API surface. Use at your own risk and ensure you comply with Apple’s terms for your account and data.