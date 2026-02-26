# recorder.py
# Windows console recorder using PyAV (FFmpeg) + DirectShow, with:
#   - Background keyboard listener (global 'R' to start/stop; also 'Q'/'Esc' to stop)
#   - Optional real-time preview (OpenCV)
#   - Three modes: Automatic (one-shot / daily), Manual (R start/stop), Hybrid (R start + auto-stop)
#   - Segment rolling (partial saves), per-session CSV log, and USB backup
#
# Prereqs:
#   pip install av opencv-python keyboard
# Notes:
#   - 'keyboard' may require running terminal as Administrator on Windows to receive global hotkeys.
#   - Set CONFIG.device_name to exact DirectShow device name (e.g., 'video=USB2.0 PC CAMERA').

import os
import csv
import time
import shutil
import signal
import threading
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Tuple

import av  # PyAV

# Optional preview dependency
try:
    import cv2
    HAVE_CV2 = True
except Exception:
    HAVE_CV2 = False

# Background keyboard listener
try:
    import keyboard  # pip install keyboard
    HAVE_KEYBOARD = True
except Exception:
    HAVE_KEYBOARD = False

# ----------------------------
# CONFIGURATION
# ----------------------------

@dataclass
class Config:
    # --- Camera (DirectShow device string must start with "video=") ---
    device_name: str = 'video=USB2.0 PC CAMERA'  # e.g., 'video=USB2.0 PC CAMERA'
    width: int = 640
    height: int = 480
    fps: int = 30
    # Input options for DirectShow; if your device worked “no options” in tests, keep it minimal:
    input_options: Optional[dict] = None  # e.g., {"video_size": "640x480", "framerate": "30"}

    # --- Output profile ---
    #   "mp4_h264"  -> H.264 in MP4  (recommended; .mp4)
    #   "mpeg_ps"   -> MPEG-2 Program Stream (.mpeg)
    output_profile: str = "mp4_h264"

    # H.264 encoder params
    h264_codec: str = "libx264"      # if missing in your PyAV build, try "h264"
    h264_pix_fmt: str = "yuv420p"
    h264_crf: int = 23
    h264_preset: str = "veryfast"
    h264_ext: str = ".mp4"

    # MPEG-2 PS params
    mpeg2_codec: str = "mpeg2video"
    mpeg2_pix_fmt: str = "yuv420p"   # some builds prefer "yuv422p"
    mpeg2_bitrate: int = 8_000_000
    mpeg2_ext: str = ".mpeg"         # as requested

    # --- Segmenting (partial saves) ---
    segment_seconds: Optional[int] = 60  # None = single file; e.g., 60 => new file every minute

    # --- Storage paths ---
    recordings_dir: str = os.path.join(os.getcwd(), "recordings")
    backup_dir: Optional[str] = r"E:\recordings_backup"  # Set your thumb drive path; None disables

    # --- Default durations ---
    default_duration_sec: int = 3600  # 1 hour

    # --- Time zone for filenames & schedule ---
    tz_name: str = "America/New_York"  # uses zoneinfo if available

    # --- Preview ---
    preview_enabled: bool = True
    preview_window_title: str = "Preview (Press Q or Esc to stop)"
    preview_resize: Optional[Tuple[int, int]] = (800, 600)  # None for native size

CONFIG = Config()

# ----------------------------
# Time helpers
# ----------------------------

def get_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(CONFIG.tz_name)
    except Exception:
        return None

TZ = get_tz()

def now_local():
    return dt.datetime.now(TZ) if TZ else dt.datetime.now()

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def build_session_basename(start_dt: dt.datetime) -> str:
    # yyyy_mm_dd_recording_start-hr_minute_second
    return start_dt.strftime("%Y_%m_%d_recording_start-%H-%M-%S")

def build_segment_path(root: str, base: str, ext: str, seg_idx: int) -> str:
    # part00, part01, ... (keeps the requested base + safe suffix)
    if seg_idx == 0:
        return os.path.join(root, f"{base}{ext}")
    return os.path.join(root, f"{base}_part{seg_idx:02d}{ext}")

