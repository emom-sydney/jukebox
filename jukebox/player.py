import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import threading


class Player:
    """Player with a real volume ramp when using mpv, plus ffplay fallback."""
    def __init__(self, library, executable="mpv", fade_seconds=2.0, audio_device="alsa/plughw:1,0", on_finished=None):
        self.library, self.executable = library, executable
        self.audio_device = audio_device
        self.on_finished = on_finished
        self.fade_seconds = fade_seconds
        self.process = self.current = None
        self.state = "stopped"
        self._socket_path = None
        self._monitor = None
        self._played_marked = False
        self._volume = 100
        self._finish_lock = threading.Lock()
        self._finished_process = None
        self.last_exit_code = None
        self.last_error = ""

    @property
    def _is_mpv(self):
        return os.path.basename(self.executable) == "mpv"

    def _command(self, command):
        if not self._socket_path:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(self._socket_path)
                sock.sendall((json.dumps({"command": command}) + "\n").encode())
        except OSError:
            pass

    def play(self, track):
        self.stop()
        if self._is_mpv:
            self._socket_path = tempfile.mktemp(prefix="jukebox-mpv-", suffix=".sock")
            args = [self.executable, "--no-video", "--msg-level=all=info", "--ao=alsa",
                    f"--audio-device={self.audio_device}",
                    f"--input-ipc-server={self._socket_path}", track["path"]]
        else:
            args = [self.executable, "-nodisp", "-autoexit", track["path"]]
        # Keep stderr attached to systemd/journald so ALSA/mpv failures are diagnosable.
        self.last_command = args[:]
        self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.current, self.state = track, "playing"
        self.last_exit_code, self.last_error = None, ""
        self._played_marked = False
        self._volume = 100
        self._finished_process = None
        if self._is_mpv:
            self._monitor = threading.Thread(target=self._monitor_progress, daemon=True)
            self._monitor.start()

    def _query(self, name):
        if not self._socket_path:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(self._socket_path)
                sock.sendall((json.dumps({"command": ["get_property", name], "request_id": 1}) + "\n").encode())
                return json.loads(sock.recv(4096).decode()).get("data")
        except (OSError, ValueError):
            return None

    def _monitor_progress(self):
        process = self.process
        track = self.current
        while process and process.poll() is None and self.current is track:
            duration, position = self._query("duration"), self._query("time-pos")
            if duration and position and position >= duration / 2 and not self._played_marked:
                self.library.mark_played(self.current["id"])
                self._played_marked = True
            time.sleep(2)
        self._handle_finished(process, track)

    def _handle_finished(self, process, track):
        if not process or process.poll() is None or self.process is not process or self.state != "playing":
            return False
        with self._finish_lock:
            if self._finished_process is process:
                return False
            self._finished_process = process
        if self.on_finished:
            self.on_finished(track)
        return True

    def pause(self):
        if self.process and self.state == "playing":
            if self._is_mpv:
                self._ramp_volume(0)
                self._command(["set_property", "pause", True])
            else:
                time.sleep(self.fade_seconds)
                self.process.send_signal(signal.SIGSTOP)
            self.state = "paused"

    def resume(self):
        if self.process and self.state == "paused":
            if self._is_mpv:
                self._command(["set_property", "pause", False])
                self._ramp_volume(100)
            else:
                self.process.send_signal(signal.SIGCONT)
            self.state = "playing"

    def _ramp_volume(self, target):
        start = self._volume
        steps = 20
        for step in range(1, steps + 1):
            volume = start + (target - start) * step / steps
            self._command(["set_property", "volume", volume])
            time.sleep(self.fade_seconds / steps)
        self._volume = target

    def _fade_and_stop(self):
        if self._is_mpv:
            self._ramp_volume(0)
            self._command(["quit"])
        elif self.process and self.process.poll() is None:
            time.sleep(self.fade_seconds)
            self.process.terminate()

    def stop(self):
        if self.process and self.process.poll() is None:
            self._fade_and_stop()
            self.process.wait(timeout=self.fade_seconds + 1)
        self.process, self.current, self.state = None, None, "stopped"
        if self._socket_path:
            try:
                os.unlink(self._socket_path)
            except FileNotFoundError:
                pass
            self._socket_path = None

    def status(self):
        if self.process and self.process.poll() is not None:
            finished_process, finished_track = self.process, self.current
            self._handle_finished(finished_process, finished_track)
            if self.process is not finished_process:
                return self.status()
            self.last_exit_code = self.process.returncode
            try:
                self.last_error = (self.process.communicate(timeout=0.2)[0] or "").strip()[-2000:]
            except subprocess.TimeoutExpired:
                self.last_error = "mpv exited but stderr was not available"
            self.process, self.current, self.state = None, None, "stopped"
        track = None
        if self.current:
            track = {"id": self.current["id"], "title": self.current["title"]}
            if self.current.get("artist"):
                track["artist"] = self.current["artist"]
        return {"state": self.state, "track": track}
