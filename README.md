# plex_organize

Organizes a folder of video files (including a network share) into the folder layout Plex expects. It handles movies, TV shows and sports.

```
Inception.2010.1080p.BluRay.x264-GRP.mkv     ->  Movies/Inception (2010)/Inception (2010).mkv
Kill.Bill.Vol.1.2003.CD1.avi                 ->  Movies/Kill Bill Vol 1 (2003)/Kill Bill Vol 1 (2003) - pt1.avi
Breaking.Bad.S01E01.Pilot.720p.HDTV.mkv      ->  TV Shows/Breaking Bad/Season 01/Breaking Bad - s01e01 - Pilot.mkv
game.of.thrones.1x03.mkv                     ->  TV Shows/Game of Thrones/Season 01/Game of Thrones - s01e03.mkv
NFL.2024.09.08.Chiefs.vs.Ravens.720p.mkv     ->  Sports/NFL/Season 2024/NFL - 2024-09-08 - Chiefs vs Ravens.mkv
Premier.League.15.03.2025.Arsenal.v.Chelsea  ->  Sports/Premier League/Season 2025/Premier League - 2025-03-15 - Arsenal v Chelsea.mkv
UFC.300.Pereira.vs.Hill.PPV.1080p.mkv        ->  Sports/UFC/UFC 300 Pereira vs Hill PPV.mkv
```

Requires Python 3.8+, with no extra packages.

## Usage

```bash
# Root media folder that mixes movies, TV and sports:
python3 plex_organize.py /mnt/nas/Media --mixed            # preview, nothing is moved
python3 plex_organize.py /mnt/nas/Media --mixed --apply    # do it

# A folder that only holds movies:
python3 plex_organize.py /mnt/nas/Movies --apply

# Undo: every --apply run writes a log into the folder
python3 plex_organize.py --undo /mnt/nas/Media/plex_organize_20261028-160941.tsv
```

On Windows, pass a network path such as `"\\NAS\Media"`.

With `--mixed`, the script creates (or reuses) `Movies/`, `TV Shows/` and `Sports/` inside the folder. Point one Plex library at each:

| Folder | Plex library type |
|---|---|
| `Movies/` | Movies |
| `TV Shows/` | TV Shows |
| `Sports/` | **Other Videos** (recommended). Plex has no reliable sports metadata source, and this type shows the files as-is. The date-based names also work with a TV Shows library. |

## Options

| Option | Meaning |
|---|---|
| `--apply` | Actually move files (default is a dry run) |
| `--mixed` | Sort movies, TV and sports into separate library folders |
| `--movies-dir`, `--tv-dir`, `--sports-dir` | Rename those folders (defaults: `Movies`, `TV Shows`, `Sports`) |
| `--tv` / `--sports` | Without `--mixed`: also organize TV or sports in place (skipped by default) |
| `--no-movies` | Leave movies alone |
| `--dest DIR` | Put the organized files in DIR instead of in the source folder |
| `--no-recurse` | Only process files directly in the folder |
| `--no-cleanup` | Keep source folders that end up empty |
| `--log FILE` | Choose where the undo log goes |

## How it decides what a file is

1. **TV**: the name has `S01E02`, `s1e2` or `1x02`. If the file is inside `Show/Season 1/`, the show folder's name is used, because it's usually better than the file name (`The Office (US)` rather than `the.office.us`). Episode titles in the file name are kept.
2. **Sports**: the name contains a known league or event (NFL, NBA, NHL, MLB, UFC, WWE, F1, MotoGP, Premier League, Champions League, ...) **and** a date or game words (`vs`, `Week 3`, `Race`, `Final`, `UFC 300`...). Requiring both stops the movie `F1 (2025)` from being treated as sports. Files already inside `Sports/<League>/` count as sports even without a league name in the file name.
3. **Movies**: everything else.

## Safety

- It only previews unless you add `--apply`, and `--undo` reverses a run.
- It never overwrites. If a target exists, the file is skipped and reported.
- Running it again is safe, because files that are already organized are left alone.
- Plex extras (`Featurettes/`, `Trailers/`, `*-trailer.mkv`...), `sample` files, hidden files and NAS system folders (`@eaDir`, `#recycle`) are never touched.
- A file inside a TV `Season` folder that has no episode number is skipped instead of being guessed.
- Subtitles and `.nfo` files move with their video and keep language tags (`.en.srt`, `.en.forced.srt`).
- Characters that SMB and Windows don't allow (`: ? * < > | "`) are replaced.