def human_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

# ----------------------------
# Session logger (per-session CSV)
# ----------------------------

class SessionLogger:
    def __init__(self, recordings_dir: str, base_name: str):
        ensure_dir(recordings_dir)
        self.path = os.path.join(recordings_dir, f"{base_name}.log.csv")
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        if os.path.getsize(self.path) == 0:
            self._w.writerow(["ts_local", "event", "info"])
        self.flush()

    def write(self, event: str, info: str = ""):
        self._w.writerow([now_local().isoformat(timespec="seconds"), event, info])
        self.flush()

    def flush(self):
        try:
            self._fh.flush()
        except Exception:
            pass

    def close(self):
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

# ----------------------------
# Output open/close
# ----------------------------

def open_output(path: str, fps: int):
    """Open output container + encoded stream per CONFIG.output_profile."""
    oc = av.open(path, mode="w")
    if CONFIG.output_profile == "mp4_h264":
        st = oc.add_stream(CONFIG.h264_codec, rate=fps)
        cc = getattr(st, "codec_context", None)
        if cc:
            cc.width = CONFIG.width
            cc.height = CONFIG.height
            cc.pix_fmt = CONFIG.h264_pix_fmt
            try:
                cc.options = {"crf": str(CONFIG.h264_crf), "preset": CONFIG.h264_preset}
            except Exception:
                pass
        else:
            st.width = CONFIG.width
            st.height = CONFIG.height
            st.pix_fmt = CONFIG.h264_pix_fmt

    elif CONFIG.output_profile == "mpeg_ps":
        st = oc.add_stream(CONFIG.mpeg2_codec, rate=fps)
        cc = getattr(st, "codec_context", None)
        if cc:
            cc.width = CONFIG.width
            cc.height = CONFIG.height
            cc.pix_fmt = CONFIG.mpeg2_pix_fmt
            cc.bit_rate = CONFIG.mpeg2_bitrate
        else:
            st.width = CONFIG.width
            st.height = CONFIG.height
            st.pix_fmt = CONFIG.mpeg2_pix_fmt
    else:
        oc.close()
        raise ValueError(f"Unknown output_profile: {CONFIG.output_profile}")

    return oc, st

def output_extension() -> str:
    return CONFIG.h264_ext if CONFIG.output_profile == "mp4_h264" else CONFIG.mpeg2_ext

# ----------------------------
# Camera input
# ----------------------------

def open_camera():
    """
    Open the DirectShow camera via PyAV. Device name must be 'video=EXACT NAME'.
    """
    opts = CONFIG.input_options or {
        "video_size": f"{CONFIG.width}x{CONFIG.height}",
        "framerate": str(CONFIG.fps),
    }
    ic = av.open(CONFIG.device_name, format="dshow", options=opts)
    vstream = next((s for s in ic.streams if s.type == "video"), None)
    if vstream is None:
        ic.close()
        raise RuntimeError("No video stream found on device.")
    return ic, vstream

# ----------------------------
# Background keyboard controller (global hotkeys)
# ----------------------------

