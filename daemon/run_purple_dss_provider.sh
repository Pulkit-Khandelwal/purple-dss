#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export DSS_SERVER=${DSS_SERVER:-https://dss.itksnap.org}
if [[ -f /data/pulkit/pulks_env/bin/activate ]]; then
  source /data/pulkit/pulks_env/bin/activate
fi
if [[ $# == 0 ]]; then set -- run; fi
exec python3 "$script_dir/purple_dss_v120.py" "$@"
