# recorder_gui.py
# Windows GUI recorder using Tkinter + PyAV (FFmpeg) + DirectShow.
# Panels:
#   - Manual panel (Start/Stop, R toggles). Disabled during a scheduled recording.
#   - Scheduling panel driven by "Repeat" dropdown:
#       * One shot
#       * Every N days (1..7), with End Date (inclusive)
#     Buttons:
#       * Set Scheduled Recording(s)
#       * Cancel Schedule
#       * End Scheduled Recording Now (with warning prompt)
#
# Features:
#   - Live preview (Pillow)
#   - Writes a SINGLE full session file per recording (no partial/segment files)
#   - Optional backup of the full file at the end of recording
#   - Per-session CSV logs
#   - Esc quits the app; ending any recording never closes the program.
#
# Prereqs:
#   pip install av pillow
#
# IMPORTANT:
#   Set CONFIG.device_name to the exact DirectShow device name, e.g., 'video=USB2.0 PC CAMERA'.

import os
import csv
import time
import shutil
import threading
import queue
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageTk
import av  # PyAV

# ----------------------------
# Configuration
# ----------------------------

@dataclass
class Config:
    # Camera (DirectShow requires 'video=EXACT NAME')
    device_name: str = 'video=USB2.0 PC CAMERA'
    width: int = 640
    height: int = 480
    fps: int = 30
    input_options: Optional[dict] = None  # e.g., {"video_size": "640x480", "framerate": "30"}

    # Output profile
    #   "mp4_h264"  -> H.264 in MP4 (.mp4)  [recommended]
    #   "mpeg_ps"   -> MPEG-2 Program Stream (.mpeg)
    output_profile: str = "mp4_h264"

    # H.264 settings
    h264_codec: str = "libx264"      # try "h264" if libx264 missing
    h264_pix_fmt: str = "yuv420p"
    h264_crf: int = 23
    h264_preset: str = "veryfast"
    h264_ext: str = ".mp4"

    # MPEG-2 PS settings
    mpeg2_codec: str = "mpeg2video"
    mpeg2_pix_fmt: str = "yuv420p"   # some builds prefer "yuv422p"
    mpeg2_bitrate: int = 8_000_000
    mpeg2_ext: str = ".mpeg"

    # Storage
    recordings_dir: str = os.path.join(os.getcwd(), "recordings")
    backup_dir: Optional[str] = r"E:\recordings_backup"  # Set your thumb drive; None disables backup

    # Defaults
    default_duration_sec: int = 3600  # 1 hour

    # Time zone / filenames
    tz_name: str = "America/New_York"  # uses zoneinfo if present

    # Preview
    preview_enabled_default: bool = True
    preview_size: Tuple[int, int] = (800, 600)  # display size in GUI

CONFIG = Config()

# ----------------------------
# Time helpers & logging
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

def output_extension() -> str:
    return CONFIG.h264_ext if CONFIG.output_profile == "mp4_h264" else CONFIG.mpeg2_ext

def human_dur(seconds: float) -> str:
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

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
# PyAV open/close helpers
# ----------------------------

def open_camera():
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

def open_output(path: str, fps: int):
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

# ----------------------------
# Recording engine (thread) — FULL FILE ONLY
# ----------------------------