class KeyboardController:
    """
    Global 'R' toggles start/stop.
    Also accept 'Q'/'Esc' to stop.
    Uses 'keyboard' library if available; otherwise no-ops (you can still stop by Ctrl+C).
    """
    def __init__(self):
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self._hooks = []
        self._fallback_thread = None
        self._running = False

    def _on_r(self, event=None):
        if not self.start_event.is_set():
            print("[KEY] Start detected ('R')")
            self.start_event.set()
        else:
            print("[KEY] Stop detected ('R')")
            self.stop_event.set()

    def _on_stop_key(self, event=None):
        print("[KEY] Stop detected ('Q'/'Esc')")
        self.stop_event.set()

    def start(self):
        if self._running:
            return
        self._running = True
        if HAVE_KEYBOARD:
            self._hooks.append(keyboard.on_press_key("r", self._on_r))
            self._hooks.append(keyboard.on_press_key("q", self._on_stop_key))
            self._hooks.append(keyboard.on_press_key("esc", self._on_stop_key))
        else:
            # Minimal fallback: no global hook. (Stop with Ctrl+C.)
            def poll():
                print("[WARN] 'keyboard' module not installed; global hotkeys disabled. Use Ctrl+C to stop.")
                while self._running:
                    time.sleep(0.1)
            self._fallback_thread = threading.Thread(target=poll, daemon=True)
            self._fallback_thread.start()

    def clear(self):
        self.start_event.clear()
        self.stop_event.clear()

    def stop(self):
        if HAVE_KEYBOARD:
            for h in self._hooks:
                try:
                    keyboard.unhook(h)
                except Exception:
                    pass
            self._hooks.clear()
        self._running = False

# ----------------------------
# Preview helpers
# ----------------------------

def _preview_init():
    if not (CONFIG.preview_enabled and HAVE_CV2):
        return False
    cv2.namedWindow(CONFIG.preview_window_title, cv2.WINDOW_NORMAL)
    if CONFIG.preview_resize:
        w, h = CONFIG.preview_resize
        cv2.resizeWindow(CONFIG.preview_window_title, w, h)
    return True

def _preview_show(frame):
    """Display frame; note: we do NOT read keys from OpenCV. Global keyboard handles keys."""
    if not (CONFIG.preview_enabled and HAVE_CV2):
        return
    bgr = frame.to_ndarray(format="bgr24")
    cv2.imshow(CONFIG.preview_window_title, bgr)
    # Pump window events
    cv2.waitKey(1)

def _preview_teardown():
    if HAVE_CV2:
        try:
            cv2.destroyWindow(CONFIG.preview_window_title)
        except Exception:
            pass

# ----------------------------
# Recorder core (one session)
# ----------------------------

def _backup(path: str, logger: SessionLogger):
    """Copy a finished segment/file to backup_dir if configured and available."""
    if not CONFIG.backup_dir:
        return
    try:
        if not os.path.isdir(CONFIG.backup_dir):
            logger.write("backup_skip", f"missing: {CONFIG.backup_dir}")
            return
        ensure_dir(CONFIG.backup_dir)
        dst = os.path.join(CONFIG.backup_dir, os.path.basename(path))
        shutil.copy2(path, dst)
        logger.write("backup_ok", f"{path} -> {dst}")
        print(f"[INFO] Backed up to {dst}")
    except Exception as e:
        logger.write("backup_fail", f"{path}: {e}")
        print(f"[WARN] Backup failed: {e}")

