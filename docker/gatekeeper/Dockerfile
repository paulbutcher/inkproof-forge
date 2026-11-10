FROM python:3.14.10-slim
RUN useradd --uid 1000 --create-home gatekeeper
COPY --chmod=0755 gatekeeper.py /usr/local/bin/gatekeeper
USER gatekeeper
CMD ["/usr/local/bin/gatekeeper"]
