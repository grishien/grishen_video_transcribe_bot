# Android app

Open **Transcribe** after installing the debug APK. The launcher still shows "Share videos from TikTok here" and, on Android 13 and newer, asks for notification permission.

The server address and token are entered on this screen. They are not compiled into the APK.

## Settings

- **Server (IP:PORT)** — host and port only, for example `203.0.113.10:8080`. Do not type `http://`. The app adds that when it uploads.
- **Token** — the same bearer token the VPS uses. The field is masked. Use the show/hide icon to check it.
- **Save** — checks that both fields are filled and that the server is `host:port` with a port from 1 to 65535. A valid save shows `Saved`. Anything else shows an error and is not stored. Opening the app again fills both fields from the saved values.

The values are kept in app-private storage. The next share uses whatever is saved then, so changing them does not need a new APK.

If a video is shared before valid settings are saved, the upload notification says "Set server and token in the app". Tapping it opens this screen.

## Cleartext HTTP

The receiver is plain HTTP. `app/src/main/res/xml/network_security_config.xml` sets `base-config cleartextTrafficPermitted="true"`, so whatever host is typed in settings can be reached without TLS. Cleartext is allowed for any host, not only one address.
