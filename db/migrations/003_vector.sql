-- 003_vector.sql - vector store (pgvector) for similarity search.
-- battery_embedding: each pack's normalised SoH-degradation curve + usage profile as a 16-dim
-- vector; "which vehicles degrade like this one?" is a k-NN query over an HNSW index.
-- kb_chunk: maintenance knowledge base for the copilot's retrieval (RAG).
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE battery_embedding (
    vin         CHAR(17) PRIMARY KEY REFERENCES vehicle(vin) ON DELETE CASCADE,
    tenant_id   INT NOT NULL,
    embedding   vector(16) NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX battery_embedding_hnsw ON battery_embedding USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE kb_chunk (
    id         SERIAL PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    embedding  vector(256) NOT NULL
);
CREATE INDEX kb_chunk_hnsw ON kb_chunk USING hnsw (embedding vector_cosine_ops);
