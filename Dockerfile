# beans — ONE container, ONE source of truth (BEANS_ROOT/<model>/ tree).
# beansd (inotify supervisor) wires one event-driven bridge per model; the
# bridge IS the OpenAI-compatible server (load on demand, die on idle).
FROM ghcr.io/ggml-org/llama.cpp:server-cuda

COPY vendor/llama_ondemand /opt/llama_ondemand
COPY beansd.py /opt/beansd.py
COPY bridge.py /opt/bridge.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]