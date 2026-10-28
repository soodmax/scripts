#!/usr/bin/env python3
"""
plex_organize.py - Move media files into Plex-compliant folders.

Walks a (network) folder and moves every video file into the folder layout
Plex expects:

    Movies:   Inception.2010.1080p.BluRay.x264-GRP.mkv
          ->  Inception (2010)/Inception (2010).mkv

    Multi-part movies (cd1/part1/disc1...):
              Kill.Bill.2003.CD1.avi
          ->  Kill Bill (2003)/Kill Bill (2003) - pt1.avi

    TV episodes (SxxEyy / 1x02), with --tv:
              the.office.us.s02e05.the.dundies.720p.mkv
          ->  The Office Us/Season 02/The Office Us - s02e05 - The Dundies.mkv

    Sports (league + date / "vs" / round...), with --sports:
              NFL.2024.09.08.Chiefs.vs.Ravens.720p.mkv
          ->  NFL/Season 2024/NFL - 2024-09-08 - Chiefs vs Ravens.mkv

Use --mixed on a root media folder that holds all of the above: each type
is sorted into its own library folder (Movies/, TV Shows/, Sports/), so you
can point one Plex library at each.

Matching subtitles / .nfo files (e.g. Inception.2010.1080p.en.srt) are moved
along with their video and renamed to match (Inception (2010).en.srt).

By default this is a DRY RUN: it only prints what it would do.
Add --apply to actually move files. Every applied run writes an undo log
that can be replayed with --undo.

Works with mounted shares (/mnt/nas/Media, /Volumes/Media) and Windows
UNC paths (\\\\NAS\\Media). Requires Python 3.8+, no extra packages.
"""

import argparse
import csv
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts",
    ".m2ts", ".mts", ".webm", ".flv", ".vob", ".divx", ".ogm", ".3gp", ".iso",
}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi", ".sup", ".nfo"}

# Folders that NAS devices / OSes create and that should never be touched.
SKIP_DIRS = {"@eadir", "#recycle", "#snapshot", ".appledouble", "$recycle.bin",
             "system volume information", ".trash", ".trashes", "lost+found"}

# Plex "local extras" folders/suffixes - these belong where they are.
EXTRAS_DIRS = {"behind the scenes", "deleted scenes", "featurettes", "interviews",
               "scenes", "shorts", "trailers", "other", "extras"}
EXTRAS_SUFFIX_RE = re.compile(
    r"-(?:behindthescenes|deleted|featurette|interview|scene|short|trailer|other)$",
    re.IGNORECASE)
SEASON_DIR_RE = re.compile(
    r"^(?:season|series|staffel|saison|temporada)\s*\d{1,3}$|^specials$", re.IGNORECASE)

# Release/quality tags. Everything from the first one onward is junk.
JUNK_TAGS = (
    r"2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr|hdr10|dv|dolby\s?vision|"
    r"bluray|blu-ray|bdrip|brrip|bdremux|remux|web-?dl|webrip|web|hdtv|hdrip|"
    r"dvdrip|dvdscr|dvd|r5|cam|ts|telesync|x264|x265|h\s?264|h\s?265|hevc|avc|"
    r"xvid|divx|aac|ac3|eac3|dts|dts-hd|truehd|atmos|ddp?5\s?1|"
    r"10bit|8bit|proper|repack|internal|limited|extended|unrated|uncut|"
    r"remastered|imax|multi|dual|subbed|dubbed|yts|yify|rarbg|60fps|50fps"
)
JUNK_RE = re.compile(rf"(?:^|\s)(?:{JUNK_TAGS})(?=\s|$)", re.IGNORECASE)
YEAR_RE = re.compile(r"(?:^|[\s(\[])((?:19|20)\d{2})(?=[\s)\]]|$)")
EPISODE_RE = re.compile(r"[\s.\-_]s(\d{1,2})[\s.\-_]?e(\d{1,3})(?:[\s.\-_]?e(\d{1,3}))?", re.IGNORECASE)
EPISODE_X_RE = re.compile(r"[\s.\-_](\d{1,2})x(\d{2,3})(?=[\s.\-_]|$)", re.IGNORECASE)
PART_RE = re.compile(r"[\s.\-_\[(](?:cd|disc|disk|dvd|part|pt)[\s.\-_]?(\d{1,2})(?=[\s.\-_\])]|$)", re.IGNORECASE)
SAMPLE_RE = re.compile(r"(?:^|[\s.\-_])sample(?:[\s.\-_]|$)", re.IGNORECASE)
# Characters illegal on Windows/SMB shares.
ILLEGAL_CHARS_RE = re.compile(r'[<>"/\\|?*\x00-\x1f]')

