from pathlib import Path
from photoScraper import pair_and_rename_live_video  # adjust import

test_dir = Path("D:/icloud_test")

if not test_dir.exists():
    print(f"Error: Directory {test_dir} does not exist!")
    exit(1)

if not test_dir.is_dir():
    print(f"Error: {test_dir} is not a directory!")
    exit(1)

for f in test_dir.iterdir():
    if f.is_file():
        print(f"Checking {f.name} …")
        pair_and_rename_live_video(f)