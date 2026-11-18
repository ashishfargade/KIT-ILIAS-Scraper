# KIT ILIAS Sync — Quick README

Simple Python/Playwright command-line tool for syncing course files from KIT ILIAS. Traverses nested folders, supports visual section grouping, and skips forums by default. Existing downloads are retained.
* Only lecture materials - slides and pdfs are downloaded
* Ignores forum messages and lecture recordings
* Ignores literature

## First-time setup

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

**Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

**macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Run commands below from the project directory with the virtual environment activated. On Windows use `python .\ilias_sync.py` in place of `python ilias_sync.py` if you prefer; both forms work from PowerShell. **Use Python directly rather than `ilias.cmd` when passing URLs containing `&`.**

## First run (in order)

```bash
python ilias_sync.py login --terminal   # Prompt for KIT username/password (headless)
python ilias_sync.py init               # Add example course
python ilias_sync.py list               # Confirm registered courses
python ilias_sync.py check-auth         # Verify authenticated course access
python ilias_sync.py sync               # Download/sync all courses
```

Alternatively, use `python ilias_sync.py login` for a visible browser login; use `add` instead of `init` for custom courses.

## Everyday commands

```bash
python ilias_sync.py sync                   # Sync all registered courses
python ilias_sync.py sync --course [course_id] # Sync course
python ilias_sync.py list                   # List courses
python ilias_sync.py status                 # Show sync history
python ilias_sync.py check-auth             # Check saved login
python ilias_sync.py login --terminal       # Renew login if expired
```

## Manage courses

**PowerShell** (single-quote URLs to preserve `&`):

```powershell
python .\ilias_sync.py add 'https://ilias.studium.kit.edu/ilias.php?baseClass=ilrepositorygui&cmdClass=ilObjCourseGUI&cmd=view&ref_id=[course_id]' --name 'course_name'

python .\ilias_sync.py remove [course_id]
```

`add` also updates a registered course with the same ID. `remove` unregisters it **without deleting downloaded files**. On Linux/macOS, the same quoted URL commands work with `python ilias_sync.py`.

## Sync options

```bash
python ilias_sync.py sync --max-pages 500     # Increase traversal limit (default 250)
python ilias_sync.py sync --delay 1.5        # Wait 1.5 s between requests (default 0.7)
python ilias_sync.py sync --max-file-mb 1000 # Raise file limit (default 250 MB)
python ilias_sync.py sync --inspect-links    # Print discovered/ignored links and groups
python ilias_sync.py sync --show-browser     # Display Chromium during synchronization
python ilias_sync.py sync --save-pages       # Also save HTML/text snapshots
python ilias_sync.py --verbose sync         # Verbose logging (global option before command)
```

Options may be combined, e.g.:

```bash
python ilias_sync.py --verbose sync --course [course_id] --max-pages 500 --inspect-links
```

## Other commands

```bash
python ilias_sync.py --help                 # All commands
python ilias_sync.py sync --help            # Sync options (also: add/login/remove --help)
python -m unittest discover -v              # Run offline tests
python ilias_sync.py --data-dir ./my_data sync # Use separate data directory
```

`--data-dir` and `--verbose` must appear **before** the subcommand. If you use a custom data directory, use it consistently for login, add, and sync. To specify an optional direct IdP URL for headless login: `python ilias_sync.py login --terminal --sso-url 'URL'` (SSO execution URLs can expire).

## Data and security

Downloads and the course database are stored in `ilias_data/` by default. `session-state.json` and `browser-profile/` contain sensitive login-session data: do not upload or share them. Avoid syncing those authentication files to OneDrive or other cloud storage. The tool has offline tests but may need adjustments if KIT changes its authenticated ILIAS pages.
