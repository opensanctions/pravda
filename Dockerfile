FROM mcr.microsoft.com/playwright:v1.60.0-noble

# The base image has browsers but not the playwright npm package.
# Install it globally so xvfb-run doesn't hang on npx download.
RUN npm install -g playwright@1.60.0

# Run in headed mode inside a virtual framebuffer.
# Headed Chrome avoids headless-detection by websites.
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", \
     "playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
