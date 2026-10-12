# RepoLens image for local use inside AgentHQ's docker compose.
#
# RepoLens's own Dockerfile targets Render (public demo mode, port 10000, a demo index built into
# the image). Here we want a private instance on port 8000 that indexes whatever repo a run needs,
# so AgentHQ keeps its own Dockerfile and reads the RepoLens source as a named build context
# ("repolens_src", set in docker-compose.yml). The RepoLens repo itself is never modified.
FROM python:3.13-slim

# git is needed to clone GitHub repositories for indexing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
# Create the data folder before switching user, so the named volume mounted here is writable.
RUN mkdir /data && chown user:user /data
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    REPOLENS_DATA_DIR=/data \
    REPOLENS_PUBLIC_DEMO=false
WORKDIR /home/user/app

COPY --from=repolens_src --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Download the 80 MB ONNX embedding model at build time, so the first index run doesn't wait for it.
RUN python -c "from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2; ONNXMiniLM_L6_V2()(['warm up'])"

# Copy only the code the server needs. Copying folders one by one (instead of ".") keeps the
# RepoLens .env file with its API key out of the image.
COPY --from=repolens_src --chown=user repolens ./repolens
COPY --from=repolens_src --chown=user app ./app

EXPOSE 8000
CMD ["uvicorn", "repolens.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
