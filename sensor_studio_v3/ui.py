from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .controller import RecordingController, StudioState


log = logging.getLogger(__name__)


class StudioWindow:
    WIDTH = 760
    HEIGHT = 520

    def __init__(self, controller: RecordingController, sources: list[Any]) -> None:
        self.controller = controller
        self.sources = sources
        self._exit_requested = threading.Event()
        self._window = None
        font_path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
        self._pillow_font = (
            ImageFont.truetype(str(font_path), 14)
            if font_path.exists()
            else ImageFont.load_default()
        )
        self._small_font = (
            ImageFont.truetype(str(font_path), 11)
            if font_path.exists()
            else ImageFont.load_default()
        )

    @staticmethod
    def _fit_image(
        canvas: Image.Image,
        frame: np.ndarray,
        box: tuple[int, int, int, int],
    ) -> None:
        x0, y0, x1, y1 = box
        max_w = max(1, x1 - x0 - 16)
        max_h = max(1, y1 - y0 - 42)
        rgb = np.ascontiguousarray(frame[:, :, ::-1])
        image = Image.fromarray(rgb, mode="RGB")
        if image.width > max_w or image.height > max_h:
            scale = min(max_w / image.width, max_h / image.height)
            size = (
                max(1, int(image.width * scale)),
                max(1, int(image.height * scale)),
            )
            image = image.resize(size, Image.Resampling.NEAREST)
        px = x0 + (x1 - x0 - image.width) // 2
        py = y0 + 34 + (max_h - image.height) // 2
        canvas.paste(image, (px, py))

    def _draw_message(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        box: tuple[int, int, int, int],
        color: str,
    ) -> None:
        x0, y0, x1, y1 = box
        draw.multiline_text(
            (x0 + 16, y0 + 80),
            text,
            fill=color,
            font=self._pillow_font,
            spacing=5,
        )

    def _render_canvas(self) -> np.ndarray:
        canvas = Image.new("RGB", (self.WIDTH, self.HEIGHT), "#05070d")
        draw = ImageDraw.Draw(canvas)
        dvs = self.sources[0]
        if hasattr(dvs, "latest_frames"):
            slave_frame, master_frame = dvs.latest_frames()
        else:
            slave_frame, master_frame = dvs.latest_frame(), None
        panels = [
            (
                (16, 16, 372, 416),
                "DVS SLAVE",
                getattr(dvs, "slave_serial", ""),
                slave_frame,
            )
        ]
        if master_frame is not None:
            panels.append(
                (
                    (388, 16, 744, 416),
                    "DVS MASTER",
                    getattr(dvs, "master_serial", ""),
                    master_frame,
                )
            )
        for box, title, serial, frame in panels:
            draw.rounded_rectangle(
                box, radius=8, fill="#0d1320", outline="#334155", width=2
            )
            draw.text(
                (box[0] + 14, box[1] + 10),
                f"{title} {serial}".strip(),
                fill="#e5e7eb",
                font=self._pillow_font,
            )
            if frame is None:
                self._draw_message(
                    draw,
                    dvs.error or f"{title} NO FRAME",
                    box,
                    "#ef4444",
                )
            else:
                self._fit_image(canvas, frame, box)
        state = self.controller.state
        colors = {
            StudioState.PREVIEW: "#22c55e" if self.controller.ready else "#f59e0b",
            StudioState.RECORDING: "#ef4444",
            StudioState.FINALIZING: "#38bdf8",
            StudioState.FAULT: "#ef4444",
            StudioState.STARTING: "#f59e0b",
            StudioState.EXIT: "#94a3b8",
        }
        status_color = colors.get(state, "#e5e7eb")
        state_text = state.value
        if state == StudioState.PREVIEW and not self.controller.ready:
            state_text = "NOT READY"
        record_text = (
            f"REC {self.controller.elapsed_seconds:7.1f}s"
            if state == StudioState.RECORDING
            else "SPACE record   Q/Esc stop + exit"
        )
        draw.rectangle((16, 426, 744, 507), fill="#111827")
        draw.ellipse((30, 440, 44, 454), fill=status_color)
        draw.text((54, 435), state_text, fill=status_color, font=self._pillow_font)
        source_health = "  ".join(
            f"{source.name.upper()}="
            f"{'OK' if source.ready else ('RECONNECT' if source.recovering else 'ERR')}"
            for source in self.sources
        )
        draw.text((170, 435), source_health, fill="#94a3b8", font=self._pillow_font)
        draw.text((30, 452), record_text, fill="#d1d5db", font=self._pillow_font)
        draw.text(
            (30, 471),
            "DVS pair: hardware sync  |  CIS/IMU: software-coordinated session",
            fill="#94a3b8",
            font=self._small_font,
        )
        if self.controller.message:
            draw.text(
                (30, 489),
                self.controller.message[:180],
                fill="#cbd5e1",
                font=self._small_font,
            )
        rgb = np.asarray(canvas, dtype=np.uint8)
        return np.ascontiguousarray(rgb[:, :, ::-1])

    def run(self) -> None:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

        window = None
        previous_handlers = {}
        try:
            window = Gtk.Window(title="DVS - Sensor Studio v3")
            window.set_default_size(self.WIDTH, self.HEIGHT)
            window.set_resizable(False)
            image = Gtk.Image()
            window.set_child(image)
            window.present()

            def on_key(_controller, keyval, _keycode, _state):
                if keyval == Gdk.KEY_space:
                    self.controller.start_recording()
                    return True
                if keyval in {Gdk.KEY_q, Gdk.KEY_Q, Gdk.KEY_Escape}:
                    self._exit_requested.set()
                    return True
                return False

            def on_delete(*_args):
                self._exit_requested.set()
                return True

            key_controller = Gtk.EventControllerKey.new()
            key_controller.connect("key-pressed", on_key)
            window.add_controller(key_controller)
            window.connect("close-request", on_delete)

            def request_exit_signal(_signum, _frame):
                self._exit_requested.set()

            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(
                    signum, request_exit_signal
                )
            loop = GLib.MainLoop()

            def update_frame():
                if self._exit_requested.is_set():
                    loop.quit()
                    return False
                try:
                    self.controller.tick()
                    canvas = np.ascontiguousarray(self._render_canvas()[:, :, ::-1])
                    data = GLib.Bytes.new(canvas.tobytes())
                    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
                        data,
                        GdkPixbuf.Colorspace.RGB,
                        False,
                        8,
                        self.WIDTH,
                        self.HEIGHT,
                        self.WIDTH * 3,
                    )
                    image.set_from_pixbuf(pixbuf)
                except Exception:
                    log.exception("Rendering preview frame failed")
                return True

            GLib.timeout_add(33, update_frame)
            loop.run()
        except KeyboardInterrupt:
            log.info("Interrupted from keyboard")
        finally:
            self.controller.request_exit()
            if window is not None:
                try:
                    window.destroy()
                except Exception:
                    log.exception("Destroying main window failed")
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            self._window = None

