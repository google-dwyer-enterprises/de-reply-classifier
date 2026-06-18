# Deploy-pipeline test

Throwaway marker to test whether merging a PR triggers an automatic Railway
deploy (vs. the "external contributor — Needs approval" prompt).

Test: have the Railway workspace owner (support@dwyer-enterprises.com) merge this
PR using the default **"Create a merge commit"** button, then check whether
`lead-scrape-worker` / `lead-reviewer` deploy on their own or still say
"Needs approval".

Safe to delete after the test.
