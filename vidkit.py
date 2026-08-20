#!/usr/bin/env python3
"""vidkit - inspect a video's full metadata and re-encode to efficient HEVC.

Requires: ffprobe + ffmpeg (VideoToolbox HEVC on Apple Silicon).

Subcommands:
  vidkit info    <file>            show all available metadata
  vidkit convert <file|dir>        re-encode to HEVC (hardware by default)

Run `vidkit <cmd> -h` for options.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

FFPROBE = shutil.which("ffprobe") or "ffprobe"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".ts", ".m2ts", ".webm"}


def die(msg, code=1):
    print(f"vidkit: error: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path, extra=None):
    cmd = [FFPROBE, "-v", "error", "-of", "json",
           "-show_format", "-show_streams", "-show_chapters", "-show_programs"]
    if extra:
        cmd += extra
    cmd.append(path)
    r = run(cmd)
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def hsize(b):
    if b is None:
        return "?"
    b = float(b)
    for u in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} EB"


def hdur(s):
    try:
        s = float(s)
    except (TypeError, ValueError):
        return "?"
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h}h{m:02d}m{sec:04.1f}s"


def hbr(bps):
    if not bps:
        return "?"
    bps = float(bps)
    for u in ["bps", "kbps", "Mbps", "Gbps"]:
        if bps < 1000:
            return f"{bps:.0f} {u}"
        bps /= 1000
    return f"{bps:.0f} Gbps"


def fps_of(rate):
    if not rate or "/" not in rate:
        return rate
    n, d = rate.split("/")
    try:
        return round(int(n) / int(d), 3)
    except Exception:
        return rate


def video_codec_of(d):
    for s in (d or {}).get("streams", []):
        if s.get("codec_type") == "video":
            return s.get("codec_name")
    return None


def audio_is_aac_of(d):
    for s in (d or {}).get("streams", []):
        if s.get("codec_type") == "audio":
            return s.get("codec_name") == "aac"
    return False


def fmt_clk(sec):
    if sec is None or sec < 0 or sec != sec:
        return "--:--"
    sec = int(max(sec, 0))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def cmd_info(args):
    path = args.file
    if not os.path.isfile(path):
        die(f"not a file: {path}")
    raw = args.json or args.full
    if raw:
        extra = ["-show_packets", "-show_frames"] if args.full else None
        d = probe(path, extra=extra)
        print(json.dumps(d or {}, indent=2, ensure_ascii=False))
        return
    d = probe(path)
    if not d:
        die("ffprobe could not read this file (corrupt or unsupported?)")
    fmt = d.get("format", {})
    print("=" * 78)
    print(f"FILE: {path}")
    print(f"  size      = {hsize(os.path.getsize(path))}")
    print(f"  duration  = {hdur(fmt.get('duration'))} ({fmt.get('duration', '?')}s)")
    print(f"  bitrate   = {hbr(fmt.get('bit_rate'))}")
    print(f"  format    = {fmt.get('format_long_name') or fmt.get('format_name', '?')}")
    tags = fmt.get("tags") or {}
    if tags:
        print("  FORMAT TAGS:")
        for k, v in tags.items():
            print(f"    {k} = {v}")
    for s in d.get("streams", []):
        print("-" * 78)
        idx = s.get("index")
        ct = s.get("codec_type", "?")
        name = s.get("codec_name", "?")
        longn = s.get("codec_long_name", "")
        print(f"STREAM #{idx} [{ct}] {name}  profile={s.get('profile', '')}")
        if longn:
            print(f"  long_name = {longn}")
        if ct == "video":
            print(f"  {s.get('width')}x{s.get('height')}  fps={fps_of(s.get('r_frame_rate'))}  "
                  f"pix_fmt={s.get('pix_fmt')}  bit_rate={hbr(s.get('bit_rate'))}")
            print(f"  color: space={s.get('color_space')} transfer={s.get('color_transfer')} "
                  f"primaries={s.get('color_primaries')} range={s.get('color_range')}")
            if s.get("level") is not None:
                print(f"  level={s.get('level')}  has_b_frames={s.get('has_b_frames')}")
        elif ct == "audio":
            print(f"  sample_rate={s.get('sample_rate')}Hz  channels={s.get('channels')}  "
                  f"bit_rate={hbr(s.get('bit_rate'))}")
        stags = s.get("tags") or {}
        if stags:
            print("  STREAM TAGS:")
            for k, v in stags.items():
                print(f"    {k} = {v}")
    ch = d.get("chapters") or []
    if ch:
        print("-" * 78)
        print(f"CHAPTERS: {len(ch)}")
        for c in ch:
            t = (c.get("tags") or {}).get("title", "")
            print(f"  #{c.get('id')} {c.get('start_time')} -> {c.get('end_time')}  {t}")
    print("=" * 78)


def gather_inputs(target):
    if os.path.isfile(target):
        return [target]
    if os.path.isdir(target):
        out = []
        for e in sorted(os.listdir(target)):
            p = os.path.join(target, e)
            if os.path.isfile(p) and os.path.splitext(e)[1].lower() in VIDEO_EXTS:
                out.append(p)
        return out
    die(f"not a file or directory: {target}")


def video_codec(path):
    d = probe(path)
    if not d:
        return None
    for s in d.get("streams", []):
        if s.get("codec_type") == "video":
            return s.get("codec_name")
    return None


def audio_is_aac(path):
    d = probe(path)
    if not d:
        return False
    for s in d.get("streams", []):
        if s.get("codec_type") == "audio":
            return s.get("codec_name") == "aac"
    return False


def encoder_label(args):
    e = args.encoder
    if e in ("media", "gpu", "hw"):
        return f"hevc_videotoolbox (media engine, q={args.quality})"
    if e == "nvenc":
        return f"hevc_nvenc (Nvidia GPU, CQ {args.cq}, p5)"
    if e == "cpu":
        return f"libx265 (CPU, CRF {args.crf})"
    if e == "av1":
        return f"libsvtav1 (CPU AV1, CRF {args.av1_crf} p{args.av1_preset})"
    return e


def target_codec(args):
    e = args.encoder
    if e == "av1":
        return "av1"
    return "hevc"


def build_cmd(infile, outfile, args):
    cmd = [FFMPEG, "-hide_banner", "-y", "-i", infile]
    e = args.encoder
    if e in ("media", "gpu", "hw"):
        cmd += ["-c:v", "hevc_videotoolbox", "-q:v", str(args.quality), "-tag:v", "hvc1"]
    elif e == "nvenc":
        cmd += [
            "-c:v", "hevc_nvenc",
            "-preset", "p5",
            "-rc", "vbr",
            "-cq", str(args.cq),
            "-b:v", "0",
            "-spatial-aq", "1",
            "-temporal-aq", "1",
            "-tag:v", "hvc1"
        ]
    elif e == "cpu":
        cmd += ["-c:v", "libx265", "-crf", str(args.crf), "-preset", "fast", "-tag:v", "hvc1"]
    elif e == "av1":
        cmd += ["-c:v", "libsvtav1", "-preset", str(args.av1_preset), "-crf", str(args.av1_crf)]
    else:
        die(f"unknown encoder: {e}")
    if args.scale:
        cmd += ["-vf", f"scale={args.scale}"]
    if args.audio_bitrate:
        cmd += ["-c:a", "aac", "-b:a", str(args.audio_bitrate) + "k"]
    elif args.audio == "copy":
        cmd += ["-c:a", "copy"]
    elif args.audio == "reencode":
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        if audio_is_aac(infile):
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", "-map", "0", "-map", "-0:d?", outfile]
    return cmd


def cmd_convert(args):
    inputs = gather_inputs(args.target)
    if not inputs:
        die("no video files found")
    if args.out_dir:
        out_dir = args.out_dir
    elif os.path.isdir(args.target):
        out_dir = os.path.join(args.target, "hevc")
    else:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(args.target)), "hevc")
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)

    enc_label = encoder_label(args)
    if args.encoder in ("gpu", "hw"):
        print("note: '--encoder gpu/hw' uses the Apple media engine (hevc_videotoolbox); "
              "there is no separate GPU-shader video encoder on Apple Silicon.")

    jobs = []
    for path in inputs:
        stem = os.path.splitext(os.path.basename(path))[0]
        outfile = os.path.join(out_dir, f"{stem}.{args.ext}")
        size = os.path.getsize(path) if os.path.exists(path) else 0
        job = {"path": path, "outfile": outfile, "skip": None, "dur": 0.0, "size": size}
        d = probe(path)
        if d is None:
            job["skip"] = "could not probe (corrupt file?)"
        else:
            vc = video_codec_of(d)
            if vc is None:
                job["skip"] = "no video stream"
            elif vc in args.skip_codec:
                job["skip"] = f"skipping codec {vc} (--skip-codec)"
            elif vc == target_codec(args) and not args.force:
                job["skip"] = f"already {target_codec(args).upper()}"
            elif os.path.exists(outfile) and not args.force:
                job["skip"] = "output exists"
            else:
                try:
                    job["dur"] = float((d.get("format") or {}).get("duration") or 0)
                except (TypeError, ValueError):
                    job["dur"] = 0.0
        jobs.append(job)

    todo = [j for j in jobs if not j["skip"]]
    batch_total_dur = sum(j["dur"] for j in todo)
    batch_total_size = sum(j["size"] for j in todo)
    n = len(todo)

    if args.dry_run:
        for j in jobs:
            print("\n" + "=" * 78)
            print(f"INPUT : {os.path.basename(j['path'])}  ({hsize(j['size'])})")
            if j["skip"]:
                print(f"  -> SKIP: {j['skip']}")
                continue
            print(f"OUTPUT: {j['outfile']}")
            print(f"ENCODER: {enc_label}")
            cmd = build_cmd(j["path"], j["outfile"], args)
            print("CMD   : " + " ".join('"%s"' % c if " " in c else c for c in cmd))
        print("\n(dry run: no files written)")
        return

    done = failed = skipped = 0
    total_after = 0
    batch_start = time.time()
    processed_dur = 0.0
    encode_idx = 0
    tty = sys.stdout.isatty()

    for idx, j in enumerate(jobs):
        print("\n" + "=" * 78)
        print(f"INPUT : {os.path.basename(j['path'])}  ({hsize(j['size'])})")
        if j["skip"]:
            print(f"  -> SKIP: {j['skip']}")
            skipped += 1
            continue
        print(f"OUTPUT: {j['outfile']}")
        print(f"ENCODER: {enc_label}")
        encode_idx += 1

        core = build_cmd(j["path"], j["outfile"], args)
        cmdp = core[:-1] + ["-nostats", "-progress", "pipe:1"] + [core[-1]]

        start = time.time()
        proc = subprocess.Popen(cmdp, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        err_data = []

        def drain():
            try:
                for line in proc.stderr:
                    err_data.append(line)
            except Exception:
                pass

        t = threading.Thread(target=drain, daemon=True)
        t.start()

        out_t = 0.0
        speed = 0.0
        out_size = 0
        last_disp = 0.0
        for line in proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "out_time_us":
                try:
                    out_t = float(v) / 1_000_000
                except ValueError:
                    pass
            elif k == "speed":
                m = re.search(r"([\d.]+)", v)
                speed = float(m.group(1)) if m else 0.0
            elif k == "total_size" and v.isdigit():
                out_size = int(v)
            elif k == "progress":
                now = time.time()
                is_end = v == "end"
                if now - last_disp >= (0.25 if tty else 5.0) or is_end:
                    dur = j["dur"]
                    fp = (out_t / dur) if dur > 0 else None
                    fe = now - start
                    feta = (fe * (1 - fp) / fp) if (fp and fp > 0.001) else None
                    bp = ((processed_dur + out_t) / batch_total_dur) if batch_total_dur > 0 else None
                    be = now - batch_start
                    beta = (be * (1 - bp) / bp) if (bp and bp > 0.001) else None
                    fpc = f"{fp*100:3.0f}%" if fp is not None else "  ?%"
                    bpc = f"{bp*100:2.0f}%" if bp is not None else " ?%"
                    stat = (f"[#{encode_idx}/{n}] {fpc} {fmt_clk(out_t)}/{fmt_clk(dur)} "
                            f"{speed:4.1f}x ETA {fmt_clk(feta)} {hsize(out_size)} | "
                            f"batch {bpc} ETA {fmt_clk(beta)}")
                    if tty:
                        sys.stdout.write("\r  " + stat + "   ")
                        sys.stdout.flush()
                    else:
                        print("  " + stat)
                    last_disp = now
        proc.wait()
        t.join(timeout=1.0)
        if tty:
            sys.stdout.write("\n")
            sys.stdout.flush()

        if proc.returncode != 0 or not os.path.exists(j["outfile"]):
            tail = "\n".join("".join(err_data).splitlines()[-6:]) or "(no stderr)"
            print(f"  -> FAILED (ffmpeg error): {tail}")
            failed += 1
            continue
        before = j["size"]
        after = os.path.getsize(j["outfile"])
        total_after += after
        saved = 100 * (1 - after / before) if before else 0
        print(f"  -> DONE  {hsize(before)} -> {hsize(after)}   saved {saved:.1f}%   "
              f"({time.time()-start:.0f}s, {speed:.1f}x)")
        done += 1
        processed_dur += j["dur"]

    print("\n" + "=" * 78)
    print(f"BATCH SUMMARY: {done} encoded, {skipped} skipped, {failed} failed")
    if done:
        print(f"  total {hsize(batch_total_size)} -> {hsize(total_after)}   "
              f"saved {100*(1-total_after/batch_total_size):.1f}%   ({time.time()-batch_start:.0f}s)")
    print(f"  output dir: {out_dir}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="vidkit", description="inspect + re-encode videos to HEVC")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="show full metadata for a video file")
    p_info.add_argument("file")
    p_info.add_argument("--json", action="store_true", help="print raw ffprobe JSON")
    p_info.add_argument("--full", action="store_true", help="include packet/frame-level data (large)")
    p_info.set_defaults(func=cmd_info)

    p_conv = sub.add_parser("convert", help="re-encode videos (media engine HEVC by default)")
    p_conv.add_argument("target", help="file or directory")
    p_conv.add_argument("--out-dir", help="output directory (default: <source>/hevc)")
    p_conv.add_argument("--encoder", choices=["media", "gpu", "hw", "nvenc", "cpu", "av1"], default="media",
                        help="encoder backend. media=hevc_videotoolbox (default, Apple media engine); "
                             "gpu/hw=alias for media; nvenc=hevc_nvenc (Nvidia GPU); "
                             "cpu=libx265 software; av1=libsvtav1 software. Default media")
    p_conv.add_argument("--quality", type=int, default=50, help="VideoToolbox quality 1-100 (higher=better). Default 50")
    p_conv.add_argument("--cq", type=int, default=28, help="Constant Quality for Nvidia NVENC (1-51, lower=better). Default 28")
    p_conv.add_argument("--crf", type=int, default=24, help="CRF for libx265 (lower=better). Default 24")
    p_conv.add_argument("--av1-crf", type=int, default=35, help="CRF for libsvtav1. Default 35")
    p_conv.add_argument("--av1-preset", type=int, default=8, help="libsvtav1 preset (0=slowest/best, 13=fastest). Default 8")
    p_conv.add_argument("--audio", choices=["auto", "copy", "reencode"], default="auto",
                        help="audio handling (ignored if --audio-bitrate set). auto=copy if AAC else re-encode. Default auto")
    p_conv.add_argument("--audio-bitrate", type=int, default=None, help="re-encode audio to AAC at this kbit/s (e.g. 64)")
    p_conv.add_argument("--ext", default="mp4", choices=["mp4", "mov"], help="output container. Default mp4")
    p_conv.add_argument("--force", action="store_true", help="overwrite / re-encode even if target codec or output exists")
    p_conv.add_argument("--scale", default=None, help="downscale video to WxH (e.g. 852:480) before encoding")
    p_conv.add_argument("--skip-codec", action="append", default=[], choices=["h264", "hevc", "av1"],
                        help="skip inputs whose video codec matches (repeatable, e.g. --skip-codec av1)")
    p_conv.add_argument("--dry-run", action="store_true", help="print planned commands, write nothing")
    p_conv.set_defaults(func=cmd_convert)

    args = ap.parse_args(argv)
    if not shutil.which(FFMPEG):
        die("ffmpeg/ffprobe not found on PATH")
    args.func(args)


if __name__ == "__main__":
    main()
