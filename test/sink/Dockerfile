# Test-only image. Never published.
FROM python:3.13-alpine

RUN apk add --no-cache openssl \
    && pip install --no-cache-dir aiosmtpd==1.4.6 cryptography==44.0.0

COPY sink.py gen_ca.py /opt/

WORKDIR /opt
ENTRYPOINT ["python", "-u", "/opt/sink.py"]
