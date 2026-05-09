#!/usr/bin/with-contenv bashio

set -e

bashio::log.info "Starting SMS Gammu Gateway..."

DEVICE_PATH=$(bashio::config 'device_path')
DEVICE_WAIT_SECONDS=$(bashio::config 'device_wait_seconds')
DEVICE_WAIT_SECONDS=${DEVICE_WAIT_SECONDS:-20}

bashio::log.info "Configured modem device: ${DEVICE_PATH}"

if [ -d /dev/serial/by-id ]; then
    bashio::log.info "Available /dev/serial/by-id devices:"
    ls -la /dev/serial/by-id/ || true
fi

if [ ! -c "${DEVICE_PATH}" ]; then
    if [ "${DEVICE_WAIT_SECONDS}" -gt 0 ]; then
        bashio::log.warning "Device ${DEVICE_PATH} not found. Waiting up to ${DEVICE_WAIT_SECONDS}s for modem/udev..."
        END_TIME=$((SECONDS + DEVICE_WAIT_SECONDS))
        while [ $SECONDS -lt $END_TIME ]; do
            if [ -c "${DEVICE_PATH}" ]; then
                bashio::log.info "Device ${DEVICE_PATH} appeared."
                break
            fi
            sleep 2
        done
    fi
fi

if [ ! -c "${DEVICE_PATH}" ]; then
    bashio::log.warning "Device ${DEVICE_PATH} still not found. Please check the GSM modem connection and selected USB interface."
    bashio::log.info "Available tty devices:"
    ls -la /dev/tty* || true
fi

cd /app
exec python3 -u run.py
