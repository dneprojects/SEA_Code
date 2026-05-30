#!/usr/bin/env sh
# Smart Energy Agent add-on entrypoint.
set -e

# Read add-on options (provided by Supervisor as /data/options.json) and export
# them as environment variables consumed by the Python app.
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
  export SEA_LOG_LEVEL="$(python3 -c "import json;print(json.load(open('$OPTIONS_FILE')).get('log_level','info'))")"
  export SEA_HISTORY_DB="$(python3 -c "import json;print(json.load(open('$OPTIONS_FILE')).get('history_db_path','/data/smart_energy_agent.db'))")"
  export SEA_HISTORY_DAYS="$(python3 -c "import json;print(json.load(open('$OPTIONS_FILE')).get('history_retention_days',90))")"
fi

cd /app
exec python3 -m smart_energy_agent.main
