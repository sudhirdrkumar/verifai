FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        postgresql \
        postgresql-contrib \
        curl \
        git \
        sudo \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash ubuntu || true \
    && echo "ubuntu ALL=(ALL) NOPASSWD: /usr/sbin/service postgresql *" >> /etc/sudoers.d/ubuntu-postgresql \
    && chmod 0440 /etc/sudoers.d/ubuntu-postgresql \
    && service postgresql start \
    && sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"

USER ubuntu
WORKDIR /home/ubuntu
