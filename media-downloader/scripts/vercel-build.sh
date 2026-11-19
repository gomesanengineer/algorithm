#!/bin/sh
# Builds the static website for Vercel. Set API_BASE in the Vercel project's
# Environment Variables to your Render server, e.g. https://media-grabber.onrender.com
set -e
if [ -z "$API_BASE" ]; then
  echo "ERROR: set the API_BASE environment variable in Vercel (your Render URL)." >&2
  exit 1
fi
rm -rf public
mkdir -p public
cp -r app/static/. public/
printf "window.API_BASE = '%s';\n" "$API_BASE" > public/config.js
echo "Built public/ with API_BASE=$API_BASE"
