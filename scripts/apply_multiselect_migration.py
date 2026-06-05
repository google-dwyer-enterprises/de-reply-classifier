"""Migration: convert scrape_requests.{industries,skip_industries,countries}
from text[] (Postgres array) to text (NocoDB MultiSelect storage format).

NocoDB's form view has no widget for `uidt=SpecificDBType / dt=ARRAY` columns,
so they're invisible on the public submit form. By switching the storage to
comma-separated text and re-tagging the column UI type as MultiSelect (done
separately via the NocoDB API), Jam gets proper checkbox dropdowns in the
form for industries and countries.

Idempotent — checks the current data type before migrating, preserves any
existing values via array_to_string.

Run:
    python scripts/apply_multiselect_migration.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import connect


DDL = """
DO $$
DECLARE
    ind_is_array boolean;
    skip_is_array boolean;
    cnt_is_array boolean;
BEGIN
    SELECT data_type = 'ARRAY' INTO ind_is_array
      FROM information_schema.columns
     WHERE table_schema='public' AND table_name='scrape_requests'
       AND column_name='industries';
    SELECT data_type = 'ARRAY' INTO skip_is_array
      FROM information_schema.columns
     WHERE table_schema='public' AND table_name='scrape_requests'
       AND column_name='skip_industries';
    SELECT data_type = 'ARRAY' INTO cnt_is_array
      FROM information_schema.columns
     WHERE table_schema='public' AND table_name='scrape_requests'
       AND column_name='countries';

    IF coalesce(ind_is_array, false) THEN
        ALTER TABLE scrape_requests ADD COLUMN industries_new text NOT NULL DEFAULT '';
        UPDATE scrape_requests
           SET industries_new = COALESCE(array_to_string(industries, ','), '');
        ALTER TABLE scrape_requests DROP COLUMN industries;
        ALTER TABLE scrape_requests RENAME COLUMN industries_new TO industries;
        RAISE NOTICE 'industries: ARRAY -> text (NocoDB MultiSelect format)';
    ELSE
        RAISE NOTICE 'industries already text, skipping';
    END IF;

    IF coalesce(skip_is_array, false) THEN
        ALTER TABLE scrape_requests ADD COLUMN skip_industries_new text NOT NULL DEFAULT '';
        UPDATE scrape_requests
           SET skip_industries_new = COALESCE(array_to_string(skip_industries, ','), '');
        ALTER TABLE scrape_requests DROP COLUMN skip_industries;
        ALTER TABLE scrape_requests RENAME COLUMN skip_industries_new TO skip_industries;
        RAISE NOTICE 'skip_industries: ARRAY -> text';
    ELSE
        RAISE NOTICE 'skip_industries already text, skipping';
    END IF;

    IF coalesce(cnt_is_array, false) THEN
        ALTER TABLE scrape_requests ADD COLUMN countries_new text NOT NULL DEFAULT 'United States,Canada';
        UPDATE scrape_requests
           SET countries_new = COALESCE(array_to_string(countries, ','), 'United States,Canada');
        ALTER TABLE scrape_requests DROP COLUMN countries;
        ALTER TABLE scrape_requests RENAME COLUMN countries_new TO countries;
        RAISE NOTICE 'countries: ARRAY -> text (default ''United States,Canada'')';
    ELSE
        RAISE NOTICE 'countries already text, skipping';
    END IF;
END $$;
"""


def main() -> None:
    print("Connecting to production Supabase...")
    conn = connect()
    conn.autocommit = True

    print("Applying MultiSelect migration (idempotent)...")
    with conn.cursor() as cur:
        cur.execute(DDL)
        for n in conn.notices:
            print(f"  {n.strip()}")
    print("  DDL executed.\n")

    print("Verifying new column types:")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, column_default, is_nullable
              FROM information_schema.columns
             WHERE table_schema='public' AND table_name='scrape_requests'
               AND column_name IN ('industries', 'skip_industries', 'countries')
             ORDER BY column_name
        """)
        for name, dtype, default, nullable in cur.fetchall():
            print(f"  {name:22s} {dtype:10s} default={default!r:32s} nullable={nullable}")

    conn.close()
    print("\nDone. Next: run scripts/configure_nocodb_multiselect.py to set NocoDB MultiSelect UI on these columns.")


if __name__ == "__main__":
    main()
