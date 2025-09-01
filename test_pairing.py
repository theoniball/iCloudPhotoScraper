from pathlib import Path
from photoScraper import pair_and_rename_live_video  # adjust import

test_dir = Path("D:/icloud_test")

for f in test_dir.iterdir():
    if f.is_file():
        print(f"Checking {f.name} …")
        pair_and_rename_live_video(f)