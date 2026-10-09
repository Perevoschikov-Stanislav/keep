#!/bin/sh

# Then run the build
echo "Building"
NODE_OPTIONS="--max-old-space-size=8192" next build