class RecorderEngine(threading.Thread):
    """
    PyAV capture/encode loop on a background thread.
    - Updates preview frames into a queue (numpy RGB) => GUI thread displays it.
    - Writes a single FULL session file per recording; no partial/segment files.
    - Optionally backs up the full file at the end.
    """
    def __init__(self,
                 preview_queue: queue.Queue,
                 logger: SessionLogger,
                 duration_sec: Optional[int] = None,
                 preview_enabled: bool = True):
        super().__init__(daemon=True)
        self.preview_queue = preview_queue
        self.logger = logger
        self.duration_sec = duration_sec
        self.preview_enabled = preview_enabled
        self._stop_evt = threading.Event()
        self.error: Optional[str] = None
        self.summary: Optional[str] = None
        self.full_path: Optional[str] = None

    def request_stop(self):
        self._stop_evt.set()

    def _backup(self, path: str):
        if not CONFIG.backup_dir:
            return
        try:
            if not os.path.isdir(CONFIG.backup_dir):
                self.logger.write("backup_skip", f"missing: {CONFIG.backup_dir}")
                return
            ensure_dir(CONFIG.backup_dir)
            dst = os.path.join(CONFIG.backup_dir, os.path.basename(path))
            shutil.copy2(path, dst)
            self.logger.write("backup_ok", f"{path} -> {dst}")
        except Exception as e:
            self.logger.write("backup_fail", f"{path}: {e}")

    def run(self):
        try:
            self._record()
        except Exception as e:
            self.error = str(e)

    def _record(self):
        ic, vstream = open_camera()
        self.logger.write("camera_open", f"{CONFIG.device_name} {CONFIG.width}x{CONFIG.height}@{CONFIG.fps}")

        start_dt = now_local()
        base = build_session_basename(start_dt)
        ext = output_extension()

        # FULL session file
        self.full_path = os.path.join(CONFIG.recordings_dir, f"{base}{ext}")
        oc, ost = open_output(self.full_path, CONFIG.fps)
        self.logger.write("full_open", self.full_path)

        total_frames = 0
        t0 = time.perf_counter()
        deadline = t0 + self.duration_sec if self.duration_sec else None

        try:
            for packet in ic.demux(vstream):
                for frame in packet.decode():
                    # Stop conditions
                    if self._stop_evt.is_set():
                        raise KeyboardInterrupt
                    if deadline is not None and time.perf_counter() >= deadline:
                        self.logger.write("auto_stop", f"duration_reached {human_dur(self.duration_sec)}")
                        raise KeyboardInterrupt

                    # Preview
                    if self.preview_enabled:
                        try:
                            rgb = frame.to_ndarray(format="rgb24")
                            # Drop older frames to avoid lag
                            try:
                                while True:
                                    self.preview_queue.get_nowait()
                            except queue.Empty:
                                pass
                            self.preview_queue.put_nowait((rgb, time.time()))
                        except Exception:
                            pass

                    # Reformat for encoder if needed
                    need_reformat = (
                        frame.width != CONFIG.width or frame.height != CONFIG.height or
                        (CONFIG.output_profile == "mp4_h264" and frame.format.name != CONFIG.h264_pix_fmt) or
                        (CONFIG.output_profile == "mpeg_ps" and frame.format.name != CONFIG.mpeg2_pix_fmt)
                    )
                    enc_frame = frame.reformat(
                        width=CONFIG.width,
                        height=CONFIG.height,
                        format=(CONFIG.h264_pix_fmt if CONFIG.output_profile == "mp4_h264" else CONFIG.mpeg2_pix_fmt)
                    ) if need_reformat else frame

                    # Encode & mux
                    for pkt in ost.encode(enc_frame):
                        oc.mux(pkt)
                    total_frames += 1

        except KeyboardInterrupt:
            pass
        finally:
            # Flush & close full
            try:
                for pkt in ost.encode(None):
                    oc.mux(pkt)
            except Exception:
                pass
            try:
                oc.close()
            except Exception:
                pass
            self.logger.write("full_close", self.full_path)

            # Close camera
            try:
                ic.close()
            except Exception:
                pass

            # Backup the full file (optional)
            if self.full_path and os.path.exists(self.full_path):
                self._backup(self.full_path)

            end_dt = now_local()
            dur = (end_dt - start_dt).total_seconds()
            self.logger.write("session_end", f"frames={total_frames} duration={human_dur(dur)}")
            self.summary = f"Recorded {human_dur(dur)}, frames: {total_frames}"

# ----------------------------
# Repeating scheduler (thread)
# ----------------------------

