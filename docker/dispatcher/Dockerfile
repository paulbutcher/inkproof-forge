FROM python:3.14.10-slim
RUN useradd --uid 1000 --create-home dispatcher && mkdir /state && chown dispatcher /state
COPY --chmod=0755 dispatcher.py /usr/local/bin/dispatcher
COPY --chmod=0755 activity.py /usr/local/bin/activity
USER dispatcher
CMD ["/usr/local/bin/dispatcher"]
