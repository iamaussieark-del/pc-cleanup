<p align="center">
  <img src="docs/icon.png" width="96" alt="PC Cleanup icon">
</p>

<h1 align="center">PC Cleanup</h1>

<p align="center">
  A free, open-source Windows clean-up and tune-up tool that explains what it finds and asks before it changes anything.<br>
  Disk space, caches, old files, deleted-file recovery, startup apps, services, drivers, registry leftovers, and a performance check, in one app.
</p>

<p align="center">
  <a href="https://github.com/iamaussieark-del/pc-cleanup/releases/latest"><img alt="Download" src="https://img.shields.io/github/v/release/iamaussieark-del/pc-cleanup?label=download&style=flat-square"></a>
  <img alt="Windows 10 and 11" src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?style=flat-square">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square">
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-green?style=flat-square"></a>
</p>

---

## Contents

- [Download and run](#download-and-run)
- [What it does](#what-it-does)
- [How it keeps your PC safe](#how-it-keeps-your-pc-safe)
- [Privacy](#privacy)
- [Requirements](#requirements)
- [Run from source](#run-from-source)
- [Build the exe yourself](#build-the-exe-yourself)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Contributing](#contributing)
- [Licence and disclaimer](#licence-and-disclaimer)

## Download and run

1. Download **PC Cleanup.exe** from the [latest release](https://github.com/iamaussieark-del/pc-cleanup/releases/latest).
2. Double-click it. Nothing needs installing: Python and every library are inside the one file.
3. Windows asks for administrator. Say **Yes** for the full tool, or **No** to run with fewer features. The app tells you which parts need administrator.

> **"Windows protected your PC"?** The exe is not code-signed, which costs money every year, so Windows SmartScreen warns about any new download. Choose **More info**, then **Run anyway**. You can check the download against the SHA-256 checksum in the release notes, or [build it yourself](#build-the-exe-yourself) from this source.

The first time it opens, it offers a full scan. Nothing is deleted by a scan.

## What it does

Every page explains what it found in plain words. Anything that changes your PC asks first, and most changes can be put back.

### Overview and disk map
- Every internal drive: free space, SSD or hard drive, health, and model.
- **Where the space is**: a chart per drive, your largest folders, the biggest installed apps, and the biggest files.
- **Folder breakdown**: a fast, expandable tree of the whole drive. Open any folder in place, see its share of its parent and of the drive, and open it in File Explorer.
- Points out what is worth a look: a nearly full drive, a huge Recycle Bin, a piled-up temp folder, a leftover `Windows.old`, a large hibernation file.

### Performance
- A live processor and memory meter, a health score, and plain findings: a restart Windows is waiting for, days since the last restart, memory pressure, too many startup apps, a nearly full system drive, weekly drive optimisation turned off.
- **Boost** with one click, and **Undo boost** with another. It changes ordinary Windows settings only: a faster power plan (it leaves a custom plan alone), Game Mode, window animations, and transparency on low-memory PCs. It shows what will change before it changes it.
- **Apps using the most** memory and processor, grouped by program, with **End task**. Parts of Windows cannot be ended from here.
- **Optimize drives** runs Windows' own TRIM for SSDs and defragmenting for hard drives.
- There is deliberately no "RAM cleaner". Emptying memory only makes Windows load it all again, which is slower.

### Caches
Measures and clears caches that programs rebuild on their own:
- Windows and user temp files, thumbnail and icon caches, internet cache, error reports, crash dumps, `MEMORY.DMP`, Delivery Optimization, Windows Update downloads, and the font cache.
- Browser page caches for Chrome, Edge, Brave, Vivaldi, Opera, and Firefox. History, cookies, bookmarks, and passwords are never touched.
- App caches for Discord, Slack, Teams, Steam, VS Code, Cursor, Epic Games, and Battle.net.
- GPU shader caches (DirectX, NVIDIA, AMD), and the pip and npm package caches.
- The Recycle Bin.

Safe items are ticked for you. App and system caches are listed for you to choose. Files changed in the last two minutes are skipped, in case something is using them.

### Old files
Finds large personal files you have not used in a long time and scores how likely each one is to be a leftover. It looks at age, size, file type, name, and folder. Old installers, disk images, and logs sitting in Downloads are ticked. Photos, documents, and videos never are. Files go to the Recycle Bin unless you choose to delete them for good. Windows and program folders are never touched.

### Recover deleted files
Finds files you deleted:
- In the **Recycle Bin**, including deleted folders. Put them back where they were, or copy them to a folder.
- On **NTFS drives after the bin was emptied**, by reading the drive's file table (needs administrator). Each file is rated **Good**, **Poor**, or **Name only**, from whether its old disk space has been reused.

Restoring onto the drive you are recovering from is refused, because writing there can destroy the very data you are trying to save. On an SSD, files emptied from the Recycle Bin are usually erased by the drive within minutes. The app detects this, and reports it instead of saving an empty file.

### Services
Every Windows service, with its memory, processor use, and what it is for. Optional Windows services and third-party ones can be set to **Manual** (start only when needed), **Automatic**, or stopped. Security and core Windows services are locked.

### Startup
Programs that start when you sign in, from the registry and the Startup folders. Turn any of them off without uninstalling it, and turn it back on later from the same list. Entries pointing at a missing program are flagged.

### Drivers
Checks the driver behind every device, and looks for newer ones in three places at once:

| Source | What it covers |
|---|---|
| **Windows Update** | Drivers Microsoft offers this PC. Installed in-app. |
| **Microsoft Update Catalog** | Every driver version Microsoft has published. A result only counts when the package lists one of the device's exact hardware IDs, so it never offers a package built for someone else's hardware. Installed in-app, after a checksum check. |
| **NVIDIA** | The latest GeForce Game Ready driver, usually weeks before Microsoft has it. Opens the official download. |

It also:
- Flags devices with problems, devices with no driver, and maker's drivers more than four years old.
- Shows graphics drivers in NVIDIA's own version numbers, such as 617.14.
- Finds **old driver packages** that no device uses any more (often hundreds of MB) and removes them safely.
- Lets you right-click any device and choose **Search the web for a driver**, which searches by name, or by the exact hardware ID when Windows does not know the name.

A restore point is made before drivers are installed, and Windows only installs a driver it ranks as a better match than the current one.

### Registry
Finds leftovers that point at files which no longer exist: startup entries, uninstall records, application paths, shared DLL counts, application registrations, help files, fonts, installer folders, and cached program names. A `.reg` backup is saved before each change. This tidies the registry. It will not make a healthy PC faster, and the app says so.

### AI analyst (optional)
Builds a plain-text report of the scan that you can read first. Only when you press **Analyze with AI** is it sent: to ChatGPT (sign in with the ChatGPT plan you already have), or to any OpenAI-compatible API (OpenAI, OpenRouter, or a model running on your own PC in LM Studio). The report contains folder sizes, program names, and settings. It never contains file contents, passwords, or browsing history.

### Activity
A local log of every scan and change, kept on your PC.

## How it keeps your PC safe

- **Nothing changes without asking.** Scans only read. Every change shows what it will do, and needs a click to confirm.
- **Deletes stay inside known cache folders.** The cache cleaner checks every path against an approved list before deleting, and refuses links and junctions.
- **Personal files go to the Recycle Bin** unless you choose permanent delete. Windows and Program Files are off-limits.
- **Changes can be undone.** There is a `.reg` backup before registry edits, a restore point before driver installs, Undo boost for performance settings, and a way to turn startup entries back on.
- **Windows' own tools do the risky work.** Drivers install with `pnputil`, which refuses to replace a driver with a worse one, and drives are optimised with Windows' own `defrag`.
- **Downloads are verified.** Catalogue drivers are checked against Microsoft's published checksum before they are installed.
- **Self-test.** `PC Cleanup.exe --self-test` runs checks against a scratch folder and a synthetic NTFS volume. They cover the delete gates, file scoring, deleted-file recovery, the saved-scan format, the driver matching rules, and every page of the window.

## Privacy

Everything runs on your PC. The app talks to the internet only for:

- **Driver lookups** on the Drivers page: Windows Update, the Microsoft Update Catalog, and NVIDIA. Only hardware and product IDs are sent. Nothing about your files is sent.
- **The AI analyst**, and only when you press Analyze.

Settings, logs, backups, and the last scan are kept in `%LOCALAPPDATA%\PcCleanup`. Delete that folder to reset the app.

## Requirements

**For the exe:**
- Windows 10 or Windows 11, 64-bit.
- Administrator for the system-wide features: services, all-users startup items, drive recovery, driver installs, drive optimisation, and some caches. Everything else works without it.
- An internet connection only for driver lookups and the AI analyst. The rest works offline.

**For running from source:**
- Python 3.10 or newer, 64-bit.
- The packages in `requirements.txt`: CustomTkinter, psutil, and cryptography.

**Notes:**
- On Windows 10 and early Windows 11, the Drivers page reads devices through WMI instead of `pnputil`. It works the same, but the old-driver-package clean-up needs a newer Windows 11.
- The app follows your Windows display scaling, and fits on small laptop screens.
- On networks that use a proxy, driver lookups go through the proxy set in Windows or in `HTTPS_PROXY`.
- The app is built to work in any Windows display language. It reads system tools' output in the right character set and uses language-neutral data wherever Windows offers it.

## Run from source

```bat
git clone https://github.com/iamaussieark-del/pc-cleanup.git
cd pc-cleanup
run.bat
```

`run.bat` finds Python 3.10 or newer, installs the requirements the first time, and starts the app. It ignores the Microsoft Store "python" placeholder. You can also run `python main.py`. Add `--no-admin` to skip the administrator prompt, or `--self-test` to run the checks.

## Build the exe yourself

Double-click **`build.bat`**. It:

1. Finds Python 3.10 or newer. If there is none, it downloads the official installer from python.org, checks it is signed by the Python Software Foundation, and installs it for your user only, with no administrator prompt.
2. Creates a private build environment in `.build-venv`, so your own Python setup is left alone.
3. Installs the requirements and PyInstaller.
4. Builds **`dist\PC Cleanup.exe`**, a single file that runs on any Windows 10 or 11 PC with nothing else installed.

The build recipe is `PC Cleanup.spec`. The version details shown under the exe's Properties come from `version_info.txt`.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Windows protected your PC" | The exe is not code-signed. Choose **More info**, then **Run anyway**, or build it yourself. |
| An antivirus flags the exe | Some antivirus tools distrust any app packed with PyInstaller. The source is all here, and you can build the exe yourself. Please report it as a false positive to the antivirus vendor. |
| A page says it needs administrator | Click **Restart as admin** at the bottom left. |
| The Drivers page cannot reach the online sources | Check the internet connection or proxy. Devices and problems are still listed. |
| `build.bat` says PC Cleanup.exe is open | Close the app, then run the build again. |
| Something went wrong | The **Activity** page and `%LOCALAPPDATA%\PcCleanup\cleanup.log` have the details. Please include them in an issue. |

## Project layout

```
main.py                  starts the app (asks for administrator, checks the Python version)
run.bat                  run from source
build.bat                build dist\PC Cleanup.exe
PC Cleanup.spec          PyInstaller recipe
version_info.txt         version details shown in the exe's Properties
tools/get-python.ps1     downloads and checks the official Python installer, for build.bat
cleanup_app/
  ui.py                  main window, navigation, page caching, background jobs
  ui_tree.py             fast tree and table views, and the folder breakdown window
  ui_performance.py      Performance page
  ui_drivers.py          Drivers page
  ui_recover.py          Recover page
  space.py               multi-threaded disk map
  junk.py                cache categories, measuring and cleaning
  filescan.py            old and large file finder and scoring
  recover.py             Recycle Bin and NTFS deleted-file recovery
  drivers.py             devices, problems, driver packages, installs
  driver_lookup.py       Update Catalog and NVIDIA lookups
  performance.py         performance check and reversible speed-ups
  procsnap.py            every process's memory and CPU from one system call
  services_scan.py       services
  startup_scan.py        startup programs
  registry_scan.py       registry leftovers and .reg backups
  actions.py             overview summary, and deleting personal files
  scan_store.py          saving and loading the last scan
  ai_analyst.py          local report, and OpenAI-compatible APIs
  chatgpt_login.py       Sign in with ChatGPT
  safety.py              path gates for every delete
  selftest.py            the --self-test checks
  ntfs_fixture.py        a small NTFS volume image for the recovery tests
```

## Contributing

Issues and pull requests are welcome. Before you open a pull request:

1. Run `python main.py --self-test`, and make sure it ends with `Self-test passed.`
2. Keep every change asking first, and keep it reversible where possible.
3. Explain findings in plain words, as the rest of the app does.

## Licence and disclaimer

Released under the [MIT licence](LICENSE).

This tool changes system settings and deletes files when you ask it to. It is provided as is, without warranty of any kind. Back up anything important before cleaning, and read each confirmation before you accept it.
