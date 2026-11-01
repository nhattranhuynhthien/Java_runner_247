# NRO Cloud Controller: implementation notes

## Runtime

- Docker runs Ubuntu, Java, MicroEmulator, Xvfb, Flask/Gunicorn, and Supervisor.
- Xvfb is 240×384×24: the 240×360 game canvas plus the emulator's 24 px menu area. Screenshot capture crops the menu area and returns the 240×360 canvas.
- Supervisor owns Xvfb, emulator, and web process lifecycles. Gunicorn uses one worker because game input and framebuffer operations share one X display.
- `PORT` is inherited from the environment, defaulting to 8080. Hugging Face Spaces can set it to 7860.

## Authentication and data

- No default password is accepted. Set `ADMIN_PASSWORD` to at least 12 characters for first login; use `SESSION_SECRET` for a stable session key.
- After credentials are changed in the dashboard, the PBKDF2 password hash is saved to `/data/security_config.json` and takes precedence over the environment password.
- The authenticated browser session is carried by an HttpOnly, SameSite=Lax cookie. Do not put credentials in URLs. Legacy API credential checks use headers or request bodies only.
- Failed login attempts are limited in memory to five attempts per remote address in a rolling five-minute window. Forwarded client-IP headers are not trusted automatically.
- Uploads are limited to 64 MB, must be ZIP/JAR files, and replace the active ROM atomically. Uploaded ROMs are saved under `/data/roms/NRO.jar` unless `ROM_FILE` is set.
- Mount persistent storage at `/data` to retain uploaded ROMs and changed credentials across container replacement.

## Dashboard behavior

- Screenshot and status polling pause when the document is hidden and resume when it becomes visible.
- `/health` returns `OK` without accessing X11. It measures web process availability, not game-server availability.
- Keep X11 input injection behind authenticated routes and keep command arguments as arrays rather than shell strings.
