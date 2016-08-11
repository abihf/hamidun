FROM nginx:alpine

ADD hamidun.py /usr/bin/hamidun
ADD hamidun.conf /etc/hamidun.conf
ADD service /usr/bin/service

RUN apk add --no-cache python py-pip && \
    pip install docker-py && \
    apk del py-pip && \
    chmod +x /usr/bin/hamidun /usr/bin/service
    
CMD ["/usr/bin/service", "run"]