class RepeatingScheduler(threading.Thread):
    """
    Repeats a one-shot start every N days (1..7) between start_date and end_date inclusive.
    - Skips a run if the app is already recording (logs skip).
    - Calls back into the Tk main thread to start a scheduled session (auto-stop by duration).
    """
    def __init__(self, app, start_dt: dt.datetime, end_date: dt.date, interval_days: int, duration_sec: int):
        super().__init__(daemon=True)
        self.app = app
        self.start_dt = start_dt
        self.end_date = end_date
        self.interval_days = max(1, min(7, int(interval_days)))
        self.duration_sec = max(1, int(duration_sec))
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        now = now_local()
        next_dt = self.start_dt
        while next_dt < now:
            next_dt += dt.timedelta(days=self.interval_days)

        while not self._stop_evt.is_set() and next_dt.date() <= self.end_date:
            # Sleep until next_dt
            while not self._stop_evt.is_set():
                now = now_local()
                delta = (next_dt - now).total_seconds()
                if delta <= 0:
                    break
                time.sleep(min(1.0, delta))
            if self._stop_evt.is_set():
                break

            def _start_if_idle():
                if self.app.recording:
                    self.app._log_info(f"Scheduled run skipped {next_dt.isoformat()} (busy).")
                    return
                self.app._start_scheduled_run(duration_sec=self.duration_sec, label=f"Scheduled {next_dt.isoformat()}")

            self.app.after(0, _start_if_idle)
            next_dt += dt.timedelta(days=self.interval_days)

        self.app.after(0, lambda: self.app._update_status("Repeating schedule complete."))

# ----------------------------
# Tkinter GUI
# ----------------------------

class RecorderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("USB Camera Recorder (PyAV + Tkinter)")
        self.geometry("1120x840")
        self.protocol("WM_DELETE_WINDOW", self._on_close_clicked)

        # State
        self.preview_enabled = tk.BooleanVar(value=CONFIG.preview_enabled_default)
        self.duration_minutes = tk.IntVar(value=CONFIG.default_duration_sec // 60)

        # Scheduling inputs
        self.sched_date = tk.StringVar(value=now_local().date().isoformat())
        self.sched_time = tk.StringVar(value=now_local().strftime("%H:%M"))
        self.sched_end_time = tk.StringVar(value="")  # optional
        self.sched_end_date = tk.StringVar(value=now_local().date().isoformat())
        self.repeat_choice = tk.StringVar(value="One shot")  # One shot or Every N days

        self.status = tk.StringVar(value="Idle")
        self.recording = False
        self.recording_origin: Optional[str] = None  # 'manual' or 'scheduled'
        self.engine: Optional[RecorderEngine] = None
        self.preview_q: queue.Queue = queue.Queue(maxsize=2)
        self.current_image_tk = None
        self.logger: Optional[SessionLogger] = None
        self.scheduler: Optional[RepeatingScheduler] = None

        self._build_ui()
        self._bind_keys()
        self._poll_preview_queue()

    # UI layout
    def _build_ui(self):
        header = ttk.Frame(self, padding=8)
        header.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(header, text=f"Device: {CONFIG.device_name}").pack(side=tk.LEFT, padx=4)
        ttk.Label(header, text=f"Format: {CONFIG.width}x{CONFIG.height}@{CONFIG.fps}").pack(side=tk.LEFT, padx=12)
        ttk.Checkbutton(header, text="Preview", variable=self.preview_enabled).pack(side=tk.LEFT, padx=12)
        ttk.Label(header, textvariable=self.status, foreground="blue").pack(side=tk.RIGHT, padx=4)

        # --- Manual Panel ---
        manual = ttk.LabelFrame(self, text="Manual Recording", padding=8)
        manual.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        ttk.Label(manual, text="Duration (min) for optional auto-stop:").grid(row=0, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(manual, textvariable=self.duration_minutes, width=6).grid(row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(manual, text="(Set to 0 for indefinite; press Stop to end)").grid(row=0, column=2, columnspan=2, sticky="w")

        self.btn_start_manual = ttk.Button(manual, text="Start (R)", command=self.on_start_manual)
        self.btn_start_manual.grid(row=1, column=0, padx=4, pady=6, sticky="ew")
        self.btn_stop_manual = ttk.Button(manual, text="Stop (R)", command=self.on_stop_clicked, state="disabled")
        self.btn_stop_manual.grid(row=1, column=1, padx=4, pady=6, sticky="ew")

        ttk.Button(manual, text="Quit (Esc)", command=self.quit_app).grid(row=1, column=3, padx=4, pady=6, sticky="e")

        # --- Scheduling Panel ---
        sched = ttk.LabelFrame(self, text="Scheduling", padding=8)
        sched.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        r = 0
        ttk.Label(sched, text="Start date (YYYY-MM-DD):").grid(row=r, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(sched, textvariable=self.sched_date, width=12).grid(row=r, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(sched, text="Start time (HH:MM, 24h):").grid(row=r, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(sched, textvariable=self.sched_time, width=8).grid(row=r, column=3, sticky="w", padx=4, pady=2)

        r += 1
        ttk.Label(sched, text="Duration (min):").grid(row=r, column=0, sticky="e", padx=4, pady=2)
        ttk.Entry(sched, textvariable=self.duration_minutes, width=6).grid(row=r, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(sched, text="Or End time (HH:MM):").grid(row=r, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(sched, textvariable=self.sched_end_time, width=8).grid(row=r, column=3, sticky="w", padx=4, pady=2)

        r += 1
        ttk.Label(sched, text="Repeat:").grid(row=r, column=0, sticky="e", padx=4, pady=2)
        repeat_vals = ["One shot"] + [f"Every {i} day{'s' if i>1 else ''}" for i in range(1, 8)]
        ttk.Combobox(sched, textvariable=self.repeat_choice, values=repeat_vals, width=16, state="readonly").grid(row=r, column=1, sticky="w", padx=4, pady=2)

        ttk.Label(sched, text="End date (if repeating):").grid(row=r, column=2, sticky="e", padx=4, pady=2)
        ttk.Entry(sched, textvariable=self.sched_end_date, width=12).grid(row=r, column=3, sticky="w", padx=4, pady=2)

        r += 1
        btns = ttk.Frame(sched)
        btns.grid(row=r, column=0, columnspan=4, sticky="ew", pady=6)
        self.btn_set_schedule = ttk.Button(btns, text="Set Scheduled Recording(s)", command=self.on_set_schedule)
        self.btn_set_schedule.pack(side=tk.LEFT, padx=4)
        self.btn_cancel_repeat = ttk.Button(btns, text="Cancel Schedule", command=self.on_cancel_repeating, state="disabled")
        self.btn_cancel_repeat.pack(side=tk.LEFT, padx=4)
        self.btn_end_sched_now = ttk.Button(btns, text="End Scheduled Recording Now", command=self.on_end_scheduled_now, state="disabled")
        self.btn_end_sched_now.pack(side=tk.RIGHT, padx=4)

        # Preview
        prev_frame = ttk.LabelFrame(self, text="Preview", padding=6)
        prev_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.preview_label = ttk.Label(prev_frame)
        self.preview_label.pack(fill=tk.BOTH, expand=True)

        # Footer hint
        hint = ttk.Label(self,
            text=("Manual panel is disabled when a scheduled recording is in progress.\n"
                  "Press R to start/stop manual recordings. Esc quits the app."),
            padding=6)
        hint.pack(side=tk.BOTTOM, fill=tk.X)

    def _bind_keys(self):
        self.bind_all("<Escape>", lambda e: self.quit_app())
        self.bind_all("<KeyPress>", self._on_keypress)

    def _on_keypress(self, event):
        if event.char and event.char.lower() == 'r':
            if self.recording:
                # Only allow 'R' to stop if the recording originated from manual panel
                if self.recording_origin == 'manual':
                    self.on_stop_clicked()
            else:
                if self.btn_start_manual['state'] == 'normal':
                    self.on_start_manual()

    # ---- helpers/logging
    def _update_status(self, s: str):
        self.status.set(s)
        self.update_idletasks()

    def _log_info(self, msg: str):
        print("[INFO]", msg)
        self._update_status(msg)

    # ---- Manual panel actions
    def on_start_manual(self):
        if self.recording:
            return
        self.recording_origin = 'manual'
        # Manual: optional auto-stop with duration_minutes, else indefinite
        try:
            dur_min = int(self.duration_minutes.get())
            dur_sec = dur_min * 60 if dur_min > 0 else None
        except Exception:
            dur_sec = None
        self._start_engine(duration_sec=dur_sec, label="Manual")

    def on_stop_clicked(self):
        if not self.recording:
            return
        if self.engine:
            self.engine.request_stop()
        self._update_status("Stopping...")

    # ---- Scheduling: Set / Cancel / End-now
    def on_set_schedule(self):
        """
        Single entry-point for both one-shot and repeating:
        - If Repeat = One shot -> schedule a one-time run
        - Else -> start a repeating schedule every N days, inclusive until End date
        """
        try:
            start_dt, dur_sec = self._read_schedule_inputs()
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return

        interval_days = self._read_repeat_interval_days()
        if interval_days == 0:
            # One-shot
            if self.recording and self.recording_origin == 'scheduled':
                messagebox.showwarning("Busy", "A scheduled recording is in progress. End it or wait.")
                return
            threading.Thread(target=self._run_one_shot, args=(start_dt, dur_sec), daemon=True).start()
            self._update_status(f"One-Shot scheduled: {start_dt.isoformat()} for {human_dur(dur_sec)}")
        else:
            # Repeating
            if self.scheduler is not None:
                messagebox.showwarning("Already scheduled", "A repeating schedule is already running.")
                return
            try:
                end_date = self._parse_date(self.sched_end_date.get().strip())
                if end_date < start_dt.date():
                    raise ValueError("End date must be the same as or after Start date for repeating schedules.")
            except ValueError as e:
                messagebox.showerror("Invalid input", str(e))
                return

            self.scheduler = RepeatingScheduler(
                app=self,
                start_dt=start_dt,
                end_date=end_date,
                interval_days=interval_days,
                duration_sec=dur_sec
            )
            self.scheduler.start()
            self.btn_set_schedule.config(state="disabled")
            self.btn_cancel_repeat.config(state="normal")
            self._update_status(f"Repeating: every {interval_days} day(s) from {start_dt.date()} to {end_date}")

    def on_cancel_repeating(self):
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
            self.btn_set_schedule.config(state="normal")
            self.btn_cancel_repeat.config(state="disabled")
            self._update_status("Schedule canceled.")

    def on_end_scheduled_now(self):
        if not (self.recording and self.recording_origin == 'scheduled'):
            return
        warn = ("WARNING: you are about to end a scheduled recording!\n\n"
                "Are you sure you want to end the recording now?\n"
                "You may resume recording from the manual panel at any time.")
        if messagebox.askokcancel("End Scheduled Recording", warn):
            self.on_stop_clicked()

    # ---- Internals: start recording engine (manual or scheduled)
    def _run_one_shot(self, start_dt: dt.datetime, dur_sec: int):
        while True:
            now = now_local()
            if now >= start_dt:
                break
            time.sleep(0.25)
        self.after(0, lambda: self._start_scheduled_run(duration_sec=dur_sec, label=f"One-Shot {start_dt.isoformat()}"))

    def _start_scheduled_run(self, duration_sec: int, label: str):
        if self.recording:
            self._log_info(f"{label} skipped (busy).")
            return
        self.recording_origin = 'scheduled'
        self._start_engine(duration_sec=duration_sec, label=label)

    def _start_engine(self, duration_sec: Optional[int], label: str):
        # Prepare logger & engine
        base = build_session_basename(now_local())
        self.logger = SessionLogger(CONFIG.recordings_dir, base)
        self.logger.write("session_start", f"{label}")

        self.engine = RecorderEngine(
            preview_queue=self.preview_q,
            logger=self.logger,
            duration_sec=duration_sec,
            preview_enabled=self.preview_enabled.get()
        )
        self.engine.start()
        self.recording = True

        # Disable manual panel if scheduled
        if self.recording_origin == 'scheduled':
            self.btn_start_manual.config(state="disabled")
            self.btn_stop_manual.config(state="disabled")
            self.btn_end_sched_now.config(state="normal")
        else:
            self.btn_start_manual.config(state="disabled")
            self.btn_stop_manual.config(state="normal")
            self.btn_end_sched_now.config(state="disabled")

        self._update_status(f"{label}: Recording... (Press Stop or R if manual)")
        self.after(250, self._check_engine_done)

    # ---- completion monitoring
    def _check_engine_done(self):
        if self.engine and self.engine.is_alive():
            self.after(250, self._check_engine_done)
            return

        # Engine finished
        if self.logger:
            self.logger.close()
            self.logger = None

        summary = (self.engine.summary if self.engine else None) or "Session ended."
        self._update_status(f"Idle – {summary}")
        self.recording = False
        self.engine = None

        # Re-enable manual panel
        self.btn_start_manual.config(state="normal")
        self.btn_stop_manual.config(state="disabled")
        self.btn_end_sched_now.config(state="disabled")

        # App stays open. Only Esc (or Quit) exits.

    # ---- scheduling input parsing
    def _read_schedule_inputs(self) -> tuple[dt.datetime, int]:
        # Start date/time
        y, m, d = self._split_ymd(self.sched_date.get().strip())
        hh, mm = self._split_hm_24(self.sched_time.get().strip())
        start_dt = dt.datetime(y, m, d, hh, mm, tzinfo=TZ) if TZ else dt.datetime(y, m, d, hh, mm)

        # Duration or end time
        if self.sched_end_time.get().strip():
            eh, em = self._split_hm_24(self.sched_end_time.get().strip())
            end_dt = dt.datetime(y, m, d, eh, em, tzinfo=TZ) if TZ else dt.datetime(y, m, d, eh, em)
            dur_sec = max(0, int((end_dt - start_dt).total_seconds()))
            if dur_sec <= 0:
                raise ValueError("End time must be after Start time.")
        else:
            try:
                dur_sec = max(1, int(self.duration_minutes.get()) * 60)
            except Exception:
                dur_sec = CONFIG.default_duration_sec
        return start_dt, dur_sec

    def _read_repeat_interval_days(self) -> int:
        txt = self.repeat_choice.get()
        if txt == "One shot":
            return 0
        try:
            n = int(txt.split()[1])
        except Exception:
            n = 1
        return max(1, min(7, n))

    @staticmethod
    def _split_ymd(s: str) -> tuple[int, int, int]:
        try:
            y, m, d = map(int, s.split("-"))
            dt.date(y, m, d)  # validation
            return y, m, d
        except Exception:
            raise ValueError("Enter Start/End date as YYYY-MM-DD.")

    @staticmethod
    def _split_hm_24(s: str) -> tuple[int, int]:
        try:
            h, m = map(int, s.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
            return h, m
        except Exception:
            raise ValueError("Enter time as HH:MM (24-hour).")

    @staticmethod
    def _parse_date(s: str) -> dt.date:
        y, m, d = map(int, s.split("-"))
        return dt.date(y, m, d)

    # ---- preview
    def _poll_preview_queue(self):
        try:
            while True:
                rgb, _ts = self.preview_q.get_nowait()
                im = Image.fromarray(rgb)
                im = im.resize(CONFIG.preview_size, Image.LANCZOS)
                imgtk = ImageTk.PhotoImage(image=im)
                self.current_image_tk = imgtk
                self.preview_label.configure(image=imgtk)
        except queue.Empty:
            pass
        self.after(15, self._poll_preview_queue)

    # ---- window lifecycle
    def _on_close_clicked(self):
        if messagebox.askokcancel("Quit", "Quit the application? (Esc also quits)"):
            self.quit_app()

    def quit_app(self):
        # Stop repeating schedule
        if self.scheduler:
            self.scheduler.stop()
            self.scheduler = None
            self.btn_set_schedule.config(state="normal")
            self.btn_cancel_repeat.config(state="disabled")
        # If recording, stop it gracefully
        if self.engine and self.engine.is_alive():
            if not messagebox.askyesno("Stop Recording", "Stop current recording and exit?"):
                return
            self.engine.request_stop()
            for _ in range(60):
                if not self.engine.is_alive():
                    break
                time.sleep(0.05)
        self.destroy()

# ----------------------------
# Main
# ----------------------------

if __name__ == "__main__":
    ensure_dir(CONFIG.recordings_dir)
    # For device/driver debugging, uncomment:
    # av.logging.set_level(av.logging.VERBOSE)
    app = RecorderApp()
    app.mainloop()
