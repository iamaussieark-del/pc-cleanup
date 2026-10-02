# Changelog

## 1.1.0 — first public release

### New
- **Performance page.** A live processor and memory meter, a health score with plain findings, and the apps using the most memory and processor, with End task. **Boost** changes a small set of ordinary Windows settings, and **Undo boost** puts them all back:
  - a faster power plan (custom plans are left alone)
  - Game Mode
  - window animations
  - transparency, on PCs with little memory

  **Optimize drives** runs Windows' own TRIM and defragmenting.
- **Drivers page.** Checks every device's driver and looks for newer ones in three places at once:
  - Windows Update
  - the Microsoft Update Catalog, matched to each device's exact hardware IDs
  - NVIDIA's own driver service, for GeForce cards

  It installs Windows Update and Catalog drivers in-app, after making a restore point and checking Microsoft's published checksum. It also finds unused old driver packages and removes them safely. Right-click a device to search the web for its driver.
- **Recover page.** Finds deleted files in the Recycle Bin, including deleted folders, and on NTFS drives after the bin was emptied. Each file is rated by its chance of recovery. Restoring onto the drive being recovered from is refused. Files an SSD has already erased are reported, instead of being restored as empty files.
- **A faster folder breakdown.** It is now an expandable tree that opens instantly. It shows each folder's share of its parent and of the drive, with Open in Explorer and Copy path.
- **`PC Cleanup.exe`**: one file that runs without Python, built by `build.bat`. `build.bat` installs Python by itself when it is missing.
- An app icon, and version details in the exe's Properties.

### Faster
Measured on a PC with 3 million files and 1.3 TB in use:

| | Before | After |
|---|---|---|
| Expand a folder in the folder breakdown | 1.3–2.6 s | 18 ms |
| Switch back to a page | 0.5–1.4 s | 20–50 ms |
| Map the whole disk | 36.9 s | 16.2 s |
| Old-files scan | 54 s | 6.2 s |
| Cache scan | 4.2 s | 1.0 s |
| Services scan | 1.1 s | 0.19 s |
| Overview refresh | 2.8 s | 1.1 s |
| Window pause after each save | ~650 ms | ~8 ms |

How:
- The scanners use the file information Windows already returns while listing a folder, instead of asking for it again for every file.
- Every process's memory is read with one system call.
- Built pages are kept instead of rebuilt on every click.
- Background work no longer waits on the window.
- The saved scan is split into a small file and a compact folder tree, and dropped from 12 MB to under 3 MB.

### Fixed
- **Services page:** the Scan, Stop, Manual, and Automatic buttons crashed.
- **Cache cleaner:** it could empty a folder that was not on the approved list (for example, from an edited saved scan) while reporting that it had skipped it.
- **Deleted-file recovery** would not have worked on a real drive. Reads from a raw drive were not whole sectors, so every file's chance showed as Unknown and restores failed. Recycle Bin items could not be restored to a folder on another drive.
- **Text in other languages:** system tools' output was read in the wrong character set. Names with letters such as ü were garbled, or stopped a scan.
- **Windows on high-DPI screens:** windows could open off-centre or partly off-screen. The minimum window size was taller than a 1920×1080 laptop at 150% scaling.
- **Windows 10:** the Drivers page now works there too.
- **Driver dates:** Windows' device database swaps day and month on PCs with day-first date formats. The app now reads dates from the driver files themselves.
- **Driver lookups:** they now work behind an office or school proxy.
- **Starting the app:** `run.bat` no longer picks the Microsoft Store "python" placeholder, and both launchers explain clearly when Python is missing or too old.

## 1.0.0
- First version, not published: overview and disk map, caches, old files, services, startup, registry leftovers, AI analyst, and activity log.