def record_session(duration_sec: Optional[int],
                   logger: SessionLogger,
                   kb: Optional[KeyboardController] = None,
                   scheduled_start: Optional[dt.datetime] = None,
                   enable_preview: Optional[bool] = None):
    """
    - If scheduled_start is in the future, waits until then.
    - If duration_sec is provided, auto-stops after that duration (from actual start).
    - If kb is provided, 'R' toggles start/stop, 'Q'/'Esc' stop early.
    - Rolls segments every CONFIG.segment_seconds (if set).
    """
    if enable_preview is None:
        enable_preview = CONFIG.preview_enabled and HAVE_CV2

    # Wait until scheduled start (if any)
    if scheduled_start:
        now = now_local()
        if scheduled_start > now:
            to_wait = (scheduled_start - now).total_seconds()
            logger.write("waiting_until_start", f"{scheduled_start.isoformat()} ({human_dur(to_wait)})")
            print(f"[INFO] Waiting {human_dur(to_wait)} until {scheduled_start.isoformat()} to start...")
            while to_wait > 0:
                time.sleep(min(1.0, to_wait))
                now = now_local()
                to_wait = (scheduled_start - now).total_seconds()

    # Open camera
    ic, vstream = open_camera()
    print(f"[INFO] Camera opened: {CONFIG.device_name} at {CONFIG.width}x{CONFIG.height}@{CONFIG.fps}")
    logger.write("camera_open", f"{CONFIG.device_name} {CONFIG.width}x{CONFIG.height}@{CONFIG.fps}")

    if enable_preview:
        _preview_init()

    # Prepare session base & writer
    start_dt = now_local()
    base = build_session_basename(start_dt)
    ext = output_extension()

    seg_idx = 0
    seg_path = build_segment_path(CONFIG.recordings_dir, base, ext, seg_idx)
    oc, ost = open_output(seg_path, CONFIG.fps)
    logger.write("segment_open", seg_path)
    print(f"[INFO] -> Writing to: {os.path.basename(seg_path)}")

    seg_open_time = time.time()
    total_frames = 0
    started_clock = time.perf_counter()
    auto_stop_deadline = (started_clock + duration_sec) if duration_sec else None

    try:
        for packet in ic.demux(vstream):
            for frame in packet.decode():
                # Stop conditions (keyboard)
                if kb and kb.stop_event.is_set():
                    raise KeyboardInterrupt

                # Stop conditions (auto duration)
                if auto_stop_deadline is not None and time.perf_counter() >= auto_stop_deadline:
                    logger.write("auto_stop", f"duration_reached {human_dur(duration_sec)}")
                    raise KeyboardInterrupt

                # Preview (no key handling here)
                if enable_preview:
                    _preview_show(frame)

                # Reformat if needed for encoder
                need_reformat = (
                    frame.width != CONFIG.width or frame.height != CONFIG.height or
                    (CONFIG.output_profile == "mp4_h264" and frame.format.name != CONFIG.h264_pix_fmt) or
                    (CONFIG.output_profile == "mpeg_ps" and frame.format.name != CONFIG.mpeg2_pix_fmt)
                )
                if need_reformat:
                    enc_frame = frame.reformat(
                        width=CONFIG.width, height=CONFIG.height,
                        format=(CONFIG.h264_pix_fmt if CONFIG.output_profile == "mp4_h264" else CONFIG.mpeg2_pix_fmt)
                    )
                else:
                    enc_frame = frame

                # Encode & mux
                for pkt in ost.encode(enc_frame):
                    oc.mux(pkt)
                total_frames += 1

                # Segment rotation
                if CONFIG.segment_seconds:
                    if (time.time() - seg_open_time) >= CONFIG.segment_seconds:
                        # flush & close current
                        for pkt in ost.encode(None):
                            oc.mux(pkt)
                        oc.close()
                        _backup(seg_path, logger)
                        logger.write("segment_close", seg_path)

                        # next
                        seg_idx += 1
                        seg_path = build_segment_path(CONFIG.recordings_dir, base, ext, seg_idx)
                        oc, ost = open_output(seg_path, CONFIG.fps)
                        logger.write("segment_open", seg_path)
                        print(f"[INFO] -> Rolling to: {os.path.basename(seg_path)}")
                        seg_open_time = time.time()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.write("error", str(e))
        print(f"[ERROR] Recording error: {e}")
    finally:
        # Flush and close the current file
        try:
            for pkt in ost.encode(None):
                oc.mux(pkt)
        except Exception:
            pass
        try:
            oc.close()
        except Exception:
            pass
        _backup(seg_path, logger)
        logger.write("segment_close", f"{seg_path} (final)")
        try:
            ic.close()
        except Exception:
            pass
        if enable_preview:
            _preview_teardown()

        end_dt = now_local()
        dur = (end_dt - start_dt).total_seconds()
        logger.write("session_end", f"frames={total_frames} duration={human_dur(dur)}")
        print(f"[INFO] Session ended. Duration {human_dur(dur)}, frames: {total_frames}")

# ----------------------------
# Modes (Manual/Hyrbid/Automatic)
# ----------------------------

