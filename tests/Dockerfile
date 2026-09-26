FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends systemd systemd-sysv python3 curl ca-certificates gnupg openssl procps iproute2 e2fsprogs && rm -rf /var/lib/apt/lists/*
COPY . /project
WORKDIR /project
ENV SERVER_MONITOR_TEST_CONTAINER=1
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
