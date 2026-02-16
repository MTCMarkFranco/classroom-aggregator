# TDSB Classroom Assignment Aggregator

A Python console tool that scrapes **Google Classroom** and **Brightspace (D2L)** for incomplete assignments using browser automation, then displays a unified summary in the terminal.

Built for TDSB students whose schools use both platforms — no API keys or app registration required.

## How It Works

1. Launches a Chrome browser (headless by default)
2. Logs into Google Classroom via TDSB's Microsoft Entra SSO
3. Scrapes classes and incomplete assignments from Google Classroom
4. Reuses the SSO session to log into Brightspace
5. Scrapes classes and assignments from Brightspace
6. Displays a combined, color-coded report in the terminal

## Requirements

- Python 3.11+
- Google Chrome installed locally
- Playwright browser drivers

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser drivers
playwright install
```

Create a `.env` file in the project root (or copy and edit the example below):

```dotenv
TDSB_USERNAME=your_student_number@tdsb.ca
TDSB_PASSWORD=your_password
SEMESTER_CLASSES=ENG,GLE,PPL,History
HEADLESS=true
```

| Variable           | Description                                              |
|--------------------|----------------------------------------------------------|
| `TDSB_USERNAME`    | Your TDSB email (e.g. `123456789@tdsb.ca`)              |
| `TDSB_PASSWORD`    | Your TDSB password                                       |
| `SEMESTER_CLASSES`  | Comma-separated list of course codes to filter for       |
| `HEADLESS`         | `true` to hide the browser window, `false` to show it   |

## Usage

```bash
python main.py
```

### Options

| Flag          | Description                                |
|---------------|--------------------------------------------|
| `--headless`  | Force headless mode (overrides `.env`)     |
| `--debug`     | Enable verbose logging                     |
| `--username`  | Pass username on the command line           |
| `--password`  | Pass password on the command line           |

If credentials aren't set in `.env` or via flags, you'll be prompted interactively.

## Project Structure

```
├── main.py                     # Entry point and Rich output formatting
├── auth.py                     # TDSB SSO authentication (Google + Brightspace)
├── google_classroom_scraper.py # Google Classroom scraper
├── brightspace_scraper.py      # Brightspace (D2L) scraper
├── models.py                   # Data models (ClassInfo, Assignment, enums)
├── requirements.txt            # Python dependencies
├── .env                        # Credentials and configuration (not committed)
└── .gitignore
```

## Notes

- The tool uses Playwright to automate a real Chrome browser because neither Google Classroom nor TDSB's Brightspace instance offer student API access.
- On first run with `HEADLESS=false`, you can watch the login flow to verify it works correctly.
- The `SEMESTER_CLASSES` filter uses case-insensitive substring matching — `ENG` matches "ENG3U1 - English" etc.