def mode_manual_start_stop():
    """
    Mode 2: Manual — Press R to start, Press R again to stop.
    (Also 'Q' or Esc will stop recording.)
    """
    # Keyboard listener
    kb = KeyboardController()
    kb.clear()
    kb.start()

    print("\n[MANUAL] Press R to START recording...")
    print("         Then press R again (or Q/Esc) to STOP.")
    # Wait for R to start
    while not kb.start_event.is_set():
        time.sleep(0.05)

    # Start session
    base_dt = now_local()
    base = build_session_basename(base_dt)
    logger = SessionLogger(CONFIG.recordings_dir, base)
    logger.write("session_start", "manual")
    try:
        record_session(duration_sec=None, logger=logger, kb=kb)
    finally:
        kb.stop()
        logger.close()

def mode_hybrid_auto_stop():
    """
    Mode 3: Hybrid — Press R to start; auto-stop after duration (still allow R/Q/Esc to stop early).
    """
    dur = read_int("Duration (seconds)", default=CONFIG.default_duration_sec)

    kb = KeyboardController()
    kb.clear()
    kb.start()

    print("\n[HYBRID] Press R to START; will auto-stop after duration.")
    print("         You can also press R again (or Q/Esc) to stop early.")
    while not kb.start_event.is_set():
        time.sleep(0.05)

    base_dt = now_local()
    base = build_session_basename(base_dt)
    logger = SessionLogger(CONFIG.recordings_dir, base)
    logger.write("session_start", f"hybrid duration={dur}s")
    try:
        record_session(duration_sec=dur, logger=logger, kb=kb)
    finally:
        kb.stop()
        logger.close()

def mode_automatic_one_shot():
    """
    Mode 1 (one-shot): Start at specified time; run for duration (no R key needed).
    """
    t_str = read_time("Start time (HH:MM)", default=None)
    dur = read_int("Duration (seconds)", default=CONFIG.default_duration_sec)
    start_dt = compute_next_datetime_today_or_tomorrow(t_str)
    print(f"[AUTO] Scheduled for {start_dt.isoformat()} for {dur}s")

    base_dt = start_dt
    base = build_session_basename(base_dt)
    logger = SessionLogger(CONFIG.recordings_dir, base)
    logger.write("session_planned", f"start={start_dt.isoformat()} duration={dur}s")
    try:
        record_session(duration_sec=dur, logger=logger, kb=None, scheduled_start=start_dt)
    finally:
        logger.close()

def mode_automatic_daily():
    """
    Mode 1 (daily): Between dates; each day starts at time and runs for duration (or until end time).
    """
    sd = read_date("Start date (YYYY-MM-DD)", default=now_local().date().isoformat())
    ed = read_date("End date   (YYYY-MM-DD)", default=now_local().date().isoformat())
    st = read_time("Daily start time (HH:MM)", default="09:00")
    use_duration = yesno("Specify a duration? (y) Or use end time? (n)", default=True)
    if use_duration:
        dur = read_int("Duration (seconds)", default=CONFIG.default_duration_sec)
        for day in daterange(sd, ed):
            start_dt = combine_day_time(day, st)
            if start_dt <= now_local():
                continue
            base = build_session_basename(start_dt)
            logger = SessionLogger(CONFIG.recordings_dir, base)
            logger.write("session_planned", f"daily start={start_dt.isoformat()} duration={dur}s")
            try:
                record_session(duration_sec=dur, logger=logger, kb=None, scheduled_start=start_dt)
            finally:
                logger.close()
    else:
        et = read_time("Daily END time (HH:MM)", default="10:00")
        for day in daterange(sd, ed):
            start_dt = combine_day_time(day, st)
            end_dt = combine_day_time(day, et)
            dur = max(0, int((end_dt - start_dt).total_seconds()))
            if dur <= 0 or start_dt <= now_local():
                continue
            base = build_session_basename(start_dt)
            logger = SessionLogger(CONFIG.recordings_dir, base)
            logger.write("session_planned", f"daily start={start_dt.isoformat()} duration={dur}s")
            try:
                record_session(duration_sec=dur, logger=logger, kb=None, scheduled_start=start_dt)
            finally:
                logger.close()

