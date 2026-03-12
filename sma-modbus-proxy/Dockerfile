ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN pip3 install --no-cache-dir pymodbus==3.12.1 websockets==14.2

COPY sma_proxy.py /
COPY run.sh /

RUN chmod a+x /run.sh

CMD ["/run.sh"]
