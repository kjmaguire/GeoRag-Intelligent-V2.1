# Martin tile server, with this deployment's source list baked in.
#
# WHY AN IMAGE AT ALL, rather than running the upstream one directly:
# martin.yaml sets `auto_publish: false`, so Martin serves exactly the sources
# named in that file and nothing else. That file therefore has to reach the
# container. Under docker-compose it is a bind mount; Azure Container Apps has
# no equivalent that does not drag in an Azure Files share, a storage account
# and a mount definition — three moving parts to deliver one 26 KB text file
# that changes only when a migration adds a tile function.
#
# Baking it in also makes the config versioned with the code that depends on
# it: a deploy cannot land a new silver.*_by_project function without the
# martin.yaml entry that publishes it, because both ride the same commit.
#
# WHY auto_publish STAYS FALSE: Martin would otherwise publish every table
# with a geometry column it can see, which for this database includes the
# *_history audit tables and anything else in silver/public_geo — an
# accidental public surface. The explicit list is the allowlist.
#
# Pinned by digest, matching every other image in this repo. Martin 1.11.0 is
# the version docker-compose.yml already runs locally, so local and Azure
# serve identical tiles.
FROM ghcr.io/maplibre/martin:1.11.0@sha256:0650e9025f5fcffdc686358114679421b5e6b0ca37b374ad8a66f14709d59d2b

# Root only to place the config; the image drops back to its own user below.
USER root
COPY martin/martin.yaml /config/martin.yaml
RUN chmod 0444 /config/martin.yaml

# Upstream runs as a non-root user already. Re-assert it explicitly so a base
# image change cannot silently promote this container to root.
USER 1000:1000

# Container Apps health-probes this; Martin answers /health once its sources
# are loaded and validated, which on this config means after it has resolved
# all 19 PostGIS functions.
EXPOSE 3000

# DATABASE_URL is supplied by the container app as a secret reference. It is
# deliberately NOT given a default here: a wrong-but-present connection string
# fails at query time with a confusing error, while an absent one fails at
# boot with an obvious one.
ENTRYPOINT ["/usr/local/bin/martin"]
CMD ["--config", "/config/martin.yaml"]