# ----------------------------
# CLI helpers
# ----------------------------

def read_int(prompt: str, default: Optional[int] = None) -> int:
    while True:
        s = input(f"{prompt}" + (f" [default {default}]:" if default is not None else ": ")).strip()
        if not s and default is not None:
            return default
        try:
            return int(s)
        except ValueError:
            print("Please enter a whole number.")

def read_time(prompt: str, default: Optional[str]) -> dt.time:
    while True:
        s = input(f"{prompt}" + (f" [default {default}]:" if default else ": ")).strip()
        if not s and default:
            s = default
        try:
            h, m = map(int, s.split(":"))
            return dt.time(hour=h, minute=m)
        except Exception:
            print("Enter time as HH:MM, e.g. 08:30")

def read_date(prompt: str, default: Optional[str]) -> dt.date:
    while True:
        s = input(f"{prompt}" + (f" [default {default}]:" if default else ": ")).strip()
        if not s and default:
            s = default
        try:
            y, m, d = map(int, s.split("-"))
            return dt.date(y, m, d)
        except Exception:
            print("Enter date as YYYY-MM-DD")

def yesno(prompt: str, default: bool) -> bool:
    s = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not s:
        return default
    return s in ("y", "yes")

def combine_day_time(day: dt.date, t: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, t, tzinfo=TZ) if TZ else dt.datetime.combine(day, t)

def daterange(d1: dt.date, d2: dt.date):
    d = d1
    while d <= d2:
        yield d
        d += dt.timedelta(days=1)

def compute_next_datetime_today_or_tomorrow(t: dt.time) -> dt.datetime:
    now = now_local()
    start_dt = combine_day_time(now.date(), t)
    if start_dt <= now:
        start_dt += dt.timedelta(days=1)
    return start_dt

# ----------------------------
# Main menu
# ----------------------------

def main():
    ensure_dir(CONFIG.recordings_dir)
    print("=== USB Camera Recorder (Windows, PyAV) ===")
    print(f"Device     : {CONFIG.device_name}")
    print(f"Resolution : {CONFIG.width}x{CONFIG.height}@{CONFIG.fps}")
    print(f"Profile    : {CONFIG.output_profile} (ext {output_extension()})")
    print(f"Segments   : {'every '+str(CONFIG.segment_seconds)+'s' if CONFIG.segment_seconds else 'single file'}")
    print(f"Output dir : {CONFIG.recordings_dir}")
    print(f"Backup dir : {CONFIG.backup_dir or '(disabled)'}")
    print(f"Preview    : {'ON' if (CONFIG.preview_enabled and HAVE_CV2) else 'OFF'}")
    if CONFIG.preview_enabled and not HAVE_CV2:
        print("  (OpenCV not installed; preview disabled. Install: pip install opencv-python)")
    if not HAVE_KEYBOARD:
        print("  (Global hotkeys disabled. Install: pip install keyboard)")
    print("-------------------------------------------")
    print("1) Manual: R to start, R to stop (Mode 2)")
    print("2) Manual: R to start, auto-stop after duration (Mode 3)")
    print("3) Automatic: daily between dates (Mode 1)")
    print("4) Automatic: one-shot start at time (Mode 1)")
    print("q) Quit")
    choice = input("Select: ").strip().lower()

    # Friendly SIGINT (Ctrl+C) handling
    signal.signal(signal.SIGINT, lambda s, f: (_ for _ in ()).throw(KeyboardInterrupt))

    try:
        if choice == "1":
            mode_manual_start_stop()
        elif choice == "2":
            mode_hybrid_auto_stop()
        elif choice == "3":
            mode_automatic_daily()
        elif choice == "4":
            mode_automatic_one_shot()
        else:
            print("Bye.")
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")

if __name__ == "__main__":
    # For driver/device debugging, uncomment:
    # av.logging.set_level(av.logging.VERBOSE)
    main()
