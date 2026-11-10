# PaperRelay

PaperRelay is a research assistant that answers engineering questions using evidence from a version-pinned library of research papers.

It combines retrieval-augmented generation with cited answers, answer verification, and explicit handling of model-provider limits. The project explores the trade-offs between answer quality, response latency, token consumption, and availability under load.

## Technology

- Python and FastAPI for the backend
- Streamlit for the user interface
- Groq-hosted models for inference
- A configurable stand-in model service for reproducible load testing

## Research library

PaperRelay uses these exact paper versions:

- Attention Is All You Need — arXiv:1706.03762v7
- Moshi: a speech-text foundation model for real-time dialogue — arXiv:2410.00037v1
- Dynamic Chunking for End-to-End Hierarchical Sequence Modeling — arXiv:2507.07955v2

## Planned capabilities

- Retrieve relevant passages with traceable source references.
- Generate and verify cited answers.
- Refuse questions without sufficient supporting evidence.
- Handle bounded queues, deadlines, retries, and provider quotas.
- Compare retrieval configurations and measure behavior under load.
- Reproduce experiments and generate results tables from raw records.

## Development status

Initial project setup is complete. The application and experiments are under development.

Installation, execution, and experiment reproduction instructions will be added alongside the corresponding implementation.

## Configuration

Local credentials belong in `.env`, which is excluded from version control. `.env.example` documents configuration variables without secrets.
