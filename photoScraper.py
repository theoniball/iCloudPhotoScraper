import argparse
import os
import sys
import time
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from getpass import getpass
from datetime import datetime
from datetime import timezone
from pyicloud import PyiCloudService
from PIL import Image
from PIL.ExifTags import TAGS

IMAGE_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}

class ExportState:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS processed (
                    asset_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    size INTEGER,
                    mtime REAL
                )
            """)
        # Build an in-memory cache of processed IDs for fast membership checks
        self._processed_ids: set[str] = set()
        with self._conn() as con:
            try:
                cur = con.execute("SELECT asset_id FROM processed")
                for row in cur:
                    self._processed_ids.add(row[0])
            except Exception:
                # If the table is empty or unreadable, fall back to empty cache
                self._processed_ids = set()
    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.db_path)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def is_done(self, asset_id: str) -> bool:
        # Fast path using in-memory cache
        return asset_id in self._processed_ids

    def mark_done(self, asset_id: str, path: Path):
        try:
            st = path.stat()
            size, mtime = st.st_size, st.st_mtime
        except Exception:
            size, mtime = None, None
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO processed (asset_id, path, size, mtime) VALUES (?,?,?,?)",
                (asset_id, str(path), size, mtime),
            )
        # Keep cache in sync
        self._processed_ids.add(asset_id)

def bootstrap_state_from_disk(api, out_root: Path, state: ExportState):
    """
    Populate the state DB by matching existing files on disk to iCloud assets.
    - Matches by asset ID (old naming) OR filename (with _LIVE normalization).
    - If multiple assets share the same filename, disambiguates by date proximity.
    """
    print(f"Bootstrapping state from {out_root} …")

    # Build a quick in-memory index of your library
    asset_ids = set()
    by_filename = {}  # lowercased filename -> list[(asset_id, created_dt)]
    count_assets = 0

    for asset in api.photos.all:
        count_assets += 1
        aid = getattr(asset, "id", None)
        fname = getattr(asset, "filename", None) or (f"{aid}.jpg" if aid else None)
        created = getattr(asset, "created", None)

        if not aid or not fname:
            continue

        asset_ids.add(aid)
        key = fname.lower()
        by_filename.setdefault(key, []).append((aid, created))

    print(f"Indexed {count_assets} assets. Starting filesystem scan…")

    boot_done = 0
    ambiguous = 0
    unmatched = 0

    for p in out_root.rglob("*"):
        if not p.is_file():
            continue

        # 1) Direct old-style match: filename stem == asset_id
        stem = p.stem
        if stem in asset_ids and not state.is_done(stem):
            state.mark_done(stem, p)
            boot_done += 1
            print(f"✔ by ID: {p} -> {stem}")
            continue

        # 2) Filename match (normalize _LIVE → original name)
        candidate_name = original_name_from_live(p.name).lower()
        candidates = by_filename.get(candidate_name)

        if not candidates:
            unmatched += 1
            # Optional: also try matching just by name case-insensitively if the FS changed case
            continue

        if len(candidates) == 1:
            aid, _ = candidates[0]
            if not state.is_done(aid):
                state.mark_done(aid, p)
                boot_done += 1
                print(f"✔ by filename: {p} -> {aid}")
            continue

        # 3) Disambiguate same-name assets using capture date vs file's EXIF/mtime
        try:
            dt = get_exif_datetime(p) or datetime.fromtimestamp(p.stat().st_mtime)
        except Exception:
            dt = None

        chosen = None
        if dt:
            # within ±5 days window; normalize tz to avoid naive/aware mismatch
            dt_norm = to_utc_naive(dt)
            for aid, created in candidates:
                if isinstance(created, datetime):
                    created_norm = to_utc_naive(created)
                    if dt_norm and created_norm and abs((created_norm - dt_norm).total_seconds()) <= 5 * 86400:
                        chosen = aid
                        break

        if chosen and not state.is_done(chosen):
            state.mark_done(chosen, p)
            boot_done += 1
            print(f"✔ by name+date: {p} -> {chosen}")
        else:
            ambiguous += 1
            print(f"⚠ Ambiguous (skipped): {p} matches {len(candidates)} assets")

    print(f"\nBootstrap complete.")
    print(f"  Inserted: {boot_done}")
    print(f"  Ambiguous (skipped): {ambiguous}")
    print(f"  Unmatched: {unmatched}")

def canonical_stem(name: str) -> str:
    """
    Normalize filenames so edited variants still pair:
    - Turn IMG_E1234 -> IMG_1234
    - Strip common iOS suffix artifacts like "~photo" if present.
    """
    stem = Path(name).stem
    # Normalize edited prefix
    if stem.startswith("IMG_E") and len(stem) >= 8 and stem[5:9].isdigit():
        stem = "IMG_" + stem[5:]
    # Some tools append things like '~photo' or '(1)'—trim lightweight suffixes
    # Keep this conservative to avoid collisions
    for tail in ("~photo", "~video", "~2", "~3"):
        if stem.endswith(tail):
            stem = stem[: -len(tail)]
    return stem

def original_name_from_live(name: str) -> str:
    """
    If a filename is in the Live Photo companion form <stem>_LIVE<ext>,
    return the original still name <stem><ext>. Otherwise return unchanged.
    Case-sensitive on purpose; iOS naming uses uppercase _LIVE.
    """
    p = Path(name)
    stem = p.stem
    if stem.endswith("_LIVE"):
        return stem[:-5] + p.suffix
    return name

def pair_and_rename_live_video(final_path: Path):
    """
    If final_path is part of a Live Photo pair, ensure the VIDEO sits next to the still
    with the name: <photo_stem>_LIVE<video_ext>.

    Idempotent and safe to call for both stills and videos.
    """
    parent = final_path.parent
    ext = final_path.suffix.lower()
    name = final_path.name
    stem_canon = canonical_stem(name)

    # Find sibling still/video with same canonical stem
    # We scan only the current folder (your script already sorts by date)
    candidates = list(parent.iterdir())

    def siblings_of_kind(kind_exts):
        out = []
        for p in candidates:
            if p == final_path or not p.is_file():
                continue
            if p.suffix.lower() in kind_exts and canonical_stem(p.name) == stem_canon:
                out.append(p)
        return out

    is_video = ext in VIDEO_EXTS
    is_image = ext in IMAGE_EXTS

    if not (is_video or is_image):
        return  # not a type we manage

    if is_video:
        # Look for the still we should anchor to
        stills = siblings_of_kind(IMAGE_EXTS)
        if not stills:
            return  # still not here (yet)
        # Choose one (if multiple, prefer one with exact stem match; else first)
        still = next((s for s in stills if s.stem == Path(name).stem), stills[0])
        target = still.with_name(f"{still.stem}_LIVE{final_path.suffix}")
        if target == final_path:
            return  # already correct
        if target.exists():
            # If a correctly named video already exists, and it's not us, do nothing.
            return
        try:
            final_path.rename(target)
            print(f"Paired Live Photo: renamed video -> {target.name}")
        except Exception as e:
            print(f"Pair/rename failed for {final_path.name}: {e}")
        return

    if is_image:
        # If we saved the still, see if the video is already here and fix its name
        videos = siblings_of_kind(VIDEO_EXTS)
        if not videos:
            return
        target = final_path.with_name(f"{final_path.stem}_LIVE{videos[0].suffix}")
        # Prefer exact match rename; if exact already okay, skip
        for vid in videos:
            if vid == target:
                return  # already properly named
        # Otherwise rename the first one found to the _LIVE form (leave any others untouched)
        vid = videos[0]
        if target.exists():
            # Another file already holds the preferred name; leave as-is
            return
        try:
            vid.rename(target)
            print(f"Paired Live Photo: renamed video -> {target.name}")
        except Exception as e:
            print(f"Pair/rename failed for {vid.name}: {e}")

# Optional HEIC/HEIF support for EXIF (install: pillow-heif)
try:
    import pillow_heif  # noqa: F401
    pillow_heif.register_heif_opener()  # lets PIL open .heic
    HEIF_OK = True
except Exception:
    HEIF_OK = False

def log(msg: str):
    print(msg, flush=True)


def map_month(month_number: int) -> str:
    import calendar
    return calendar.month_name[int(month_number)]


def get_exif_datetime(img_path: Path) -> datetime | None:
    """
    Try EXIF DateTimeOriginal first; fallback to DateTime.
    Returns timezone-naive datetime if present, else None.
    """
    try:
        with Image.open(img_path) as im:
            exif = im.getexif()
            if not exif:
                return None
            # Prefer DateTimeOriginal
            for tag_id, value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                    # format "YYYY:MM:DD HH:MM:SS"
                    try:
                        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        continue
    except Exception as e:
        log(f"EXIF read error on {img_path.name}: {e}")
    return None


def to_utc_naive(dt: datetime | None) -> datetime | None:
    """
    Convert timezone-aware datetimes to UTC-naive; leave naive as-is.
    Returns None if input is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return dt.replace(tzinfo=None)

