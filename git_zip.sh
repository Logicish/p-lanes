#!/bin/bash
MSG="${1:-Updates}"
git add .
git commit -m "$MSG"
git archive --format=zip --output=p-lanes-src.zip HEAD setup.py src/
echo "Done → p-lanes-src.zip"