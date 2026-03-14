# Covasify – What to Enter in the Client

When you add the Covasify plugin, COVAS asks for **Client ID**, **Client Secret**, and **Redirect URI**. Here’s where to get them and what to type.

---

## 1. Create a Spotify app (one-time)

1. Open **[Spotify Developer Dashboard](https://developer.spotify.com/dashboard)** and log in with your Spotify account.
2. Click **“Create app”**.
3. Fill in:
   - **App name**: e.g. `Covasify` or `COVAS Spotify`
   - **App description**: optional
   - **Redirect URI**: add exactly:
     ```text
     http://127.0.0.1:8888/callback
     ```
   - Accept the terms and click **“Save”**.

---

## 2. Add your Spotify account to the app

1. In your app’s page, open the **“User management”** tab (top of the page).
2. Click **“Add user”** and add the **Spotify email** you use with COVAS.
3. Save.  
   (Spotify often requires this for playback control; without it, auth may fail.)

---

## 3. Get Client ID and Client Secret

1. Open the **“Settings”** tab of your app (or click the app name / “Edit settings”).
2. You’ll see:
   - **Client ID** – long string of letters and numbers.
   - **Client secret** – click **“Show client secret”** to reveal it.

Copy both; you’ll paste them into COVAS in the next step.

---

## 4. What to enter in COVAS (Covasify settings)

In COVAS, go to the **Covasify** (or “Covasify Spotify Integration”) settings and paste:

| Field            | What to enter |
|------------------|----------------|
| **Client ID**    | The **Client ID** from your Spotify app (Settings tab). |
| **Client Secret**| The **Client secret** from your Spotify app (after “Show client secret”). |
| **Redirect URI**| Leave as: `http://127.0.0.1:8888/callback` (must match what you added in the Spotify app). |

Then save. The first time you use a Covasify command, your browser will open so you can log in to Spotify and allow the app; after that, it should work without asking again.

---

## Quick reference

- **Client ID** → from Spotify app → **Settings** → “Client ID”.
- **Client Secret** → from Spotify app → **Settings** → “Show client secret”.
- **Redirect URI** → use exactly: `http://127.0.0.1:8888/callback` (same in Spotify app and in COVAS).

You need a **Spotify Premium** account for playback control. Make sure Spotify is open (desktop, web, or mobile) and playing or ready to play when you use Covasify.
