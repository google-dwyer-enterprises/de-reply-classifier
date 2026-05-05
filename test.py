import json
from collections import Counter

with open('debug/campaigns.json') as f:
    campaigns = json.load(f)

# Extract prefix (first segment before " | ")
prefixes = Counter()
no_pipe = []
for cid, name in campaigns.items():
    if ' | ' in name:
        prefix = name.split(' | ')[0].strip()
        prefixes[prefix] += 1
    else:
        no_pipe.append(name)

print("UNIQUE PREFIXES AND COUNTS:")
for prefix, count in prefixes.most_common():
    print(f"  {count:4d}  {prefix}")

print(f"\nCampaigns WITHOUT ' | ' delimiter: {len(no_pipe)}")
for name in no_pipe[:20]:
    print(f"  - {name}")