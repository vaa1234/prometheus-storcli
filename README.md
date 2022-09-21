# prometheus-storcli
Script to parse StorCLI's JSON output and expose MegaRAID metrics via Prometheus client.

Based on https://github.com/prometheus-community/node-exporter-textfile-collector-scripts

Run via docker

```
docker run --rm -it --name prometheus-storcli --privileged -p 9909:9909 vaa12345/prometheus-storcli
```

Example grafana dashboard

![plot](./grafana%20example.jpg)