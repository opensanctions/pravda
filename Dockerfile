FROM mcr.microsoft.com/playwright:v1.61.0-noble

# The base image has browsers but not the playwright npm package.
# Install it globally so xvfb-run doesn't hang on npx download.
RUN npm install -g playwright@1.61.0

# Install Google Chrome and its system dependencies.
# The base image has Chromium but not branded Chrome.
RUN playwright install --with-deps chrome

# Disable Chrome's built-in PDF viewer so PDFs download instead of being
# consumed by the viewer component extension. Without this, navigating to a
# PDF never exposes the body to Playwright — the viewer eats the stream.
RUN mkdir -p /etc/opt/chrome/policies/managed && \
    printf '{ "AlwaysOpenPdfExternally": true }\n' \
    > /etc/opt/chrome/policies/managed/pdf.json

# Run in headed mode inside a virtual framebuffer.
# Headed Chrome avoids headless-detection by websites.
CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", \
     "playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
