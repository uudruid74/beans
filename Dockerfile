# beans — ONE container, ONE source of truth (BEANS_ROOT/<model>/ tree).
# beansd (inotify supervisor) derives Olla config + llama-ondemand configs and
# wires one event-driven bridge per model; models load on demand and die on idle.
FROM ghcr.io/ggml-org/llama.cpp:server-cuda AS llama
FROM ghcr.io/thushan/olla:latest AS olla-src

FROM llama
COPY --from=olla-src /usr/local/bin/olla /usr/local/bin/olla
COPY vendor/llama_ondemand /opt/llama_ondemand
COPY beansd.py /opt/beansd.py
COPY bridge.py /opt/bridge.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]