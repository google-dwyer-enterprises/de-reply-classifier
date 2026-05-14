# Commands Cheat Sheet

Think of this project like a kitchen. Each command is a tool. You use them in the right order to cook the meal.

First, turn on the stove (every time you open a new terminal):

```bash
source venv/Scripts/activate
```

---

## The Daily Job (do this most days)

Pull new email replies, label them, and update the leads list.

```bash
python run.py refresh
```

That's it. One command. It does 4 things in order:
1. Get new replies from Instantly (the email tool).
2. Get the latest "interest status" for each lead from Instantly.
3. Read the new replies and tag each one (booked, interested, not_now, etc.).
4. Update the master leads table so NocoDB shows the new info.

If you want to look back further than the default:
```bash
python run.py refresh --days 7
```

---

## Adding New Leads (when you get a new Apollo export)

You got a CSV/xlsx of leads from Apollo. Load them into the database:

```bash
python run.py upload-leads <file>
```

Example:
```bash
python run.py upload-leads "original_data/leads.csv"
```

This also **auto-matches** those leads to SmartScout brands at the end. You don't need to do anything extra.

---

## Adding/Updating SmartScout Brands

You got a new SmartScout export of Amazon brand stats:

```bash
python run.py upload-smartscout <file>
```

After uploading new brand data, re-match all leads to the new brand list:

```bash
python run.py resolve-smartscout --rerun
```

(Optional) Use AI to match the leads that fuzzy matching wasn't sure about. Costs about **$1**:

```bash
python run.py llm-resolve-smartscout --dry-run    # see cost first
python run.py llm-resolve-smartscout --yes        # do it for real
```

---

## The Pieces of `refresh` (if you want to run them one by one)

You almost never need these alone. They're inside `refresh` already.

```bash
python run.py sync                # 1. get new replies
python run.py refresh-status      # 2. get lead "interest status"
python run.py classify            # 3. label replies with AI
python run.py update-status       # 4. update leads table
```

---

## Other Helpful Commands

| Command | What it does |
|---|---|
| `python run.py backfill-tags` | Adds campaign tags to old replies that are missing them |
| `python run.py resolve-companies` | Uses AI to fix confusing company names |
| `python run.py export` | Old way to make an Excel file (NocoDB is the new way) |

---

## Typical Workflows

### Most days
```bash
source venv/Scripts/activate
python run.py refresh
```

### First-time setup (or you have BOTH new brands AND new leads)
**Always brands first, leads second.** `upload-leads` auto-matches against whatever brands are in the database — so brands need to be there first.
```bash
source venv/Scripts/activate
python run.py upload-smartscout "original_data/brands-seller.csv"   # 1. brands first
python run.py upload-smartscout "original_data/brands-vendor.csv"   # 2. brands first
python run.py upload-leads "original_data/leads.csv"         # 3. then leads (auto-matches)
python run.py refresh
```

### Got a new Apollo export (brands already loaded)
```bash
source venv/Scripts/activate
python run.py upload-leads "original_data/new_leads.csv"     # auto-matches at the end
python run.py refresh
```

### Got a new SmartScout export only
```bash
source venv/Scripts/activate
python run.py upload-smartscout "original_data/new_brands.csv"
python run.py resolve-smartscout --rerun                     # re-match all existing leads
python run.py llm-resolve-smartscout --yes                   # optional, costs ~$1
```

### Oops — uploaded leads before brands
```bash
python run.py resolve-smartscout --rerun     # redo the matching
```

### Brand new prompt for the AI labeler
1. Bump `PROMPT_VERSION` in `config.py`.
2. Edit `prompts/classifier.txt`.
3. Re-label everything: `python classify.py --reclassify`
4. Push the new labels to the leads table: `python run.py update-status`
