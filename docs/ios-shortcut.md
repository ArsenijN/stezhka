# iPhone: "Send to Stezhka" on the share sheet

After a ride, share the GPX from Open GPX Tracker (or the Files app) and pick **Send to Stezhka**.
The Shortcut uploads it with a per-device **upload key** that can do nothing but upload rides.
Uploading the same file twice is harmless: Stezhka recognises it and says so.

## 1. Create a key

Stezhka → **Settings → Upload keys** → name it (e.g. `iPhone`) → **Create** → **Copy**.
The key (`stz_up_…`) is shown only once. Lost it? Revoke it and create a new one.

## 2. Build the Shortcut (Shortcuts app, ~2 minutes)

1. **+** (new shortcut), name it **Send to Stezhka**.
2. Tap the **ⓘ** (details) → enable **Show in Share Sheet** → under *Receive*, allow only **Files**.
3. Add **Get Contents of URL**:
   - URL: `https://<your Stezhka site>/api/rides`
   - Method: **POST**
   - Headers:
     - `Authorization` = `Bearer stz_up_…` (your key)
     - `X-Filename` = *Shortcut Input* → *Name* (optional; Stezhka shows it as the source file)
   - Request Body: **File** → *Shortcut Input*
4. Add **Get Dictionary Value**: key `error`, from *Contents of URL*.
5. Add **If** *Dictionary Value* **has any value**:
   - **Show Notification**: `Stezhka: upload failed (Dictionary Value)`
   - **Otherwise**: **Show Notification**: `Stezhka: ride uploaded`
   - (End If)

Answers you may see in that notification: `invalid_gpx` (not a usable track),
`unauthorized` (key wrong or revoked), `file_too_large`.

## 3. Use it

Open GPX Tracker → the saved track → **Share** → **Send to Stezhka**. If the Shortcut is not in the
first row, scroll the share sheet to *Edit Actions* and add it to favourites.

## Test from a computer

```sh
curl -X POST https://<your Stezhka site>/api/rides \
     -H "Authorization: Bearer stz_up_…" -H "X-Filename: test.gpx" \
     --data-binary @ride.gpx
# {"id": 12, "created": true}   or   {"id": 12, "created": false} if it was already there
```

## Safety

The key is stored inside the Shortcut. Anyone holding it can add rides, but cannot read, change or
delete anything. If the phone is lost, revoke the key in Settings.