# Sports: league/competition name patterns -> folder name.
SPORTS_LEAGUES = [
    (r"nfl|super\s?bowl", "NFL"), (r"nba", "NBA"), (r"wnba", "WNBA"), (r"nhl", "NHL"),
    (r"mlb", "MLB"), (r"mls", "MLS"), (r"ncaa[fb]?|college\s?football", "NCAA"),
    (r"ufc", "UFC"), (r"bellator", "Bellator"), (r"pfl", "PFL"), (r"boxing", "Boxing"),
    (r"wwe|wrestlemania|summerslam", "WWE"), (r"aew", "AEW"),
    (r"f1|formula\s?1|formula\s?one", "Formula 1"), (r"f2|formula\s?2", "Formula 2"),
    (r"motogp", "MotoGP"), (r"nascar", "NASCAR"), (r"indycar", "IndyCar"), (r"wrc", "WRC"),
    (r"epl|premier\s?league", "Premier League"), (r"la\s?liga", "La Liga"),
    (r"serie\s?a", "Serie A"), (r"bundesliga", "Bundesliga"), (r"ligue\s?1", "Ligue 1"),
    (r"ucl|uefa\s?champions\s?league|champions\s?league", "UEFA Champions League"),
    (r"uel|europa\s?league", "UEFA Europa League"), (r"fifa\s?world\s?cup|world\s?cup", "FIFA World Cup"),
    (r"pga(?:\s?tour)?", "PGA Tour"), (r"atp", "ATP"), (r"wta", "WTA"), (r"wimbledon", "Wimbledon"),
    (r"ipl", "IPL"), (r"cricket", "Cricket"), (r"six\s?nations", "Six Nations"),
    (r"olympics", "Olympics"),
]
LEAGUE_RE = re.compile(
    r"(?:^|\s)(?:" + "|".join(f"(?P<l{i}>{p})" for i, (p, _) in enumerate(SPORTS_LEAGUES)) + r")(?=\s|$)",
    re.IGNORECASE)
# Words that show a league-named file really is a game/event, not e.g. the movie "F1 (2025)".
SPORTS_CONTEXT_RE = re.compile(
    r"(?:^|\s)(?:vs?|versus|@|week\s?\d+|wk\s?\d+|round\s?\d+|r\d{1,2}|race|qualifying|quali|"
    r"practice|fp[123]|sprint|grand\s?prix|gp|game\s?\d+|match(?:day)?|finals?|semi\s?finals?|"
    r"playoffs?|highlights|ppv|fight\s?night|main\s?card|prelims|\d{1,3})(?=\s|$)",
    re.IGNORECASE)
DATE_YMD_RE = re.compile(r"(?:^|\s)((?:19|20)\d{2})[\s-](\d{1,2})[\s-](\d{1,2})(?=\s|$)")
DATE_DMY_RE = re.compile(r"(?:^|\s)(\d{1,2})[\s-](\d{1,2})[\s-]((?:19|20)\d{2})(?=\s|$)")

KINDS = ("movie", "tv", "sports")


# --------------------------------------------------------------------------- #
# Name parsing
# --------------------------------------------------------------------------- #

