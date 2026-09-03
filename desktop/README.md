# ZacoAgents desktop shell

A thin Electron window around the deployed web app
(https://zacodasboard.co.za). It reuses the live app entirely — login, PDF
extraction, nett apportionment, Excel export — so this folder holds no business
logic, only the native shell.

## Why it works this way

- **One source of truth.** The window loads the deployed app, so a fix pushed to
  Vercel reaches every installed copy immediately. Nothing to re-distribute.
- **Talks to the database while running.** The loaded app calls Supabase exactly
  as the browser version does. The database is cloud-hosted, so the app needs an
  internet connection — a desktop build does not change that.
- **Native file save.** Running in a real desktop window (HTTPS origin) means the
  browser file-save API works cleanly for writing the .xlsx back to disk.

## Develop

```bash
cd desktop
npm install
npm start          # opens the app in a native window
```

## Build a Windows .exe

```bash
npm run dist            # portable single .exe -> dist/ZacoAgents-1.0.0-portable.exe
npm run dist:installer  # NSIS installer with desktop + start-menu shortcuts
```

Output lands in `desktop/dist/`.

## Notes

- **Point at a different environment** by editing `config.json` (`appUrl`) — e.g.
  a staging URL — then rebuild.
- **Code signing:** the .exe is unsigned, so Windows SmartScreen shows a
  "unrecognised app" warning on first run (More info → Run anyway). Removing that
  requires an OV/EV code-signing certificate (~R2 000+/yr); worth it only once
  this is distributed beyond a couple of trusted machines.
- **Icon:** add `build/icon.ico` (256×256) and reference it under `build.win.icon`
  in package.json to replace the default Electron icon.
