FROM rockylinux:8.6
ENV PATH "/opt/MegaRAID/storcli:/opt/MegaRAID/MegaCli:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

RUN curl -sSL -o /tmp/storcli-007.1616.0000.0000-1.x86_64.rpm https://downloads.linux.hpe.com/SDR/repo/mcp/centos/8.0/x86_64/current/storcli-007.1616.0000.0000-1.x86_64.rpm && \ 
    rpm -ivh /tmp/storcli-007.1616.0000.0000-1.x86_64.rpm && \
    yum -y install python39 && \
    pip3 install prometheus_client && \
    rm -rf /root/.cache/ && \
    yum clean all && \
    rm -rf /var/cache/yum

COPY ./storcli.py /storcli.py

EXPOSE 9909
ENTRYPOINT ["/usr/bin/python3", "-u", "/storcli.py"]