def normalize(stem: str) -> str:
    """Turn 'The.Movie_Name-[YTS.MX]' into 'The Movie Name'."""
    s = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", stem)          # [tags] and {tags}
    s = s.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -")


def clean_title(raw: str) -> str:
    title = JUNK_RE.split(raw, maxsplit=1)[0]                # cut at first junk tag
    title = re.sub(r"[(\[]\s*[)\]]", " ", title)             # leftover empty brackets
    title = re.sub(r"\s+", " ", title).strip(" -([")
    if title and (title.islower() or title.isupper()):
        title = smart_title(title)
    return title


def smart_title(s: str) -> str:
    small = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of",
             "on", "or", "the", "to", "vs", "with"}
    words = s.lower().split(" ")
    out = []
    for i, w in enumerate(words):
        if i and w in small:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def sanitize(name: str) -> str:
    """Make a name safe for Windows/SMB/NFS shares."""
    name = name.replace(":", " -")
    name = ILLEGAL_CHARS_RE.sub("", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" .")


def find_date(s: str):
    """Return (date, match) for the first valid YYYY-MM-DD or DD-MM-YYYY in s."""
    for rx, order in ((DATE_YMD_RE, (0, 1, 2)), (DATE_DMY_RE, (2, 1, 0))):
        for m in rx.finditer(s):
            y, mo, d = (int(m.group(i + 1)) for i in order)
            try:
                return date(y, mo, d), m
            except ValueError:
                continue
    return None, None


def episode_match(stem: str):
    padded = " " + stem
    return padded, (EPISODE_RE.search(padded) or EPISODE_X_RE.search(padded))


def parse_tv(stem: str, folder_show: str = None):
    """TV episode -> dict, or None if the name has no SxxEyy/1x02 marker."""
    padded, m = episode_match(stem)
    if not m:
        return None
    show_raw = normalize(padded[: m.start()])
    year = None
    ym = list(YEAR_RE.finditer(show_raw))
    if ym and ym[-1].start() > 0:
        year = ym[-1].group(1)
        show_raw = show_raw[: ym[-1].start()]
    show = clean_title(show_raw)
    if year and show:
        show = f"{show} ({year})"
    # A "Show/Season 01/" folder is a more reliable show name than the file name.
    show = folder_show or show
    if not show:
        return None

    rest = normalize(padded[m.end():])
    if re.fullmatch(r"[A-Z0-9]+", rest):        # lone release-group tag like "GRP"
        rest = ""
    ep_title = clean_title(rest) if rest else ""

    groups = m.groups()
    return {
        "kind": "tv",
        "show": sanitize(show),
        "season": int(groups[0]),
        "episode": int(groups[1]),
        "episode2": int(groups[2]) if len(groups) > 2 and groups[2] else None,
        "title": sanitize(ep_title),
    }


def parse_sports(stem: str, force: bool = False, folder_league: str = None):
    """
    Sports event -> dict, or None. A file counts as sports when it names a
    known league AND has a date or game-ish words ("vs", "Week 3", "Race"...).
    With force=True (file already lives in the Sports folder) the league may
    come from the folder instead.
    """
    s = normalize(" " + stem)
    lm = LEAGUE_RE.search(s)
    d, dm = find_date(s)
    if lm:
        league = next(SPORTS_LEAGUES[int(k[1:])][1] for k, v in lm.groupdict().items() if v)
    else:
        league = folder_league
    if not league:
        return None
    if not force and not (lm and (dm or SPORTS_CONTEXT_RE.search(s[: lm.start()] + s[lm.end():]))):
        return None

    if dm:
        # Everything except the league name and the date is the event title.
        spans = sorted([(dm.start(), dm.end())] + ([(lm.start(), lm.end())] if lm else []))
        parts, pos = [], 0
        for a, b in spans:
            parts.append(s[pos:a])
            pos = b
        parts.append(s[pos:])
        title = clean_title(" ".join(parts).strip())
        return {"kind": "sports", "league": sanitize(league), "date": d,
                "year": d.year, "title": sanitize(title)}

    # No date: keep the whole (cleaned) name, with the league spelled consistently.
    full = s[: lm.start()] + " " + league + " " + s[lm.end():] if lm else s
    name = clean_title(full.strip())
    ym = YEAR_RE.search(name)
    if lm and not name.lower().startswith(league.lower()):
        name = f"{league} - {name}"
    return {"kind": "sports", "league": sanitize(league), "date": None,
            "year": int(ym.group(1)) if ym else None, "title": sanitize(name)}


