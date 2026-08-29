ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8

# Install build dependencies
RUN apk update && \
    apk upgrade && \
    apk add --no-cache --virtual build-deps \
        alpine-sdk cmake git libusb-dev pulseaudio-dev \
        soapy-sdr-dev fftw-dev

# Install SoapySDR runtime
RUN apk add --no-cache soapy-sdr

# Build rx_tools (provides rx_fm - SoapySDR equivalent of rtl_fm)
RUN cd /tmp && \
    git clone https://github.com/rxseger/rx_tools.git && \
    cd rx_tools && \
    mkdir build && cd build && \
    cmake .. && \
    make && \
    make install && \
    cd / && rm -rf /tmp/rx_tools

# Build SoapyHackRF driver
RUN cd /tmp && \
    git clone https://github.com/mossmann/hackrf.git && \
    cd hackrf/host && \
    mkdir build && cd build && \
    cmake .. -DINSTALL_UDEV_RULES=ON && \
    make && make install && \
    cd /tmp && \
    git clone https://github.com/pothosware/SoapyHackRF.git && \
    cd SoapyHackRF && \
    mkdir build && cd build && \
    cmake .. && \
    make && \
    make install && \
    cd / && rm -rf /tmp/hackrf /tmp/SoapyHackRF

# Build SoapyRTLSDR driver (for RTL-SDR dongles)
RUN cd /tmp && \
    git clone https://gitea.osmocom.org/sdr/rtl-sdr.git && \
    cd rtl-sdr && \
    mkdir build && cd build && \
    cmake ../ -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON && \
    make && make install && \
    cd /tmp && \
    git clone https://github.com/pothosware/SoapyRTLSDR.git && \
    cd SoapyRTLSDR && \
    mkdir build && cd build && \
    cmake .. && \
    make && \
    make install && \
    cd / && rm -rf /tmp/rtl-sdr /tmp/SoapyRTLSDR

# Install multimon-ng for FLEX pager decoding
RUN cd /tmp && \
    git clone https://github.com/EliasOenal/multimon-ng.git && \
    cd multimon-ng && \
    mkdir build && cd build && \
    cmake .. && \
    make && \
    make install && \
    cd / && rm -rf /tmp/multimon-ng

# Cleanup build environment and install runtimes
RUN apk del build-deps && \
    apk add --no-cache libusb pulseaudio fftw py3-pip py3-requests py3-geopy sqlite

# Install Python dependencies
RUN pip3 install --no-cache-dir --break-system-packages aiohttp paho-mqtt==1.6.1

# Copy S6 service and set permissions
COPY rootfs /
RUN chmod +x /etc/services.d/meldkamer/*

# Copy application code
COPY src /app/src

# Set working directory
WORKDIR /app

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Labels
ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION

LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version=${BUILD_VERSION} \
    maintainer="cyberjunky" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.created=${BUILD_DATE} \
    org.opencontainers.image.revision=${BUILD_REF} \
    org.opencontainers.image.version=${BUILD_VERSION}
