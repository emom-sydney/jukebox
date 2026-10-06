import os
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import SysLogHandler
from flask import Flask, abort, jsonify, request, Response, send_file, url_for
from jukebox.db import Library
from jukebox.player import Player


class ScanManager:
    def __init__(self, library, logger):
        self.library = library
        self.logger = logger
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="library-scan")
        self.lock = threading.Lock()
        self.jobs = {}

    def start(self, roots, source):
        with self.lock:
            running = next((job for job in self.jobs.values() if job["status"] in {"queued", "running"}), None)
            if running:
                return None, running
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "status": "queued", "source": source, "roots": roots,
                   "files_seen": 0, "tracks_updated": 0, "tracks_removed": 0,
                   "artwork_saved": 0, "errors": []}
            self.jobs[job_id] = job
        self.executor.submit(self._run, job_id)
        self.logger.info("event=scan_queued source=%s job_id=%s roots=%s", source, job_id, roots)
        return job, None

    def _run(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            roots = list(job["roots"])
        self.logger.info("event=scan_started source=%s job_id=%s roots=%s", job["source"], job_id, roots)

        def progress(result):
            with self.lock:
                job.update(result)
                job["errors"] = result["errors"][-100:]

        try:
            result = self.library.scan(roots, progress=progress)
            with self.lock:
                job.update(result)
                job["errors"] = result["errors"][-100:]
                job["status"] = "completed"
            self.logger.info("event=scan_completed source=%s job_id=%s files_seen=%d tracks_updated=%d tracks_removed=%d artwork_saved=%d errors=%d",
                             job["source"], job_id, result["files_seen"], result["tracks_updated"],
                             result["tracks_removed"], result["artwork_saved"], len(result["errors"]))
        except Exception as error:
            with self.lock:
                job["status"] = "failed"
                job["errors"] = (job.get("errors", []) + [{"message": str(error)}])[-100:]
            self.logger.exception("event=scan_failed source=%s job_id=%s", job["source"], job_id)

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE=os.environ.get("JUKEBOX_DATABASE", "jukebox.db"),
        MUSIC_ROOTS=os.environ.get("JUKEBOX_MUSIC_ROOTS", "/media"),
        FFPPLAY=os.environ.get("JUKEBOX_PLAYER", "mpv"),
        AUDIO_DEVICE=os.environ.get("JUKEBOX_AUDIO_DEVICE", "alsa/plughw:1,0"),
        ARTWORK_DIR=os.environ.get("JUKEBOX_ARTWORK_DIR", "artwork"),
        CORS_ORIGIN=os.environ.get("JUKEBOX_CORS_ORIGIN", "*"),
    )
    if config:
        app.config.update(config)
    library = Library(app.config["DATABASE"], app.config["ARTWORK_DIR"])
    player = Player(library, app.config["FFPPLAY"], audio_device=app.config["AUDIO_DEVICE"])

    def track_finished(track):
        next_track = library.random_next()
        if next_track:
            player.play(next_track)
            app.logger.info("event=play source=player reason=track_finished track_id=%d path=%s",
                            next_track["id"], next_track["path"])
        else:
            player.state = "stopped"
            app.logger.info("event=stop source=player reason=library_empty track_id=%s", track.get("id"))

    player.on_finished = track_finished
    scanner = ScanManager(library, app.logger)
    app.extensions.update(library=library, player=player, scanner=scanner)
    if os.path.exists("/dev/log"):
        for handler in list(app.logger.handlers):
            app.logger.removeHandler(handler)
        syslog = SysLogHandler(address="/dev/log")
        syslog.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        app.logger.addHandler(syslog)
        app.logger.setLevel(logging.INFO)
        app.logger.propagate = False

    def event_source():
        return "web" if request.headers.get("X-Jukebox-Client") == "web" else "api"

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Access-Control-Allow-Origin"] = app.config["CORS_ORIGIN"]
        return response

    @app.get("/api/v1/status")
    def status():
        app.logger.info("event=status source=%s", event_source())
        had_process = player.process is not None
        previous_track = player.current
        playback = player.status()
        if had_process and previous_track and playback["state"] == "stopped":
            app.logger.warning("event=stop source=player reason=process_exit track_id=%s path=%s exit_code=%s command=%r output=%r",
                               previous_track.get("id"), previous_track.get("path"),
                               player.last_exit_code, getattr(player, "last_command", None), player.last_error)
        return jsonify(data={"service": {"status": "ok"}, "playback": playback})

    @app.get("/")
    def index():
        return Response("""<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Jukebox</title>
<style>body{font:18px system-ui;max-width:40rem;margin:3rem auto;padding:0 1rem}button{font-size:1rem;padding:.7rem 1rem;margin:.25rem}</style></head>
<body><h1>Jukebox</h1><p id="now">Loading…</p><img id="art" alt="" style="display:none;max-width:20rem;max-height:20rem">
<button onclick="transport('resume')">Resume</button><button onclick="transport('pause')">Pause</button><button onclick="transport('stop')">Stop</button><button onclick="scan()">Scan library</button><p id="scan"> </p>
<script>const opts={headers:{'X-Jukebox-Client':'web'}};async function refresh(){const r=await fetch('/api/v1/status',opts);const j=await r.json();const p=j.data.playback;document.querySelector('#now').textContent=(p.track?.title||'Nothing playing')+' — '+p.state;const art=document.querySelector('#art');if(p.track?.id){const i=await fetch('/api/v1/tracks/'+p.track.id+'/images');const x=await i.json();if(x.data.images.length){art.src=x.data.images[0].url;art.style.display='block'}else{art.style.display='none'}}else{art.style.display='none'}}async function transport(a){await fetch('/api/v1/transport/'+a,{method:'POST',...opts});refresh()}async function scan(){const r=await fetch('/api/v1/scan',{method:'POST',...opts});const j=await r.json();if(!r.ok){document.querySelector('#scan').textContent='Scan already running';return}const id=j.data.scan.id;const poll=setInterval(async()=>{const s=await fetch('/api/v1/scan/'+id,opts);const x=(await s.json()).data.scan;document.querySelector('#scan').textContent='Scan: '+x.status+' ('+x.files_seen+' files, '+x.tracks_updated+' tracks, '+x.tracks_removed+' removed)';if(['completed','failed'].includes(x.status))clearInterval(poll)},1000)}refresh();setInterval(refresh,5000)</script>
</body></html>""", mimetype="text/html")

    @app.get("/api/v1/tracks")
    def tracks():
        return jsonify(data={"tracks": library.all()})

    @app.get("/api/v1/tracks/<int:track_id>/images")
    def track_images(track_id):
        if not library.get(track_id):
            return jsonify(error={"code": "not_found", "message": "Track not found."}), 404
        images = [{"url": url_for("track_image", image_id=image["id"]), "mime_type": image["mime_type"]}
                  for image in library.images(track_id)]
        return jsonify(data={"images": images})

    @app.get("/api/v1/images/<int:image_id>")
    def track_image(image_id):
        image = library.image(image_id)
        if not image or not os.path.isfile(image["path"]):
            abort(404)
        return send_file(image["path"], mimetype=image["mime_type"], conditional=True)

    @app.post("/api/v1/scan")
    def scan():
        roots = request.json.get("roots") if request.is_json else None
        roots = roots or [x for x in app.config["MUSIC_ROOTS"].split(os.pathsep) if x]
        job, running = scanner.start(roots, event_source())
        if running:
            return jsonify(error={"code": "scan_in_progress", "message": "A library scan is already running.", "scan": running}), 409
        return jsonify(data={"scan": job}), 202

    @app.get("/api/v1/scan/<job_id>")
    def scan_status(job_id):
        job = scanner.get(job_id)
        if not job:
            return jsonify(error={"code": "not_found", "message": "Scan job not found."}), 404
        return jsonify(data={"scan": job})

    @app.post("/api/v1/play/<int:track_id>")
    def play(track_id):
        track = library.get(track_id)
        if not track:
            return jsonify(error={"code": "not_found", "message": "Track not found."}), 404
        player.play(track)
        app.logger.info("event=play source=%s track_id=%d path=%s", event_source(), track_id, track["path"])
        return jsonify(data={"playback": player.status()})

    @app.post("/api/v1/transport/<action>")
    def transport(action):
        if action not in {"pause", "resume", "stop"}:
            return jsonify(error={"code": "invalid_action", "message": "Unsupported transport action."}), 400
        if action == "resume" and player.state == "stopped":
            track = library.random_next()
            if track:
                player.play(track)
                app.logger.info("event=play source=%s reason=resume track_id=%d path=%s", event_source(), track["id"], track["path"])
        else:
            getattr(player, action)()
            app.logger.info("event=%s source=%s", action, event_source())
        return jsonify(data={"playback": player.status()})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=443, ssl_context=(os.environ["JUKEBOX_CERT"], os.environ["JUKEBOX_KEY"]))
