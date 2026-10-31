# https://contributors.home-assistant.io/apps/guides/dockerfile/
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.23
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8
# CMake 4 rejects the old cmake_minimum_required() of the SDR projects built below
ARG CMAKE_POLICY_VERSION_MINIMUM=3.5
# Time for the app to stop the SDR pipeline cleanly on shutdown (see __main__.py)
ENV S6_SERVICES_GRACETIME=8000

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

# Build SoapyRTLTCP driver (network receiver: rtl_tcp server via rx_fm)
RUN cd /tmp && \
    git clone https://github.com/pothosware/SoapyRTLTCP.git && \
    cd SoapyRTLTCP && \
    mkdir build && cd build && \
    cmake .. && \
    make && \
    make install && \
    cd / && rm -rf /tmp/SoapyRTLTCP

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

# Copy application code, and config.yaml for the add-on version shown in the dashboard
COPY src /app/src
COPY config.yaml /app/config.yaml

# Set working directory
WORKDIR /app

# Set shell
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

LABEL \
    org.opencontainers.image.title="Meldkamer" \
    org.opencontainers.image.description="P2000 FLEX pager decoder dashboard for Home Assistant" \
    org.opencontainers.image.source="https://github.com/cyberjunky/addon-meldkamer" \
    org.opencontainers.image.licenses="MIT"
