#!/bin/sh
# Runs once, on first start of an empty Postgres volume.
# Creates the owner (migrations) and runtime (API/worker) logins so the
# migration never needs CREATEROLE and the app never owns its tables.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE ROLE remembrance_app NOLOGIN;
CREATE ROLE remembrance_token_reader NOLOGIN;
CREATE ROLE remembrance_owner LOGIN PASSWORD '${REMEMBRANCE_OWNER_PASSWORD}';
CREATE ROLE remembrance_api LOGIN PASSWORD '${REMEMBRANCE_API_PASSWORD}' IN ROLE remembrance_app;
CREATE DATABASE remembrance OWNER remembrance_owner;
SQL
