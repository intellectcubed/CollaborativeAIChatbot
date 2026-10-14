# retrieve_groupme_history.py

Downloads the complete message history of a GroupMe group into a JSON Lines file.

## Setup

```bash
cd CollaborativeAIChatbot
source .venv/bin/activate
pip install -r requirements.txt   # only needed if the venv is recreated
```

Get an access token at https://dev.groupme.com (log in, then "Access Token") and export it:

```bash
export GROUPME_TOKEN=your_token_here
```

## Usage

Find your group ID:

```bash
python scripts/retrieve_groupme_history.py --list-groups
```

Download the history:

```bash
python scripts/retrieve_groupme_history.py --group-id 12345678
```

Output goes to `data/groupme_<group_id>.jsonl` (override with `--output`). Messages are sorted oldest-first, one JSON object per line, with GroupMe's original fields (`id`, `created_at`, `name`, `sender_id`, `text`, `attachments`, ...).

## Notes

- The API returns up to 100 messages per request, newest first. The script pages backward with `before_id` until it gets a 304 or an empty page.
- Rate limits (429) and 5xx errors are retried with exponential backoff.
- The downloaded data contains real names and message text. Keep `data/` out of version control and never send it to an LLM or other external service without redacting any incident or caller details first.
