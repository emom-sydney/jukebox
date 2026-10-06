# EMOM Jukebox HTTP Contract

This document defines the integration contract for the separate jukebox
application. It intentionally does not prescribe the jukebox's Python
framework, audio library, process model, persistence, user interface, or
hardware layout.

## Deployment model

The EMOM now-playing kiosk runs in the same venue LAN as the jukebox. The
kiosk browser calls the jukebox directly. The EMOM VPS must not be expected to
reach a private LAN address and does not proxy this request.

The website stores the complete status URL in the `app_settings` value
`now_playing_jukebox_url`. The URL may include a path and port. An empty or
null value disables jukebox polling.

## Status request

The jukebox must expose one configurable HTTP `GET` status endpoint. A path
such as `/api/v1/status` is recommended, but the website does not assume a
particular path because the complete URL is configured separately.

The endpoint must:

- return JSON with `Content-Type: application/json`;
- return `Cache-Control: no-store`;
- respond promptly enough for a browser polling interval of approximately
  five seconds;
- allow cross-origin `GET` requests from the kiosk website origin;
- require no write/control capability for this first integration.

Successful responses use the EMOM API envelope:

```json
{
  "data": {
    "service": {"status": "ok"},
    "playback": {
      "state": "playing",
      "track": {
        "title": "Example Track",
        "artist": "Example Artist"
      }
    }
  }
}
```

If the endpoint returns an HTTP error, use the corresponding EMOM error
envelope, for example:

```json
{
  "error": {
    "code": "jukebox_unavailable",
    "message": "The jukebox audio service is unavailable."
  }
}
```

Error responses should use an appropriate 4xx or 5xx status. The kiosk does
not display error messages to the audience.

`service.status` must be `ok` for the response to be usable. Playback state
must be one of `playing`, `paused`, `stopped`, or `unknown`. The website only
displays jukebox information when the state is `playing` and
`playback.track.title` is a non-empty string.

The `artist` field is optional. Additional metadata may be included, but the
website must not depend on fields beyond the service status, playback state,
and track title.

## Failure behavior

Timeouts, network errors, CORS failures, non-2xx responses, invalid JSON,
missing required fields, and non-playing states must be treated by the kiosk
as inactive jukebox state. The kiosk then applies its normal performer and
banner rules.

Future play/pause/skip controls are intentionally outside this contract and
must receive a separate authentication and authorization design.
