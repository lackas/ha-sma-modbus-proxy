ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir --break-system-packages pymodbus==3.12.1 websockets==14.2

COPY sma_proxy.py /
COPY run.sh /

RUN chmod a+x /run.sh

CMD ["/run.sh"]
