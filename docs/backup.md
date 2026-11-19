# Backups

What matters is small: the database (rides, bikes, notes, weather) and the original GPX files.
The basemap and DEM tiles are re-downloadable and are not backed up.

`stezhka-backup.timer` runs every night at ~03:30 (installed by `deploy/install.sh`):

1. a consistent database snapshot (SQLite online backup, safe while Stezhka runs), keeping the
   newest `STEZHKA_BACKUP_KEEP` (default 14) in `/srv/stezhka/data/backups/`;
2. a mirror of those snapshots and of `data/raw/` (the GPX originals) to `STEZHKA_BACKUP_DIR`.

Deleting a ride in the app moves it to *Settings → Recently deleted* for 30 days; only then (or on
"Delete now") are the ride and its GPX file erased, and the next night's mirror drops the file too.
The database snapshots still hold the ride row for another 14 nights.

It refuses to run if `STEZHKA_BACKUP_DIR` is on the same disk as the data: an unmounted network
share leaves an empty folder on the local disk, and a "backup" there would be worthless.

## SMB cold storage

Give Stezhka its own folder and its own SMB user on the Windows PC, so its access is separate from
Immich's. Then on the server:

```sh
sudo apt install cifs-utils
sudo install -d -m 700 /mnt/cold-stezhka
sudo tee /etc/smb-stezhka.cred >/dev/null <<'EOF'
username=<smb user>
password=<smb password>
EOF
sudo chmod 600 /etc/smb-stezhka.cred
```

`/etc/fstab` (one line; `x-systemd.automount` mounts it on first use, `nofail` keeps boot working
when the PC is off):

```
//<windows-pc>/<share>/stezhka  /mnt/cold-stezhka  cifs  credentials=/etc/smb-stezhka.cred,vers=3.0,uid=0,gid=0,file_mode=0600,dir_mode=0700,_netdev,nofail,x-systemd.automount  0  0
```

On the Windows side, a new share is **read-only by default**: give the SMB user **Change** under
Sharing → Advanced Sharing → Permissions and **Modify** under Security. Renaming or re-creating the
share drops these, so check them again after such changes.

```sh
sudo systemctl daemon-reload
# a new x-systemd.automount entry is only armed at boot: start it once by hand
sudo systemctl start "$(systemd-escape -p --suffix=automount /mnt/cold-stezhka)"
sudo ls /mnt/cold-stezhka                  # triggers the mount
findmnt /mnt/cold-stezhka                  # must list a cifs line, not just autofs
echo 'STEZHKA_BACKUP_DIR=/mnt/cold-stezhka' | sudo tee -a /etc/stezhka.env
sudo systemctl start stezhka-backup        # run once now
sudo journalctl -u stezhka-backup -n 20    # "backup ok: N snapshots, M GPX files"
```

If the Windows PC is asleep at 03:30 the run fails and is logged; the timer is persistent, so a
missed night is caught up after the next boot of the server, and the following night tries again.

## Restore

```sh
sudo systemctl stop stezhka
sudo install -o stezhka -g stezhka -m 640 /mnt/cold-stezhka/db/stezhka-<date>.sqlite3 /srv/stezhka/data/stezhka.sqlite3
sudo rm -f /srv/stezhka/data/stezhka.sqlite3-wal /srv/stezhka/data/stezhka.sqlite3-shm
sudo rsync -a /mnt/cold-stezhka/raw/ /srv/stezhka/data/raw/ && sudo chown -R stezhka:stezhka /srv/stezhka/data/raw
sudo systemctl start stezhka
sudo stezhka-admin basemap                 # if the map is gone too
```
