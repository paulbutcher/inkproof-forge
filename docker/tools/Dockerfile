# Admin tools run against Forgejo's API: bootstrap and tests (which also read a
# copy of its database, for what the API leaves out). Known bash and
# jq versions, whatever the host has.
FROM alpine:3.24
RUN apk add --no-cache bash curl jq git openssl coreutils sqlite
COPY --chmod=0755 kit /usr/local/bin/kit
COPY --chmod=0755 lib/ /usr/local/lib/kit/
ENV HOME=/tmp
ENTRYPOINT ["/usr/local/bin/kit"]
