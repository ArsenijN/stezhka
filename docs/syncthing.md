# Getting rides onto the server automatically

Goal: finish a ride, come home, and the ride is in Stezhka without touching anything.
Syncthing copies the recorder's export folder from the phone to the server over your home Wi-Fi;
Stezhka watches that folder (`STEZHKA_INBOX`) and imports new GPX files within a minute.
Nothing goes through a third party: with the settings below, devices only find each other on the
local network, and the data is TLS-encrypted end to end anyway.

```
phone: recorder ──auto-export──▶ export folder ──Syncthing (home Wi-Fi)──▶ server: /srv/syncthing/stezhka-inbox ──▶ Stezhka
```

## 1. Server

```sh
sudo apt install syncthing acl
sudo systemctl enable --now syncthing@<user>      # runs Syncthing as your user
sudo bash ~/stezhka-src/deploy/setup-inbox.sh <user>
```

The Syncthing web UI listens on the server's localhost only. Open it through an SSH tunnel from the
laptop:

```sh
ssh -p <ssh-port> -i <your-key> -L 8384:127.0.0.1:8384 <user>@<server>
# then browse http://127.0.0.1:8384
```

In **Settings → Connections**: keep *Local Discovery* on, turn **off** *Global Discovery* and
*Enable Relaying* (the phone only syncs at home, so the internet helpers are not needed).
Set a GUI user/password under **Settings → GUI**.

## 2. Poco (Android): OpenTracks + Syncthing-Fork

1. **OpenTracks**: in its settings, enable automatic export of finished recordings as **GPX** into
   a folder, e.g. `Documents/OpenTracks`. (Menu names differ between versions; look under
   export/backup.)
2. Install **Syncthing-Fork** from F-Droid (the official Syncthing Android app was discontinued;
   this is the maintained continuation).
3. In Syncthing-Fork: Settings → Connections: Global Discovery **off**, Relaying **off**.
   Run conditions: **only on Wi-Fi**, optionally restricted to your home network name; allow it to
   run in the background (battery: *No restrictions*, as for OpenTracks).
4. Pair the devices: add the server's device ID on the phone (and accept on the server). If they
   do not find each other, give the phone the server address explicitly:
   `tcp://<server LAN IP>:22000`.
5. Share the export folder. Phone side: folder type **Send Only**. Server side: accept it with the
   path `/srv/syncthing/stezhka-inbox` and folder type **Receive Only**.

Stezhka never moves or deletes files in the inbox. Deleting old exports on the phone later is safe:
imported rides keep their own copy, and a ride you delete in Stezhka is never re-imported from the
inbox.

## 3. iPhone: Open GPX Tracker

iOS has no official Syncthing app and limits apps running in the background, so sync only happens
while a Syncthing app is open. Options, simplest first:

1. **Upload in the browser** (works today, nothing to install): in Open GPX Tracker share the
   GPX → *Save to Files*; later open your Stezhka site in Safari → **Upload GPX** (several files at
   once are fine; duplicates are recognised and skipped).
2. **Synctrain** (free, open source) or **Möbius Sync** (paid) as an iOS Syncthing client: share
   Open GPX Tracker's folder (Files → On My iPhone → GPX Tracker) the same way as on Android, then
   open the app at home for a moment so it syncs.
3. **Planned**: a per-device upload token + an iOS Shortcut on the share sheet
   ("Share → Send to Stezhka"), one tap after each ride. See ROADMAP.

## Checking it works

```sh
journalctl -u stezhka -f        # on the server; a new ride logs "imported ride N from ..."
```