def choose_dates(asset, downloaded_path: Path) -> datetime:
    """
    Decide the date to sort on.
    1) EXIF DateTimeOriginal (if available)
    2) asset.created (iCloud metadata)
    3) downloaded file's mtime
    """
    exif_dt = get_exif_datetime(downloaded_path)
    if exif_dt:
        return exif_dt

    # Fallback to iCloud metadata
    created = getattr(asset, "created", None)
    if created:
        return created if isinstance(created, datetime) else None

    # Final fallback: file mtime
    try:
        ts = downloaded_path.stat().st_mtime
        return datetime.fromtimestamp(ts)
    except Exception:
        return datetime.now()


def ensure_session(api: PyiCloudService):
    """
    Handle 2FA or 2SA if required.
    """
    if getattr(api, "requires_2fa", False):
        log("Two-factor authentication required.")
        code = input("Enter the code you received: ").strip()
        if not api.validate_2fa_code(code):
            sys.exit("❌ 2FA validation failed.")
        if not api.is_trusted_session:
            try:
                api.trust_session()
                log("Trusted session established.")
            except Exception:
                log("Warning: Could not establish a trusted session.")
    elif getattr(api, "requires_2sa", False):  # older accounts
        log("Two-step authentication required.")
        devices = api.trusted_devices
        for i, device in enumerate(devices):
            log(f"[{i}] {device.get('deviceName', 'Device')} - {device.get('phoneNumber','')}")
        choice = int(input("Choose a device for a verification code: "))
        device = devices[choice]
        if not api.send_verification_code(device):
            sys.exit("❌ Failed to send verification code.")
        code = input("Enter the code you received: ").strip()
        if not api.validate_verification_code(device, code):
            sys.exit("❌ 2SA validation failed.")
        try:
            api.trust_session()
            log("Trusted session established.")
        except Exception:
            log("Warning: Could not establish a trusted session.")


