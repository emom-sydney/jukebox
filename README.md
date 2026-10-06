# EMOM Jukebox

Small Raspberry Pi jukebox service using Flask, SQLite, and `mpv`.

Stopping or skipping ramps the player volume from 100% to 0% over two seconds. Set
`JUKEBOX_PLAYER=ffplay` only on systems where mpv is unavailable; ffplay cannot
perform runtime volume fades.

## Local run

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
JUKEBOX_CERT=cert.pem JUKEBOX_KEY=key.pem python app.py
```

## API

All API routes use the `/api/v1` prefix. With the self-signed certificate, use
`curl -k` or trust the certificate explicitly.

| Method | Route | Description |
|---|---|---|
| `GET` | `/api/v1/status` | Current service and playback status |
| `GET` | `/api/v1/tracks` | List tracks in the library |
| `POST` | `/api/v1/scan` | Start an asynchronous library scan; returns `202` with a job ID |
| `GET` | `/api/v1/scan/<job_id>` | Get scan progress and result |
| `POST` | `/api/v1/play/<track_id>` | Start a specific track |
| `POST` | `/api/v1/transport/pause` | Pause playback |
| `POST` | `/api/v1/transport/resume` | Resume playback, or start the next random track |
| `POST` | `/api/v1/transport/stop` | Stop playback with a two-second fade |
| `GET` | `/api/v1/tracks/<track_id>/images` | List artwork URLs for a track |
| `GET` | `/api/v1/images/<image_id>` | Serve an extracted artwork image |

Examples:

```sh
curl -k https://jukebox.local/api/v1/status
curl -k https://jukebox.local/api/v1/tracks
curl -k -X POST https://jukebox.local/api/v1/scan
curl -k https://jukebox.local/api/v1/scan/<job_id>
curl -k -X POST https://jukebox.local/api/v1/play/10
curl -k https://jukebox.local/api/v1/tracks/10/images
curl -k -X POST https://jukebox.local/api/v1/transport/resume
```

Deploy with:

```sh
ansible-playbook -i ansible/inventory.ini ansible/01_setup.yml
ansible-playbook -i ansible/inventory.ini ansible/02_deployapp.yml
```
