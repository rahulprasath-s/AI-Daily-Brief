# Automated AI Research & News Pipeline 🧠 -> 📊

**An automated ETL pipeline that ingests, filters, and ranks AI research papers using Google Gemini 1.5 and n8n.**

![Architecture Diagram](assets/architecture_diagram.png)

## 🚀 Key Features
* **Multi-Source Ingestion:** Aggregates real-time data from ArXiv (API) and Hugging Face (RSS).
* **LLM-Based Filtering:** Uses **Gemini 1.5 Flash** to semantic rank papers by technical significance, filtering out 80% of marketing fluff.
* **Idempotency:** Implements custom JavaScript deduplication logic to ensure zero data redundancy.
* **Automated Delivery:** Pushes structured, ranked insights directly to a Notion Knowledge Base.

## 🛠️ Tech Stack
* **Orchestration:** n8n (Self-Hosted)
* **Intelligence:** Google Gemini 1.5 Flash API
* **Database:** Notion API
* **Scripting:** JavaScript (ES6) for data normalization

## 🧠 Logic Highlights
**Prompt Engineering Strategy:**
To ensure strict JSON compliance from the LLM, I implemented a "Role-Based" system prompt forcing a specific schema output, coupled with a regex-based fallback parser in JavaScript.

**Sample Output:**
![Notion Dashboard](assets/notion_dashboard.png)

## 🔧 How to Run
1.  Import `workflows/main_pipeline.json` into n8n.
2.  Set up credentials for Google Gemini and Notion.
3.  Activate the Schedule Trigger.