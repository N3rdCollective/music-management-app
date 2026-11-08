"""Run the loopback review desk: python3 -m djlibreview"""

import sys

from djlibreview.server import main

raise SystemExit(main(sys.argv[1:]))
