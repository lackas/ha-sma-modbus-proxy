ARG BUILD_FROM
FROM python:3.12-alpine

RUN pip3 install --no-cache-dir pymodbus==3.12.1 websockets==14.2

COPY sma_proxy.py /

ENTRYPOINT ["python3", "/sma_proxy.py", "--options", "/data/options.json"]
