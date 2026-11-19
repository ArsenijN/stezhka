# iPhone: "Send to Stezhka" on the share sheet

After a ride, share the GPX (one or several) from Open GPX Tracker or the Files app and pick **Send to Stezhka**.
The Shortcut uploads it with a per-device **upload key** that can do nothing but upload rides.
Uploading the same file twice is harmless: Stezhka recognises it and says so.

## 1. Create a key

Stezhka → **Settings → Upload keys** → name it (e.g. `iPhone`) → **Create** → **Copy**.
The key (`stz_up_…`) is shown only once. Lost it? Revoke it and create a new one.

## 2. Build the Shortcut (Shortcuts app, ~2 minutes)

1. **+** (new shortcut), name it **Send to Stezhka**.
2. Tap the **ⓘ** (details) → enable **Show in Share Sheet** → under *Receive*, allow only **Files**.
3. Add **Repeat with Each** item in *Shortcut Input*. Stezhka takes one file per request, so
   this loop is what makes sharing several tracks at once work (one file works the same way).
   Inside the loop:
   1. **Get Contents of URL**:
      - URL: `https://<your Stezhka site>/api/rides`
      - Method: **POST**
      - Headers:
        - `Authorization` = `Bearer stz_up_…` (your key)
        - `X-Filename` = *Repeat Item* → *Name* (optional; Stezhka shows it as the source file)
      - Request Body: **File** → *Repeat Item*
   2. **Get Dictionary Value**: key `error`, from *Contents of URL*.
   3. **If** *Dictionary Value* **has any value**:
      **Show Notification**: `Stezhka: Repeat Item failed (Dictionary Value)` → **End If**
4. After **End Repeat**: **Show Notification**: `Stezhka: done (Count of Shortcut Input files)`
   (use a **Count** action on *Shortcut Input*, or just write `Stezhka: done`).

Answers you may see in a failure notification: `invalid_gpx` (not a usable track),
`unauthorized` (key wrong or revoked), `file_too_large`.

## 3. Use it

Open GPX Tracker → the saved track → **Share** → **Send to Stezhka**. If the Shortcut is not in the
first row, scroll the share sheet to *Edit Actions* and add it to favourites.

## Back it up or share it

Build it once by hand: iOS imports only Shortcut files that Apple has signed, so a ready-made file
cannot be shipped with Stezhka. A Shortcut you export yourself (**Share → Export File**, or
**Copy iCloud Link**) is signed and imports fine on a new phone.

- **The upload key is inside the exported file.** Keep it like a password; if it leaks, revoke the key
  in Settings and put a new one into the Shortcut.
- **For someone else's Stezhka**, never send yours with the key in it: under the Shortcut's details →
  **Set Up Import Questions**, add questions for the URL and the `Authorization` header. Whoever adds
  it is then asked for their own site and key, and yours is not exported.

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
