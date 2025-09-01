import argparse
import os
import sys
import time
from pathlib import Path
from datetime import datetime
from getpass import getpass

from pyicloud import PyiCloudService
from PIL import Image
from PIL.ExifTags import TAGS

IMAGE_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}


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


def export_asset(api, asset, root: Path, dry_run: bool = False, retries: int = 3, throttle_s: float = 0.2):
    """
    Download one asset and place it in /YYYY/MM Month/filename, preserving times.
    Skips if file already exists.
    """
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
    args = parser.parse_args()

    username = args.username or input("iCloud Email: ").strip()
    password = args.password or getpass("Password (input hidden): ")

    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

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
        export_asset(api, asset, out_root, dry_run=args.dry_run, retries=args.retries, throttle_s=args.throttle)
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
