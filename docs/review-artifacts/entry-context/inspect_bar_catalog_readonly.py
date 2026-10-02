"""Read-only catalog check for a hidden bar revision-timestamp trigger."""
import json

import psycopg
from project_mai_tai.settings import Settings

settings = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
dsn = settings.database_url.replace('postgresql+psycopg://', 'postgresql://')
with psycopg.connect(dsn, connect_timeout=5, options=(
    '-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000'
)) as connection:
    with connection.cursor() as cursor:
        cursor.execute('''SELECT tgname, pg_get_triggerdef(oid) FROM pg_trigger
            WHERE tgrelid='strategy_bar_history'::regclass AND NOT tgisinternal''')
        print(json.dumps({'noninternal_bar_triggers': cursor.fetchall()}))
        cursor.execute('''SELECT column_name, column_default FROM information_schema.columns
            WHERE table_name='strategy_bar_history' AND column_name IN ('created_at','updated_at')''')
        print(json.dumps({'timestamp_defaults': cursor.fetchall()}))
        cursor.execute("SELECT now(), current_setting('transaction_read_only')")
        print(json.dumps({'capture': cursor.fetchone()}, default=str))
