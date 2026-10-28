#!/usr/bin/env python3
"""
plex_organize.py - Move media files into Plex-compliant folders.

Walks a (network) folder and moves every video file into its own folder,
named the way Plex expects:

    Movies:   Inception.2010.1080p.BluRay.x264-GRP.mkv
          ->  Inception (2010)/Inception (2010).mkv

    Multi-part movies (cd1/part1/disc1...):
              Kill.Bill.2003.CD1.avi
          ->  Kill Bill (2003)/Kill Bill (2003) - pt1.avi

    TV episodes (SxxEyy) - Plex needs Show/Season folders, not one folder
    per file, so episodes are grouped instead:
              the.office.us.s02e05.720p.mkv
          ->  The Office Us/Season 02/The Office Us - s02e05.mkv

Matching subtitles / .nfo files (e.g. Inception.2010.1080p.en.srt) are moved
along with their video and renamed to match (Inception (2010).en.srt).

By default this is a DRY RUN: it only prints what it would do.
Add --apply to actually move files. Every applied run writes an undo log
that can be replayed with --undo.

Works with mounted shares (/mnt/nas/Movies, /Volumes/Media) and Windows
UNC paths (\\\\NAS\\Movies). Requires Python 3.8+, no extra packages.
"""

import argparse
import csv
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

VIDEO_EXTS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts",
    ".m2ts", ".mts", ".webm", ".flv", ".vob", ".divx", ".ogm", ".3gp", ".iso",
}
SIDECAR_EXTS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi", ".sup", ".nfo"}

# Folders that NAS devices / OSes create and that should never be touched.
SKIP_DIRS = {"@eadir", "#recycle", "#snapshot", ".appledouble", "$recycle.bin",
             "system volume information", ".trash", ".trashes", "lost+found"}

# Release/quality tags. Everything from the first one onward is junk.
JUNK_TAGS = (
    r"2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr|hdr10|dv|dolby\s?vision|"
    r"bluray|blu-ray|bdrip|brrip|bdremux|remux|web-?dl|webrip|web|hdtv|hdrip|"
    r"dvdrip|dvdscr|dvd|r5|cam|ts|telesync|x264|x265|h\s?264|h\s?265|hevc|avc|"
    r"xvid|divx|aac|ac3|eac3|dts|dts-hd|truehd|atmos|ddp?5\s?1|"
    r"10bit|8bit|proper|repack|internal|limited|extended|unrated|uncut|"
    r"remastered|imax|multi|dual|subbed|dubbed|yts|yify|rarbg"
)
JUNK_RE = re.compile(rf"(?:^|\s)(?:{JUNK_TAGS})(?=\s|$)", re.IGNORECASE)
YEAR_RE = re.compile(r"(?:^|[\s(\[])((?:19|20)\d{2})(?=[\s)\]]|$)")
EPISODE_RE = re.compile(r"[\s.\-_]s(\d{1,2})[\s.\-_]?e(\d{1,3})(?:[\s.\-_]?e(\d{1,3}))?", re.IGNORECASE)
EPISODE_X_RE = re.compile(r"[\s.\-_](\d{1,2})x(\d{2,3})(?=[\s.\-_]|$)", re.IGNORECASE)
PART_RE = re.compile(r"[\s.\-_\[(](?:cd|disc|disk|dvd|part|pt)[\s.\-_]?(\d{1,2})(?=[\s.\-_\])]|$)", re.IGNORECASE)
SAMPLE_RE = re.compile(r"(?:^|[\s.\-_])sample(?:[\s.\-_]|$)", re.IGNORECASE)
# Characters illegal on Windows/SMB shares.
ILLEGAL_CHARS_RE = re.compile(r'[<>"/\\|?*\x00-\x1f]')


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


def parse_media(stem: str):
    """
    Returns a dict describing the media, or None if no title could be found.
      kind='movie': title, year, part
      kind='tv':    show, season, episode, episode2
    """
    padded = " " + stem
    m = EPISODE_RE.search(padded) or EPISODE_X_RE.search(padded)
    if m:
        show_raw = normalize(padded[: m.start()])
        year = None
        ym = list(YEAR_RE.finditer(show_raw))
        if ym and ym[-1].start() > 0:
            year = ym[-1].group(1)
            show_raw = show_raw[: ym[-1].start()]
        show = clean_title(show_raw)
        if not show:
            return None
        if year:
            show = f"{show} ({year})"
        groups = m.groups()
        return {
            "kind": "tv",
            "show": sanitize(show),
            "season": int(groups[0]),
            "episode": int(groups[1]),
            "episode2": int(groups[2]) if len(groups) > 2 and groups[2] else None,
        }

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


def plex_target(info: dict, ext: str):
    """Return (relative folder Path, new file stem) for parsed media."""
    if info["kind"] == "tv":
        ep = f"s{info['season']:02d}e{info['episode']:02d}"
        if info["episode2"]:
            ep += f"-e{info['episode2']:02d}"
        folder = Path(info["show"]) / f"Season {info['season']:02d}"
        return folder, f"{info['show']} - {ep}"

    base = info["title"] + (f" ({info['year']})" if info["year"] else "")
    stem = base + (f" - pt{info['part']}" if info["part"] else "")
    return Path(base), stem


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


def organize(root: Path, dest: Path, apply: bool, recursive: bool,
             tv: bool, cleanup: bool, log_rows: list):
    stats = {"moved": 0, "ok": 0, "skipped": 0}
    videos = find_videos(root, recursive)
    print(f"Found {len(videos)} video file(s) under {root}\n")

    for video in videos:
        if SAMPLE_RE.search(video.stem):
            print(f"{video}\n  ! SKIP (sample file)")
            stats["skipped"] += 1
            continue

        info = parse_media(video.stem)
        if info is None:
            print(f"{video}\n  ! SKIP (could not work out a title)")
            stats["skipped"] += 1
            continue
        if info["kind"] == "tv" and not tv:
            print(f"{video}\n  ! SKIP (TV episode; use --tv to organize into Show/Season NN)")
            stats["skipped"] += 1
            continue

        rel_folder, new_stem = plex_target(info, video.suffix)
        target_dir = dest / rel_folder
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
        try:
            dst.parent.rmdir()  # remove the folder we created if now empty
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Move each video file into its own Plex-compliant folder.",
        epilog="Runs as a dry run unless --apply is given.")
    ap.add_argument("folder", nargs="?", help="Folder / network share to organize")
    ap.add_argument("--apply", action="store_true",
                    help="Actually move files (default is a dry run)")
    ap.add_argument("--dest", help="Put the new folders here instead of inside FOLDER")
    ap.add_argument("--no-recurse", action="store_true",
                    help="Only look at files directly inside FOLDER")
    ap.add_argument("--tv", action="store_true",
                    help="Also organize TV episodes into 'Show/Season NN/' (skipped by default)")
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

    if not args.apply:
        print("=== DRY RUN - nothing will be moved. Re-run with --apply to do it. ===\n")

    log_rows = []
    try:
        stats = organize(root, dest, args.apply, not args.no_recurse,
                         args.tv, not args.no_cleanup, log_rows)
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
