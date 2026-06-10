FROM mcr.microsoft.com/playwright:v1.60.0-noble

RUN npx playwright install chrome

CMD ["xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1080x24", \
     "npx", "playwright", "run-server", "--port", "3000", "--host", "0.0.0.0"]
