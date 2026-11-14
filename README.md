# OnQ Agent

OnQ Agent is a Windows automation app for Queen's onQ. It collects course materials, preserves file revisions, builds searchable PDF and Markdown bundles, and can publish verified bundles to the ChatGPT Projects you assign. The agent runs on your computer, and the dashboard shows what each run collected, published, or needs you to review.

## Get started

Open **OnQ Agent** from the desktop shortcut or run `release\win-unpacked\OnQ Agent.exe`. Keep the entire `win-unpacked` folder together when moving the portable build.

1. Connect onQ and complete sign-in and any two-factor authentication in the dedicated Chrome window.
2. Select **Discover courses** after signing in. A fresh installation starts with an empty course list. In **Courses**, select **Collect this course** for each course you want to track. **File Library** shows those courses. Deselecting a course keeps its archived materials on disk.
3. Confirm the saved ChatGPT Project URL for each course in its settings. In **Ready for ChatGPT → ChatGPT Publishing**, open dedicated Chrome to sign in to ChatGPT, then close that sign-in window to release the profile.
4. Use **Sync all courses** or **Sync now** on a course to collect materials and stage its bundle. If the bundle already exists, use **Queue desktop agent** to stage it for the saved project.
5. For a new project, choose **Verify and publish** in **ChatGPT Publishing**. Review the course and exact URL, then confirm. The agent checks that Sources is empty and the staged bundle fits, uploads the course files, and verifies the exact inventory and each downloaded file. After verification, you can choose **Publish after sync**.
6. Review **Activity** for sync results and **Ready for ChatGPT** for the current local and verified project releases.

## Scheduled runs and the Windows tray

By default, the agent checks enabled courses at **7 p.m. Toronto time**. Use **Automations** to adjust the schedule or pause it. Courses can have their own schedules. Manual sync remains available while schedules are paused.

The agent starts in the Windows system tray at sign-in. Closing the dashboard hides it. Double-click the tray icon or choose **Open OnQ Agent** from its menu to bring it back. The dashboard opens to **Activity** after a scheduled batch finishes, including when the run needs attention.

While the batch runs, the worker opens or reuses dedicated Chrome windows and pages as needed. It opens onQ for collection and opens ChatGPT when there is a staged release to publish to an eligible project. Chrome uses the saved sessions. You may need to sign in or complete browser verification if a session expires. The computer must be awake and the worker running. After sleep or shutdown, the worker can catch up on a missed run when it becomes available.

## Publishing and recovery

- If Sources already contains files, use **Manual upload** for that project or save an empty project link and stage the bundle again. Changing the saved destination resets verification and the automatic publishing setting.
- After an interruption, retry **Verify and publish** for the same staged bundle. The agent checks its upload journal and downloads files left by that attempt before it continues. If a previous upload cannot be confirmed, check Sources and follow the action shown in the dashboard.
- **Resume publishing** sends already staged, current releases for eligible courses. It processes one course at a time and skips verified releases. After reconnecting ChatGPT, click it to publish pending bundles without collecting again.
- **Publish now** runs publishing for an individual eligible course. **Publish after sync** lets scheduled and manual syncs publish new releases automatically.
- The agent verifies the project destination, file and storage limits, final Sources inventory, and downloaded file bytes. It only removes verified app-owned files and keeps rollback copies. If the result of an upload or removal is uncertain, it checks its journal and the project before proceeding.
- The manual fallback is **Ready for ChatGPT → Manual upload**. Prepare the files, open the upload folder and project, and confirm the uploads you completed. Manual confirmations are shown separately from verified releases.

## Local files and privacy

New originals use their Toronto collection time in the filename. New or changed ChatGPT bundle files use the local release time. When a file is unchanged, it keeps its existing name.

The default archive is the Windows Documents folder under `onQ Library`. Existing installs keep their configured archive and their metadata, run history, and dedicated Chrome profiles in `%LOCALAPPDATA%\onQ Library`. The older folder name is retained for compatibility. The package and dashboard use the OnQ Agent name.

- **Originals** stores immutable revisions, retrieval dates, and source URLs.
- **Project Bundles** stores PDF and Markdown releases with a source inventory and page mappings.
- **Harness Queue** stores staged publishing snapshots. **Upload Queue** stores copies prepared for manual upload.
- **Converted** caches Office-derived PDFs. Word and PowerPoint conversion uses installed Office with macros disabled. Notebook and code content is exported as text.

OnQ Agent is an independent project for Queen’s onQ. The collector uses your authenticated session for course-content reads. Use ChatGPT publishing only for materials you have permission to upload. It reports unavailable materials and external resources in coverage notes. Dedicated Chrome session snapshots are encrypted for the Windows user using DPAPI. The local service binds to loopback and restricts mutations to the local dashboard.

## Install, upgrade, or move

The portable build requires Windows x64 and installed Chrome. Office is needed for Word and PowerPoint conversion. To build and install from source, use `install.ps1` with Python 3.11+ and Node.js LTS installed. It builds the app and creates the desktop and Windows sign-in shortcuts. To upgrade an existing installation, wait for the worker to become idle, exit the dashboard or tray app, and run `stop-worker.ps1` before building or starting the new version.

For a new archive on another computer, export and import settings, then reconnect onQ and ChatGPT. To preserve versions and publication history, stop both workers, copy the archive structure and a consistent copy of `library.sqlite3`, then run `relocate.py` with the copied archive path before starting the destination worker. Reconnect in dedicated Chrome on the destination machine and run schedules on one machine at a time.

## Development

The worker uses Python, FastAPI, SQLite, Playwright, and pypdf. The dashboard uses React, TypeScript, Vite, and Electron.

```powershell
.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix desktop test
.\build-desktop.ps1
```

Worker logs are under `%LOCALAPPDATA%\onQ Library\service.log`.
