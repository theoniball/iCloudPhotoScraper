import argparse
import getpass as getpass_module
import io
import os
import re
import sys
import tempfile
import time
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from getpass import getpass
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudServiceUnavailable, PyiCloudAPIResponseException
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
                    mtime REAL,
                    processed_at REAL DEFAULT (julianday('now'))
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # Add processed_at column if it doesn't exist (for existing databases)
            try:
                con.execute("ALTER TABLE processed ADD COLUMN processed_at REAL DEFAULT (julianday('now'))")
            except sqlite3.OperationalError:
                pass  # Column already exists
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

    def get_last_processed_id(self) -> str | None:
        """
        Get the asset_id of the most recently processed asset.
        Returns None if no assets have been processed yet.
        Uses processed_at if available, otherwise falls back to rowid or mtime.
        """
        with self._conn() as con:
            # First try: use processed_at column (most accurate)
            try:
                cur = con.execute(
                    "SELECT asset_id FROM processed WHERE processed_at IS NOT NULL ORDER BY processed_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0]
            except sqlite3.OperationalError:
                pass  # Column might not exist yet
            
            # Fallback 1: use rowid (insertion order, works for existing databases)
            try:
                cur = con.execute(
                    "SELECT asset_id FROM processed ORDER BY rowid DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0]
            except Exception:
                pass
            
            # Fallback 2: use mtime (file modification time, approximate)
            try:
                cur = con.execute(
                    "SELECT asset_id FROM processed WHERE mtime IS NOT NULL ORDER BY mtime DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return row[0]
            except Exception:
                pass
            
            # Final fallback: just get any asset_id (better than nothing)
            try:
                cur = con.execute("SELECT asset_id FROM processed LIMIT 1")
                row = cur.fetchone()
                if row:
                    return row[0]
            except Exception:
                pass
            
            return None
    
    def has_processed_assets(self) -> bool:
        """
        Check if we have any processed assets at all.
        """
        return len(self._processed_ids) > 0

    def is_backfill_complete(self) -> bool:
        """
        True once a prior run has made it all the way through the iCloud
        library at least once without being cut short (by --max or an
        interruption). Only then is it safe to stop scanning as soon as we
        hit the first already-processed asset — otherwise an interrupted
        initial backfill could look "caught up" while most of the library
        is still undownloaded.
        """
        with self._conn() as con:
            cur = con.execute("SELECT value FROM meta WHERE key = 'backfill_complete'")
            row = cur.fetchone()
            return row is not None and row[0] == "1"

    def mark_backfill_complete(self):
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('backfill_complete', '1')"
            )

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

    try:
        photos_iter = api.photos.all
    except PyiCloudServiceUnavailable as e:
        log(f"❌ Photos service not available: {e}")
        log("Cannot bootstrap state without access to photos.")
        sys.exit(1)
    except PyiCloudAPIResponseException as e:
        log(f"❌ iCloud API error: {e}")
        sys.exit(1)

    for asset in photos_iter:
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


def get_asset_at(photos, index: int):
    """
    Direct positional lookup into the photos album (photos[index]),
    without walking the iterator. Returns None if the index is invalid
    or the lookup fails for any reason.
    """
    try:
        return photos[index]
    except (IndexError, StopIteration, KeyError):
        return None
    except Exception as e:
        log(f"Warning: failed to fetch photo at index {index}: {e}")
        return None


def detect_newest_end(photos, total: int) -> str | None:
    """
    Empirically determine which end of the album (index 0 or index
    total-1) holds the newest photo, using two direct index lookups.

    We can't just trust the album's declared sort direction: pyicloud's
    "All Photos" iterator is configured "descending" but has been observed
    to actually *yield* oldest-to-newest (its offset-stepping logic walks
    the position array backwards). Direct indexing isn't necessarily
    subject to that same iterator bug, so we verify directly instead of
    assuming either way. Returns "start", "end", or None if inconclusive
    (e.g. missing dates, or too few photos to tell).
    """
    if not total or total < 2:
        return "start" if total else None
    first = get_asset_at(photos, 0)
    last = get_asset_at(photos, total - 1)
    first_dt = to_utc_naive(getattr(first, "created", None)) if first else None
    last_dt = to_utc_naive(getattr(last, "created", None)) if last else None
    if not first_dt or not last_dt or first_dt == last_dt:
        return None
    return "start" if first_dt > last_dt else "end"


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


def clear_pyicloud_session_cache(account_name: str):
    """
    Remove pyicloud's cached session/cookiejar files for this account.

    pyicloud reuses a cached session_token across runs so you don't have to
    re-auth every time. If a stale/failed 2FA attempt leaves that cache
    behind, the next run will "validate" the old token instead of doing a
    fresh sign-in — which means Apple never sends a new push/popup, and you
    get stuck waiting for a code that was never sent. Wiping the cache here
    forces the next run to do a full fresh login.
    """
    topdir = Path(tempfile.gettempdir()) / "pyicloud"
    cookie_dir = topdir / getpass_module.getuser()
    safe_name = "".join(c for c in account_name if re.match(r"\w", c))
    for suffix in (".session", ".cookiejar"):
        f = cookie_dir / f"{safe_name}{suffix}"
        try:
            f.unlink()
            log(f"Cleared stale session cache: {f}")
        except FileNotFoundError:
            pass
        except Exception as e:
            log(f"Warning: Could not clear session cache {f}: {e}")


def request_code_via_trusted_device(api: PyiCloudService):
    """
    Explicitly ask Apple to send a verification code to a chosen trusted
    device/phone number, instead of waiting for the automatic push. Apple's
    automatic 2FA push only reaches a device that's already signed into
    iCloud with this Apple ID and online right now — if none is available
    (or the account really only has a trusted phone number), no push ever
    arrives and there's no other way to get a code short of this explicit
    request.

    Returns (device, code); device/code are None/"" if the request failed
    or was aborted.
    """
    try:
        devices = api.trusted_devices
    except Exception as e:
        log(f"Could not retrieve trusted devices: {e}")
        return None, ""
    if not devices:
        log("No trusted devices/phone numbers found on this account.")
        return None, ""
    for i, device in enumerate(devices):
        log(f"[{i}] {device.get('deviceName', 'Device')} - {device.get('phoneNumber', '')}")
    try:
        choice = int(input("Choose a device to send a code to: ").strip())
        device = devices[choice]
    except (ValueError, IndexError):
        log("Invalid selection.")
        return None, ""
    if not api.send_verification_code(device):
        log("❌ Failed to send verification code.")
        return None, ""
    return device, input("Enter the code you received: ").strip()


def ensure_session(api: PyiCloudService):
    """
    Handle 2FA or 2SA if required.
    """
    if getattr(api, "requires_2fa", False):
        log("Two-factor authentication required.")
        code = input(
            "Enter the code you received, or press Enter if nothing arrived "
            "to explicitly request one be sent to a trusted device/phone number: "
        ).strip()
        if not code:
            _, code = request_code_via_trusted_device(api)
        if not code or not api.validate_2fa_code(code):
            clear_pyicloud_session_cache(api.account_name)
            sys.exit("❌ 2FA validation failed.")
        if not api.is_trusted_session:
            try:
                api.trust_session()
                log("Trusted session established.")
            except Exception:
                log("Warning: Could not establish a trusted session.")
    elif getattr(api, "requires_2sa", False):  # older accounts
        log("Two-step authentication required.")
        device, code = request_code_via_trusted_device(api)
        if not device or not code or not api.validate_verification_code(device, code):
            clear_pyicloud_session_cache(api.account_name)
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


def export_asset(api, asset, root: Path, dry_run: bool = False, retries: int = 3, throttle_s: float = 0.2, state: ExportState | None = None, quiet_skips: bool = False, skip_manifest_check: bool = False):
    """
    Check state for file before downloading
    Download one asset and place it in /YYYY/MM Month/filename, preserving times.
    Skips if file already exists.
    
    Args:
        skip_manifest_check: If True, skip checking the manifest (assumes asset is unprocessed)
    """
    if state and not skip_manifest_check and state.is_done(asset.id):
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
            data = asset.download()  # pyicloud 2.6.5+ returns raw bytes, not a streaming Response
            if data is None:
                raise RuntimeError("Download returned no data")
            atomic_write(temp_path, io.BytesIO(data))

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
    parser.add_argument("--force-full-scan", action="store_true", help="Ignore the backfill-complete marker and do a full resume scan (safety net / re-verification).")
    args = parser.parse_args()

    username = args.username or input("iCloud Email: ").strip()
    password = args.password or getpass("Password (input hidden): ")

    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state) if args.state else (out_root / ".state" / "icloud.sqlite")
    state = ExportState(state_path)

    log("Authenticating to iCloud…")
    api = PyiCloudService(username, password)
    ensure_session(api)

    # Check if we still need 2FA/2SA (sometimes it's required after initial auth)
    if getattr(api, "requires_2fa", False) or getattr(api, "requires_2sa", False):
        log("⚠ Additional authentication required. Please complete 2FA/2SA.")
        ensure_session(api)

    # Verify photos service is available
    log("Checking Photos service availability…")
    try:
        photos = api.photos.all
    except PyiCloudServiceUnavailable as e:
        log("❌ Photos service is not available.")
        log("This usually means:")
        log("  1. Your account doesn't have iCloud Photos enabled")
        log("  2. Authentication failed or session expired")
        log("  3. iCloud service is temporarily unavailable")
        log(f"   Error details: {e}")
        sys.exit(1)
    except PyiCloudAPIResponseException as e:
        log(f"❌ iCloud API error: {e}")
        log("This usually means authentication failed. Please check your credentials.")
        sys.exit(1)

    if args.bootstrap_state:
        bootstrap_state_from_disk(api, out_root, state)
        sys.exit(0)
    try:
        total = len(photos)  # may be slow; safe to keep for user feedback
    except Exception:
        total = None

    if total is not None:
        log(f"Found {total} photos.")
    else:
        log("Enumerating photos…")

    has_previous_runs = state.has_processed_assets()
    fast_catchup = state.is_backfill_complete() and not args.force_full_scan
    newest_end = None
    if fast_catchup:
        newest_end = detect_newest_end(photos, total) if total else None
        if newest_end is None:
            log("Could not verify which end of your library is newest — falling back to a full resume scan this run.")
            fast_catchup = False

    reached_end = False
    count = 0
    skipped_count = 0

    if fast_catchup:
        # A prior run already made it all the way through the library once, so the
        # entire thing is a contiguous "done" block anchored at the newest photo.
        # New photos can only appear beyond that block, so the moment we hit an
        # already-processed asset (walking from the verified-newest end) we know
        # everything past it is already handled too. We walk by direct index
        # rather than the natural iterator, since that iterator has been observed
        # to walk oldest-to-newest regardless of the album's declared direction.
        idx = 0 if newest_end == "start" else total - 1
        step = 1 if newest_end == "start" else -1
        asset = get_asset_at(photos, idx)
        if asset is not None:
            first_name = getattr(asset, "filename", None) or getattr(asset, "id", "?")
            first_dt = getattr(asset, "created", None)
            log(f"Backfill previously completed — scanning newest-first from index {idx}. First item: {first_name} (created {first_dt}).")
        else:
            log(f"Backfill previously completed — scanning newest-first from index {idx}, but couldn't fetch it.")
        while asset is not None and 0 <= idx < total:
            asset_id = getattr(asset, "id", None)
            if state.is_done(asset_id):
                log(f"Caught up to already-processed photos after {count} new asset(s). Stopping scan.")
                break
            export_asset(api, asset, out_root, dry_run=args.dry_run, retries=args.retries, throttle_s=args.throttle, state=state, quiet_skips=args.quiet_skips, skip_manifest_check=True)
            count += 1
            if args.max and count >= args.max:
                break
            idx += step
            asset = get_asset_at(photos, idx) if 0 <= idx < total else None
    else:
        # Get the last processed asset ID to skip to that point
        last_processed_id = state.get_last_processed_id()

        if last_processed_id:
            log(f"Resuming from last processed asset: {last_processed_id}")
            log("Processing new photos until we reach the last processed asset...")
        elif has_previous_runs:
            # We have processed assets but couldn't determine the last one
            # This can happen with old databases or migration issues
            log("Found existing processed assets but couldn't determine last processed asset.")
            log("Will process all photos (already-processed ones will be skipped efficiently).")
            last_processed_id = None  # Ensure it's None so we don't try to skip
        else:
            log("No previous processing found. Starting from the beginning.")

        found_last_processed = False
        photos_before_last = 0
        max_search_before_warning = 50000  # Safety: warn if we process many photos without finding last processed

        for asset in photos:
            asset_id = getattr(asset, "id", None)

            # Determine if we should skip manifest check
            # After finding last processed, we know everything after it is unprocessed
            skip_manifest = found_last_processed

            # If we have a last processed ID, process photos until we find it
            # (these are new photos added since last run)
            if last_processed_id and not found_last_processed:
                if asset_id == last_processed_id:
                    found_last_processed = True
                    log(f"Found last processed asset after {photos_before_last} photos. Skipping it and continuing with older photos...")
                    log("Skipping manifest checks for older photos (we know they're unprocessed)...")
                    # Skip this one since we already processed it
                    skipped_count += 1
                    continue
                else:
                    # This is a new photo (appears before last processed in the list)
                    # We need to check manifest for these since they might have been added in a previous interrupted run
                    photos_before_last += 1
                    if photos_before_last > 0 and photos_before_last % max_search_before_warning == 0:
                        log(f"Warning: Processed {photos_before_last} photos without finding last processed asset.")
                        log("It may have been deleted from iCloud. Continuing to process all photos...")
                    # Check manifest for new photos (before last processed)
                    skip_manifest = False

            # Process this asset
            # For photos after last processed: skip_manifest=True (we know they're unprocessed)
            # For photos before last processed: skip_manifest=False (need to check manifest)
            export_asset(api, asset, out_root, dry_run=args.dry_run, retries=args.retries, throttle_s=args.throttle, state=state, quiet_skips=args.quiet_skips, skip_manifest_check=skip_manifest)
            count += 1
            if args.max and count >= args.max:
                break
        else:
            reached_end = True

    if reached_end and not args.dry_run and not state.is_backfill_complete():
        state.mark_backfill_complete()
        log("Reached the end of your iCloud library — future runs will use the fast catch-up scan.")

    if skipped_count > 0:
        log(f"Skipped {skipped_count} already-processed asset(s).")
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
