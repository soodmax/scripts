# plex_organize

Moves every video file in a folder (including a network share) into its own Plex-compliant folder:

```
Inception.2010.1080p.BluRay.x264-GRP.mkv  ->  Inception (2010)/Inception (2010).mkv
Inception.2010.1080p.BluRay.x264-GRP.en.srt -> Inception (2010)/Inception (2010).en.srt
Kill.Bill.Vol.1.2003.CD1.avi              ->  Kill Bill Vol 1 (2003)/Kill Bill Vol 1 (2003) - pt1.avi
the_matrix_(1999)_[YTS.MX].mp4            ->  The Matrix (1999)/The Matrix (1999).mp4
```

Requires Python 3.8+, with no extra packages.

## Usage

```bash
# 1. Preview (dry run, nothing is moved)
python3 plex_organize.py /mnt/nas/Movies          # Linux / NAS mount
python3 plex_organize.py /Volumes/Movies          # macOS
python  plex_organize.py "\\NAS\Movies"           # Windows UNC path

# 2. Do it
python3 plex_organize.py /mnt/nas/Movies --apply

# 3. Changed your mind? Every --apply writes an undo log into the folder
python3 plex_organize.py --undo /mnt/nas/Movies/plex_organize_20261028-160941.tsv
```

| Option | Meaning |
|---|---|
| `--apply` | Actually move files (default is a dry run) |
| `--dest DIR` | Create the new folders in DIR instead of in the source folder |
| `--no-recurse` | Only process files directly in the folder |
| `--tv` | Also organize `SxxEyy` episodes into `Show/Season NN/Show - sNNeNN.ext` |
| `--no-cleanup` | Keep source folders that end up empty |
| `--log FILE` | Choose where the undo log goes |

## What it does

- Removes release junk (resolution, source, codec, group tags, `[YTS.MX]`, and so on) and converts dots and underscores to spaces.
- Detects the year, including tricky titles like `2001 A Space Odyssey (1968)` and `Blade Runner 2049 (2017)`.
- Renames multi-part files (`CD1`, `part2`, `disc1`) to Plex's ` - ptN` format.
- Moves matching subtitles and `.nfo` files with the video and keeps language tags (`.en.srt`, `.en.forced.srt`).
- Replaces characters that SMB and Windows don't allow (`: ? * < > | "`).
- Skips `sample` files, hidden files, and NAS system folders (`@eaDir`, `#recycle`, ...).
- Never overwrites: if the target already exists, the file is skipped and reported.
- Running it again is safe, because files that are already organized are left alone.
- TV episodes are skipped unless you pass `--tv`, because Plex expects them grouped by show and season, not one folder per file.