def parse_movie(stem: str):
    padded = " " + stem
    part = None
    pm = PART_RE.search(padded)
    if pm:
        part = int(pm.group(1))
        padded = padded[: pm.start()] + " " + padded[pm.end():]

    s = normalize(padded)
    year = None
    # Use the last plausible year that isn't the very first word,
    # so "2001 A Space Odyssey 1968" -> title "2001 A Space Odyssey", year 1968.
    for ym in reversed(list(YEAR_RE.finditer(s))):
        if ym.start(1) > 0 and int(ym.group(1)) <= datetime.now().year + 1:
            year = ym.group(1)
            s = s[: ym.start()]
            break

    title = sanitize(clean_title(s))
    if not title:
        return None
    return {"kind": "movie", "title": title, "year": year, "part": part}


def plex_target(info: dict):
    """Return (relative folder Path, new file stem) for parsed media."""
    if info["kind"] == "tv":
        ep = f"s{info['season']:02d}e{info['episode']:02d}"
        if info["episode2"]:
            ep += f"-e{info['episode2']:02d}"
        stem = f"{info['show']} - {ep}" + (f" - {info['title']}" if info["title"] else "")
        return Path(info["show"]) / f"Season {info['season']:02d}", stem

    if info["kind"] == "sports":
        folder = Path(info["league"])
        if info["year"]:
            folder /= f"Season {info['year']}"
        if info["date"]:
            stem = f"{info['league']} - {info['date'].isoformat()}"
            stem += f" - {info['title']}" if info["title"] else ""
        else:
            stem = info["title"]
        return folder, stem

    base = info["title"] + (f" ({info['year']})" if info["year"] else "")
    stem = base + (f" - pt{info['part']}" if info["part"] else "")
    return Path(base), stem


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def classify(video: Path, root: Path, lib_dirs: dict):
    """
    Work out what a file is. Returns (info, None) or (None, reason-to-skip).
    lib_dirs maps kind -> library folder name (only used with --mixed),
    so files already sitting in e.g. 'TV Shows/' are treated as TV.
    """
    stem, parent = video.stem, video.parent
    if SAMPLE_RE.search(stem):
        return None, "sample file"
    if parent.name.lower() in EXTRAS_DIRS or EXTRAS_SUFFIX_RE.search(stem):
        return None, "Plex extra (trailer/featurette...), left in place"

    rel = video.relative_to(root).parts[:-1]
    hint = None
    if rel:
        for kind, name in lib_dirs.items():
            if rel[0].lower() == name.lower():
                hint = kind
    in_season_dir = bool(SEASON_DIR_RE.match(parent.name))

    # TV: SxxEyy marker, or inside a "Season NN" folder.
    folder_show = None
    if in_season_dir and parent.parent != root and parent.parent.name.lower() not in \
            {n.lower() for n in lib_dirs.values()}:
        folder_show = parent.parent.name
    info = parse_tv(stem, folder_show)
    if info:
        return info, None
    if in_season_dir or hint == "tv":
        return None, "looks like TV but has no SxxEyy episode number"

    # Sports
    folder_league = None
    if hint == "sports" and len(rel) > 1:
        folder_league = rel[1]
    info = parse_sports(stem, force=(hint == "sports"), folder_league=folder_league)
    if info:
        return info, None
    if hint == "sports":
        return None, "in the sports folder but no league could be worked out"

    info = parse_movie(stem)
    if info:
        return info, None
    return None, "could not work out a title"