def atomic_write(dst: Path, data_stream, chunk_size: int = 1024 * 1024):
    """
    Write to dst via dst.tmp, then rename (atomic on same filesystem).
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    with open(tmp, "wb") as f:
        while True:
            chunk = data_stream.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dst)


def save_times(path: Path, dt: datetime):
    """
    Update file's modified & access times to match the chosen datetime.
    """
    try:
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception:
        pass


def export_asset(api, asset, root: Path, dry_run: bool = False, retries: int = 3, throttle_s: float = 0.2, state: ExportState | None = None, quiet_skips: bool = False):
    """
    Check state for file before downloading
    Download one asset and place it in /YYYY/MM Month/filename, preserving times.
    Skips if file already exists.
    """
    if state and state.is_done(asset.id):
        if not quiet_skips:
            print(f"Skipped (manifest): {getattr(asset, 'filename', asset.id)}")
        return
    # Prefer the original filename; fallback to id-based name
    filename = getattr(asset, "filename", None) or f"{asset.id}.jpg"
    # Very important: keep the extension (HEIC, PNG, MOV, etc.)
    safe_name = filename.replace("/", "_")
    temp_dir = root / "_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / safe_name

    # Skip if we’ve already exported this asset anywhere under root
    # (fast path: check canonical final path below; deeper duplicate checks would require hashing)
    # We’ll compute final path after download when we know the date.

    # Download with retries
    attempt = 0
    while attempt < retries:
        try:
            if dry_run:
                # We still may need date to compute final destination; try to use created for layout.
                chosen_dt = getattr(asset, "created", datetime.now())
                year = f"{chosen_dt:%Y}"
                month_num = int(f"{chosen_dt:%m}")
                month_dir = f"{month_num:02d} {map_month(month_num)}"
                final_dir = root / year / month_dir
                final_path = final_dir / safe_name
                log(f"[DRY RUN] would save -> {final_path}")
                return

            log(f"Downloading: {safe_name}")
            resp = asset.download()  # pyicloud provides .raw for streaming
            with resp.raw as stream:
                atomic_write(temp_path, stream)

            # Decide the final date (EXIF if present)
            chosen_dt = choose_dates(asset, temp_path)
            year = f"{chosen_dt:%Y}"
            month_num = int(f"{chosen_dt:%m}")
            month_dir = f"{month_num:02d} {map_month(month_num)}"
            final_dir = root / year / month_dir
            final_dir.mkdir(parents=True, exist_ok=True)
            final_path = final_dir / safe_name

            if final_path.exists():
                log(f"Skipped (exists): {final_path}")
                temp_path.unlink(missing_ok=True)
                return

            # Move into place and set times
            temp_path.replace(final_path)
            save_times(final_path, chosen_dt)
            pair_and_rename_live_video(final_path)
            log(f"Saved to: {final_path}")

            if state:
                state.mark_done(asset.id, final_path)

            time.sleep(throttle_s)
            return
        except KeyboardInterrupt:
            log("Interrupted by user.")
            raise
        except Exception as e:
            attempt += 1
            log(f"Download failed (attempt {attempt}/{retries}) for {safe_name}: {e}")
            time.sleep(1.5 * attempt)
    log(f"❌ Giving up on {safe_name} after {retries} attempts.")
    # Cleanup temp on failure if present
    try:
        temp_path.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Export iCloud Photos to a local drive.")
    parser.add_argument("--username", "-u", help="Apple ID email (will prompt if not provided)")
    parser.add_argument("--password", "-p", help="Apple ID password (not recommended to pass via CLI)")
    parser.add_argument("--output", "-o", default="D:/iCloud", help="Destination folder (e.g., external drive)")
    parser.add_argument("--max", type=int, default=None, help="Limit number of assets (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Don’t download; just show where files would go")
    parser.add_argument("--throttle", type=float, default=0.2, help="Seconds to sleep between downloads")
    parser.add_argument("--retries", type=int, default=3, help="Retry attempts per asset")
    parser.add_argument("--state", default=None, help="Path to resume DB (e.g. D:/iCloud/.state/icloud.sqlite). If omitted, one is created under --output.")
    parser.add_argument("--bootstrap-state", action="store_true", help="Scan --output and populate the state DB from existing files (one-time).")
    parser.add_argument("--quiet-skips", action="store_true", help="Don’t log per-asset skip messages for already-processed items")
    args = parser.parse_args()

    username = args.username or input("iCloud Email: ").strip()
    password = args.password or getpass("Password (input hidden): ")

    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    out_root = Path(args.output).expanduser().resolve()
    state_path = Path(args.state) if args.state else (out_root / ".state" / "icloud.sqlite")
    state = ExportState(state_path)
    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    api = PyiCloudService(username, password)
    ensure_session(api)

    if args.bootstrap_state:
        bootstrap_state_from_disk(api, out_root, state)
        sys.exit(0)

    log("Authenticating to iCloud…")
    api = PyiCloudService(username, password)
    ensure_session(api)

    # Photos handle can be large; iterate lazily
    photos = api.photos.all
    try:
        total = len(photos)  # may be slow; safe to keep for user feedback
    except Exception:
        total = None

    if total is not None:
        log(f"Found {total} photos.")
    else:
        log("Enumerating photos…")

    count = 0
    for asset in photos:
        export_asset(api, asset, out_root, dry_run=args.dry_run, retries=args.retries, throttle_s=args.throttle, state=state, quiet_skips=args.quiet_skips)
        count += 1
        if args.max and count >= args.max:
            break

    log(f"Done. Processed {count} asset(s).")


if __name__ == "__main__":
    """ parser = argparse.ArgumentParser()
    parser.add_argument("--test-pairing", metavar="DIR",
                        help="Run pairing/renaming on all files in DIR (no iCloud required).")
    # keep your existing args (username/output/etc.)
    args, unknown = parser.parse_known_args()

    if args.test_pairing:
        test_dir = Path(args.test_pairing)
        for f in sorted(test_dir.iterdir()):
            if f.is_file():
                print(f"Checking {f.name} …")
                pair_and_rename_live_video(f)
        raise SystemExit(0) """
    main()
