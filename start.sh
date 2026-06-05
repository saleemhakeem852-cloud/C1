#!/bin/bash
# Verify binaries are available and start server
echo "chromium: $(which chromium 2>/dev/null || echo NOT FOUND)"
echo "chromedriver: $(which chromedriver 2>/dev/null || echo NOT FOUND)"

# Virtual display for headed Chromium (CL rejects many headless fills as "autofilled")
if command -v Xvfb >/dev/null 2>&1 && [ -z "${XVFB_RUNNING}" ]; then
  Xvfb :99 -screen 0 1280x800x24 -ac >/tmp/xvfb.log 2>&1 &
  export DISPLAY=:99
  export XVFB_RUNNING=1
  sleep 1
  echo "Xvfb started on DISPLAY=$DISPLAY"
fi

exec python server.py