# --------------------------------------------------------------------------- #
# File discovery
# --------------------------------------------------------------------------- #

def find_videos(root: Path, recursive: bool):
    if not recursive:
        return sorted(p for p in root.iterdir()
                      if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                      and not p.name.startswith("."))
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if not f.startswith(".") and Path(f).suffix.lower() in VIDEO_EXTS:
                found.append(Path(dirpath) / f)
    return sorted(found)


def find_sidecars(video: Path):
    """Subtitle/nfo files in the same folder whose name starts with the video's stem."""
    stem = video.stem.lower()
    result = []
    try:
        for p in video.parent.iterdir():
            if (p.is_file() and p.suffix.lower() in SIDECAR_EXTS
                    and p.name.lower().startswith(stem)):
                # e.g. ".en.forced" from "Movie.2010.en.forced.srt"
                extra = p.name[len(video.stem): -len(p.suffix)]
                if extra == "" or extra.startswith("."):
                    result.append((p, extra))
    except OSError:
        pass
    return result


# --------------------------------------------------------------------------- #
# Moving
# --------------------------------------------------------------------------- #

def same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except OSError:
        return False


def move(src: Path, dst: Path, apply: bool, log_rows: list) -> bool:
    if same_path(src, dst):
        return False
    # Case-only rename on a case-insensitive share: dst "exists" but is src itself.
    if dst.exists() and not (src.exists() and os.path.samefile(src, dst)):
        print(f"  ! SKIP (target exists): {dst}")
        return False
    print(f"{src}\n  -> {dst}")
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():  # case-only rename: go via a temp name
            tmp = dst.with_name(dst.name + ".plextmp")
            os.rename(src, tmp)
            os.rename(tmp, dst)
        else:
            shutil.move(str(src), str(dst))
        log_rows.append((str(src), str(dst)))
    return True


def remove_empty_dirs(start: Path, stop: Path):
    """Remove now-empty folders left behind (only below the root)."""
    d = start
    while not same_path(d, stop) and stop in d.parents:
        try:
            # ignore junk files that keep a folder "non-empty"
            leftovers = [p for p in d.iterdir()
                         if p.name.lower() not in {"thumbs.db", ".ds_store", "desktop.ini"}]
            if leftovers:
                return
            for p in d.iterdir():
                p.unlink()
            d.rmdir()
            print(f"  removed empty folder: {d}")
        except OSError:
            return
        d = d.parent


def organize(root: Path, dest: Path, apply: bool, recursive: bool, enabled: set,
             lib_dirs: dict, cleanup: bool, log_rows: list):
    stats = {"moved": 0, "ok": 0, "skipped": 0}
    videos = find_videos(root, recursive)
    print(f"Found {len(videos)} video file(s) under {root}\n")

    for video in videos:
        info, reason = classify(video, root, lib_dirs)
        if info is None:
            print(f"{video}\n  ! SKIP ({reason})")
            stats["skipped"] += 1
            continue
        if info["kind"] not in enabled:
            print(f"{video}\n  ! SKIP ({info['kind']} not enabled; see --tv / --sports / --mixed)")
            stats["skipped"] += 1
            continue

        rel_folder, new_stem = plex_target(info)
        target_dir = dest / lib_dirs.get(info["kind"], "") / rel_folder
        # Without --mixed, a movie already in a correctly named folder stays
        # where it is (e.g. Movies/Kids/Frozen (2013)/) instead of being pulled up.
        if info["kind"] == "movie" and not lib_dirs and video.parent.name == rel_folder.name:
            target_dir = video.parent
        target = target_dir / (new_stem + video.suffix.lower())

        if same_path(video, target):
            stats["ok"] += 1
            continue

        sidecars = find_sidecars(video)
        old_parent = video.parent
        if move(video, target, apply, log_rows):
            stats["moved"] += 1
            for sc, extra in sidecars:
                move(sc, target_dir / (new_stem + extra + sc.suffix.lower()), apply, log_rows)
            if apply and cleanup:
                remove_empty_dirs(old_parent, root)
        else:
            stats["skipped"] += 1

    return stats


def undo(log_file: Path):
    with open(log_file, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f, delimiter="\t"))[1:]
    for src, dst in reversed(rows):
        src, dst = Path(src), Path(dst)
        if not dst.exists():
            print(f"! missing, cannot undo: {dst}")
            continue
        if src.exists():
            print(f"! original location occupied, skipping: {src}")
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(src))
        print(f"{dst}\n  -> {src}")
        d = dst.parent
        while True:  # remove the folders we created if now empty
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def main():
    ap = argparse.ArgumentParser(
        description="Move video files into Plex-compliant folders.",
        epilog="Runs as a dry run unless --apply is given.")
    ap.add_argument("folder", nargs="?", help="Folder / network share to organize")
    ap.add_argument("--apply", action="store_true",
                    help="Actually move files (default is a dry run)")
    ap.add_argument("--mixed", action="store_true",
                    help="FOLDER holds movies, TV and sports: sort each into its own "
                         "library folder (see --movies-dir/--tv-dir/--sports-dir)")
    ap.add_argument("--tv", action="store_true",
                    help="Organize TV episodes into 'Show/Season NN/' (skipped by default)")
    ap.add_argument("--sports", action="store_true",
                    help="Organize sports into 'League/Season YYYY/' (skipped by default)")
    ap.add_argument("--no-movies", action="store_true", help="Leave movies alone")
    ap.add_argument("--movies-dir", default="Movies", help="Library folder for --mixed (default: Movies)")
    ap.add_argument("--tv-dir", default="TV Shows", help="Library folder for --mixed (default: 'TV Shows')")
    ap.add_argument("--sports-dir", default="Sports", help="Library folder for --mixed (default: Sports)")
    ap.add_argument("--dest", help="Put the organized files here instead of inside FOLDER")
    ap.add_argument("--no-recurse", action="store_true",
                    help="Only look at files directly inside FOLDER")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="Don't delete folders that become empty after moving")
    ap.add_argument("--log", help="Where to write the undo log (default: inside FOLDER)")
    ap.add_argument("--undo", metavar="LOGFILE", help="Reverse a previous --apply run")
    args = ap.parse_args()

    if args.undo:
        undo(Path(args.undo))
        return
    if not args.folder:
        ap.error("folder is required")

    root = Path(args.folder)
    if not root.is_dir():
        sys.exit(f"Not a folder or not reachable: {root}")
    dest = Path(args.dest) if args.dest else root

    enabled = {"movie"} if not args.no_movies else set()
    if args.tv or args.mixed:
        enabled.add("tv")
    if args.sports or args.mixed:
        enabled.add("sports")
    lib_dirs = ({"movie": args.movies_dir, "tv": args.tv_dir, "sports": args.sports_dir}
                if args.mixed else {})

    if not args.apply:
        print("=== DRY RUN - nothing will be moved. Re-run with --apply to do it. ===\n")

    log_rows = []
    try:
        stats = organize(root, dest, args.apply, not args.no_recurse, enabled,
                         lib_dirs, not args.no_cleanup, log_rows)
    finally:
        if log_rows:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = Path(args.log) if args.log else root / f"plex_organize_{stamp}.tsv"
            with open(log_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter="\t")
                w.writerow(["original", "new"])
                w.writerows(log_rows)
            print(f"\nUndo log written to: {log_path}")
            print(f"To revert: python {Path(sys.argv[0]).name} --undo \"{log_path}\"")

    verb = "Moved" if args.apply else "Would move"
    print(f"\n{verb}: {stats['moved']}  |  Already OK: {stats['ok']}  |  Skipped: {stats['skipped']}")


if __name__ == "__main__":
    main()
